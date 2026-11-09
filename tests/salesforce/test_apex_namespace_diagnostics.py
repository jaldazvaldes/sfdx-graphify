"""Recognized namespace receivers are not misdiagnosed as record resources."""
import hashlib

import pytest

from graphify.salesforce.apex import extract_apex, index_apex
from graphify.salesforce.inventory import index_definitions
from graphify.salesforce.schema import FactStore
from graphify.salesforce.triggers import extract_triggers, index_triggers
from graphify.salesforce.xml_source import parse_xml


def analyze(body, *, parameters="Account row", namespace=True, superclass="", members="", extra=None):
    store = FactStore()
    if namespace:
        index_definitions([parse_xml(
            "objects/vendor__Settings__c/vendor__Settings__c.object-meta.xml", b"<CustomObject/>")], store)
    source = (f"public class Sample {superclass} {{ {members} "
              f"public void run({parameters}) {{ {body} }} }}").encode()
    classes = {"classes/Sample.cls": source, **(extra or {})}
    index = index_apex(classes, store)
    extract_apex(index, store)
    return store, source


def calls(store):
    return [relation for relation in store.relationships.values()
            if relation["source"] == "sf:apexmethod:sample.run" and relation["relation"] == "calls"]


def test_recognized_namespace_receiver_has_no_false_symbol_diagnostic():
    store, source = analyze("vendor.Worker.run(); vendor.Worker.run();")
    call, = calls(store)
    assert call["target"] == "sf:apexmethod:vendor.worker.run"
    assert len(call["evidence_ids"]) == 2
    assert store.components[call["target"]]["definition_status"] == "referenced_only"
    assert not store.diagnostics
    for eid in call["evidence_ids"]:
        evidence = store.evidence[eid]
        assert evidence["rule"] == "apex.namespace_static_call"
        assert evidence["content_hash"] == hashlib.sha256(source).hexdigest()
        assert evidence["resolution"]["target_validation"] == "referenced_only"


def test_arguments_still_have_reads_writes_calls_and_real_diagnostics():
    store, source = analyze("vendor.Worker.run(row.Name, row.Phone = 'Fictional', "
                            "vendor.Other.check(row.Id), unknown.Value);")
    assert {r["target"] for r in calls(store)} == {
        "sf:apexmethod:vendor.worker.run", "sf:apexmethod:vendor.other.check"}
    fields = {(r["relation"], r["target"]) for r in store.relationships.values()}
    assert {("reads", "sf:field:account.name"), ("reads", "sf:field:account.id"),
            ("writes", "sf:field:account.phone")} <= fields
    symbol_diagnostics = [d for d in store.diagnostics if d["code"] == "APEX_SYMBOL_UNRESOLVED"]
    assert len(symbol_diagnostics) == 1
    assert symbol_diagnostics[0]["location"]["column"] > 0
    assert source[symbol_diagnostics[0]["location"]["column"] - 1:].startswith(b"unknown.Value")


def test_known_namespace_in_non_call_expression_is_not_globally_silenced():
    store, _ = analyze("Object value = vendor.Worker; vendor.Worker.run();")
    assert len(calls(store)) == 1
    assert any(d["code"] == "APEX_SYMBOL_UNRESOLVED" for d in store.diagnostics)


def test_unrecognized_namespace_retains_receiver_and_call_diagnostics():
    store, _ = analyze("vendor.Worker.run(row.Name);", namespace=False)
    assert not calls(store)
    assert {"APEX_SYMBOL_UNRESOLVED", "APEX_EXTERNAL_CALL_UNSUPPORTED"} <= {
        d["code"] for d in store.diagnostics}
    assert any(r["relation"] == "reads" and r["target"] == "sf:field:account.name"
               for r in store.relationships.values())


@pytest.mark.parametrize("options", [
    {"parameters": "Account row, Object vendor"},
    {"members": "public Object vendor;"},
    {"extra": {"classes/vendor.cls": b"public class vendor {}"}},
    {"superclass": "extends UnknownBase"},
    {"superclass": "extends Base", "extra": {
        "classes/Base.cls": b"public virtual class Base { public Object vendor; }"}},
])
def test_shadowing_and_unknown_hierarchies_are_not_accepted_as_namespaces(options):
    store, _ = analyze("vendor.Worker.run(row.Name);", **options)
    assert not calls(store)
    assert any(d["code"] == "APEX_EXTERNAL_CALL_UNSUPPORTED" for d in store.diagnostics)
    assert any(r["relation"] == "reads" and r["target"] == "sf:field:account.name"
               for r in store.relationships.values())


def test_complete_local_inheritance_retains_clean_namespace_call():
    store, _ = analyze("vendor.Worker.run(row.Name);", superclass="extends Base", extra={
        "classes/Base.cls": b"public virtual class Base {}"})
    assert len(calls(store)) == 1
    assert not any(d["code"] == "APEX_SYMBOL_UNRESOLVED" for d in store.diagnostics)


def test_longer_receiver_path_is_not_mistaken_for_namespace_class():
    store, _ = analyze("vendor.Worker.child.run(row.Name);")
    assert not calls(store)
    assert any(d["code"] == "APEX_EXTERNAL_CALL_UNSUPPORTED" for d in store.diagnostics)


def analyze_trigger(body, *, namespace=True):
    store = FactStore()
    if namespace:
        index_definitions([parse_xml("objects/vendor__Settings__c/vendor__Settings__c.object-meta.xml",
                                     b"<CustomObject/>")], store)
    index = index_apex({}, store)
    source = ("trigger Sample on Account(before insert) { " + body + " }").encode()
    triggers = index_triggers({"triggers/Sample.trigger": source}, [], index, store)
    extract_triggers(triggers, index, store)
    assert not any(d["code"] == "APEX_PARSE_ERROR" for d in store.diagnostics)
    return store, source


def test_trigger_namespace_calls_keep_argument_evidence_without_receiver_warning():
    store, source = analyze_trigger("Account row; vendor.Worker.run(Trigger.new, row.Name, "
                                    "row.Phone='Fictional', vendor.Other.check(row.Id), unknown.Value);")
    calls = [r for r in store.relationships.values() if r["relation"] == "calls"]
    assert {r["target"] for r in calls} == {"sf:apexmethod:vendor.worker.run", "sf:apexmethod:vendor.other.check"}
    fields = {(r["relation"], r["target"]) for r in store.relationships.values()}
    assert {("reads", "sf:object:account"), ("reads", "sf:field:account.name"),
            ("reads", "sf:field:account.id"), ("writes", "sf:field:account.phone")} <= fields
    symbols = [d for d in store.diagnostics if d["code"] == "APEX_SYMBOL_UNRESOLVED"]
    assert len(symbols) == 1
    assert source[symbols[0]["location"]["column"] - 1:].startswith(b"unknown.Value")
    for r in calls:
        for eid in r["evidence_ids"]:
            assert store.evidence[eid]["content_hash"] == hashlib.sha256(source).hexdigest()
            assert store.evidence[eid]["source_component"] == "sf:apextrigger:sample"


@pytest.mark.parametrize("namespace,prefix", [(False, ""), (True, "Object vendor;")])
def test_trigger_unknown_or_shadowed_namespace_retains_diagnostics(namespace, prefix):
    store, _ = analyze_trigger(prefix + " vendor.Worker.run(Trigger.new);", namespace=namespace)
    assert not any(r["relation"] == "calls" for r in store.relationships.values())
    assert any(d["code"] == "TRIGGER_CALL_UNRESOLVED" for d in store.diagnostics)
    assert any(r["relation"] == "reads" and r["target"] == "sf:object:account" for r in store.relationships.values())
