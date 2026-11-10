"""Synthetic regressions for formula evidence and outbound action boundaries."""
from __future__ import annotations

import pytest

from graphify.salesforce import query
from graphify.salesforce.flow import extract_flow
from graphify.salesforce.formula_refs import formula_references
from graphify.salesforce.inventory import extract_inventory, index_definitions
from graphify.salesforce.schema import FactStore
from graphify.salesforce.workflow import extract_workflow, index_workflow
from graphify.salesforce.xml_source import parse_xml


def flow(body):
    document = parse_xml("force-app/main/default/flows/FormulaDemo.flow-meta.xml", (
        '<Flow xmlns="http://soap.sforce.com/2006/04/metadata"><apiVersion>64.0</apiVersion>'
        + body + '</Flow>').encode())
    store = FactStore()
    store.ensure_component("flow", "FormulaDemo", document.location(document.root))
    return document, store


def test_lexical_scanner_ignores_strings_comments_functions_and_numeric_exponents():
    refs, issues = formula_references('''IF(TRUE, {!$Record.Stage__c}, "{!$Record.Fake__c}")
        & 'Bob\\\'s $Record.Nope__c' & 'it''s $Record.Nope2__c'
        /* {!$Record.Nope3__c} */ + 1e3 + 0.1E-2 + GetPolicy.Allow__c''')
    assert [ref.text for ref in refs] == ["$Record.Stage__c", "GetPolicy.Allow__c"]
    assert issues == []


@pytest.mark.parametrize("expression", [
    "Rows[0].Other", "$Record..Other", "5$Record.Other", "$Record.Name . Other",
    "Rows['quoted]index'].Other", "Rows[0][1].Other", "$Record/* comment */.Other",
    "$Record.Name$Other", "Rows[0].Other.More",
])
def test_unsupported_path_cannot_salvage_suffix_as_independent_formula_resource(expression):
    references, issues = formula_references(expression)
    assert not references
    assert issues
    document, store = flow('''<start><object>Account</object><filterFormula>'''
                           + expression + '''</filterFormula></start>
<formulas><name>Other</name><expression>{!$Record.Secret__c}</expression></formulas>''')
    extract_flow(document, store)
    assert not any(edge["relation"] == "filters" for edge in store.relationships.values())
    assert any(edge["target"] == "sf:field:account.secret__c" and edge["relation"] == "reads"
               for edge in store.relationships.values())


def test_unsupported_indexed_path_does_not_hide_separate_valid_reference():
    references, issues = formula_references("Rows[0].Other + $Record.Name")
    assert [ref.text for ref in references] == ["$Record.Name"]
    assert issues == ["indexed_reference"]


def test_start_formula_extracts_record_setup_and_metadata_fields_without_literals():
    document, store = flow('''
<start><object>Account</object><filterFormula>
AND(ISPICKVAL({!$Record.Stage__c}, 'Ready'),
NOT({!$Setup.FeatureSwitch__c.Disabled__c}),
{!$CustomMetadata.Policy__mdt.Default.Enabled__c},
{!$Permission.UseFeature})</filterFormula></start>''')
    extract_flow(document, store)
    relations = [r for r in store.relationships.values() if r["relation"] == "filters"]
    assert {r["target"] for r in relations} == {
        "sf:field:account.stage__c", "sf:field:featureswitch__c.disabled__c", "sf:field:policy__mdt.enabled__c"}
    assert {r["qualifiers"]["operation"] for r in relations} == {"start"}
    assert all("/filterFormula/reference[" in store.evidence[e]["locator"] for r in relations for e in r["evidence_ids"])
    assert all(store.evidence[e]["content_hash"] == document.content_hash for r in relations for e in r["evidence_ids"])
    assert "FLOW_FORMULA_ANALYSIS_BOUNDED" in {d["code"] for d in store.diagnostics}
    assert any(d["code"] == "FLOW_SYMBOL_UNRESOLVED" and d["symbol"] == "$Permission" for d in store.diagnostics)


def test_formula_resources_follow_typed_get_records_and_preserve_repeated_occurrences():
    document, store = flow('''
<recordLookups><name>Policy</name><object>Policy__mdt</object><storeOutputAutomatically>true</storeOutputAutomatically>
<getFirstRecordOnly>true</getFirstRecordOnly></recordLookups>
<formulas><name>Enabled</name><dataType>Boolean</dataType><expression>AND({!Policy.Allow__c}, {!Policy.Allow__c})</expression></formulas>
<formulas><name>Wrapper</name><dataType>Boolean</dataType><expression>{!Enabled}</expression></formulas>
<decisions><name>Choose</name><rules><name>Yes</name><conditions><leftValueReference>Wrapper</leftValueReference>
<operator>EqualTo</operator><rightValue><booleanValue>true</booleanValue></rightValue></conditions></rules></decisions>''')
    extract_flow(document, store)
    field_relations = [r for r in store.relationships.values() if r["target"] == "sf:field:policy__mdt.allow__c"]
    assert {r["relation"] for r in field_relations} == {"reads", "filters"}
    assert all(len(r["evidence_ids"]) == 2 for r in field_relations)
    assert not any(d["code"] in {"FLOW_RESOURCE_DEPENDENCIES_UNSUPPORTED", "FLOW_CONSTRUCT_UNSUPPORTED"}
                   for d in store.diagnostics)


def test_reused_formula_dag_expands_once_per_context_without_losing_occurrences(monkeypatch):
    import graphify.salesforce.flow as flow_module
    scans = []
    original = flow_module.formula_references

    def record_scan(expression):
        scans.append(expression)
        return original(expression)

    monkeypatch.setattr(flow_module, "formula_references", record_scan)
    resources = '<formulas><name>Base</name><expression>{!$Record.Name} &amp; {!$Record.Name}</expression></formulas>'
    previous = "Base"
    for number in range(18):
        name = f"Layer{number}"
        resources += f'<formulas><name>{name}</name><expression>{{!{previous}}} &amp; {{!{previous}}}</expression></formulas>'
        previous = name
    document, store = flow('<start><object>Account</object><filterFormula>{!Layer17}</filterFormula></start>' + resources)
    extract_flow(document, store)
    assert len(scans) == 39  # 19 resources each as reads/filters, plus the start expression.
    edges = [r for r in store.relationships.values() if r["target"] == "sf:field:account.name"]
    assert {r["relation"] for r in edges} == {"reads", "filters"}
    assert all(len(r["evidence_ids"]) == 2 for r in edges)
    occurrences = [store.evidence[e] for r in edges for e in r["evidence_ids"]]
    assert {e["resolution"]["formula_reference"]["decoded_character_offset"] for e in occurrences} == {0, 18}
    assert all(e["resolution"]["formula_reference"]["source_position_basis"] == "containing_xml_expression_element"
               for e in occurrences)


def test_formula_cycle_and_malformed_literals_remain_diagnostic():
    document, store = flow('''
<formulas><name>A</name><expression>{!B}</expression></formulas>
<formulas><name>B</name><expression>{!A}</expression></formulas>
<formulas><name>C</name><expression>"unclosed {!$Record.Fake__c}</expression></formulas>''')
    extract_flow(document, store)
    assert {d["code"] for d in store.diagnostics} >= {"FLOW_FORMULA_CYCLE", "FLOW_FORMULA_SYNTAX_UNSUPPORTED"}
    assert not store.relationships


def test_assignment_to_formula_never_marks_its_input_field_written():
    document, store = flow('''<start><object>Account</object></start>
<formulas><name>Derived</name><expression>{!$Record.Name}</expression></formulas>
<assignments><name>InvalidAssignment</name><assignmentItems><assignToReference>Derived</assignToReference>
<operator>Assign</operator><value><stringValue>X</stringValue></value></assignmentItems></assignments>''')
    extract_flow(document, store)
    assert not any(r["relation"] == "writes" for r in store.relationships.values())
    assert "FLOW_FORMULA_DESTINATION_UNSUPPORTED" in {d["code"] for d in store.diagnostics}


def test_formula_parent_path_uses_declared_lookup_and_retains_full_resolution():
    document, store = flow('''
<start><object>Review__c</object><filterFormula>OR({!$Record.Item__r.Product2.UDICode__c} = 'X',
{!$Record.Item__r.Product2.UDICode__c} = 'Y')</filterFormula></start>''')
    field = parse_xml("force-app/main/default/objects/Review__c/fields/Item__c.field-meta.xml", b'''<CustomField>
<fullName>Item__c</fullName><type>Lookup</type><referenceTo>Asset</referenceTo></CustomField>''')
    index_definitions([field], store)
    extract_inventory([field], store)
    extract_flow(document, store)
    edges = [r for r in store.relationships.values() if r["relation"] == "filters"]
    assert len(edges) == 1 and edges[0]["target"] == "sf:field:product2.udicode__c"
    occurrence = store.evidence[edges[0]["evidence_ids"][0]]
    assert occurrence["resolution"]["relationship_path"] == "Item__r.Product2.UDICode__c"
    assert [step["basis"] for step in occurrence["resolution"]["steps"]] == ["declared_lookup", "standard_schema"]
    assert "navigation_step" not in occurrence["resolution"]
    bundle = {"manifest": {"snapshot_id": "synthetic"}, "facts": store.finish(), "diagnostics": store.diagnostics}
    for step, field_name in enumerate(("Review__c.Item__c", "Asset.Product2Id"), 1):
        navigation, = query.refs(bundle, field_name, relation="reads")["results"]
        assert navigation["source"] == "sf:flow:formulademo"
        assert navigation["qualifiers"] == {"context": "flow_relationship_navigation"}
        assert {e["resolution"]["navigation_step"]["index"] for e in navigation["evidence"]} == {step}
        assert len({e["resolution"]["formula_reference"]["decoded_character_offset"] for e in navigation["evidence"]}) == 2
        assert all(e["content_hash"] == document.content_hash and e["line"] == occurrence["line"]
                   and e["locator"].endswith(f"/navigation[{step}]") for e in navigation["evidence"])
        impact, = query.impact(bundle, field_name, depth=1)["results"]
        assert impact["id"] == navigation["id"]


def test_relationship_assignment_preserves_terminal_write_and_navigation_read():
    document, store = flow('''<start><object>Contact</object></start>
<assignments><name>Rename</name><assignmentItems><assignToReference>$Record.Account.Name</assignToReference>
<operator>Assign</operator><value><stringValue>Example</stringValue></value></assignmentItems></assignments>''')
    extract_flow(document, store)
    write, = [edge for edge in store.relationships.values() if edge["relation"] == "writes"]
    navigation, = [edge for edge in store.relationships.values() if edge["relation"] == "reads"]
    assert write["target"] == "sf:field:account.name"
    assert write["qualifiers"]["effect"] == "memory"
    assert navigation["target"] == "sf:field:contact.accountid"
    assert navigation["qualifiers"] == {"context": "flow_relationship_navigation"}


def test_unresolved_parent_hop_does_not_emit_partial_flow_navigation():
    document, store = flow('''<start><object>Contact</object>
<filterFormula>{!$Record.Account.Unknown__r.Name} = 'X'</filterFormula></start>''')
    extract_flow(document, store)
    assert not any(edge["relation"] in {"reads", "filters"} for edge in store.relationships.values())
    assert "sf:field:contact.accountid" not in store.components
    assert any(d["code"] == "FLOW_RELATIONSHIP_PATH_UNSUPPORTED" for d in store.diagnostics)


def test_process_builder_object_is_explicit_and_duplicate_metadata_is_not_chosen():
    metadata = '''<processMetadataValues><name>ObjectType</name><value><stringValue>Invoice__c</stringValue></value></processMetadataValues>'''
    document, store = flow('<processType>Workflow</processType>' + metadata + '''
<processMetadataValues><name>TriggerType</name><value><stringValue>onCreateOnly</stringValue></value></processMetadataValues>''')
    extract_flow(document, store)
    edge = next(iter(store.relationships.values()))
    assert edge["target"] == "sf:object:invoice__c"
    assert edge["qualifiers"] == {"operation": "trigger", "trigger_type": "onCreateOnly", "process_type": "Workflow"}
    assert store.components["sf:flow:formulademo"]["trigger_object"] == "Invoice__c"
    document, store = flow('<processType>Workflow</processType>' + metadata * 2)
    extract_flow(document, store)
    assert not store.relationships
    assert "FLOW_PROCESS_TRIGGER_AMBIGUOUS" in {d["code"] for d in store.diagnostics}


@pytest.mark.parametrize("metadata", [
    '<name>ObjectType</name><value><stringValue>Account</stringValue></value><value><stringValue>Contact</stringValue></value>',
    '<name>ObjectType</name><name>TriggerType</name><value><stringValue>Account</stringValue></value>',
    '<name>ObjectType</name><value><stringValue>Account</stringValue><stringValue>Contact</stringValue></value>',
    '<name>TriggerType</name><value><stringValue>onCreateOnly</stringValue><stringValue>onAllChanges</stringValue></value>',
])
def test_process_builder_repeated_scalar_metadata_cannot_select_first_value(metadata):
    owner = ('<processMetadataValues><name>ObjectType</name><value><stringValue>Account</stringValue></value></processMetadataValues>'
             if metadata.startswith('<name>TriggerType') else '')
    document, store = flow('<processType>Workflow</processType>' + owner
                           + '<processMetadataValues>' + metadata + '</processMetadataValues>')
    extract_flow(document, store)
    assert not store.relationships
    assert any(d["code"] in {"FLOW_PROCESS_TRIGGER_AMBIGUOUS", "FLOW_PROCESS_TRIGGER_UNRESOLVED"} for d in store.diagnostics)


def test_outbound_message_links_definition_and_payload_without_endpoint_values():
    document, store = flow('''<actionCalls><name>Send</name><actionType>outboundMessage</actionType>
<actionName>Invoice__c.SendInvoice</actionName></actionCalls>''')
    workflow = parse_xml("force-app/main/default/workflows/Invoice__c.workflow-meta.xml", b'''<Workflow>
<outboundMessages><fullName>SendInvoice</fullName><endpointUrl>https://example.invalid/private</endpointUrl>
<fields>Id</fields><fields>Amount__c</fields></outboundMessages></Workflow>''')
    index_workflow([workflow], store)
    extract_workflow([workflow], store)
    extract_flow(document, store)
    message = store.components["sf:outboundmessage:invoice__c.sendinvoice"]
    assert message["definition_status"] == "defined"
    assert "endpoint" not in str(store.finish()).lower()
    assert {(r["source"], r["target"]) for r in store.relationships.values() if r["relation"] == "calls"} == {
        ("sf:flow:formulademo", message["id"])}
    assert {r["target"] for r in store.relationships.values() if r["relation"] == "reads"} == {
        "sf:field:invoice__c.id", "sf:field:invoice__c.amount__c"}


def test_missing_or_duplicate_outbound_definition_does_not_invent_unique_local_target():
    document, store = flow('''<actionCalls><name>Send</name><actionType>outboundMessage</actionType>
<actionName>Account.SendNotice</actionName></actionCalls>''')
    extract_flow(document, store)
    assert store.components["sf:outboundmessage:account.sendnotice"]["definition_status"] == "referenced_only"
    document, store = flow('''<actionCalls><name>Send</name><actionType>outboundMessage</actionType>
<actionName>Account.SendNotice</actionName></actionCalls>''')
    workflows = [parse_xml(f"pkg{i}/workflows/Account.workflow-meta.xml", b'''<Workflow>
<outboundMessages><fullName>SendNotice</fullName><fields>Name</fields></outboundMessages></Workflow>''') for i in range(2)]
    index_workflow(workflows, store)
    extract_workflow(workflows, store)
    extract_flow(document, store)
    assert not store.relationships
    assert any(d["code"] == "ambiguous_component" for d in store.diagnostics)
