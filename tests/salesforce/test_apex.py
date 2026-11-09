"""Synthetic Apex AST contracts; none of these classes came from an org."""

from __future__ import annotations

import socket

import pytest

from graphify.salesforce.apex import extract_apex, index_apex, resolve_invocable_calls
from graphify.salesforce.flow import extract_flow
from graphify.salesforce.schema import FactStore
from graphify.salesforce.xml_source import parse_xml


def analyze(body, *, declaration="public static void run(List<Account> rows)", annotation="@InvocableMethod", extra=""):
    source = f"public class SampleAction {{ {annotation} {declaration} {{ {body} }} {extra} }}"
    store = FactStore()
    index = index_apex({"classes/SampleAction.cls": source.encode()}, store)
    extract_apex(index, store)
    return store, index


def field_relations(store):
    return {(store.components[edge["target"]]["api_name"], edge["relation"], edge["qualifiers"].get("effect"))
            for edge in store.relationships.values() if store.components[edge["target"]]["kind"] == "field"}


def codes(store):
    return {item["code"] for item in store.diagnostics}


def attach_flow(store, target="SampleAction"):
    source = f'''<Flow xmlns="http://soap.sforce.com/2006/04/metadata"><apiVersion>64.0</apiVersion>
      <actionCalls><name>RunAction</name><actionType>apex</actionType><actionName>{target}</actionName></actionCalls>
      </Flow>'''
    document = parse_xml("flows/SyntheticFlow.flow-meta.xml", source.encode())
    store.ensure_component("flow", "SyntheticFlow", document.location(document.root))
    extract_flow(document, store)


def test_invocable_bridge_retains_class_call_and_traces_method_query_and_dml():
    store, index = analyze("List<Contact> found = [SELECT Id, LastName FROM Contact WHERE AccountId != null]; update rows;")
    attach_flow(store)
    resolve_invocable_calls(store, index)
    calls = [r for r in store.relationships.values() if r["source"] == "sf:flow:syntheticflow" and r["relation"] == "calls"]
    assert {r["target"] for r in calls} == {"sf:apexclass:sampleaction", "sf:apexmethod:sampleaction.run"}
    method_call = next(r for r in calls if r["target"].startswith("sf:apexmethod"))
    assert method_call["qualifiers"]["invocation"] == "invocable_method"
    occurrence = store.evidence[method_call["evidence_ids"][0]]
    assert occurrence["resolution"]["basis"] == "unique_local_invocable"
    assert occurrence["path"].endswith(".flow-meta.xml")
    assert field_relations(store) == {("Contact.Id", "reads", None), ("Contact.LastName", "reads", None), ("Contact.AccountId", "filters", None)}
    assert any(r["source"] == "sf:apexmethod:sampleaction.run" and r["target"] == "sf:object:account" and r["qualifiers"].get("effect") == "database" for r in store.relationships.values())


def test_memory_assignments_are_not_promoted_to_database_field_writes():
    store, _ = analyze("Account row = new Account(Name='Example'); row.Description = 'Text'; update row;")
    assert field_relations(store) == {("Account.Name", "writes", "memory"), ("Account.Description", "writes", "memory")}
    dml = next(r for r in store.relationships.values() if r["target"] == "sf:object:account" and r["relation"] == "writes")
    assert dml["qualifiers"]["field_set"] == "unknown"
    assert not store.diagnostics


def test_inline_constructor_proves_database_fields_but_delete_does_not_write_id():
    store, _ = analyze("insert new Contact(LastName='Example'); delete new Account(Id='fictional');")
    assert ("Contact.LastName", "writes", "database") in field_relations(store)
    assert ("Account.Id", "writes", "database") not in field_relations(store)
    assert ("Account.Id", "writes", "memory") in field_relations(store)


def test_database_methods_resolve_typed_collection_without_guessing_fields():
    store, _ = analyze("Database.update(rows, false); Database.delete(rows); List<SObject> unknown = new List<SObject>(); update unknown;")
    writes = [r for r in store.relationships.values() if r["relation"] == "writes" and r["target"] == "sf:object:account"]
    assert {r["qualifiers"]["operation"] for r in writes} == {"update", "delete"}
    assert all(r["qualifiers"]["collection"] and r["qualifiers"]["field_set"] == "unknown" for r in writes)
    assert "APEX_DML_TYPE_UNRESOLVED" in codes(store)
    assert not field_relations(store)


def test_static_soql_preserves_root_ownership_and_having_filters():
    store, _ = analyze("List<AggregateResult> counts = [SELECT COUNT(Id) FROM Contact WHERE LastName != null GROUP BY AccountId HAVING COUNT(Id) > 1];")
    assert field_relations(store) == {("Contact.Id", "reads", None), ("Contact.Id", "filters", None),
                                      ("Contact.LastName", "filters", None), ("Contact.AccountId", "references", None)}
    assert not store.diagnostics


def test_relationship_chains_and_subqueries_are_not_assigned_to_outer_object():
    store, _ = analyze("List<Account> found = [SELECT Id, Owner.Name, (SELECT LastName FROM Contacts) FROM Account WHERE Parent.Name = 'Example'];")
    assert field_relations(store) == {("Account.Id", "reads", None), ("Account.Name", "filters", None)}
    assert {"APEX_SOQL_FIELD_PATH_UNSUPPORTED", "APEX_SOQL_CONSTRUCT_UNSUPPORTED"} <= codes(store)
    assert "sf:field:account.lastname" not in store.components
    assert "sf:field:account.owner" not in store.components


def test_soql_aliases_fail_explicitly_instead_of_guessing_field_owners():
    store, _ = analyze("List<Account> found = [SELECT a.Name FROM Account a WHERE a.Id != null];")
    assert not field_relations(store)
    assert "APEX_SOQL_ROOT_UNSUPPORTED" in codes(store)


def test_comments_and_query_strings_never_create_fake_dependencies():
    store, _ = analyze("""
      // insert new Secret__c(Flag__c=true); [SELECT Password__c FROM Secret__c]
      /* @InvocableMethod public static void fake() { delete secret; } */
      String example = 'SELECT Secret__c FROM Contact';
      Database.query(example); Database.query('SELECT AnotherSecret__c FROM Account');
      List<Contact> real = [SELECT Id FROM Contact];
    """)
    assert field_relations(store) == {("Contact.Id", "reads", None)}
    assert "APEX_DYNAMIC_SOQL_UNSUPPORTED" in codes(store)
    assert not any("secret" in cid for cid in store.components)
    assert len([c for c in store.components.values() if c["kind"] == "apexmethod"]) == 1


def test_direct_record_fields_obey_lexical_scope_and_same_named_fields():
    store, _ = analyze("Account outer = new Account(); outer.Name = 'A'; { Contact inner = new Contact(); inner.Name = outer.Name; } outer.Name = 'B';")
    assert field_relations(store) == {("Account.Name", "writes", "memory"), ("Account.Name", "reads", "memory"), ("Contact.Name", "writes", "memory")}
    edge = next(r for r in store.relationships.values() if r["target"] == "sf:field:account.name" and r["relation"] == "writes")
    assert len(edge["evidence_ids"]) == 2


def test_unknown_scoped_class_and_indexed_collection_do_not_become_record_fields():
    store, _ = analyze("Account row = new Account(); String text = 'A'; text.Name = 'B'; rows[0].Name = 'C'; row.Owner.Name = 'D';")
    assert not field_relations(store)
    assert {"APEX_FIELD_PATH_UNSUPPORTED", "APEX_RECORD_TYPE_UNRESOLVED"} <= codes(store)


def test_loop_aliases_do_not_reuse_an_outer_variable_type():
    store, _ = analyze("Account row = new Account(); for(Contact row : [SELECT Id FROM Contact]) { row.LastName='X'; update row; }")
    assert field_relations(store) == {("Contact.Id", "reads", None)}
    assert {"APEX_LOOP_ALIAS_UNSUPPORTED", "APEX_RECORD_TYPE_UNRESOLVED", "APEX_DML_TYPE_UNRESOLVED"} <= codes(store)


def test_unique_static_helper_is_linked_and_its_effects_keep_method_source():
    store, _ = analyze("helper();", extra="private static void helper() { insert new Contact(LastName='Synthetic'); }")
    assert any(r["source"] == "sf:apexmethod:sampleaction.run" and r["target"] == "sf:apexmethod:sampleaction.helper" and r["relation"] == "calls" for r in store.relationships.values())
    assert all(r["source"] == "sf:apexmethod:sampleaction.helper" for r in store.relationships.values() if r["relation"] == "writes")


def test_different_arity_overloads_have_distinct_identities_and_resolve_by_arity():
    store, _ = analyze("helper();", extra="private static void helper() {} private static void helper(String text) {}")
    assert "APEX_METHOD_AMBIGUOUS" not in codes(store)
    calls = [r for r in store.relationships.values() if r["relation"] == "calls"]
    assert len(calls) == 1
    assert store.components[calls[0]["target"]]["api_name"] == "SampleAction.helper()"
    assert {c["api_name"] for c in store.components.values() if c.get("qualified_name") == "SampleAction.helper"} == {
        "SampleAction.helper()", "SampleAction.helper(String)"}


@pytest.mark.parametrize("declaration", ["private static void run(List<Account> rows)", "public void run(List<Account> rows)",
                                          "public static void run(Account row)", "public static void run(List<Account> rows, String extra)"])
def test_unsupported_invocable_signatures_keep_only_class_level_flow_call(declaration):
    store, index = analyze("", declaration=declaration)
    attach_flow(store)
    resolve_invocable_calls(store, index)
    calls = [r for r in store.relationships.values() if r["source"] == "sf:flow:syntheticflow"]
    assert {r["target"] for r in calls} == {"sf:apexclass:sampleaction"}
    assert {"APEX_INVOCABLE_SIGNATURE_UNSUPPORTED", "APEX_INVOCABLE_TARGET_UNRESOLVED"} <= codes(store)


def test_multiple_invocable_declarations_are_never_chosen_by_order():
    store, index = analyze("", extra="@InvocableMethod public static void other(List<Account> rows) {}")
    attach_flow(store)
    resolve_invocable_calls(store, index)
    assert not any(r["source"] == "sf:flow:syntheticflow" and r["target"].startswith("sf:apexmethod") for r in store.relationships.values())
    assert "APEX_INVOCABLE_AMBIGUOUS" in codes(store)


def test_parse_error_retains_filename_only_and_extracts_no_recovery_edges():
    store = FactStore()
    index = index_apex({"classes/Broken.cls": b"public class Broken { @InvocableMethod public static void run( { insert new Account(Name='X'); }"}, store)
    extract_apex(index, store)
    assert set(store.components) == {"sf:apexclass:broken"}
    assert not store.relationships
    assert "APEX_PARSE_ERROR" in codes(store)


def test_duplicate_class_files_prevent_method_bridge():
    source = b"public class SampleAction { @InvocableMethod public static void run(List<Account> rows) { update rows; } }"
    store = FactStore()
    index = index_apex({"one/SampleAction.cls": source, "two/SampleAction.cls": source}, store)
    extract_apex(index, store)
    attach_flow(store)
    resolve_invocable_calls(store, index)
    assert store.components["sf:apexclass:sampleaction"]["definition_status"] == "ambiguous"
    assert not store.relationships


def test_namespaced_fields_and_whitespace_stable_occurrences():
    source = "public class SampleAction { @InvocableMethod public static void run(List<String> ids) { insert new pkg__Item__c(pkg__Value__c='X'); } }"
    results = []
    for text in (source, source.replace(" { ", " {\n  ")):
        store = FactStore()
        index = index_apex({"classes/SampleAction.cls": text.encode()}, store)
        extract_apex(index, store)
        assert ("pkg__Item__c.pkg__Value__c", "writes", "database") in field_relations(store)
        results.append(store.finish())
    assert {r["id"] for r in results[0]["relationships"]} == {r["id"] for r in results[1]["relationships"]}
    assert {e["id"] for e in results[0]["evidence"]} == {e["id"] for e in results[1]["evidence"]}
    assert all(e["line"] and e["column"] and e["content_hash"] for e in results[0]["evidence"])


def test_apex_parser_and_bridge_run_without_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network access attempted")
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    store, index = analyze("update rows;")
    attach_flow(store)
    resolve_invocable_calls(store, index)
    assert any(r["target"] == "sf:apexmethod:sampleaction.run" and r["relation"] == "calls" for r in store.relationships.values())


def test_catch_parameter_shadows_outer_record_without_inventing_fields():
    store, _ = analyze("Account row = new Account(); try {} catch(Exception row) { row.Name='X'; update row; }")
    assert not field_relations(store)
    assert "APEX_DML_TYPE_UNRESOLVED" in codes(store)


def test_static_query_locator_is_not_mislabeled_as_dynamic_soql():
    store, _ = analyze("Database.getQueryLocator([SELECT Id FROM Account]);")
    assert field_relations(store) == {("Account.Id", "reads", None)}
    assert "APEX_DYNAMIC_SOQL_UNSUPPORTED" not in codes(store)


def test_dml_user_mode_and_upsert_key_are_explicit():
    store, _ = analyze("update as user rows; upsert rows Account.External__c;")
    writes = [r for r in store.relationships.values() if r["relation"] == "writes"]
    assert any(r["qualifiers"].get("security_mode") == "user" for r in writes)
    assert "APEX_DML_KEY_UNSUPPORTED" in codes(store)
    assert not field_relations(store)


def test_select_alias_is_never_fabricated_as_a_root_object_field():
    store, _ = analyze("List<AggregateResult> counts=[SELECT count(Id) cnt FROM Account GROUP BY Type ORDER BY cnt];")
    assert "sf:field:account.cnt" not in store.components
    assert field_relations(store) == {("Account.Type", "references", None)}
    assert "APEX_SOQL_ALIAS_UNSUPPORTED" in codes(store)


@pytest.mark.parametrize("member", ["private static FakeDb Database;", "public static FakeDb Database {get;set;}"])
def test_class_field_or_property_shadows_database_receiver(member):
    store, _ = analyze("Database.update(new Contact(LastName='Synthetic'));", extra=member)
    assert not any(r["relation"] == "writes" and r["qualifiers"].get("effect") == "database" for r in store.relationships.values())
    assert ("Contact.LastName", "writes", "memory") in field_relations(store)
    assert "APEX_EXTERNAL_CALL_UNSUPPORTED" in codes(store)


def test_helper_arity_mismatch_does_not_bind_local_method():
    store, _ = analyze("helper(new Contact());", extra="private static void helper() {}")
    assert not any(r["relation"] == "calls" for r in store.relationships.values())
    assert "APEX_CALL_UNRESOLVED" in codes(store)


def test_inherited_dispatch_and_database_shadowing_are_not_guessed():
    source = b'''public class SampleAction extends Base {
      @InvocableMethod public static void run(List<Account> rows) {
        helper(new Contact()); Database.update(new Contact(LastName='Synthetic'));
      } private static void helper() {} }'''
    store = FactStore()
    index = index_apex({"classes/SampleAction.cls": source}, store)
    extract_apex(index, store)
    assert not any(r["relation"] == "calls" for r in store.relationships.values())
    assert not any(r["relation"] == "writes" and r["qualifiers"].get("effect") == "database" for r in store.relationships.values())
    assert "APEX_INHERITED_DISPATCH_UNSUPPORTED" in codes(store)


def test_comments_are_not_call_arguments_or_generic_type_arguments():
    store, _ = analyze("""
      List</* record type */ Contact> contacts = [SELECT Id FROM Contact];
      Contact contact = new Contact(/* initializer */ LastName='Synthetic');
      Database.update(/* first argument */ contact /* before comma */, false);
      update contacts;
      helper(/* first */ contact /* trailing */);
    """, extra="private static void helper(Contact value) {}")
    writes = [r for r in store.relationships.values() if r["target"] == "sf:object:contact" and r["relation"] == "writes" and r["qualifiers"].get("effect") == "database"]
    assert {r["qualifiers"]["collection"] for r in writes} == {False, True}
    assert any(r["relation"] == "calls" and r["target"] == "sf:apexmethod:sampleaction.helper" for r in store.relationships.values())
    assert not store.diagnostics


def test_invocable_parameter_generic_comments_preserve_supported_signature():
    store, index = analyze("update rows;", declaration="public static void run(List</* records */ Contact> rows)")
    attach_flow(store)
    resolve_invocable_calls(store, index)
    assert any(r["source"] == "sf:flow:syntheticflow" and r["target"] == "sf:apexmethod:sampleaction.run" for r in store.relationships.values())
    assert not store.diagnostics


def test_expression_receiver_preserves_direct_field_read_and_nested_call():
    store, _ = analyze("Account row = new Account(); String name = row.Name.trim(); String other = text().trim();",
                       extra="private static String text() { return 'Synthetic'; }")
    assert ("Account.Name", "reads", "memory") in field_relations(store)
    assert any(r["relation"] == "calls" and r["target"] == "sf:apexmethod:sampleaction.text" for r in store.relationships.values())
    assert "APEX_EXTERNAL_CALL_UNSUPPORTED" in codes(store)


def test_complex_field_receiver_preserves_call_without_truncating_field_chain():
    store, _ = analyze("String name = getRow().Owner.Name.trim();",
                       extra="private static Account getRow() { return new Account(); }")
    assert any(r["relation"] == "calls" and r["target"] == "sf:apexmethod:sampleaction.getrow" for r in store.relationships.values())
    assert not field_relations(store)
    assert "APEX_FIELD_PATH_UNSUPPORTED" in codes(store)


@pytest.mark.parametrize("second", [
    b"public class SampleAction { public static void disjoint() {} }",
    b"public class SampleAction { broken( ",
])
def test_duplicate_class_owners_do_not_expose_disjoint_or_fallback_methods(second):
    first = b"public class SampleAction { @InvocableMethod public static void run(List<Account> rows) { update rows; } }"
    store = FactStore()
    index = index_apex({"package-one/classes/SampleAction.cls": first, "package-two/classes/SampleAction.cls": second}, store)
    extract_apex(index, store)
    attach_flow(store)
    resolve_invocable_calls(store, index)
    assert store.components["sf:apexclass:sampleaction"]["definition_status"] == "ambiguous"
    assert not any(c["kind"] == "apexmethod" for c in store.components.values())
    assert not store.relationships
    diagnostics = [d for d in store.diagnostics if d["code"] == "APEX_CLASS_AMBIGUOUS"]
    assert {d["location"]["path"] for d in diagnostics} == {"package-one/classes/SampleAction.cls", "package-two/classes/SampleAction.cls"}
