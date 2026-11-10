"""Bounded fflib dispatch from fictional local trigger and domain sources."""
from __future__ import annotations

import pytest

from graphify.salesforce import cli, query
from graphify.salesforce.graphify_adapter import projection_graph
from graphify.salesforce.schema import DataError, SCHEMA_VERSION, relationship_resolution, stable_id, validate_bundle
from graphify.salesforce.viewer import SnapshotView
from tests.salesforce.test_apex_review_fixes import build


_FACTORY = """
    public class Constructor implements fflib_SObjectDomain.IConstructable {
        public fflib_SObjectDomain construct(List<SObject> items) {
            return new ExampleDomain(items);
        }
    }
"""
_DOMAIN = """public class ExampleDomain extends fflib_SObjectDomain {
    public ExampleDomain(List<Account> rows) { super(rows); }
    public override void onBeforeInsert() { }
    public override void onAfterInsert() { ExampleHelper.touch(); }
    public override void onAfterUpdate(Map<Id, SObject> oldRows) { ExampleHelper.touch(); }
    public void onArbitraryHook() { }
""" + _FACTORY + "}"
_HELPER = "public class ExampleHelper { public static void touch() { Account row = new Account(Name='Example'); } }"
_DISPATCH = "fflib_SObjectDomain.triggerHandler(ExampleDomain.class);"


def _bundle(store):
    facts = store.finish()
    inputs = {definition["path"]: definition["content_hash"]
              for component in facts["components"] for definition in component["definitions"]}
    bundle = {"manifest": {"schema_version": SCHEMA_VERSION, "inputs": inputs},
              "facts": facts, "diagnostics": store.diagnostics}
    bundle["manifest"]["snapshot_id"] = stable_id("snapshot", bundle)
    validate_bundle(bundle)
    return bundle


def _extract(domain=_DOMAIN, statement=_DISPATCH, *, extra=()):
    store = build([("ExampleDomain", domain), ("ExampleHelper", _HELPER), *extra],
                  trigger=f"trigger Sample on Account (after insert, after update) {{\n  {statement}\n}}")
    return _bundle(store)


@pytest.mark.parametrize("constructor_type,argument", [
    ("Account", "items"),
    ("Account", "(List<Account>) items"),
    ("SObject", "(List<SObject>) items"),
])
def test_external_fflib_dispatch_matches_event_hooks_and_preserves_three_hop_evidence(constructor_type, argument):
    domain = _DOMAIN.replace("List<Account> rows", f"List<{constructor_type}> rows")
    bundle = _extract(domain.replace("new ExampleDomain(items)", f"new ExampleDomain({argument})"))
    trigger = "sf:apextrigger:sample"
    dependencies = query.deps(bundle, trigger, relation="calls")
    rows = dependencies["results"]
    assert dependencies["diagnostic_context"]["paths"] == ["classes/ExampleDomain.cls", "triggers/Sample.trigger"]
    assert {(row["qualifiers"]["trigger_event"], row["target"]) for row in rows} == {
        ("after_insert", "sf:apexmethod:exampledomain.onafterinsert"),
        ("after_update", "sf:apexmethod:exampledomain.onafterupdate"),
    }
    for row in rows:
        assert row["resolution"] == {"status": "candidate", "confidence": "AMBIGUOUS",
                                     "reason": "fflib_event_dispatch", "missing_ancestors": []}
        occurrence, = row["evidence"]
        assert occurrence["path"] == "triggers/Sample.trigger" and occurrence["line"] == 2
        proof = occurrence["resolution"]
        assert proof["dispatcher"] == "fflib_SObjectDomain.triggerHandler(Type)"
        assert proof["conditional"] is True
        assert proof["class_literal"]["path"] == occurrence["path"]
        for key in ("construct_site", "constructor", "hook"):
            assert proof[key]["path"] == "classes/ExampleDomain.cls"
            assert proof[key]["content_hash"] == bundle["manifest"]["inputs"][proof[key]["path"]]
        assert query.refs(bundle, row["target"], relation="calls")["results"] == [row]
        assert query.explain(bundle, row["id"])["results"] == [row]
        assert projection_graph(bundle)[trigger][row["target"]]["confidence"] == "AMBIGUOUS"
        assert SnapshotView(bundle).evidence(row["id"])["resolution"]["reason"] == "fflib_event_dispatch"
    result = query.impact(bundle, "Account.Name", depth=3)
    candidates = [row for row in result["results"] if row["source"] == trigger]
    assert len(candidates) == 2
    assert all(row["depth"] == 3 and row["id"] in row["traversal"]["candidate_relationship_ids"] for row in candidates)
    output = cli._text(result)
    assert "CANDIDATE: fflib event dispatch is conditional" in output
    assert "Factory construction: classes/ExampleDomain.cls:" in output
    assert "Hook: classes/ExampleDomain.cls:" in output


@pytest.mark.parametrize("old,new,expected", [
    (_FACTORY, "", set()),
    (".IConstructable", ".OtherInterface", set()),
    ("new ExampleDomain(items)", "new DifferentDomain(items)", set()),
    ("new ExampleDomain(items)", "new ExampleDomain(new List<SObject>())", set()),
    ("public class Constructor", "public class ExampleDomain {} public class Constructor", set()),
    ("List<Account> rows", "List<Contact> rows", set()),
    ("extends fflib_SObjectDomain", "extends OtherBase", set()),
    ("public override void onAfterInsert()", "public void onAfterInsert()", {"after_update"}),
    ("onAfterUpdate(Map<Id, SObject>", "onAfterUpdate(Map<Id, Account>", {"after_insert"}),
    ("public void onArbitraryHook()", "public override void handleAfterInsert()", {"after_update"}),
])
def test_fflib_does_not_guess_incomplete_bindings_or_default_event_handlers(old, new, expected):
    bundle = _extract(_DOMAIN.replace(old, new))
    rows = query.deps(bundle, "sf:apextrigger:sample", relation="calls")["results"]
    assert {row["qualifiers"]["trigger_event"] for row in rows} == expected


@pytest.mark.parametrize("argument", [
    "(List<Contact>) items",
    "(Set<Account>) items",
    "(List<Account>) otherItems",
    "(List<Account>) this.items",
    "(List<Account>) new List<Account>()",
    "(List<Account>) (List<SObject>) items",
])
def test_fflib_unrecognized_factory_cast_preserves_literal_and_reports_specific_diagnostic(argument):
    bundle = _extract(_DOMAIN.replace("new ExampleDomain(items)", f"new ExampleDomain({argument})"))
    trigger = "sf:apextrigger:sample"
    dependencies = query.deps(bundle, trigger)
    assert not any(row["relation"] == "calls" for row in dependencies["results"])
    assert any(row["target"] == "sf:apexclass:exampledomain" and row["relation"] == "references"
               for row in dependencies["results"])
    diagnostic, = [item for item in dependencies["diagnostic_context"]["diagnostics"]
                   if item["code"].startswith("TRIGGER_")]
    assert diagnostic["code"] == "TRIGGER_FFLIB_FACTORY_UNRESOLVED"
    assert diagnostic["state"] == "unresolved"
    assert diagnostic["location"]["path"] == "triggers/Sample.trigger"
    assert diagnostic["target_component"] == "sf:apexclass:exampledomain"


@pytest.mark.parametrize("nested", [True, False])
def test_fflib_factory_cast_does_not_infer_a_record_type_when_an_apex_type_shadows_it(nested):
    domain = _DOMAIN.replace("List<Account> rows", "List<SObject> rows")
    domain = domain.replace("new ExampleDomain(items)", "new ExampleDomain((List<Account>) items)")
    if nested:
        domain = domain.replace("public class Constructor", "public class Account {} public class Constructor")
    extra = [] if nested else [("Account", "public class Account {}")]
    bundle = _extract(domain, extra=extra)
    assert query.deps(bundle, "sf:apextrigger:sample", relation="calls")["results"] == []
    assert any(item["code"] == "TRIGGER_FFLIB_FACTORY_UNRESOLVED" for item in bundle["diagnostics"])


def test_fflib_diagnoses_blocked_hooks_but_an_ordinary_domain_needs_no_override():
    original = _DOMAIN.replace("onAfterUpdate(Map<Id, SObject>", "onAfterUpdate(Map<Id, Account>")
    blocked = original.replace("public void onArbitraryHook()", "public override void handleAfterInsert()")
    assert {item["code"] for item in _extract(blocked)["diagnostics"] if item["code"].startswith("TRIGGER_")} == {
        "TRIGGER_FFLIB_HANDLER_UNSUPPORTED", "TRIGGER_FFLIB_HOOK_UNRESOLVED"}
    empty = _DOMAIN.replace("onAfterInsert()", "onBeforeDelete()").replace("onAfterUpdate(", "onBeforeUpdate(")
    assert not any(item["code"].startswith("TRIGGER_") for item in _extract(empty)["diagnostics"])


def test_fflib_contract_does_not_override_a_local_framework_implementation():
    bundle = _extract(extra=[("fflib_SObjectDomain", "public virtual class fflib_SObjectDomain { "
                             "public static void triggerHandler(Type domainClass) {} }")])
    dispatcher, = query.deps(bundle, "sf:apextrigger:sample", relation="calls")["results"]
    assert dispatcher["target"] == "sf:apexmethod:fflib_sobjectdomain.triggerhandler"
    assert dispatcher["resolution"]["status"] == "extracted"


@pytest.mark.parametrize("statement", [
    "Unrelated.triggerHandler(ExampleDomain.class);",
    "Type domainType = ExampleDomain.class; fflib_SObjectDomain.triggerHandler(domainType);",
    "Object fflib_SObjectDomain = null; fflib_SObjectDomain.triggerHandler(ExampleDomain.class);",
])
def test_fflib_requires_an_unshadowed_dispatcher_and_literal_argument(statement):
    bundle = _extract(statement=statement)
    assert query.deps(bundle, "sf:apextrigger:sample", relation="calls")["results"] == []
    assert not any(item["code"] == "TRIGGER_FFLIB_FACTORY_UNRESOLVED" for item in bundle["diagnostics"])


def test_fflib_dispatch_requires_schema_seven_and_checked_nested_evidence():
    bundle = _extract()
    bundle["manifest"]["schema_version"] = 6
    with pytest.raises(DataError, match="Dispatch candidates require snapshot schema version 7"):
        validate_bundle(bundle)
    bundle["manifest"]["schema_version"] = 7
    occurrence = next(item for item in bundle["facts"]["evidence"] if item["rule"] == "trigger.fflib_event_dispatch")
    occurrence["resolution"]["construct_site"]["content_hash"] = "0" * 64
    with pytest.raises(DataError, match="Provenance content hash differs"):
        validate_bundle(bundle)


def test_constructed_local_virtual_dispatch_is_a_candidate_in_impact():
    store = build([
        ("Base", "public virtual class Base { public void run() { onEvent(); } protected virtual void onEvent() {} }"),
        ("Derived", "public class Derived extends Base { protected override void onEvent() { ExampleHelper.touch(); } }"),
        ("ExampleHelper", _HELPER),
    ], trigger="trigger Sample on Account (before insert) { new Derived().run(); }")
    bundle = _bundle(store)
    result = query.impact(bundle, "Account.Name", depth=3)
    candidate, = [row for row in result["results"] if row["source"] == "sf:apextrigger:sample"]
    assert candidate["resolution"]["reason"] == "conditional_virtual_dispatch"
    assert candidate["traversal"]["candidate_relationship_ids"] == [candidate["id"]]
    assert "CANDIDATE: local virtual dispatch is conditional" in cli._text(result)
    legacy = {"relation": "calls", "qualifiers": {"invocation": "constructed_virtual_dispatch"}}
    assert relationship_resolution(legacy) == candidate["resolution"]
