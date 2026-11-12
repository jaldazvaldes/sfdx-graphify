"""Synthetic type-literal classification; no org source or metadata."""
from __future__ import annotations

import hashlib

import pytest

from graphify.salesforce.apex import PARSER_VERSION, extract_apex, index_apex
from graphify.salesforce.schema import FactStore
from tests.salesforce.test_apex_review_fixes import build


def _edges(store):
    return [edge for edge in store.relationships.values() if edge["qualifiers"].get("context") == "class_literal"]


def _extract(expression, *, extra=(), metadata=(), members="", superclass="", parameters=""):
    source = (f"public class Example {superclass} {{ {members}\n"
              f"  public static void run({parameters}) {{ Type result = {expression}; }}\n}}")
    store = build([("Example", source), *extra], metadata=metadata)
    return store, source


def _object(name):
    return (f"objects/{name}/{name}.object-meta.xml", "<CustomObject/>")


@pytest.mark.parametrize("name", ["Account", "AsyncApexJob", "LocalComplaint__c", "Country_Options__mdt", "vendor__Event__e"])
def test_proven_sobject_literals_reference_objects_without_creating_apex_classes(name):
    metadata = [] if name in {"Account", "AsyncApexJob"} else [_object(name)]
    store, source = _extract(name + ".class", metadata=metadata)
    edge, = _edges(store)
    assert edge["source"] == "sf:apexmethod:example.run"
    assert edge["target"] == "sf:object:" + name.casefold()
    assert edge["relation"] == "references"
    assert "sf:apexclass:" + name.casefold() not in store.components
    occurrence, = [store.evidence[eid] for eid in edge["evidence_ids"]]
    assert occurrence["rule"] == "apex.class_literal"
    assert occurrence["resolution"]["basis"] == "literal_sobject_type"
    assert occurrence["path"] == "classes/Example.cls"
    assert occurrence["line"] == occurrence["end_line"] == 2
    line = source.splitlines()[1]
    assert line[occurrence["column"] - 1:occurrence["end_column"] - 1] == name + ".class"
    assert occurrence["content_hash"] == hashlib.sha256(source.encode()).hexdigest()


def test_static_soql_proves_an_object_outside_the_bounded_standard_vocabulary():
    source = "public class Example { void run() { Type t = TaskRelation.class; List<TaskRelation> rows = [SELECT Id FROM TaskRelation]; } }"
    store = build([("Example", source)])
    edge, = _edges(store)
    assert edge["target"] == "sf:object:taskrelation"
    assert "sf:apexclass:taskrelation" not in store.components
    assert store.evidence[edge["evidence_ids"][0]]["resolution"]["object_proof"] == "indexed_object"


@pytest.mark.parametrize("name", ["Domain", "AccountReader"])
def test_unique_local_apex_class_remains_a_class(name):
    store, _ = _extract(name + ".class", extra=[(name, f"public class {name} {{}}")])
    edge, = _edges(store)
    assert edge["target"] == "sf:apexclass:" + name.casefold()
    assert "sf:object:" + name.casefold() not in store.components
    assert store.evidence[edge["evidence_ids"][0]]["resolution"] == {"basis": "literal_class_token", "parser": PARSER_VERSION}


def test_schema_qualification_selects_proven_object_even_with_same_named_local_class():
    store, _ = _extract("Schema.Account.class", extra=[("Account", "public class Account {}")])
    edge, = _edges(store)
    assert edge["target"] == "sf:object:account"
    assert "sf:apexclass:schema.account" not in store.components


def test_proven_package_namespace_preserves_external_apex_class_reference():
    store, _ = _extract("vendor /* namespace */ . Service.class", metadata=[_object("vendor__Config__c")])
    edge, = _edges(store)
    assert edge["target"] == "sf:apexclass:vendor.service"
    assert store.components[edge["target"]]["definition_status"] == "referenced_only"
    assert store.components[edge["target"]]["namespace"] == "vendor"
    assert store.evidence[edge["evidence_ids"][0]]["resolution"] == {"basis": "literal_class_token", "parser": PARSER_VERSION}


@pytest.mark.parametrize("expression", ["Missing.class", "Unknown__c.class", "unproven.Service.class",
                                         "Outer.Inner.class", "Schema.Unknown.class", "String.class",
                                         "Account?.class", "Schema?.Account.class", "(Account).class"])
def test_unknown_unsupported_or_expression_types_do_not_fabricate_classes(expression):
    store, _ = _extract(expression)
    assert not _edges(store)
    assert {component["api_name"] for component in store.components.values() if component["kind"] == "apexclass"} == {"Example"}
    assert any(item["code"] == "APEX_CLASS_LITERAL_UNRESOLVED" for item in store.diagnostics)


@pytest.mark.parametrize("name,metadata", [("Account", []), ("LocalComplaint__c", [_object("LocalComplaint__c")])])
def test_competing_class_and_object_proofs_remain_ambiguous(name, metadata):
    store, _ = _extract(name + ".class", extra=[(name, f"public class {name} {{}}")], metadata=metadata)
    assert not _edges(store)
    diagnostic, = [item for item in store.diagnostics if item["code"] == "APEX_CLASS_LITERAL_UNRESOLVED"]
    assert diagnostic["state"] == "ambiguous"


@pytest.mark.parametrize("kwargs", [
    {"parameters": "Object Account"},
    {"members": "public Object Account;"},
    {"members": "public Object Account { get; set; }"},
    {"members": "public class Account {}"},
    {"superclass": "extends MissingBase"},
    {"superclass": "extends Base", "extra": [("Base", "public virtual class Base { public Object Account; }")]},
    {"superclass": "extends Base", "extra": [("Base", "public virtual class Base { public class Account {} }")]},
])
def test_lexical_and_inherited_shadows_prevent_unqualified_type_resolution(kwargs):
    store, _ = _extract("Account.class", **kwargs)
    assert not _edges(store)
    assert "sf:apexclass:account" not in store.components
    assert any(item["code"] == "APEX_CLASS_LITERAL_UNRESOLVED" for item in store.diagnostics)


@pytest.mark.parametrize("root,expression,extra,metadata", [
    ("vendor", "vendor.Service.class", [], [_object("vendor__Config__c")]),
    ("Schema", "Schema.Account.class", [], []),
    ("Domain", "Domain.class", [("Domain", "public class Domain {}")], []),
])
def test_scope_shadowing_recovers_after_inner_block(root, expression, extra, metadata):
    source = (f"public class Example {{ void run() {{ {{ Object {root}; Type blocked = {expression}; }} "
              f"Type permitted = {expression}; }} }}")
    store = build([("Example", source), *extra], metadata=metadata)
    edge, = _edges(store)
    assert len(edge["evidence_ids"]) == 1
    assert any(item["code"] == "APEX_CLASS_LITERAL_UNRESOLVED" for item in store.diagnostics)


@pytest.mark.parametrize("members,extra", [("", [("vendor", "public class vendor {}")]), ("public class vendor {}", [])])
def test_namespace_that_is_also_a_local_type_is_not_assumed_to_be_a_package(members, extra):
    store, _ = _extract("vendor.Service.class", members=members, extra=extra, metadata=[_object("vendor__Config__c")])
    assert not _edges(store)
    assert "sf:apexclass:vendor.service" not in store.components


@pytest.mark.parametrize("root,literal,metadata", [
    ("vendor", "vendor.Service.class", [_object("vendor__Config__c")]),
    ("Schema", "Schema.Account.class", []),
])
def test_filename_only_local_class_blocks_namespace_assumption(root, literal, metadata):
    store, _ = _extract(literal, extra=[(root, "not a valid Apex class")], metadata=metadata)
    assert store.components["sf:apexclass:" + root.casefold()]["inventory_method"] == "filename"
    assert not _edges(store)
    assert any(item["code"] == "APEX_CLASS_LITERAL_UNRESOLVED" for item in store.diagnostics)


def test_initializing_local_shadow_is_not_a_type_qualifier():
    store = build([("Example", "public class Example { void run() { Object Account = Account.class; } }")])
    assert not _edges(store)
    assert "sf:apexclass:account" not in store.components


@pytest.mark.parametrize("kind,name", [("apexclass", "Domain"), ("object", "LocalComplaint__c")])
def test_duplicate_target_definitions_do_not_emit_literal_edges(kind, name):
    store = FactStore()
    store.ensure_component(kind, name, {"path": "one", "locator": "/"})
    store.ensure_component(kind, name, {"path": "two", "locator": "/"})
    index = index_apex({"classes/Example.cls": f"public class Example {{ void run() {{ Type t = {name}.class; }} }}".encode()}, store)
    extract_apex(index, store)
    assert not _edges(store)
    assert any(item["state"] == "ambiguous" for item in store.diagnostics)


def test_trigger_literals_keep_exact_source_and_distinguish_domain_from_object():
    source = ("trigger Sample on Account (before insert) {\n"
              "  Framework.dispatch(Domain.class); Type record = Account.class;\n}")
    store = build([("Domain", "public class Domain {}")], trigger=source)
    assert {edge["target"] for edge in _edges(store)} == {"sf:apexclass:domain", "sf:object:account"}
    assert {edge["source"] for edge in _edges(store)} == {"sf:apextrigger:sample"}
    assert all(store.evidence[edge["evidence_ids"][0]]["path"] == "triggers/Sample.trigger" for edge in _edges(store))
    assert not any(edge["relation"] == "calls" for edge in store.relationships.values())


def test_comments_and_strings_do_not_create_class_literal_dependencies():
    store = build([("Example", "public class Example { void run() { /* Hidden.class */ String t = 'Unknown.class'; Type real = aCcOuNt /* info */ . class; } }")])
    edge, = _edges(store)
    assert edge["target"] == "sf:object:account"
    assert "sf:apexclass:hidden" not in store.components and "sf:apexclass:unknown" not in store.components
