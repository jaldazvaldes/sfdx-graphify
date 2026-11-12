"""Evidence and unsupported shapes for Flow collection filter formulas."""

import pytest

from graphify.salesforce.flow import extract_flow
from graphify.salesforce.schema import FactStore
from graphify.salesforce.xml_source import parse_xml


FLOW_PATH = "force-app/main/default/flows/FilterOrders.flow-meta.xml"
PROCESSOR = """<collectionProcessors>
  <name>SelectedOrders</name>
  <elementSubtype>FilterCollectionProcessor</elementSubtype>
  <collectionProcessorType>FilterCollectionProcessor</collectionProcessorType>
  <assignNextValueToReference>CurrentOrder</assignNextValueToReference>
  <collectionReference>Orders</collectionReference>
  <conditionLogic>formula_evaluates_to_true</conditionLogic>
  <formula>AND(
    ISPICKVAL({!CurrentOrder.Status}, 'On Hold'),
    {!CurrentOrder.Account.Name} = 'Synthetic',
    {!RoutingPolicy.Enabled__c},
    'Order.Fictional__c' = 'label'
  )</formula>
</collectionProcessors>"""


def _lookup(name, owner, *, first_only):
    return f"""<recordLookups>
  <name>{name}</name><object>{owner}</object>
  <storeOutputAutomatically>true</storeOutputAutomatically>
  <getFirstRecordOnly>{str(first_only).lower()}</getFirstRecordOnly>
</recordLookups>"""


def _analyze(processor=PROCESSOR, *, alias_owner="Order", first_only=False):
    rows = _lookup("Orders", "Order", first_only=first_only)
    policy = _lookup("RoutingPolicy", "RoutingPolicy__mdt", first_only=True)
    body = f"""<Flow xmlns="http://soap.sforce.com/2006/04/metadata">
  <apiVersion>64.0</apiVersion>
  {processor}
  {rows}
  {policy}
  <variables>
    <name>CurrentOrder</name><dataType>SObject</dataType>
    <objectType>{alias_owner}</objectType><isCollection>false</isCollection>
  </variables>
</Flow>"""
    document = parse_xml(FLOW_PATH, body.encode("utf-8"))
    store = FactStore()
    store.ensure_component("flow", "FilterOrders", document.location(document.root))
    extract_flow(document, store)
    return document, store


@pytest.mark.parametrize("logic", ["formula_evaluates_to_true", "Formula"])
def test_collection_filter_formula_records_item_metadata_and_parent_field_evidence(logic):
    document, store = _analyze(PROCESSOR.replace("formula_evaluates_to_true", logic))
    filters = {edge["target"]: edge for edge in store.relationships.values() if edge["relation"] == "filters"}
    assert set(filters) == {
        "sf:field:order.status",
        "sf:field:account.name",
        "sf:field:routingpolicy__mdt.enabled__c",
    }
    formula = document.root.all("collectionProcessors")[0].child("formula")
    assert formula is not None
    for edge in filters.values():
        assert edge["source"] == "sf:flow:filterorders"
        assert edge["qualifiers"] == {"operation": "collection_filter", "context": "filter_formula"}
        evidence_id, = edge["evidence_ids"]
        evidence = store.evidence[evidence_id]
        assert evidence["rule"] == "flow.formula_reference"
        assert evidence["path"] == FLOW_PATH
        assert evidence["line"] == document.location(formula)["line"]
        assert evidence["locator"].startswith(f"{formula.locator}/reference[")
        assert evidence["content_hash"] == document.content_hash
        assert evidence["resolution"]["formula_reference"]["expression_locator"] == formula.locator

    traversal_id, = filters["sf:field:account.name"]["evidence_ids"]
    assert store.evidence[traversal_id]["resolution"]["relationship_path"] == "Account.Name"
    assert any(edge["target"] == "sf:field:order.accountid" and edge["relation"] == "reads"
               for edge in store.relationships.values())
    assert all(edge["relation"] != "writes" for edge in store.relationships.values())
    assert {item["code"] for item in store.diagnostics} == {"FLOW_FORMULA_ANALYSIS_BOUNDED"}


def test_collection_filter_cannot_project_fields_from_the_input_collection():
    _, store = _analyze(PROCESSOR.replace("CurrentOrder.Status", "Orders.Status"))
    assert {edge["target"] for edge in store.relationships.values() if edge["relation"] == "filters"} == {
        "sf:field:account.name", "sf:field:routingpolicy__mdt.enabled__c",
    }
    assert any(item["code"] == "FLOW_COLLECTION_FIELD_UNSUPPORTED" for item in store.diagnostics)


@pytest.mark.parametrize(("processor", "options", "diagnostic"), [
    (PROCESSOR.replace("FilterCollectionProcessor", "SortCollectionProcessor"),
     {}, "FLOW_CONSTRUCT_UNSUPPORTED"),
    (PROCESSOR.replace(
        "<conditionLogic>formula_evaluates_to_true</conditionLogic>",
        "<conditionLogic>and</conditionLogic>"
        "<conditions><leftValueReference>CurrentOrder.Status</leftValueReference></conditions>",
    ), {}, "FLOW_CONSTRUCT_UNSUPPORTED"),
    (PROCESSOR.replace("collectionProcessors", "collectionFilter"),
     {}, "FLOW_CONSTRUCT_UNSUPPORTED"),
    (PROCESSOR, {"alias_owner": "Contact"}, "FLOW_COLLECTION_FILTER_UNRESOLVED"),
    (PROCESSOR, {"first_only": True}, "FLOW_COLLECTION_FILTER_UNRESOLVED"),
    (PROCESSOR.replace("</formula>", "</formula><formula>{!CurrentOrder.Other__c}</formula>"),
     {}, "FLOW_COLLECTION_FILTER_AMBIGUOUS"),
])
def test_collection_filter_unsupported_shapes_emit_diagnostics_without_filter_edges(processor, options, diagnostic):
    _, store = _analyze(processor, **options)
    assert diagnostic in {item["code"] for item in store.diagnostics}
    assert all(edge["relation"] != "filters" for edge in store.relationships.values())
    assert not any(item["kind"] == "field" for item in store.components.values())
