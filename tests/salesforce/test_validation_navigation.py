"""Cross-object validation dependencies remain visible from every used lookup."""
from __future__ import annotations

from xml.sax.saxutils import escape

import pytest

from graphify.salesforce import query
from graphify.salesforce.formula_refs import formula_references
from graphify.salesforce.inventory import extract_inventory, index_definitions
from graphify.salesforce.relationship_paths import resolve_field_path
from graphify.salesforce.schema import FactStore, SCHEMA_VERSION, stable_id, validate_bundle
from graphify.salesforce.validation_rules import extract_validation_rule
from graphify.salesforce.xml_source import parse_xml


def rule(expression, owner="Review__c"):
    return parse_xml(f"force-app/main/default/objects/{owner}/validationRules/CheckLink.validationRule-meta.xml",
                     ("<ValidationRule><active>true</active><errorConditionFormula>" + escape(expression)
                      + "</errorConditionFormula></ValidationRule>").encode())


def field(owner, name, *, data_type="Lookup", target=None, package="force-app/main/default"):
    body = f"<CustomField><type>{data_type}</type>"
    if target:
        body += f"<referenceTo>{target}</referenceTo>"
    return parse_xml(f"{package}/objects/{owner}/fields/{name}.field-meta.xml", (body + "</CustomField>").encode())


def extract(document, *definitions):
    documents = [document, *definitions]
    store = FactStore()
    index_definitions(documents, store)
    extract_inventory(documents, store)
    extract_validation_rule(document, store)
    bundle = {"manifest": {"schema_version": SCHEMA_VERSION,
                           "inputs": {doc.path: doc.content_hash for doc in documents}},
              "facts": store.finish(), "diagnostics": store.diagnostics}
    bundle["manifest"]["snapshot_id"] = stable_id("snapshot", bundle)
    validate_bundle(bundle)
    return store, bundle


def test_each_traversed_lookup_is_queryable_without_losing_formula_occurrences():
    expression = 'AND(Place__r.Company__r.Name = "First", Place__r.Company__r.Name = "Second")'
    document = rule(expression)
    store, bundle = extract(document, field("Review__c", "Place__c", target="Place__c"),
                            field("Place__c", "Company__c", target="Company__c"))
    source = "sf:validationrule:review__c.checklink"
    reads = query.deps(bundle, source, relation="reads")["results"]
    assert len(reads) == 3
    assert {edge["target"] for edge in reads} == {
        "sf:field:review__c.place__c", "sf:field:place__c.company__c", "sf:field:company__c.name"}
    assert all(len(edge["evidence"]) == 2 for edge in reads)
    for step, lookup in enumerate(("Review__c.Place__c", "Place__c.Company__c"), 1):
        refs = query.refs(bundle, lookup, relation="reads")
        assert not refs["truncated"] and len(refs["results"]) == 1
        navigation = refs["results"][0]
        assert navigation["source"] == source
        assert navigation["qualifiers"] == {"context": "validation_formula_navigation"}
        assert {e["resolution"]["navigation_step"]["index"] for e in navigation["evidence"]} == {step}
        assert len({e["resolution"]["formula_reference"]["decoded_character_offset"]
                    for e in navigation["evidence"]}) == 2
        assert all(e["content_hash"] == document.content_hash
                   and e["locator"].endswith(f"/navigation[{step}]") for e in navigation["evidence"])
        impact = query.impact(bundle, lookup, depth=1)
        assert not impact["truncated"]
        assert [edge["source"] for edge in impact["results"]] == [source]
    terminal, = query.refs(bundle, "Company__c.Name", relation="reads")["results"]
    assert terminal["qualifiers"] == {"context": "validation_formula"}
    assert all("navigation_step" not in e["resolution"] for e in terminal["evidence"])
    assert not any(edge["relation"] == "writes" for edge in store.relationships.values())


def test_recursive_navigation_retains_each_hop_even_when_lookup_identity_repeats():
    store, bundle = extract(rule('Parent.Parent.Name = "Example"', owner="Account"))
    navigation, = query.refs(bundle, "Account.ParentId", relation="reads")["results"]
    assert len(navigation["evidence"]) == 2
    assert {e["resolution"]["navigation_step"]["index"] for e in navigation["evidence"]} == {1, 2}
    assert len({e["locator"] for e in navigation["evidence"]}) == 2
    assert {e["resolution"]["formula_reference"]["decoded_character_offset"]
            for e in navigation["evidence"]} == {0}
    assert store.components["sf:field:account.parentid"]["definition_status"] == "referenced_only"


def test_direct_operand_and_navigation_read_of_same_lookup_are_distinguishable():
    _, bundle = extract(rule('AND(Place__c != "", Place__r.Name != "")'),
                        field("Review__c", "Place__c", target="Place__c"))
    result = query.refs(bundle, "Review__c.Place__c", relation="reads")
    assert len(result["results"]) == 2
    assert {edge["qualifiers"]["context"] for edge in result["results"]} == {
        "validation_formula", "validation_formula_navigation"}


def test_unsupported_later_hop_does_not_publish_partial_navigation_reads():
    store, _ = extract(rule('Place__r.Unknown__r.Name = "Example"'),
                       field("Review__c", "Place__c", target="Place__c"))
    assert not any(edge["relation"] == "reads" for edge in store.relationships.values())
    assert any(d["code"] == "VALIDATION_RULE_FIELD_PATH_UNRESOLVED" for d in store.diagnostics)
    assert "sf:field:place__c.unknown__c" not in store.components


@pytest.mark.parametrize("target", [None, "Account"])
def test_account_hierarchy_parent_supports_nested_record_type_and_reverse_queries(target):
    document = rule('Parent.RecordType.DeveloperName = "Example"', owner="Account")
    store, bundle = extract(document, field("Account", "ParentId", data_type="Hierarchy", target=target))
    terminal = query.refs(bundle, "RecordType.DeveloperName", relation="reads")["results"]
    assert len(terminal) == 1
    assert [step["field"] for step in terminal[0]["evidence"][0]["resolution"]["steps"]] == [
        "sf:field:account.parentid", "sf:field:account.recordtypeid"]
    assert len(query.refs(bundle, "Account.ParentId", relation="reads")["results"]) == 1
    assert len(query.refs(bundle, "Account.RecordTypeId", relation="reads")["results"]) == 1
    resolved = resolve_field_path(store, "Account", "Parent.Name")
    assert resolved is not None and resolved["owner"] == "Account"


@pytest.mark.parametrize("owner,relationship,name,target", [
    ("Account", "Parent", "ParentId", "Contact"),
    ("Order", "Account", "AccountId", "Account"),
    ("Contact", "Account", "AccountId", "Account"),
])
def test_hierarchy_exception_cannot_resolve_other_pairs_or_conflicting_targets(owner, relationship, name, target):
    store, _ = extract(rule(f'{relationship}.Name = "Example"', owner=owner),
                       field(owner, name, data_type="Hierarchy", target=target))
    assert not any(edge["relation"] == "reads" for edge in store.relationships.values())
    assert any(d["code"] == "VALIDATION_RULE_FIELD_PATH_UNRESOLVED" for d in store.diagnostics)
    if owner == "Account":
        assert resolve_field_path(store, owner, relationship + ".Name") is None


def test_ambiguous_hierarchy_declaration_remains_unresolved():
    store, _ = extract(rule('Parent.Name = "Example"', owner="Account"),
                       field("Account", "ParentId", data_type="Hierarchy"),
                       field("Account", "ParentId", data_type="Hierarchy", package="force-app/other"))
    assert not any(edge["relation"] == "reads" for edge in store.relationships.values())


@pytest.mark.parametrize("expression,expected", [
    ("NOT $Setup.Switch__c.Disabled__c", ["$Setup.Switch__c.Disabled__c"]),
    ("Not OR(First__c, Second__c)", ["First__c", "Second__c"]),
    ("not /* reason */ ISBLANK /* before call */ ( Name )", ["Name"]),
    ("NOT // reason\n (Enabled__c)", ["Enabled__c"]),
    ("NOT(Not__c) || Nothing__c || Related__r.Not__c", ["Not__c", "Nothing__c", "Related__r.Not__c"]),
    ("ISBLANK // reason\n (Name)", ["Name"]),
    ("{!Not} || {!Not.Not__c}", ["Not", "Not.Not__c"]),
])
def test_not_keyword_and_commented_function_names_do_not_become_fields(expression, expected):
    references, issues = formula_references(expression)
    assert [ref.text for ref in references] == expected
    assert not issues
    assert all(expression[ref.offset:].startswith(ref.text) or expression[ref.offset:].startswith("{!")
               for ref in references)


def test_validation_not_forms_do_not_create_phantom_components():
    store, bundle = extract(rule('AND(NOT $Setup.Switch__c.Disabled__c, Not OR(Enabled__c, Override__c))'))
    assert "sf:field:review__c.not" not in store.components
    assert {edge["target"] for edge in query.deps(bundle, "Review__c.CheckLink", relation="reads")["results"]} == {
        "sf:field:switch__c.disabled__c", "sf:field:review__c.enabled__c", "sf:field:review__c.override__c"}
