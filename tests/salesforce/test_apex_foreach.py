"""Explicit foreach declarations provide bounded, scoped record evidence."""
from __future__ import annotations

import hashlib

import pytest

from graphify.salesforce.apex import extract_apex, index_apex
from graphify.salesforce.schema import FactStore
from graphify.salesforce.triggers import extract_triggers, index_triggers


def analyze(body, *, parameters="List<Account> rows", extra=""):
    source = ("public class Example { public static void run(" + parameters + ") {\n"
              + body + "\n} " + extra + " }").encode()
    store = FactStore()
    index = index_apex({"classes/Example.cls": source}, store)
    extract_apex(index, store)
    assert not any(d["code"] == "APEX_PARSE_ERROR" for d in store.diagnostics)
    return store, source


def fields(store):
    return {(r["target"], r["relation"], r["qualifiers"].get("effect"))
            for r in store.relationships.values() if r["target"].startswith("sf:field:")}


def test_typed_custom_loop_reads_conditions_and_writes_memory_with_original_evidence():
    store, source = analyze("""for (Agreement__c item : items) {
  if (item.Unlimited__c && item.Months__c > 0 && item.ExternalRef__c != null) {
    item.EndDate__c = null;
  }
}""", parameters="List<Agreement__c> items")
    assert fields(store) == {("sf:field:agreement__c.unlimited__c", "reads", "memory"),
                             ("sf:field:agreement__c.months__c", "reads", "memory"),
                             ("sf:field:agreement__c.externalref__c", "reads", "memory"),
                             ("sf:field:agreement__c.enddate__c", "writes", "memory")}
    assert not store.diagnostics
    assert not any(r["qualifiers"].get("effect") == "database" for r in store.relationships.values())
    for proof in store.evidence.values():
        assert proof["content_hash"] == hashlib.sha256(source).hexdigest()
        if proof["rule"] == "apex.direct_field":
            assert "enhanced_for_statement" in proof["resolution"]["declaration"]["locator"]
            assert proof["line"] in (3, 4)


def test_loop_without_braces_and_compound_assignment_keep_reads_and_memory_effects():
    store, _ = analyze("for(Account row : rows) row.NumberOfEmployees += 1;")
    assert fields(store) == {("sf:field:account.numberofemployees", "reads", "memory"),
                             ("sf:field:account.numberofemployees", "writes", "memory")}
    assert not store.diagnostics


def test_iterable_uses_outer_binding_and_outer_record_is_restored_after_loop():
    store, _ = analyze("Account row; for(Contact row : choose(row.Name)) { row.LastName = 'Inside'; } row.Phone = 'After';")
    assert fields(store) == {("sf:field:account.name", "reads", "memory"),
                             ("sf:field:contact.lastname", "writes", "memory"),
                             ("sf:field:account.phone", "writes", "memory")}


def test_nested_single_statement_loop_keeps_outer_binding_in_inner_iterable():
    store, _ = analyze("for(Account row : rows) for(Contact row : choose(row.Name)) row.LastName = 'Inside';")
    assert fields(store) == {("sf:field:account.name", "reads", "memory"),
                             ("sf:field:contact.lastname", "writes", "memory")}


def test_nested_block_loop_restores_outer_alias_and_does_not_escape_body():
    store, _ = analyze("for(Account row : rows) { for(Contact row : contacts) { row.LastName = 'Inner'; } row.Name = 'Outer'; } row.Phone='Outside';",
                       parameters="List<Account> rows, List<Contact> contacts")
    assert fields(store) == {("sf:field:contact.lastname", "writes", "memory"),
                             ("sf:field:account.name", "writes", "memory")}
    assert any(d["code"] == "APEX_SYMBOL_UNRESOLVED" for d in store.diagnostics)


@pytest.mark.parametrize("element_type,extra", [
    ("SObject", ""), ("String", ""), ("List<Account>", ""), ("Account[]", ""),
    ("UnknownType", ""), ("Account", "public class Account {}"),
])
def test_unsupported_element_types_still_shadow_outer_record(element_type, extra):
    store, _ = analyze(f"Contact row; for({element_type} row : rows) {{ row.Name='Inside'; }} row.LastName='After';", extra=extra)
    assert fields(store) == {("sf:field:contact.lastname", "writes", "memory")}
    assert any(d["code"] == "APEX_LOOP_ALIAS_UNSUPPORTED" for d in store.diagnostics)


def test_explicit_dml_keeps_whole_record_effect_separate_from_memory_assignment():
    store, _ = analyze("for(Account row : rows) { row.Name='Example'; update row; }")
    assert fields(store) == {("sf:field:account.name", "writes", "memory")}
    writes = [r for r in store.relationships.values() if r["qualifiers"].get("effect") == "database"]
    assert len(writes) == 1 and writes[0]["target"] == "sf:object:account"
    assert writes[0]["qualifiers"]["collection"] is False
    assert writes[0]["qualifiers"]["field_set"] == "unknown"


def test_map_values_iterable_does_not_require_collection_propagation_to_type_explicit_element():
    store, _ = analyze("for(Account row : byId.values()) { row.Name='Example'; }", parameters="Map<Id,Account> byId")
    assert fields(store) == {("sf:field:account.name", "writes", "memory")}
    assert not any(d["code"] == "APEX_LOOP_ALIAS_UNSUPPORTED" for d in store.diagnostics)
    # Method-return semantics remain unknown; only the declared element is typed.
    assert any(d["code"] == "APEX_EXTERNAL_CALL_UNSUPPORTED" for d in store.diagnostics)


def test_empty_body_cannot_capture_the_next_statement():
    store, _ = analyze("Contact row; for(Account row : rows); row.LastName='After';")
    assert fields(store) == {("sf:field:contact.lastname", "writes", "memory")}


def test_trigger_qualifier_in_iterable_is_not_shadowed_by_loop_body_alias():
    store = FactStore()
    index = index_apex({}, store)
    triggers = index_triggers({"triggers/Example.trigger":
                               b"trigger Example on Account(before insert) { for(Account Trigger : Trigger.new) { Trigger.Name='Example'; } }"}, [], index, store)
    extract_triggers(triggers, index, store)
    assert fields(store) == {("sf:field:account.name", "writes", "memory")}
    assert any(r["relation"] == "reads" and r["target"] == "sf:object:account" for r in store.relationships.values())
    assert not any(d["code"] in {"APEX_RECORD_TYPE_UNRESOLVED", "TRIGGER_LOOP_TYPE_UNRESOLVED"} for d in store.diagnostics)
