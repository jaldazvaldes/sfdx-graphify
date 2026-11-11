"""Fictional configured metadata accessors; never project-specific defaults."""
from __future__ import annotations

import copy
import json

import pytest

from graphify.salesforce.apex import index_apex
from graphify.salesforce.custom_metadata_accessors import parse_accessor_rules, extract_custom_metadata_accessors
from graphify.salesforce.inventory import index_definitions, extract_inventory
from graphify.salesforce.schema import DataError, FactStore
from graphify.salesforce.xml_source import parse_xml


RULE = {"class": "RegionConfig", "method": "enabledRegions", "arity": 1,
        "field_argument": 1, "type": "Region_Options__mdt"}
TWO_ARGUMENT_RULE = {"class": "RegionConfig", "method": "readOption", "arity": 2,
                     "field_argument": 2, "type": "Region_Options__mdt"}
TARGET = "sf:field:region_options__mdt.enable_orders__c"
FIELD_PATH = "objects/Region_Options__mdt/fields/Enable_Orders__c.field-meta.xml"
ACCESSOR = ("public class RegionConfig { public static List<String> enabledRegions(String field) { return null; } "
            "public static Boolean readOption(String region, String field) { return false; } }")


def analyze(body, *, rules=None, accessor=ACCESSOR, parameters="", member="", inheritance="", extra_classes=None,
            include_field=True, duplicate_field=False):
    store = FactStore()
    documents = [parse_xml("objects/Region_Options__mdt/Region_Options__mdt.object-meta.xml", b"<CustomObject/>")]
    if include_field:
        documents.append(parse_xml(FIELD_PATH, b"<CustomField><type>Checkbox</type></CustomField>"))
        if duplicate_field:
            documents.append(parse_xml("second/" + FIELD_PATH, b"<CustomField><type>Checkbox</type></CustomField>"))
    index_definitions(documents, store)
    extract_inventory(documents, store)
    sources = {"classes/Runner.cls": f"public class Runner {inheritance} {{ {member} public static void run({parameters}) {{\n{body}\n}} }}".encode(),
               **(extra_classes or {})}
    if accessor is not None:
        sources["classes/RegionConfig.cls"] = accessor.encode()
    index = index_apex(sources, store)
    extract_custom_metadata_accessors(index, store, [RULE, TWO_ARGUMENT_RULE] if rules is None else rules)
    edges = [edge for edge in store.relationships.values()
             if edge["qualifiers"].get("context") == "custom_metadata_accessor"]
    return store, edges


def test_rule_normalization_is_deterministic_without_mutating_input():
    source = [TWO_ARGUMENT_RULE, RULE]
    original = copy.deepcopy(source)
    parsed = parse_accessor_rules(json.dumps(source).encode())
    assert source == original
    assert parsed == parse_accessor_rules(source[::-1])
    uppercase = [{key: value.upper() if isinstance(value, str) else value for key, value in rule.items()}
                 for rule in source]
    assert parsed == parse_accessor_rules(uppercase)
    assert parsed[0] == {"class": "regionconfig", "method": "enabledregions", "arity": 1,
                         "field_argument": 1, "type": "region_options__mdt"}


@pytest.mark.parametrize("data", [None, {}, [None], [dict(RULE, unknown=True)],
    [{k: v for k, v in RULE.items() if k != "class"}], [dict(RULE, arity=True)], [dict(RULE, arity=0)],
    [dict(RULE, field_argument=True)], [dict(RULE, field_argument=0)], [dict(RULE, field_argument=2)],
    [dict(RULE, type="Region_Options__c")], [dict(RULE, type="__mdt")],
    [dict(RULE, **{"class": "namespace.RegionConfig"})], [dict(RULE, method="method()")],
    [RULE, dict(RULE, **{"class": "REGIONCONFIG"})], b"\xff", "[", '[{"class":"One","class":"Two"}]'])
def test_invalid_or_ambiguous_rule_configuration_fails_before_extraction(data):
    with pytest.raises(DataError):
        parse_accessor_rules(data)


def test_one_and_two_argument_accessors_create_precise_field_reads_with_proof():
    store, edges = analyze("RegionConfig.enabledRegions('Enable_Orders__c');\n"
                           "RegionConfig.readOption('FICTIONAL', 'Enable_Orders__c');")
    assert len(edges) == 1
    edge = edges[0]
    assert (edge["source"], edge["target"], edge["relation"]) == ("sf:apexmethod:runner.run", TARGET, "reads")
    assert len(edge["evidence_ids"]) == 2
    evidence = [store.evidence[eid] for eid in edge["evidence_ids"]]
    assert {item["line"] for item in evidence} == {2, 3}
    assert all(item["path"] == "classes/Runner.cls" and item["rule"] == "apex.custom_metadata_accessor" for item in evidence)
    assert all(item["resolution"]["field_definition"]["path"] == FIELD_PATH for item in evidence)
    assert {item["resolution"]["accessor_method"] for item in evidence} == {
        "sf:apexmethod:regionconfig.enabledregions", "sf:apexmethod:regionconfig.readoption"}
    assert "FICTIONAL" not in json.dumps(evidence)


@pytest.mark.parametrize("argument", ["field", "'Enable_' + 'Orders__c'", "'Other__c'", "' Enable_Orders__c'",
                                       "'Region_Options__mdt.Enable_Orders__c'", "'Enable_Orders__c\\n'", "null"])
def test_dynamic_or_unresolved_field_argument_is_diagnostic_even_if_variable_was_initialized_literal(argument):
    store, edges = analyze(f"String field = 'Enable_Orders__c'; RegionConfig.enabledRegions({argument});")
    assert not edges
    diagnostics = [d for d in store.diagnostics if d["code"] == "APEX_CUSTOM_METADATA_ACCESSOR_UNRESOLVED"]
    assert len(diagnostics) == 1 and diagnostics[0]["location"]["path"] == "classes/Runner.cls"


def test_wrong_arity_other_method_and_unconfigured_calls_never_create_accessor_reads():
    _, edges = analyze("RegionConfig.enabledRegions('Enable_Orders__c', 'other');\n"
                       "RegionConfig.readOption('Enable_Orders__c');")
    assert not edges
    _, edges = analyze("RegionConfig.enabledRegions('Enable_Orders__c');", rules=[])
    assert not edges


@pytest.mark.parametrize("parameters,member,body", [
    ("Object RegionConfig", "", "RegionConfig.enabledRegions('Enable_Orders__c');"),
    ("", "Object RegionConfig;", "RegionConfig.enabledRegions('Enable_Orders__c');"),
    ("", "Object RegionConfig {get;set;}", "RegionConfig.enabledRegions('Enable_Orders__c');"),
    ("", "class RegionConfig {}", "RegionConfig.enabledRegions('Enable_Orders__c');"),
    ("", "", "Object RegionConfig; RegionConfig.enabledRegions('Enable_Orders__c');"),
    ("", "", "Object RegionConfig = RegionConfig.enabledRegions('Enable_Orders__c');"),
    ("List<Object> rows", "", "for (Object RegionConfig : rows) { RegionConfig.enabledRegions('Enable_Orders__c'); }"),
])
def test_shadowed_receiver_does_not_acquire_configured_accessor_semantics(parameters, member, body):
    store, edges = analyze(body, parameters=parameters, member=member)
    assert not edges
    assert any(d["code"] == "APEX_CUSTOM_METADATA_ACCESSOR_UNRESOLVED" for d in store.diagnostics)


def test_nested_shadow_does_not_poison_later_class_qualified_call():
    _, edges = analyze("{ Object RegionConfig; RegionConfig.enabledRegions('Enable_Orders__c'); }\n"
                       "RegionConfig.enabledRegions('Enable_Orders__c');")
    assert len(edges) == 1 and len(edges[0]["evidence_ids"]) == 1


@pytest.mark.parametrize("accessor", [None,
    "public class RegionConfig { public List<String> enabledRegions(String name) {return null;} }",
    "public class RegionConfig { private static List<String> enabledRegions(String name) {return null;} }",
    "public class RegionConfig extends MissingBase { public static List<String> enabledRegions(String name) {return null;} }",
    "public class RegionConfig { public static List<String> enabledRegions(String name) {return null;} public static List<String> enabledRegions(Integer name) {return null;} }",
])
def test_missing_instance_private_or_ambiguous_accessor_declaration_is_not_proven(accessor):
    store, edges = analyze("RegionConfig.enabledRegions('Enable_Orders__c');", accessor=accessor)
    assert not edges
    assert any(d.get("reason") == "accessor_declaration_not_proven" for d in store.diagnostics)


def test_unknown_caller_hierarchy_is_not_proof_of_an_unshadowed_qualifier():
    _, edges = analyze("RegionConfig.enabledRegions('Enable_Orders__c');", inheritance="extends MissingBase")
    assert not edges


@pytest.mark.parametrize("include_field,duplicate_field", [(False, False), (True, True)])
def test_missing_or_ambiguous_local_field_is_not_invented(include_field, duplicate_field):
    store, edges = analyze("RegionConfig.enabledRegions('Enable_Orders__c');", include_field=include_field,
                           duplicate_field=duplicate_field)
    assert not edges
    if not include_field:
        assert TARGET not in store.components


def test_case_insensitive_names_and_comments_preserve_exact_literal_read():
    _, edges = analyze("regionconfig.enabledregions(/* field */ 'enable_orders__c');")
    assert [edge["target"] for edge in edges] == [TARGET]


def test_constructor_call_is_attributed_to_constructor():
    _, edges = analyze("", member="public Runner() { RegionConfig.enabledRegions('Enable_Orders__c'); }")
    assert {(edge["source"], edge["target"]) for edge in edges} == {("sf:apexmethod:runner.new%28%29", TARGET)}


def test_comments_strings_and_instance_aliases_do_not_match_the_configured_class_call():
    store, edges = analyze("// RegionConfig.enabledRegions('Enable_Orders__c');\n"
                           "String note = 'RegionConfig.enabledRegions';\n"
                           "helper.enabledRegions('Enable_Orders__c');", parameters="RegionConfig helper")
    assert not edges
    assert not any(d["code"] == "APEX_CUSTOM_METADATA_ACCESSOR_UNRESOLVED" for d in store.diagnostics)


def test_duplicate_accessor_class_definitions_are_not_selected():
    _, edges = analyze("RegionConfig.enabledRegions('Enable_Orders__c');",
                       extra_classes={"second/RegionConfig.cls": ACCESSOR.encode()})
    assert not edges


def test_known_inherited_member_shadows_caller_qualifier():
    _, edges = analyze("RegionConfig.enabledRegions('Enable_Orders__c');", inheritance="extends Base",
                       extra_classes={"classes/Base.cls": b"public virtual class Base { public static Object RegionConfig; }"})
    assert not edges
