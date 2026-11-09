"""Definition-scoped metadata and schema contracts using synthetic source only."""
from __future__ import annotations

import pytest

from graphify.salesforce.inventory import index_definitions
from graphify.salesforce.schema import DataError, FactStore, component_id, stable_id, validate_bundle
from graphify.salesforce.xml_source import parse_xml


def flow(status="Active", *, path="force-app/main/default/flows/Sample.flow-meta.xml"):
    data = (f'<Flow><status>{status}</status><apiVersion>64.0</apiVersion>'
            '<processType>AutoLaunchedFlow</processType><start><object>Account</object>'
            '<triggerType>RecordBeforeSave</triggerType><recordTriggerType>Update</recordTriggerType>'
            '</start></Flow>').encode()
    return parse_xml(path, data)


def test_flow_metadata_preserves_local_status_and_trigger_evidence():
    store = FactStore()
    index_definitions([flow()], store)
    component = store.components["sf:flow:sample"]
    assert component["local_status"] == "Active"
    assert component["trigger_object"] == "Account"
    assert component["trigger_type"] == "RecordBeforeSave"
    assert component["record_trigger_type"] == "Update"
    assert component["api_version"] == "64.0"
    assert component["metadata_by_definition"][0]["location"] == component["definitions"][0]
    assert "deployed_status" not in component


def test_duplicate_flow_metadata_does_not_choose_a_preferred_definition():
    store = FactStore()
    index_definitions([flow(), flow("Draft", path="other/flows/Sample.flow-meta.xml")], store)
    component = store.components["sf:flow:sample"]
    assert component["definition_status"] == "ambiguous"
    assert component["local_status"] is None
    assert component["trigger_type"] is None
    assert {row["local_status"] for row in component["metadata_by_definition"]} == {"Active", "Draft"}
    assert {row["location"]["path"] for row in component["metadata_by_definition"]} == {
        "force-app/main/default/flows/Sample.flow-meta.xml", "other/flows/Sample.flow-meta.xml"}


def test_diagnostics_deduplicate_content_without_losing_distinct_occurrences():
    store = FactStore()
    for index in range(2000):
        location = {"path": f"metadata/{index}.xml", "line": 1}
        store.diagnostic("unsupported", "Unsupported construct.", location, construct="sample")
        store.diagnostic("unsupported", "Unsupported construct.", dict(location), construct="sample")
    assert len(store.diagnostics) == 2000
    assert len({row["id"] for row in store.diagnostics}) == 2000


def test_apex_method_identity_requires_a_class_and_method():
    assert component_id("apexmethod", "SampleAction.execute") == "sf:apexmethod:sampleaction.execute"
    assert component_id("apexmethod", "vendor.Action.execute") == "sf:apexmethod:vendor.action.execute"
    assert component_id("apexmethod", "SampleAction.execute()") == "sf:apexmethod:sampleaction.execute%28%29"
    for invalid in ("execute", "Namespace.Outer.Inner.execute", "SampleAction.execute(", "SampleAction.execute(,)"):
        with pytest.raises(DataError):
            component_id("apexmethod", invalid)


def test_parameter_type_does_not_assign_a_namespace_to_its_local_method():
    store = FactStore()
    local = store.ensure_component("apexmethod", "Worker.run(pkg__Entry__c)")
    packaged = store.ensure_component("apexmethod", "vendor.Worker.run(pkg__Entry__c)")
    assert store.components[local]["namespace"] is None
    assert store.components[packaged]["namespace"] == "vendor"


def test_validation_rule_identity_is_object_qualified_and_distinct_from_field():
    account = component_id("validationrule", "Account.Require_Phone")
    assert account == "sf:validationrule:account.require_phone"
    assert account == component_id("validationrule", "ACCOUNT.REQUIRE_PHONE")
    assert account != component_id("validationrule", "Contact.Require_Phone")
    assert account != component_id("field", "Account.Require_Phone")
    for invalid in ("Require_Phone", "Account.Require.Phone", "Account.", "Account.Require()"):
        with pytest.raises(DataError):
            component_id("validationrule", invalid)


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_validation_rule_cannot_be_mislabeled_as_a_legacy_snapshot(version):
    store = FactStore()
    store.ensure_component("validationrule", "Account.Require_Phone")
    bundle = {"manifest": {"schema_version": version, "inputs": {}},
              "facts": store.finish(), "diagnostics": []}
    bundle["manifest"]["snapshot_id"] = stable_id("snapshot", bundle)
    if version < 5:
        with pytest.raises(DataError, match="Validation rules require snapshot schema version 5"):
            validate_bundle(bundle)
    else:
        validate_bundle(bundle)
