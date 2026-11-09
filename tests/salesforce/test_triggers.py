"""Synthetic trigger entry points and deliberately bounded handler dispatch."""

import hashlib

import pytest

from graphify.salesforce.apex import extract_apex, index_apex
from graphify.salesforce.inventory import index_definitions
from graphify.salesforce.schema import FactStore
from graphify.salesforce.triggers import extract_triggers, index_triggers
from graphify.salesforce.xml_source import parse_xml


def analyze(body, *, classes=(), status: str | None = "Active", custom_settings_type=None, excluded=()):
    store = FactStore()
    if custom_settings_type:
        document = parse_xml("objects/Settings__c/Settings__c.object-meta.xml",
                             f"<CustomObject><customSettingsType>{custom_settings_type}</customSettingsType></CustomObject>".encode())
        index_definitions([document], store)
    sources = {f"classes/{name}.cls": code.encode() for name, code in classes}
    apex_index = index_apex(sources, store)
    extract_apex(apex_index, store)
    source = f"trigger Example on Account (before insert, after update) {{\n{body}\n}}".encode()
    metadata = [] if status is None else [parse_xml("triggers/Example.trigger-meta.xml",
                                                   f"<ApexTrigger><status>{status}</status></ApexTrigger>".encode())]
    index = index_triggers({"triggers/Example.trigger": source}, metadata, apex_index, store)
    extract_triggers(index, apex_index, store, excluded=set(excluded))
    return store, source


def trigger_relations(store, relation=None):
    return [r for r in store.relationships.values() if r["source"] == "sf:apextrigger:example"
            and (relation is None or r["relation"] == relation)]


def codes(store):
    return {d["code"] for d in store.diagnostics}


def test_event_and_static_handler_preserve_native_source_evidence():
    store, source = analyze("Handler.process(Trigger.new);", classes=[
        ("Handler", "public class Handler { public static void process(List<Account> rows) { update rows; } }")])
    component = store.components["sf:apextrigger:example"]
    assert component["trigger_object"] == "Account"
    assert component["trigger_events"] == ["after_update", "before_insert"]
    assert component["local_status"] == "Active"
    assert component["definitions"][0]["path"] == "triggers/Example.trigger"
    assert component["status_location"]["path"] == "triggers/Example.trigger-meta.xml"
    call, = trigger_relations(store, "calls")
    assert call["target"] == "sf:apexmethod:handler.process"
    evidence = store.evidence[call["evidence_ids"][0]]
    assert evidence["path"] == "triggers/Example.trigger"
    assert evidence["line"] == 2
    assert evidence["content_hash"] == hashlib.sha256(source).hexdigest()
    assert any(r["target"] == "sf:object:account" and r["qualifiers"].get("context") == "new"
               for r in trigger_relations(store, "reads"))
    assert any(r["source"] == "sf:apexmethod:handler.process" and r["qualifiers"].get("effect") == "database"
               for r in store.relationships.values())
    assert not store.diagnostics


def test_explicit_concrete_constructor_survives_interface_declared_variable():
    store, _ = analyze("Contract runner = new Concrete(); runner.run();", classes=[
        ("Concrete", "public class Concrete implements Contract { public void run() {} }")])
    assert {r["target"] for r in trigger_relations(store, "calls")} == {"sf:apexmethod:concrete.run"}
    reference = next(r for r in trigger_relations(store, "references") if r["target"] == "sf:apexclass:concrete")
    assert reference["qualifiers"] == {"invocation": "constructor"}
    assert not any(r["target"] == "sf:apexclass:concrete" for r in trigger_relations(store, "calls"))


@pytest.mark.parametrize("body", ["Contract runner = new Concrete(); runner.run();", "new Concrete().run();"])
def test_inherited_dispatch_keeps_constructor_reference_and_diagnostic(body):
    store, _ = analyze(body, classes=[
        ("Concrete", "public class Concrete extends Parent { public override void run() {} }")])
    assert not trigger_relations(store, "calls")
    assert any(r["target"] == "sf:apexclass:concrete" for r in trigger_relations(store, "references"))
    assert "TRIGGER_CALL_UNRESOLVED" in codes(store)


def test_reassigned_receiver_does_not_retain_original_concrete_dispatch():
    store, _ = analyze("Contract runner = new Concrete(); runner = other; runner.run();", classes=[
        ("Concrete", "public class Concrete { public void run() {} }")])
    assert not trigger_relations(store, "calls")
    assert "TRIGGER_CALL_UNRESOLVED" in codes(store)


@pytest.mark.parametrize("method", ["private static void run() {}", "public void run() {}",
                                     "public static void run(String arg) {}"])
def test_inaccessible_instance_wrong_arity_or_overloaded_static_targets_are_unresolved(method):
    store, _ = analyze("Handler.run();", classes=[("Handler", f"public class Handler {{ {method} }}")])
    assert not trigger_relations(store, "calls")
    assert "TRIGGER_CALL_UNRESOLVED" in codes(store)


def test_local_variable_shadowing_does_not_become_static_class_dispatch():
    store, _ = analyze("String Handler = 'not a class'; Handler.run();", classes=[
        ("Handler", "public class Handler { public static void run() {} }")])
    assert not trigger_relations(store, "calls")


def test_ambiguous_local_variable_still_shadows_class_identifier():
    store, _ = analyze("String Handler = 'a'; String Handler = 'b'; Handler.run();", classes=[
        ("Handler", "public class Handler { public static void run() {} }")])
    assert not trigger_relations(store, "calls")


def test_typed_context_loop_tracks_fields_without_claiming_database_writes():
    store, _ = analyze("for (Account row : Trigger.new) { row.Description = row.Name; }")
    fields = {(r["target"], r["relation"], r["qualifiers"].get("effect")) for r in trigger_relations(store)
              if r["target"].startswith("sf:field:")}
    assert fields == {("sf:field:account.name", "reads", "memory"),
                      ("sf:field:account.description", "writes", "memory")}
    assert not any(r["qualifiers"].get("effect") == "database" for r in trigger_relations(store))
    assert not store.diagnostics


def test_direct_context_list_access_tracks_original_object_field():
    store, _ = analyze("String value = Trigger.new.get(0).Name;")
    assert any(r["target"] == "sf:field:account.name" for r in trigger_relations(store, "reads"))
    assert not store.diagnostics


def test_database_operations_keep_bounded_apex_extraction_and_dynamic_diagnostics():
    store, _ = analyze("Account row = new Account(Name='Example'); Database.update(row); Database.query('SELECT Id FROM Contact');")
    assert any(r["target"] == "sf:object:account" and r["qualifiers"].get("effect") == "database"
               for r in trigger_relations(store, "writes"))
    assert "APEX_DYNAMIC_SOQL_UNSUPPORTED" in codes(store)
    assert "sf:object:contact" not in store.components


def test_custom_setting_guard_requires_local_setting_metadata():
    body = "if (Settings__c.getInstance().Enabled__c) { }"
    proven, _ = analyze(body, custom_settings_type="Hierarchy")
    assert any(r["target"] == "sf:field:settings__c.enabled__c" for r in trigger_relations(proven, "reads"))
    assert not proven.diagnostics
    absent, _ = analyze(body)
    assert "sf:field:settings__c.enabled__c" not in absent.components
    assert "TRIGGER_CALL_UNRESOLVED" in codes(absent)


def test_custom_setting_accessor_is_not_inferred_from_a_shadowed_variable():
    store, _ = analyze("Settings__c Settings__c = new Settings__c(); if (Settings__c.getInstance().Enabled__c) { }",
                       custom_settings_type="Hierarchy")
    assert "sf:field:settings__c.enabled__c" not in store.components
    assert "TRIGGER_CALL_UNRESOLVED" in codes(store)


def test_inactive_trigger_extraction_honors_exclusion_without_losing_indexed_state():
    store, _ = analyze("Handler.run();", status="Inactive", excluded={"sf:apextrigger:example"}, classes=[
        ("Handler", "public class Handler { public static void run() {} }")])
    assert store.components["sf:apextrigger:example"]["local_status"] == "Inactive"
    assert not trigger_relations(store)


def test_missing_status_is_unknown_and_comments_strings_are_not_calls():
    store, _ = analyze("// Ghost.run();\nString text = 'Ghost.run()';", status=None)
    assert store.components["sf:apextrigger:example"]["local_status"] == "Unknown"
    assert not trigger_relations(store, "calls")
    assert not any("ghost" in cid for cid in store.components)


@pytest.mark.parametrize("states", [("Inactive", "Active"), ("Active", "Inactive"), ("Inactive", "Inactive")])
def test_duplicate_status_elements_remain_unknown_and_keep_extraction(states):
    store = FactStore()
    apex = index_apex({}, store)
    document = parse_xml("triggers/Example.trigger-meta.xml",
                         ("<ApexTrigger>" + "".join(f"<status>{state}</status>" for state in states) + "</ApexTrigger>").encode())
    index = index_triggers({"triggers/Example.trigger": b"trigger Example on Account (before insert) {}"},
                           [document], apex, store)
    extract_triggers(index, apex, store)
    assert store.components["sf:apextrigger:example"]["local_status"] == "Unknown"
    assert trigger_relations(store, "references")
    diagnostics = [d for d in store.diagnostics if d["code"] == "TRIGGER_STATUS_AMBIGUOUS"]
    assert len(diagnostics) == 2
    assert {d["location"]["locator"] for d in diagnostics} == {"/ApexTrigger/status[1]", "/ApexTrigger/status[2]"}


def test_body_parse_failure_retains_valid_trigger_header_without_body_facts():
    store, _ = analyze("Handler.run(;")
    assert set(store.components) == {"sf:apextrigger:example", "sf:object:account"}
    assert store.components["sf:apextrigger:example"]["inventory_method"] == "apex_ast"
    assert len(store.relationships) == 1
    assert next(iter(store.relationships.values()))["qualifiers"]["operation"] == "trigger"
    assert "TRIGGER_PARSE_ERROR" in codes(store)


def test_duplicate_trigger_sources_are_ambiguous_without_preferred_metadata():
    store = FactStore()
    apex = index_apex({}, store)
    source = b"trigger Example on Account (before insert) {}"
    index = index_triggers({"one/Example.trigger": source, "two/Example.trigger": source}, [], apex, store)
    extract_triggers(index, apex, store)
    component = store.components["sf:apextrigger:example"]
    assert component["definition_status"] == "ambiguous"
    assert component["local_status"] is None
    assert component["trigger_object"] is None
    assert not store.relationships
    assert "TRIGGER_AMBIGUOUS" in codes(store)


def test_trigger_handler_continues_to_unique_cross_class_static_method():
    store, _ = analyze("Handler.run();", classes=[
        ("Handler", "public class Handler { public static void run() { Utility.work(); } }"),
        ("Utility", "public class Utility { public static void work() { insert new Account(Name='Example'); } }")])
    assert {(r["source"], r["target"]) for r in store.relationships.values() if r["relation"] == "calls"} == {
        ("sf:apextrigger:example", "sf:apexmethod:handler.run"),
        ("sf:apexmethod:handler.run", "sf:apexmethod:utility.work")}


@pytest.mark.parametrize("handler", [
    "public class Handler { public static void run() { String Utility = 'x'; Utility.work(); } }",
    "public class Handler { static String Utility; public static void run() { Utility.work(); } }",
    "public class Handler extends Parent { public static void run() { Utility.work(); } }",
])
def test_cross_class_static_call_does_not_ignore_local_member_or_inherited_shadowing(handler):
    store, _ = analyze("", classes=[("Handler", handler),
                                    ("Utility", "public class Utility { public static void work() {} }")])
    assert not any(r["target"] == "sf:apexmethod:utility.work" and r["relation"] == "calls"
                   for r in store.relationships.values())
