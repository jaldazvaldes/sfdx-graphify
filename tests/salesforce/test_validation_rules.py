"""Validation rule extraction contracts on fictional metadata only."""
from __future__ import annotations

import json

import pytest

from graphify.salesforce import query
from graphify.salesforce.discovery import supported_source
from graphify.salesforce.graphify_adapter import projection_graph
from graphify.salesforce.inventory import extract_inventory, identity, index_definitions
from graphify.salesforce.relationship_paths import resolve_field_path
from graphify.salesforce.schema import DataError, FactStore, validate_bundle
from graphify.salesforce.service import build_project
from graphify.salesforce.snapshot import load
from graphify.salesforce.validation_rules import extract_validation_rule
from graphify.salesforce.xml_source import parse_xml


def rule(body, *, owner="Review__c", name="RequireName", package="force-app/main/default"):
    return parse_xml(f"{package}/objects/{owner}/validationRules/{name}.validationRule-meta.xml",
                     (f'<ValidationRule xmlns="http://soap.sforce.com/2006/04/metadata"><fullName>{name}</fullName>'
                      + body + "</ValidationRule>").encode())


def extract(document, *metadata):
    store = FactStore()
    index_definitions([document, *metadata], store)
    extract_inventory([document, *metadata], store)
    extract_validation_rule(document, store)
    return store


def reads(store):
    return [edge for edge in store.relationships.values() if edge["relation"] == "reads"]


def terminal_reads(store):
    return [edge for edge in reads(store) if edge["qualifiers"]["context"] == "validation_formula"]


def project(tmp_path, *documents):
    root = tmp_path / "source"
    root.mkdir()
    (root / "sfdx-project.json").write_text(json.dumps({
        "packageDirectories": [{"path": "force-app"}], "sourceApiVersion": "64.0",
    }), encoding="utf-8")
    for document, xml in documents:
        path = root / document.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(xml, encoding="utf-8")
    return root


def test_validation_rules_are_discovered_and_scoped_to_their_object():
    first, second = rule(""), rule("", owner="Survey__c")
    assert supported_source(first.path)
    store = FactStore()
    index_definitions([first, second], store)
    assert identity(first) == ("validationrule", "Review__c.RequireName")
    assert set(store.components) == {
        "sf:validationrule:review__c.requirename", "sf:validationrule:survey__c.requirename"}
    assert store.components["sf:validationrule:review__c.requirename"]["owner"] == "Review__c"


def test_discovery_and_identity_accept_lowercase_validation_rule_suffix_consistently():
    document = rule("")
    document.path = document.path.replace(".validationRule-meta.xml", ".validationrule-meta.xml")
    assert supported_source(document.path)
    assert identity(document) == ("validationrule", "Review__c.RequireName")


@pytest.mark.parametrize("path,xml", [
    ("force-app/RequireName.validationRule-meta.xml", "<ValidationRule/>"),
    ("force-app/objects/X/fields/RequireName.validationRule-meta.xml", "<ValidationRule/>"),
    ("force-app/objects/X/validationRules/RequireName.validationRule-meta.xml", "<CustomField/>"),
    ("force-app/objects/X/validationRules/RequireName.validationRule-meta.xml", "<ValidationRule><fullName>Other</fullName></ValidationRule>"),
    ("force-app/objects/X/validationRules/RequireName.validationRule-meta.xml", "<ValidationRule><fullName>RequireName</fullName><fullName>RequireName</fullName></ValidationRule>"),
])
def test_invalid_metadata_identity_is_rejected(path, xml):
    with pytest.raises(DataError):
        index_definitions([parse_xml(path, xml.encode())], FactStore())


def test_formula_reads_keep_repeated_evidence_and_ignore_functions_strings_comments():
    document = rule('''<active>true</active><errorConditionFormula>
AND(ISCHANGED(Name), LEN(Name) &gt; 4, NOT(ISBLANK(Email__c)),
Name = "Fake__c", TRUE, FALSE, 1e3 &gt; 4 /* Ghost__c */)
</errorConditionFormula><errorDisplayField>Name</errorDisplayField><errorMessage>Label__c is text</errorMessage>''')
    store = extract(document)
    edges = reads(store)
    assert {edge["target"] for edge in edges} == {"sf:field:review__c.name", "sf:field:review__c.email__c"}
    name = next(edge for edge in edges if edge["target"].endswith(".name"))
    occurrences = [store.evidence[eid] for eid in name["evidence_ids"]]
    assert len(occurrences) == 3
    assert len({item["resolution"]["formula_reference"]["decoded_character_offset"] for item in occurrences}) == 3
    assert all(item["content_hash"] == document.content_hash and item["line"] == 1 for item in occurrences)
    assert all(item["resolution"]["formula_reference"]["source_position_basis"] == "containing_xml_expression_element"
               for item in occurrences)
    display = [edge for edge in store.relationships.values() if edge["relation"] == "references"]
    assert len(display) == 1 and display[0]["target"] == "sf:field:review__c.name"
    assert display[0]["qualifiers"] == {"context": "validation_error_display"}
    assert name["qualifiers"] == {"context": "validation_formula"}
    assert any(edge["source"] == "sf:object:review__c" and edge["target"] == "sf:validationrule:review__c.requirename"
               and edge["relation"] == "contains" for edge in store.relationships.values())


def test_related_fields_require_a_verified_complete_parent_path():
    lookup = parse_xml("force-app/main/default/objects/Review__c/fields/Contact__c.field-meta.xml", b'''<CustomField>
<fullName>Contact__c</fullName><type>Lookup</type><referenceTo>Contact</referenceTo></CustomField>''')
    document = rule('<active>true</active><errorConditionFormula>AND(Contact__r.Account.Name = "X", Unknown__r.Name = "Y")</errorConditionFormula>')
    store = extract(document, lookup)
    edge, = terminal_reads(store)
    assert edge["target"] == "sf:field:account.name"
    resolution = store.evidence[edge["evidence_ids"][0]]["resolution"]
    assert resolution["reference"] == "Contact__r.Account.Name"
    assert [step["basis"] for step in resolution["steps"]] == ["declared_lookup", "standard_schema"]
    assert "sf:object:unknown__c" not in store.components
    assert any(item["code"] == "VALIDATION_RULE_FIELD_PATH_UNRESOLVED"
               and item["reference"] == "Unknown__r.Name" for item in store.diagnostics)


@pytest.mark.parametrize("field", ["Id", "Name", "DeveloperName"])
def test_record_type_direct_formula_reference_retains_explicit_standard_step(field):
    store = extract(rule(f'<active>true</active><errorConditionFormula>RecordType.{field} = "Example"</errorConditionFormula>'))
    edge, = terminal_reads(store)
    assert edge["target"] == "sf:field:recordtype." + field.casefold()
    occurrence = store.evidence[edge["evidence_ids"][0]]
    assert occurrence["resolution"]["relationship_path"] == "RecordType." + field
    assert occurrence["resolution"]["steps"] == [{
        "field": "sf:field:review__c.recordtypeid", "target": "sf:object:recordtype",
        "basis": "standard_formula_record_type"}]
    assert store.components["sf:object:recordtype"]["definition_status"] == "referenced_only"


@pytest.mark.parametrize("path", ["RecordType.Other__c", "RecordType.Owner.Name", "$RecordType.Name"])
def test_record_type_fallback_does_not_expand_to_globals_or_arbitrary_paths(path):
    store = extract(rule(f'<active>true</active><errorConditionFormula>{path} = "Example"</errorConditionFormula>'))
    assert not reads(store)
    assert any(item["code"] in {"VALIDATION_RULE_FIELD_PATH_UNRESOLVED", "VALIDATION_RULE_GLOBAL_UNSUPPORTED"}
               for item in store.diagnostics)


@pytest.mark.parametrize("body", ["<type>Text</type>", "<type>Lookup</type><referenceTo>Different__c</referenceTo>",
                                  "<type>Lookup</type><referenceTo>RecordType</referenceTo><referenceTo>Different__c</referenceTo>"])
def test_record_type_standard_fallback_cannot_override_conflicting_local_field_metadata(body):
    field = parse_xml("force-app/main/default/objects/Review__c/fields/RecordTypeId.field-meta.xml",
                      ("<CustomField><fullName>RecordTypeId</fullName>" + body + "</CustomField>").encode())
    store = extract(rule('<active>true</active><errorConditionFormula>RecordType.Name = "Example"</errorConditionFormula>'), field)
    assert not reads(store)
    assert any(item["code"] == "VALIDATION_RULE_FIELD_PATH_UNRESOLVED" for item in store.diagnostics)


@pytest.mark.parametrize("kind", ["owner", "lookup", "target"])
def test_record_type_fallback_preserves_ambiguous_local_definitions(kind):
    document = rule('<active>true</active><errorConditionFormula>RecordType.Name = "Example"</errorConditionFormula>')
    if kind == "lookup":
        path, xml = "objects/Review__c/fields/RecordTypeId.field-meta.xml", b'<CustomField><type>Lookup</type></CustomField>'
    else:
        owner = "Review__c" if kind == "owner" else "RecordType"
        path, xml = f"objects/{owner}/{owner}.object-meta.xml", b'<CustomObject/>'
    first = parse_xml("force-app/main/default/" + path, xml)
    second = parse_xml("force-app/secondary/" + path, xml)
    store = extract(document, first, second)
    assert not reads(store)
    assert any(item["code"] == "VALIDATION_RULE_FIELD_PATH_UNRESOLVED" for item in store.diagnostics)


def test_record_type_declared_lookup_keeps_its_declared_evidence_basis():
    field = parse_xml("force-app/main/default/objects/Review__c/fields/RecordTypeId.field-meta.xml",
                      b'<CustomField><type>Lookup</type><referenceTo>RecordType</referenceTo></CustomField>')
    store = extract(rule('<active>true</active><errorConditionFormula>RecordType.Name = "Example"</errorConditionFormula>'), field)
    edge, = terminal_reads(store)
    assert store.evidence[edge["evidence_ids"][0]]["resolution"]["steps"][0]["basis"] == "declared_lookup"


@pytest.mark.parametrize("owner,parent,target,lookup", [
    ("Order", "Account", "Account", "AccountId"),
    ("Order", "ShipToContact", "Contact", "ShipToContactId"),
    ("OrderItem", "Order", "Order", "OrderId"),
    ("OrderItem", "Product2", "Product2", "Product2Id"),
    ("CampaignMember", "Contact", "Contact", "ContactId"),
    ("Case", "Account", "Account", "AccountId"),
    ("Opportunity", "Account", "Account", "AccountId"),
])
def test_additional_standard_parent_pairs_are_validation_only(owner, parent, target, lookup):
    store = extract(rule(f'<active>true</active><errorConditionFormula>{parent}.Name = "Example"</errorConditionFormula>', owner=owner))
    edge, = terminal_reads(store)
    assert edge["target"] == f"sf:field:{target.casefold()}.name"
    assert store.evidence[edge["evidence_ids"][0]]["resolution"]["steps"] == [{
        "field": f"sf:field:{owner.casefold()}.{lookup.casefold()}",
        "target": "sf:object:" + target.casefold(), "basis": "standard_schema"}]
    # Flow/Apex use the unchanged default vocabulary, even on the same store.
    assert resolve_field_path(store, owner, parent + ".Name") is None


@pytest.mark.parametrize("path,terminal,fields", [
    ("Order.Account.Name", "account.name", ["orderitem.orderid", "order.accountid"]),
    ("Order.RecordType.DeveloperName", "recordtype.developername", ["orderitem.orderid", "order.recordtypeid"]),
    ("Order.Account.RecordType.Name", "recordtype.name", ["orderitem.orderid", "order.accountid", "account.recordtypeid"]),
])
def test_nested_formula_parents_and_record_type_keep_every_verified_step(path, terminal, fields):
    store = extract(rule(f'<active>true</active><errorConditionFormula>{path} = "Example"</errorConditionFormula>', owner="OrderItem"))
    edge, = terminal_reads(store)
    assert edge["target"] == "sf:field:" + terminal
    resolution = store.evidence[edge["evidence_ids"][0]]["resolution"]
    assert [step["field"] for step in resolution["steps"]] == ["sf:field:" + field for field in fields]
    if "RecordType" in path:
        assert resolution["steps"][-1]["basis"] == "standard_formula_record_type"
    assert "sf:field:order.id" not in store.components  # Prefix traversal leaves no synthetic Id artifact.


@pytest.mark.parametrize("body", ["<type>Text</type>", "<type>Lookup</type><referenceTo>Different__c</referenceTo>",
                                  "<type>Text</type><referenceTo>Account</referenceTo>",
                                  "<type>Lookup</type><referenceTo>Account</referenceTo><referenceTo>Different__c</referenceTo>"])
def test_standard_formula_parent_conflicts_cannot_override_local_metadata(body):
    field = parse_xml("force-app/main/default/objects/Order/fields/AccountId.field-meta.xml",
                      ("<CustomField>" + body + "</CustomField>").encode())
    store = extract(rule('<active>true</active><errorConditionFormula>Order.Account.Name = "Example"</errorConditionFormula>', owner="OrderItem"), field)
    assert not reads(store)
    assert any(item["code"] == "VALIDATION_RULE_FIELD_PATH_UNRESOLVED" for item in store.diagnostics)


@pytest.mark.parametrize("kind", ["owner", "lookup", "target", "nested_record_type"])
def test_standard_formula_parent_and_nested_record_type_preserve_ambiguity(kind):
    document = rule('<active>true</active><errorConditionFormula>Order.Account.RecordType.Name = "Example"</errorConditionFormula>', owner="OrderItem")
    if kind in {"lookup", "nested_record_type"}:
        path = ("objects/Order/fields/AccountId.field-meta.xml" if kind == "lookup"
                else "objects/Account/fields/RecordTypeId.field-meta.xml")
        xml = b'<CustomField><type>Lookup</type></CustomField>'
    else:
        owner = "Order" if kind == "owner" else "Account"
        path, xml = f"objects/{owner}/{owner}.object-meta.xml", b'<CustomObject/>'
    store = extract(document, parse_xml("force-app/main/default/" + path, xml),
                    parse_xml("force-app/secondary/" + path, xml))
    assert not reads(store)
    assert any(item["code"] == "VALIDATION_RULE_FIELD_PATH_UNRESOLVED" for item in store.diagnostics)


def test_nested_record_type_conflicting_lookup_does_not_use_standard_fallback():
    field = parse_xml("force-app/main/default/objects/Account/fields/RecordTypeId.field-meta.xml",
                      b'<CustomField><type>Lookup</type><referenceTo>Different__c</referenceTo></CustomField>')
    store = extract(rule('<active>true</active><errorConditionFormula>Order.Account.RecordType.Name = "Example"</errorConditionFormula>',
                         owner="OrderItem"), field)
    assert not reads(store)
    assert any(item["code"] == "VALIDATION_RULE_FIELD_PATH_UNRESOLVED" for item in store.diagnostics)


def test_globals_are_explicitly_typed_and_unknown_globals_never_become_owner_fields():
    document = rule('''<active>true</active><errorConditionFormula>AND(
$User.IsActive, $Profile.Name = "Example", $Organization.Name = "Example", $UserRole.Name = "Example",
$Setup.Switches__c.Enabled__c, $CustomMetadata.Policy__mdt.Default.Enabled__c,
Amount__c &gt; VLOOKUP($ObjectType.Limit__c.Fields.Amount__c, $ObjectType.Limit__c.Fields.Name, $UserRole.Name),
$Permission.BypassValidation, $Label.SomeMessage = "X", $RecordType.Name = "X")</errorConditionFormula>''')
    store = extract(document)
    assert {edge["target"] for edge in reads(store)} == {
        "sf:field:user.isactive", "sf:field:profile.name", "sf:field:organization.name", "sf:field:userrole.name",
        "sf:field:switches__c.enabled__c", "sf:field:policy__mdt.enabled__c", "sf:field:review__c.amount__c",
        "sf:field:limit__c.amount__c", "sf:field:limit__c.name"}
    assert {item["reference"] for item in store.diagnostics if item["code"] == "VALIDATION_RULE_GLOBAL_UNSUPPORTED"} == {
        "$Permission.BypassValidation", "$Label.SomeMessage", "$RecordType.Name"}


def test_computed_context_global_properties_are_not_sobject_fields():
    store = extract(rule('<active>true</active><errorConditionFormula>'
                         '$User.UITheme = $User.UIThemeDisplayed &amp;&amp; $Profile.LicenseType = "X"'
                         '</errorConditionFormula>'))
    assert not reads(store)
    assert len([item for item in store.diagnostics if item["code"] == "VALIDATION_RULE_GLOBAL_UNSUPPORTED"]) == 3


@pytest.mark.parametrize("value,status", [("true", "Active"), ("1", "Active"), ("false", "Inactive"), ("0", "Inactive")])
def test_xml_boolean_activation(value, status):
    document = rule(f"<active>{value}</active>")
    store = extract(document)
    component = store.components["sf:validationrule:review__c.requirename"]
    assert component["local_status"] == status
    assert component["metadata_by_definition"][0]["local_status"] == status


@pytest.mark.parametrize("activation", ["", "<active/>", "<active>False</active>", "<active>future</active>",
                                         "<active>false</active><active>true</active>", "<active>false</active><active>false</active>",
                                         "<active>false<unexpected/></active>"])
def test_unknown_status_is_retained_and_diagnosed(activation):
    document = rule(activation + "<errorConditionFormula>Name = 'X'</errorConditionFormula>")
    store = extract(document)
    assert store.components["sf:validationrule:review__c.requirename"]["local_status"] == "Unknown"
    assert reads(store)
    assert any(item["code"] in {"VALIDATION_RULE_STATUS_UNKNOWN", "VALIDATION_RULE_STATUS_AMBIGUOUS"}
               for item in store.diagnostics)


def test_duplicate_definitions_keep_per_definition_activation_without_selecting_formula():
    first = rule("<active>false</active><errorConditionFormula>Name = 'First'</errorConditionFormula>")
    second = rule("<active>true</active><errorConditionFormula>Other__c = 'Second'</errorConditionFormula>", package="force-app/second")
    store = FactStore()
    index_definitions([first, second], store)
    extract_inventory([first, second], store)
    extract_validation_rule(first, store)
    extract_validation_rule(second, store)
    component = store.components["sf:validationrule:review__c.requirename"]
    assert component["definition_status"] == "ambiguous"
    assert component["local_status"] is None
    assert {item["local_status"] for item in component["metadata_by_definition"]} == {"Active", "Inactive"}
    assert not store.relationships


@pytest.mark.parametrize("expression", ["Rows[0].Other__c", "Contact__r..Name", "Contact__r.Name . Other", "5$User.Id"])
def test_unsupported_formula_path_cannot_salvage_a_suffix(expression):
    store = extract(rule(f"<active>true</active><errorConditionFormula>{expression} = 'X'</errorConditionFormula>"))
    assert not reads(store)
    assert any(item["code"] == "VALIDATION_RULE_FORMULA_SYNTAX_UNSUPPORTED" for item in store.diagnostics)


def test_ambiguous_formula_is_not_chosen_and_display_field_is_not_a_read():
    store = extract(rule("<active>true</active><errorConditionFormula>First__c</errorConditionFormula>"
                         "<errorConditionFormula>Second__c</errorConditionFormula><errorDisplayField>Name</errorDisplayField>"))
    assert not reads(store)
    assert "sf:field:review__c.first__c" not in store.components
    assert any(edge["relation"] == "references" and edge["target"] == "sf:field:review__c.name"
               for edge in store.relationships.values())
    assert any(item["code"] == "VALIDATION_RULE_FORMULA_AMBIGUOUS" for item in store.diagnostics)


def test_nested_display_field_is_diagnosed_even_without_direct_text():
    store = extract(rule('<active>true</active><errorConditionFormula>TRUE</errorConditionFormula>'
                         '<errorDisplayField><field>Name</field></errorDisplayField>'))
    assert not reads(store)
    assert not any(edge["relation"] == "references" for edge in store.relationships.values())
    assert any(item["code"] == "VALIDATION_RULE_DISPLAY_FIELD_UNSUPPORTED" for item in store.diagnostics)


def test_inactive_builds_no_orphans_and_explicit_history_restores_rule(tmp_path):
    document = rule("")
    xml = "<ValidationRule><active>false</active><errorConditionFormula>Name = 'X'</errorConditionFormula></ValidationRule>"
    root = project(tmp_path, (document, xml))
    output = tmp_path / "snapshot"
    bundle = build_project(root, out=output)
    assert bundle["facts"] == {"components": [], "relationships": [], "evidence": []}
    assert bundle["manifest"]["activity_policy"]["excluded_counts"] == {"validationrule": 1}
    assert bundle["manifest"]["coverage"]["analyzed_files"] == 0
    assert load(output) == bundle
    history = build_project(root, out=output, include_inactive=True)
    assert {item["id"] for item in history["facts"]["components"]} == {
        "sf:object:review__c", "sf:validationrule:review__c.requirename", "sf:field:review__c.name"}
    assert history["manifest"]["coverage"]["validation_rule_status_counts"] == {"Inactive": 1}
    assert history["manifest"]["snapshot_id"] != bundle["manifest"]["snapshot_id"]
    validate_bundle(history)


def test_active_rule_reaches_query_projection_and_roundtrip_snapshot(tmp_path):
    document = rule("")
    xml = "<ValidationRule><active>true</active><errorConditionFormula>ISBLANK(Name)</errorConditionFormula></ValidationRule>"
    root = project(tmp_path, (document, xml))
    output = tmp_path / "snapshot"
    bundle = build_project(root, out=output)
    assert load(output) == bundle
    assert query.components(bundle, kind="validationrule")["results"][0]["api_name"] == "Review__c.RequireName"
    result = query.deps(bundle, "Review__c.RequireName")
    assert any(edge["target"] == "sf:field:review__c.name" and edge["relation"] == "reads" for edge in result["results"])
    graph = projection_graph(bundle)
    assert graph.nodes["sf:validationrule:review__c.requirename"]["kind"] == "validationrule"
    assert graph.has_edge("sf:validationrule:review__c.requirename", "sf:field:review__c.name")
