"""Typed relationship paths as record update inputs."""

import pytest

from graphify.salesforce.inventory import extract_inventory, index_definitions
from graphify.salesforce.schema import FactStore
from graphify.salesforce.xml_source import parse_xml
from tests.salesforce.test_flow import analyze, codes


def field_document(owner, field, *, target, relationship=""):
    child_name = f"<relationshipName>{relationship}</relationshipName>" if relationship else ""
    xml = f"<CustomField><type>Lookup</type><referenceTo>{target}</referenceTo>{child_name}</CustomField>"
    return parse_xml(f"objects/{owner}/fields/{field}.field-meta.xml", xml.encode())


def store_with_fields(*documents):
    store = FactStore()
    index_definitions(documents, store)
    extract_inventory(documents, store)
    return store


def update_body(start_object, reference, *, explicit_object="", extra_input=""):
    owner = f"<object>{explicit_object}</object>" if explicit_object else ""
    return f"""
      <start><object>{start_object}</object></start>
      <recordUpdates><name>UpdateRelated</name>{owner}
        <inputReference>{reference}</inputReference>{extra_input}
        <filters><field>Id</field><operator>EqualTo</operator><value><stringValue>example</stringValue></value></filters>
        <inputAssignments><field>Status</field><value><stringValue>Ready</stringValue></value></inputAssignments>
      </recordUpdates>"""


@pytest.mark.parametrize(("start_object", "reference", "collection", "lookup_fields"), [
    ("OrderItem", "$Record.Order", False, {"sf:field:orderitem.orderid"}),
    ("Opportunity", "$Record.Orders", True, {"sf:field:order.opportunityid"}),
    ("Order", "$Record.Opportunity.Orders", True, {"sf:field:order.opportunityid"}),
])
def test_standard_related_update_targets_and_navigation(start_object, reference, collection, lookup_fields):
    facts, diagnostics = analyze(update_body(start_object, reference))
    assert not diagnostics
    writes = [edge for edge in facts["relationships"] if edge["relation"] == "writes"]
    assert {edge["target"] for edge in writes} == {"sf:object:order", "sf:field:order.status"}
    assert all(edge["qualifiers"]["effect"] == "database" for edge in writes)
    operation = next(edge for edge in writes if edge["target"] == "sf:object:order")
    assert operation["qualifiers"]["collection"] is collection
    assert {edge["target"] for edge in facts["relationships"] if edge["relation"] == "reads"} == lookup_fields
    assert {edge["target"] for edge in facts["relationships"] if edge["relation"] == "filters"} == {"sf:field:order.id"}
    occurrence = next(item for item in facts["evidence"] if item["rule"] == "flow.record_input_field")
    assert occurrence["resolution"]["relationship_path"] == reference
    assert len(occurrence["resolution"]["relationship_steps"]) == len(reference.split(".")) - 1
    assert occurrence["resolution"]["assigned_value"] == {"type": "string", "value": "Ready"}


def test_declared_child_collection_preserves_schema_source():
    lookup = field_document("Inspection__c", "Owner__c", target="Account", relationship="Inspections")
    facts, diagnostics = analyze(update_body("Account", "$Record.Inspections__r"), store=store_with_fields(lookup))
    assert not diagnostics
    assert any(edge["target"] == "sf:field:inspection__c.status" and edge["relation"] == "writes"
               for edge in facts["relationships"])
    occurrence = next(item for item in facts["evidence"] if item["rule"] == "flow.relationship_navigation")
    step = occurrence["resolution"]["navigation_step"]
    assert step["field"] == "sf:field:inspection__c.owner__c"
    assert step["schema_source"]["path"] == lookup.path


@pytest.mark.parametrize(("start_object", "reference", "explicit_object", "extra_input", "diagnostic"), [
    ("OrderItem", "$Record.Order", "Case", "", "FLOW_OBJECT_TYPE_CONFLICT"),
    ("OrderItem", "$Record.OrderId", "Order", "", "FLOW_RECORD_INPUT_RELATIONSHIP_UNRESOLVED"),
    ("Opportunity", "$Record.Orders.Opportunity", "", "", "FLOW_RECORD_INPUT_RELATIONSHIP_UNRESOLVED"),
    ("OrderItem", "$Record.Order", "", "<inputReference>$Record</inputReference>", "FLOW_RECORD_INPUT_AMBIGUOUS"),
])
def test_unproven_related_record_input_cannot_write_even_with_explicit_object(start_object, reference, explicit_object,
                                                                              extra_input, diagnostic):
    body = update_body(start_object, reference, explicit_object=explicit_object, extra_input=extra_input)
    facts, diagnostics = analyze(body)
    assert not any(edge["relation"] in {"reads", "filters", "writes"} for edge in facts["relationships"])
    assert diagnostic in codes(diagnostics)


def test_ambiguous_child_or_incompatible_standard_lookup_cannot_write():
    declared = store_with_fields(*[
        field_document(owner, "Account__c", target="Account", relationship="Inspections")
        for owner in ("First__c", "Second__c")
    ])
    conflicting_standard = store_with_fields(field_document("Order", "OpportunityId", target="Case"))
    for store, owner, path in ((declared, "Account", "Inspections__r"), (conflicting_standard, "Opportunity", "Orders")):
        facts, diagnostics = analyze(update_body(owner, f"$Record.{path}"), store=store)
        assert not any(edge["relation"] == "writes" for edge in facts["relationships"])
        assert "FLOW_RECORD_INPUT_RELATIONSHIP_UNRESOLVED" in codes(diagnostics)
