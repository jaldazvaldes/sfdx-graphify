"""Decision dependency evidence from independent synthetic Flow metadata."""
from __future__ import annotations

import hashlib

import pytest

from graphify.salesforce.flow import extract_flow
from graphify.salesforce.schema import FactStore
from graphify.salesforce.xml_source import parse_xml


def analyze(body, *, process="AutoLaunchedFlow"):
    xml = (f'<Flow xmlns="http://soap.sforce.com/2006/04/metadata">\n'
           f'<apiVersion>64.0</apiVersion><processType>{process}</processType>\n{body}\n</Flow>')
    path = "force-app/main/default/flows/DecisionExample.flow-meta.xml"
    document = parse_xml(path, xml.encode())
    store = FactStore()
    store.ensure_component("flow", "DecisionExample", document.location(document.root))
    extract_flow(document, store)
    return store.finish(), store.diagnostics, xml


def condition(left, *, right="<booleanValue>true</booleanValue>", operator="EqualTo"):
    return (f"<conditions><leftValueReference>{left}</leftValueReference><operator>{operator}</operator>"
            f"<rightValue>{right}</rightValue></conditions>")


def decision(conditions, *, name="ChooseRoute", rule="Enabled", logic="and"):
    return (f"<decisions><name>{name}</name><label>Choose a route</label>"
            "<defaultConnectorLabel>Otherwise</defaultConnectorLabel>"
            "<defaultConnector><targetReference>Finish</targetReference></defaultConnector>"
            f"<rules><name>{rule}</name><conditionLogic>{logic}</conditionLogic>{conditions}"
            "<connector><targetReference>Continue</targetReference></connector>"
            "</rules></decisions>")


def decision_edges(facts):
    return [row for row in facts["relationships"] if row["qualifiers"].get("operation") == "decision"]


def test_decision_reads_both_operands_from_record_context_and_automatic_lookup():
    facts, diagnostics, xml = analyze('''
<start><object>Account</object><triggerType>RecordAfterSave</triggerType></start>
<recordLookups><name>Policy</name><object>RegionPolicy__mdt</object>
  <storeOutputAutomatically>true</storeOutputAutomatically><getFirstRecordOnly>true</getFirstRecordOnly>
</recordLookups>
''' + decision(condition("$Record.Enabled__c", right="<elementReference>Policy.Allow__c</elementReference>")))
    edges = decision_edges(facts)
    assert {(row["target"], row["relation"], row["qualifiers"]["operand"]) for row in edges} == {
        ("sf:field:account.enabled__c", "filters", "left"),
        ("sf:field:regionpolicy__mdt.allow__c", "filters", "right"),
    }
    assert not diagnostics
    evidence = {item["id"]: item for item in facts["evidence"]}
    for edge in edges:
        assert edge["qualifiers"]["decision"] == "ChooseRoute"
        assert edge["qualifiers"]["rule"] == "Enabled"
        assert edge["qualifiers"]["condition_logic"] == "and"
        assert edge["qualifiers"]["operator"] == "EqualTo"
        occurrence = evidence[edge["evidence_ids"][0]]
        assert occurrence["content_hash"] == hashlib.sha256(xml.encode()).hexdigest()
        assert occurrence["line"] and occurrence["column"]
        assert "/decisions[@name=ChooseRoute]/rules[@name=Enabled]/conditions/" in occurrence["locator"]
        if edge["qualifiers"]["operand"] == "right":
            assert occurrence["locator"].endswith("/rightValue/elementReference")
            assert occurrence["resolution"]["basis"] == "automatic_get_records_output"
        else:
            assert occurrence["locator"].endswith("/leftValueReference")
            assert occurrence["resolution"]["basis"] == "record_trigger_context"


def test_repeated_conditions_keep_occurrences_and_literal_strings_are_not_references():
    facts, diagnostics, _ = analyze('''
<variables><name>Row</name><dataType>SObject</dataType><objectType>Account</objectType></variables>
''' + decision(condition("Row.Name", right="<stringValue>Contact.Email</stringValue>") * 2,
               logic="1 OR 2"))
    edges = decision_edges(facts)
    assert len(edges) == 1 and edges[0]["target"] == "sf:field:account.name"
    assert len(edges[0]["evidence_ids"]) == 2
    assert edges[0]["qualifiers"]["condition_logic"] == "1 OR 2"
    assert not any(item["id"] == "sf:field:contact.email" for item in facts["components"])
    assert not diagnostics


def test_process_builder_declared_record_and_prior_record_qualifiers():
    facts, diagnostics, _ = analyze('''
<variables><name>CurrentRecord</name><dataType>SObject</dataType><objectType>Contact</objectType></variables>
''' + decision(condition("CurrentRecord.Email", right="<stringValue>example.invalid</stringValue>")),
                                  process="Workflow")
    assert {row["target"] for row in decision_edges(facts)} == {"sf:field:contact.email"}
    assert {row["code"] for row in diagnostics} == {"FLOW_PROCESS_TYPE_UNTESTED"}
    facts, diagnostics, _ = analyze('<start><object>Account</object></start>' +
                                  decision(condition("$Record__Prior.Name", right="<elementReference>$Record.Name</elementReference>")))
    assert {row["qualifiers"]["record_context"] for row in decision_edges(facts)} == {"current", "prior"}
    assert not diagnostics


@pytest.mark.parametrize("reference,extra,code", [
    ("Unknown.Name", "", "FLOW_SYMBOL_UNRESOLVED"),
    ("$Record.Owner.Name", "", "FLOW_RELATIONSHIP_PATH_UNSUPPORTED"),
    ("$Record[0].Name", "", "FLOW_REFERENCE_DYNAMIC"),
    ("Untyped.Name", "<variables><name>Untyped</name><dataType>SObject</dataType></variables>", "FLOW_RECORD_TYPE_UNRESOLVED"),
    ("Rows.Name", "<variables><name>Rows</name><dataType>SObject</dataType><objectType>Account</objectType><isCollection>true</isCollection></variables>", "FLOW_COLLECTION_FIELD_UNSUPPORTED"),
])
@pytest.mark.parametrize("side", ["left", "right"])
def test_unsupported_operand_forms_are_not_guessed(reference, extra, code, side):
    scalar = '<variables><name>Scalar</name><dataType>Boolean</dataType></variables>'
    operand = condition(reference) if side == "left" else condition("Scalar", right=f"<elementReference>{reference}</elementReference>")
    facts, diagnostics, _ = analyze('<start><object>Account</object></start>' + scalar + extra + decision(operand))
    assert decision_edges(facts) == []
    assert not any(item["kind"] == "field" for item in facts["components"])
    assert code in {item["code"] for item in diagnostics}


def test_unknown_decision_extensions_and_other_conditions_are_not_walked_as_decisions():
    body = '<start><object>Account</object></start>'
    body += '<decisions><name>FutureShape</name><rules><name>FutureRule</name><conditionLogic>and</conditionLogic>'
    body += '<conditions><conditionLogic>and</conditionLogic>' + condition("$Record.Hidden__c") + '</conditions>'
    body += '<futureOperands><elementReference>$Record.Future__c</elementReference></futureOperands></rules></decisions>'
    body += '<screens><name>Screen</name><visibilityRule>' + condition("$Record.Screen__c") + '</visibilityRule></screens>'
    facts, diagnostics, _ = analyze(body)
    assert decision_edges(facts) == []
    assert not any(item["kind"] == "field" for item in facts["components"])
    assert {item["code"] for item in diagnostics} >= {
        "FLOW_DECISION_CONDITION_UNSUPPORTED", "FLOW_XML_ELEMENT_UNSUPPORTED"}


@pytest.mark.parametrize("duplicate", [
    ("<conditions><leftValueReference>$Record.Name</leftValueReference>"
     "<leftValueReference>$Record.Industry</leftValueReference><operator>EqualTo</operator>"
     "<rightValue><elementReference>$Record.Description</elementReference></rightValue></conditions>"),
    ("<conditions><leftValueReference>$Record.Name</leftValueReference><operator>EqualTo</operator>"
     "<rightValue><elementReference>$Record.Description</elementReference></rightValue>"
     "<rightValue><stringValue>Example</stringValue></rightValue></conditions>"),
    condition("$Record.Name", right="<elementReference>$Record.Description</elementReference><stringValue>Example</stringValue>"),
    ("<conditions><leftValueReference>$Record.Name</leftValueReference><operator>EqualTo</operator>"
     "<operator>IsNull</operator><rightValue><booleanValue>true</booleanValue></rightValue></conditions>"),
])
def test_repeated_scalar_operands_are_ambiguous_and_do_not_choose_first(duplicate):
    facts, diagnostics, _ = analyze('<start><object>Account</object></start>' + decision(duplicate))
    assert decision_edges(facts) == []
    assert "FLOW_DECISION_CONDITION_AMBIGUOUS" in {item["code"] for item in diagnostics}
