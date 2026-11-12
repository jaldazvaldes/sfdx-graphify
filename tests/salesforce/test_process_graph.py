"""Keep process graphs scoped to canonical dependencies and cited steps."""
from __future__ import annotations

import pytest

from graphify.salesforce import process_graph
from graphify.salesforce.graphify_adapter import projection_graph
from graphify.salesforce.schema import DataError, FactStore, relationship_resolution
from graphify.salesforce.viewer import SnapshotView


def location(line):
    return {"path": "force-app/main/default/flows/Fixture.flow-meta.xml", "line": line,
            "column": 1, "end_line": line, "end_column": 2, "locator": f"/Fixture/element[{line}]",
            "content_hash": "0" * 64}


@pytest.fixture
def process_fixture():
    store = FactStore()
    components = {
        "flow": ("flow", "OpportunityRouting"),
        "other_flow": ("flow", "UnrelatedAutomation"),
        "opportunity": ("object", "Opportunity"),
        "stage": ("field", "Opportunity.StageName"),
        "amount": ("field", "Opportunity.Amount"),
        "other_field": ("field", "Opportunity.Description"),
        "lookup": ("field", "Opportunity.AccountId"),
        "account": ("object", "Account"),
        "country_type": ("object", "Country_Settings__mdt"),
        "country_record": ("custommetadatarecord", "Country_Settings.XX"),
        "flag": ("field", "Country_Settings__mdt.Eligible__c"),
        "other_flag": ("field", "Country_Settings__mdt.Unrelated__c"),
        "handler": ("apexclass", "OpportunityRoutingHandler"),
        "method": ("apexmethod", "OpportunityRoutingHandler.execute"),
        "helper": ("apexmethod", "OpportunityRoutingHandler.evaluate"),
        "candidate": ("apexmethod", "OpportunityRoutingHandler.possible"),
        "too_deep": ("apexmethod", "OpportunityRoutingHandler.deepHelper"),
    }
    nodes = {key: store.ensure_component(kind, name) for key, (kind, name) in components.items()}
    edges = {}

    def add(key, source, target, relation, line, **qualifiers):
        edges[key] = store.relation(nodes[source], nodes[target], relation, location(line),
                                    "fixture." + relation, qualifiers=qualifiers)

    add("write", "flow", "stage", "writes", 12, effect="database", operation="update")
    add("filter", "flow", "flag", "filters", 5)
    add("lookup_read", "flow", "lookup", "reads", 7)
    add("method_call", "flow", "method", "calls", 21)
    add("helper_call", "method", "helper", "calls", 22)
    add("deep_call", "helper", "too_deep", "calls", 23)
    add("method_read", "helper", "amount", "reads", 24)
    add("candidate_call", "flow", "candidate", "calls", 25, invocation="constructed_virtual_dispatch")
    add("candidate_read", "candidate", "other_field", "reads", 26)
    add("unrelated_read", "other_flow", "stage", "reads", 27)
    add("stage_owner", "opportunity", "stage", "contains", 28)
    add("amount_owner", "opportunity", "amount", "contains", 29)
    add("lookup_owner", "opportunity", "lookup", "contains", 30)
    add("unrelated_owner", "opportunity", "other_field", "contains", 31)
    add("lookup_target", "lookup", "account", "references", 32, context="lookup_declaration")
    add("method_owner", "handler", "method", "contains", 33)
    add("helper_owner", "handler", "helper", "contains", 34)
    add("record_type", "country_record", "country_type", "instance_of", 35)
    add("flag_value", "country_record", "flag", "sets_value", 36)
    add("unrelated_value", "country_record", "other_flag", "sets_value", 37)
    add("flag_owner", "country_type", "flag", "contains", 38)

    manifest = {"snapshot_id": "snapshot:sample", "source_revision": "1" * 40}
    model = SnapshotView({"manifest": manifest, "facts": store.finish(), "diagnostics": []})
    process = {"steps": [
        {"id": "gate", "components": [{"id": nodes[key]} for key in ("flow", "country_record")],
         "effects": [], "evidence": [location(5)]},
        {"id": "write", "components": [{"id": nodes[key]} for key in ("flow", "stage")],
         "effects": [{"evidence": [{**location(12), "snapshot_matches": [{"relationship": edges["write"]}]}]}],
         "evidence": []},
    ]}
    return model, process, nodes, edges


def test_process_graph_projects_related_code_and_used_fields_without_fanout(process_fixture):
    model, process, nodes, edges = process_fixture
    result = process_graph.process_dependency_bundle(model, process)
    assert result["manifest"] == model.bundle["manifest"]
    graph = projection_graph(result)
    assert graph[nodes["flow"]][nodes["stage"]]["relation"] == "writes"
    assert graph[nodes["flow"]][nodes["flag"]]["relation"] == "filters"
    assert graph[nodes["helper"]][nodes["amount"]]["relation"] == "reads"
    assert graph[nodes["opportunity"]][nodes["amount"]]["relation"] == "contains"
    assert graph[nodes["handler"]][nodes["method"]]["relation"] == "contains"
    assert graph[nodes["lookup"]][nodes["account"]]["relation"] == "references"
    assert graph[nodes["country_record"]][nodes["flag"]]["relation"] == "sets_value"
    excluded = {"other_flow", "other_field", "other_flag", "too_deep"}
    assert set(graph).isdisjoint(nodes[key] for key in excluded)
    assert nodes["candidate"] in graph
    assert relationship_resolution(model.after_relations[edges["candidate_call"]])["status"] == "candidate"
    assert result["process_graph"]["truncated"] is True
    for relation in result["facts"]["relationships"]:
        assert relation == model.after_relations[relation["id"]]
    assert {eid for relation in result["facts"]["relationships"] for eid in relation["evidence_ids"]} == {
        item["id"] for item in result["facts"]["evidence"]
    }


def test_step_graph_uses_its_evidence_and_excludes_other_flow_operations(process_fixture):
    model, process, nodes, edges = process_fixture
    result = process_graph.process_dependency_bundle(model, process, step_id="write")
    assert {item["id"] for item in result["facts"]["components"]} == {
        nodes[key] for key in ("flow", "stage", "opportunity")
    }
    assert {item["id"] for item in result["facts"]["relationships"]} == {edges["write"], edges["stage_owner"]}
    assert result["process_graph"]["step_id"] == "write"
    assert result["process_graph"]["truncated"] is False
    gate = process_graph.process_dependency_bundle(model, process, step_id="gate")
    assert edges["filter"] in {edge["id"] for edge in gate["facts"]["relationships"]}


def test_process_projection_respects_node_and_relationship_limits(process_fixture, monkeypatch):
    model, process, _, _ = process_fixture
    monkeypatch.setattr(process_graph, "PROCESS_NODE_LIMIT", 5)
    monkeypatch.setattr(process_graph, "PROCESS_RELATIONSHIP_LIMIT", 2)
    result = process_graph.process_dependency_bundle(model, process)
    assert result["process_graph"]["truncated"] is True
    assert len(result["facts"]["components"]) <= 5
    assert len(result["facts"]["relationships"]) <= 2
    assert result == process_graph.process_dependency_bundle(model, process)
    assert len(projection_graph(result)) == len(result["facts"]["components"])


def test_process_projection_rejects_unavailable_process_or_unknown_step(process_fixture):
    model, process, _, _ = process_fixture
    with pytest.raises(DataError, match="unavailable"):
        process_graph.process_dependency_bundle(model, {"catalog_error": "The map is missing"})
    with pytest.raises(DataError, match="step does not exist"):
        process_graph.process_dependency_bundle(model, process, step_id="other")
