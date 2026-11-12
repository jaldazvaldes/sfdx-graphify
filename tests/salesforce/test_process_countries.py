"""Evaluate country settings against synthetic process and metadata facts."""
import pytest

from graphify.salesforce.process_countries import evaluate_country_process
from graphify.salesforce.schema import DataError

FIELD = "sf:field:sample__country_flags__mdt.route__c"
FIELD_XML = "force-app/objects/Sample__Country_Flags__mdt/fields/Route__c.field-meta.xml"
APEX = "force-app/classes/Example.cls"
REVISION = "a" * 40


@pytest.fixture
def model():
    field = {"id": FIELD, "kind": "field", "definition_status": "defined", "data_type": "Checkbox",
             "definitions": [{"path": FIELD_XML}]}
    components = [field]
    relationships = []
    evidence = []
    inputs = {FIELD_XML: "hash", APEX: "hash"}
    for country, value_type, value in [
        ("AA", "boolean", False), ("BB", "boolean", True), ("CC", None, None), ("DD", "null", None),
    ]:
        record = f"sf:custommetadatarecord:sample__country_flags.{country.lower()}"
        path = f"force-app/customMetadata/Sample__Country_Flags.{country}.md-meta.xml"
        inputs[path] = "hash"
        components.append({"id": record, "kind": "custommetadatarecord", "definition_status": "defined",
                           "definitions": [{"path": path, "line": 2}]})
        relationships.append({"source": record, "target": "sf:object:sample__country_flags__mdt",
                              "relation": "instance_of"})
        if value_type is not None:
            evidence.append({"id": f"ev:{country}", "path": path, "line": 5, "rule": "custom_metadata.value"})
            relationships.append({"id": f"rel:{country}", "source": record, "target": FIELD, "relation": "sets_value",
                                  "qualifiers": {"value_type": value_type, "value": value},
                                  "evidence_ids": [f"ev:{country}"]})
    provenance = {"snapshot_id": "snapshot:test", "source_revision": REVISION}
    bundle = {
        "manifest": {**provenance, "dirty": False, "inputs": inputs},
        "facts": {"components": components, "relationships": relationships, "evidence": evidence},
        "diagnostics": [],
    }
    process = {
        "id": "route",
        "steps": [{"id": "choose", "kind": "decision", "components": [field]},
                  {"id": "send", "kind": "action", "components": []},
                  {"id": "skip", "kind": "outcome", "components": []}],
        "edges": [{"from": "choose", "to": "send"}, {"from": "choose", "to": "skip"}],
    }
    catalog = {**provenance, "countries": [{"features": [{"processes": [process]}]}]}
    decision = {
        "step": "choose", "field": FIELD, "branches": {"true": "send", "false": "skip"},
        "remaining_conditions": ["The order must pass the entry filters."],
    }
    overlay = {"source_revision": REVISION, "processes": [{"id": "route", "decisions": [decision]}]}
    return bundle, catalog, overlay, decision


def _decision(model, country):
    bundle, catalog, overlay, _ = model
    return evaluate_country_process(bundle, catalog, overlay, process="route", country=country)["decisions"][0]


def _default():
    return {
        "value": False,
        "evidence": [{"path": FIELD_XML, "line": 4, "label": "Field default"},
                     {"path": APEX, "line": 12, "label": "Apply default for an omitted value"}],
    }


def test_country_branch_preserves_other_conditions_and_explicit_false(model):
    first, second = _decision(model, "aa"), _decision(model, "BB")
    assert (first["state"], first["value"], first["configured_target"]) == ("explicit", False, "skip")
    assert first["evidence"][0]["relationship"] == "rel:AA"
    assert (second["state"], second["value"], second["configured_target"]) == ("explicit", True, "send")
    assert second["remaining_conditions"] == ["The order must pass the entry filters."]
    assert _decision(model, "ZZ")["reason"] == "country_record_missing"


def test_default_requires_omission_or_an_explicit_null_policy(model):
    assert _decision(model, "CC")["state"] == "missing"
    decision = model[3]
    decision["default"] = _default()
    omitted = _decision(model, "CC")
    assert (omitted["state"], omitted["reason"], omitted["configured_target"]) == ("default", "field_omitted", "skip")
    assert [item["path"] for item in omitted["evidence"]][-2:] == [FIELD_XML, APEX]
    assert _decision(model, "DD")["reason"] == "explicit_null"
    decision["default"]["applies_to_null"] = True
    assert _decision(model, "DD")["state"] == "default"
    assert _decision(model, "ZZ")["state"] == "missing"


def test_rejected_value_and_unindexed_record_do_not_fall_back_to_default(model):
    bundle, _, _, decision = model
    decision["default"] = _default()
    bundle["diagnostics"].append({
        "code": "CUSTOM_METADATA_VALUE_INVALID",
        "location": {"path": "force-app/customMetadata/Sample__Country_Flags.CC.md-meta.xml"},
    })
    assert _decision(model, "CC")["reason"] == "unresolved_value_declaration"
    bundle["manifest"]["inputs"]["force-app/customMetadata/Sample__Country_Flags.EE.md-meta.xml"] = "hash"
    assert _decision(model, "EE")["reason"] == "country_record_unresolved"


def test_rule_rejects_missing_branch_wrong_revision_or_unproved_default(model):
    _, _, overlay, decision = model
    overlay["source_revision"] = "b" * 40
    with pytest.raises(DataError, match="matching the catalog"):
        _decision(model, "AA")
    overlay["source_revision"] = REVISION
    decision["branches"]["false"] = "missing"
    with pytest.raises(DataError, match="both branches must exist"):
        _decision(model, "AA")
    decision["branches"]["false"] = "skip"
    decision["default"] = _default()
    decision["default"]["evidence"].pop()
    with pytest.raises(DataError, match="where it is applied"):
        _decision(model, "CC")


def test_country_rules_require_one_unambiguous_metadata_type(model):
    bundle, _, overlay, decision = model
    record = "sf:custommetadatarecord:sample__country_flags.aa"
    bundle["facts"]["relationships"].append({
        "source": record, "target": "sf:object:other_country_flags__mdt", "relation": "instance_of",
    })
    assert _decision(model, "AA")["reason"] == "country_record_unresolved"
    overlay["processes"][0]["decisions"].append({
        **decision, "step": "choose-another", "field": "sf:field:other_country_flags__mdt.route__c",
    })
    with pytest.raises(DataError, match="must use one custom metadata type"):
        _decision(model, "AA")
