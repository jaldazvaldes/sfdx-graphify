"""Workflow field updates built from synthetic local metadata."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from graphify.salesforce import query
from graphify.salesforce.schema import SCHEMA_VERSION, DataError, FactStore, stable_id, validate_bundle
from graphify.salesforce.service import build_project
from graphify.salesforce.snapshot import load
from graphify.salesforce.viewer import SnapshotView
from graphify.salesforce.workflow import extract_workflow, index_workflow
from graphify.salesforce.xml_source import parse_xml


def workflow(body, package="force-app", *, owner="Account"):
    return parse_xml(f"{package}/main/default/workflows/{owner}.workflow-meta.xml",
                     f'<Workflow xmlns="http://soap.sforce.com/2006/04/metadata">{body}</Workflow>'.encode())


def action(name, kind="FieldUpdate"):
    return f"<actions><name>{name}</name><type>{kind}</type></actions>"


def rule(name, active, *actions):
    return f"<rules><fullName>{name}</fullName>{active}{''.join(actions)}</rules>"


def update(name, field, *, operation="Literal", extra=""):
    return (f"<fieldUpdates><fullName>{name}</fullName><field>{field}</field>"
            f"<operation>{operation}</operation>{extra}</fieldUpdates>")


def writes(store):
    return [edge for edge in store.relationships.values() if edge["relation"] == "writes"]


def test_active_workflow_field_updates_survive_query_snapshot_and_view(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sfdx-project.json").write_text(json.dumps({
        "packageDirectories": [{"path": "force-app"}], "sourceApiVersion": "64.0",
    }), encoding="utf-8")
    body = (
        rule("Set next stage 100%", "<active>true</active>", action("AdvanceStage"), action("AlertOwner", "Alert"),
             f'<workflowTimeTriggers>{action("QueueStage")}</workflowTimeTriggers>',
             '<formula>Account.Stage__c &lt;&gt; "IgnoredFormulaText__c"</formula>')
        + rule("Old rule", "<active>false</active>", action("Retired"), "<formula>OnlyInactiveCriteria__c</formula>")
        + rule("Unknown rule", "<active>true</active><active>false</active>", action("Uncertain"))
        + update("AdvanceStage", "Stage__c", operation="Formula",
                 extra="<formula>InternalValue__c + InternalValue__c</formula>")
        + update("QueueStage", "ACCOUNT.Stage__c", extra="<literalValue>Do not copy me</literalValue>")
        + update("Retired", "OnlyInactive__c")
        + update("Uncertain", "OnlyUnknown__c")
        + update("Unused", "OnlyUnlinked__c")
    )
    metadata = source / workflow(body).path
    metadata.parent.mkdir(parents=True)
    metadata.write_text(f"<Workflow>{body}</Workflow>", encoding="utf-8")
    output = tmp_path / "snapshot"
    bundle = build_project(source, out=output)
    assert load(output) == bundle
    assert bundle["manifest"]["schema_version"] == SCHEMA_VERSION
    assert bundle["manifest"]["activity_policy"]["excluded_counts"] == {"workflowrule": 1}
    assert bundle["manifest"]["coverage"]["analyzed_files"] == 1
    assert bundle["manifest"]["coverage"]["workflow_rule_status_counts"] == {"Active": 1, "Unknown": 1}
    diagnostics = {item["code"] for item in bundle["diagnostics"]}
    assert {"WORKFLOW_RULE_STATUS_UNRESOLVED", "WORKFLOW_RULE_ACTION_UNSUPPORTED"} <= diagnostics

    writes_to_stage = query.refs(bundle, "Account.Stage__c", relation="writes")["results"]
    rule_id = "sf:workflowrule:account.set%20next%20stage%20100%25"
    assert {edge["source"] for edge in writes_to_stage} == {rule_id}
    assert {edge["qualifiers"]["action_timing"] for edge in writes_to_stage} == {"immediate", "time_dependent"}
    assert {edge["qualifiers"]["operation"] for edge in writes_to_stage} == {"Formula", "Literal"}
    for edge in writes_to_stage:
        occurrence, = edge["evidence"]
        assert occurrence["content_hash"] == occurrence["resolution"]["field_definition"]["content_hash"]
        assert "/actions" in occurrence["locator"]
        assert occurrence["resolution"]["field_definition"]["locator"].endswith("/field")
    criteria_read, = query.refs(bundle, "Account.Stage__c", relation="reads")["results"]
    assert criteria_read["qualifiers"]["context"] == "workflow_criteria_formula"
    update_read, = query.refs(bundle, "Account.InternalValue__c", relation="reads")["results"]
    assert update_read["qualifiers"]["context"] == "workflow_field_update_formula"
    update_locations = [occurrence["resolution"] for occurrence in update_read["evidence"]]
    assert {location["formula_reference"]["decoded_character_offset"] for location in update_locations} == {0, 19}
    assert all("/actions" in location["action_site"]["locator"] for location in update_locations)
    assert {edge["source"] for edge in query.impact(bundle, "Account.Stage__c")["results"]} == {rule_id}
    assert query.components(bundle, kind="workflowrule")["results"][0]["id"] == rule_id
    graph = SnapshotView(bundle).view("sf:object:account", mode="automation")
    assert any(node["id"] == rule_id and node["local_status"] == "Active" for node in graph["nodes"])
    text = json.dumps(bundle).casefold()
    for ignored in ("ignoredformulatext__c", "do not copy me", "onlyinactive__c",
                    "onlyinactivecriteria__c", "onlyunknown__c", "onlyunlinked__c"):
        assert ignored not in text

    history = build_project(source, out=output, include_inactive=True)
    assert history["manifest"]["coverage"]["workflow_rule_status_counts"]["Inactive"] == 1
    historical_write, = query.refs(history, "Account.OnlyInactive__c", relation="writes")["results"]
    assert historical_write["qualifiers"]["local_status"] == "Inactive"
    historical_read, = query.refs(history, "Account.OnlyInactiveCriteria__c", relation="reads")["results"]
    assert historical_read["qualifiers"]["local_status"] == "Inactive"

    incompatible = deepcopy(bundle)
    manifest = incompatible["manifest"]
    manifest["schema_version"] = 7
    manifest.pop("snapshot_id")
    manifest["snapshot_id"] = stable_id("snapshot", incompatible)
    with pytest.raises(DataError, match="Workflow rules require snapshot schema version 8"):
        validate_bundle(incompatible)


@pytest.mark.parametrize(("definition", "reference", "code"), [
    ("", action("Update"), "WORKFLOW_FIELD_UPDATE_DEFINITION_UNRESOLVED"),
    (update("Update", "Name") * 2, action("Update"), "WORKFLOW_FIELD_UPDATE_DEFINITION_UNRESOLVED"),
    (update("Update", "Contact.Name"), action("Update"), "WORKFLOW_FIELD_UPDATE_TARGET_UNSUPPORTED"),
    (update("Update", "Name", extra="<sourceObject>ParentId</sourceObject>"),
     action("Update"), "WORKFLOW_FIELD_UPDATE_TARGET_UNSUPPORTED"),
    (update("Update", "Name", extra="<field>Phone</field>"),
     action("Update"), "WORKFLOW_FIELD_UPDATE_TARGET_UNSUPPORTED"),
    (update("Update", "Name", operation="Unknown"), action("Update"), "WORKFLOW_FIELD_UPDATE_OPERATION_UNSUPPORTED"),
    (update("Update", "Name"), "<actions><type>FieldUpdate</type></actions>", "WORKFLOW_FIELD_UPDATE_ACTION_UNRESOLVED"),
])
def test_workflow_writes_require_a_unique_explicit_direct_field_update(definition, reference, code):
    document = workflow(rule("Update", "<active>true</active>", reference) + definition)
    store = FactStore()
    index_workflow([document], store)
    extract_workflow([document], store)
    assert not writes(store)
    assert any(diagnostic["code"] == code for diagnostic in store.diagnostics)


def test_workflow_field_update_cannot_join_a_definition_from_another_package():
    documents = [
        workflow(rule("Update", "<active>true</active>", action("Update"))),
        workflow(update("Update", "Name"), "other-package"),
    ]
    store = FactStore()
    index_workflow(documents, store)
    extract_workflow(documents, store)
    assert not writes(store)
    assert any(d["code"] == "WORKFLOW_FIELD_UPDATE_DEFINITION_UNRESOLVED" for d in store.diagnostics)


def test_target_object_update_does_not_write_the_triggering_objects_field():
    document = workflow(
        rule("Move owner", "<active>true</active>", action("Transfer"))
        + update("Transfer", "OwnerId", operation="Formula",
                 extra="<targetObject>ParentId</targetObject><formula>OwnerId</formula>"),
        owner="EmailMessage",
    )
    store = FactStore()
    index_workflow([document], store)
    extract_workflow([document], store)
    assert not writes(store)
    assert "sf:field:emailmessage.ownerid" not in store.components
    diagnostic, = store.diagnostics
    assert diagnostic["code"] == "WORKFLOW_FIELD_UPDATE_TARGET_OBJECT_UNRESOLVED"
    assert diagnostic["location"]["locator"].endswith("/targetObject")
    assert diagnostic["location"]["content_hash"] == document.content_hash


def test_workflow_criteria_items_keep_typed_field_operands_and_ignore_literal_values():
    document = workflow(
        rule("Check company", "<active>true</active>",
             "<criteriaItems><field>Contact.Account.Name</field><operation>equals</operation>"
             "<valueField>Contact.LastName</valueField></criteriaItems>",
             "<criteriaItems><field>Contact.Description</field><operation>equals</operation>"
             "<value>Account.Fictional__c</value></criteriaItems>"),
        owner="Contact",
    )
    store = FactStore()
    index_workflow([document], store)
    extract_workflow([document], store)
    reads = [edge for edge in store.relationships.values() if edge["relation"] == "reads"]
    assert {edge["target"] for edge in reads} == {
        "sf:field:account.name", "sf:field:contact.accountid", "sf:field:contact.lastname",
        "sf:field:contact.description",
    }
    criteria = [edge for edge in reads if edge["qualifiers"]["context"] == "workflow_criteria_item"]
    assert len(criteria) == 3
    right, = [edge for edge in criteria if edge["target"] == "sf:field:contact.lastname"]
    evidence = store.evidence[right["evidence_ids"][0]]
    assert evidence["resolution"]["operand"] == "valueField"
    assert "fictional__c" not in json.dumps(store.finish()).casefold()


def test_competing_workflow_criteria_forms_are_diagnosed_without_guessing():
    document = workflow(rule("Competing criteria", "<active>true</active>",
                             "<formula>Name</formula><criteriaItems><field>Phone</field></criteriaItems>"))
    store = FactStore()
    index_workflow([document], store)
    extract_workflow([document], store)
    assert {edge["relation"] for edge in store.relationships.values()} == {"contains"}
    assert [diagnostic["code"] for diagnostic in store.diagnostics] == ["WORKFLOW_CRITERIA_AMBIGUOUS"]
