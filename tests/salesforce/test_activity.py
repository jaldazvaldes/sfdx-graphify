"""Activation policy contracts with fictional, temporary Salesforce projects."""
from __future__ import annotations

import json

import pytest

from graphify.salesforce import query
from graphify.salesforce.activity import inactive_components
from graphify.salesforce.schema import validate_bundle
from graphify.salesforce.service import build_project
from graphify.salesforce.snapshot import load


def project(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "sfdx-project.json").write_text(json.dumps({
        "packageDirectories": [{"path": "force-app"}], "sourceApiVersion": "64.0",
    }), encoding="utf-8")
    return root


def flow(root, name, status, *, body="", directory="force-app/main/default/flows"):
    path = root / directory / f"{name}.flow-meta.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    activation = f"<status>{status}</status>" if status is not None else ""
    path.write_text(
        f"<Flow><apiVersion>64.0</apiVersion><processType>AutoLaunchedFlow</processType>"
        f"{activation}{body}</Flow>", encoding="utf-8")
    return path


def start(object_name):
    return (f"<start><object>{object_name}</object><triggerType>RecordBeforeSave</triggerType>"
            "<recordTriggerType>Update</recordTriggerType><filters><field>Enabled__c</field>"
            "<operator>EqualTo</operator><value><booleanValue>true</booleanValue></value>"
            "</filters></start>")


@pytest.mark.parametrize("status", ["Draft", "InvalidDraft", "Obsolete"])
def test_only_explicitly_inactive_flow_builds_empty_facts_and_opt_in_restores(tmp_path, status):
    root, output = project(tmp_path), tmp_path / "snapshot"
    flow(root, "Dormant", status, body=start("DormantObject__c"))
    excluded = build_project(root, out=output)
    assert excluded["facts"] == {"components": [], "relationships": [], "evidence": []}
    assert load(output) == excluded
    policy = excluded["manifest"]["activity_policy"]
    assert policy == excluded["manifest"]["coverage"]["activity_policy"]
    assert policy["mode"] == "exclude_explicitly_inactive"
    assert policy["excluded_counts"] == {"flow": 1}
    assert policy["excluded_status_counts"] == {status: 1}
    assert policy["skipped_definition_files"] == 1
    assert policy["local_metadata_only"] is True
    assert query.components(excluded, kind="flow")["results"] == []
    assert query.freshness(excluded, root)["state"] == "current"

    included = build_project(root, out=output, include_inactive=True)
    assert load(output) == included
    assert {item["id"] for item in included["facts"]["components"]} == {
        "sf:flow:dormant", "sf:object:dormantobject__c", "sf:field:dormantobject__c.enabled__c",
    }
    assert included["facts"]["relationships"] and included["facts"]["evidence"]
    assert included["manifest"]["activity_policy"]["mode"] == "include_inactive"
    assert included["manifest"]["activity_policy"]["excluded_component_count"] == 0
    assert included["manifest"]["snapshot_id"] != excluded["manifest"]["snapshot_id"]
    assert query.freshness(included, root)["state"] == "current"
    assert build_project(root, out=output) == excluded


def test_active_callers_retain_proof_of_excluded_targets_without_dangling_facts(tmp_path):
    root = project(tmp_path)
    caller = flow(root, "Current", "Active", body=start("Shared__c") +
                  "<subflows><name>FirstCall</name><flowName>Old</flowName></subflows>"
                  "<subflows><name>SecondCall</name><flowName>Old</flowName></subflows>")
    flow(root, "Old", "Obsolete", body=start("OnlyInactive__c"))
    field = root / "force-app/main/default/objects/Declared__c/fields/Keep__c.field-meta.xml"
    field.parent.mkdir(parents=True)
    field.write_text("<CustomField><fullName>Keep__c</fullName><type>Text</type></CustomField>",
                     encoding="utf-8")
    bundle = build_project(root, out=tmp_path / "snapshot")
    ids = {item["id"] for item in bundle["facts"]["components"]}
    assert "sf:flow:current" in ids and "sf:flow:old" not in ids
    assert "sf:object:onlyinactive__c" not in ids
    assert "sf:field:onlyinactive__c.enabled__c" not in ids
    assert {"sf:object:shared__c", "sf:field:shared__c.enabled__c",
            "sf:object:declared__c", "sf:field:declared__c.keep__c"} <= ids
    assert all(item["source"] in ids and item["target"] in ids
               for item in bundle["facts"]["relationships"])
    diagnostics = [item for item in bundle["diagnostics"]
                   if item["code"] == "inactive_dependency_excluded"]
    assert len(diagnostics) == 2
    assert len({item["location"]["locator"] for item in diagnostics}) == 2
    relative = caller.relative_to(root).as_posix()
    for diagnostic in diagnostics:
        assert diagnostic["state"] == "excluded"
        assert diagnostic["source_component"] == "sf:flow:current"
        assert diagnostic["target_component"] == "sf:flow:old"
        assert diagnostic["local_status"] == "Obsolete"
        assert diagnostic["location"]["path"] == relative
        assert diagnostic["location"]["content_hash"] == bundle["manifest"]["inputs"][relative]
    assert bundle["manifest"]["activity_policy"]["omitted_relationship_count"] == 1
    validate_bundle(bundle)


def test_unknown_missing_unrecognized_and_ambiguous_flow_statuses_are_retained(tmp_path):
    root = project(tmp_path)
    for name, status in [("Current", "Active"), ("Missing", None),
                         ("Unknown", "Unknown"), ("Future", "FutureStatus")]:
        flow(root, name, status, body=start("Shared__c"))
    flow(root, "Conflicting", "Draft")
    flow(root, "Conflicting", "Active", directory="force-app/second/flows")
    bundle = build_project(root, out=tmp_path / "snapshot")
    found = {item["api_name"]: item for item in bundle["facts"]["components"] if item["kind"] == "flow"}
    assert set(found) == {"Current", "Missing", "Unknown", "Future", "Conflicting"}
    assert found["Conflicting"]["definition_status"] == "ambiguous"
    assert found["Conflicting"]["local_status"] is None
    assert bundle["manifest"]["activity_policy"]["excluded_component_count"] == 0
    assert bundle["manifest"]["activity_policy"]["unknown_status_policy"] == "retain"


def test_reference_only_and_non_flow_status_are_not_classified_as_inactive():
    assert inactive_components([
        {"id": "sf:flow:external", "kind": "flow", "local_status": "Draft",
         "definition_status": "referenced_only", "definitions": []},
        {"id": "sf:apexclass:sample", "kind": "apexclass", "local_status": "Inactive",
         "definition_status": "defined", "definitions": [{"path": "Sample.cls"}]},
    ]) == {}


@pytest.mark.parametrize("second_status", ["Active", "Draft"])
def test_repeated_flow_status_retains_dependencies_as_unknown(tmp_path, second_status):
    root = project(tmp_path)
    flow(root, "ConflictingStatus", "Draft", body=f"<status>{second_status}</status>" + start("Retained__c"))
    bundle = build_project(root, out=tmp_path / "snapshot")
    found = {item["id"]: item for item in bundle["facts"]["components"]}
    component = found["sf:flow:conflictingstatus"]
    assert component["local_status"] == "Unknown"
    assert component["metadata_by_definition"][0]["local_status"] == "Unknown"
    assert "sf:field:retained__c.enabled__c" in found
    assert bundle["manifest"]["activity_policy"]["excluded_component_count"] == 0
    diagnostics = [item for item in bundle["diagnostics"] if item["code"] == "FLOW_STATUS_AMBIGUOUS"]
    assert len(diagnostics) == 2
    assert len({item["location"]["locator"] for item in diagnostics}) == 2
    assert all(item["state"] == "ambiguous" and item["candidates"] == ["Draft", second_status]
               for item in diagnostics)


def test_activating_excluded_source_is_stale_and_rebuild_includes_it(tmp_path):
    root, output = project(tmp_path), tmp_path / "snapshot"
    path = flow(root, "Transition", "Draft", body=start("TransitionObject__c"))
    before = build_project(root, out=output)
    assert query.freshness(before, root)["state"] == "current"
    flow(root, "Transition", "Active", body=start("TransitionObject__c"))
    freshness = query.freshness(before, root)
    assert freshness["state"] == "stale"
    assert freshness["changed"] == [path.relative_to(root).as_posix()]
    after = build_project(root, out=output)
    assert query.freshness(after, root)["state"] == "current"
    assert query.components(after, kind="flow")["results"][0]["api_name"] == "Transition"
    assert after["manifest"]["activity_policy"]["excluded_component_count"] == 0
