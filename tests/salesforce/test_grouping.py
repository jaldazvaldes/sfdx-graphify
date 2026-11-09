"""Grouping changes presentation, never Salesforce identities or evidence."""
from __future__ import annotations

import copy

import networkx as nx
import pytest

from graphify.salesforce.grouping import grouping
from graphify.salesforce.schema import component_id


def node(graph, kind, name, **attrs):
    identity = component_id(kind, name)
    graph.add_node(identity, kind=kind, label=name, **attrs)
    return identity


def member_group(result, identity):
    communities, labels, _ = result
    return next((cid, labels[cid]) for cid, members in communities.items() if identity in members)


def assert_partition(graph, result):
    communities, labels, descriptions = result
    members = [node for group in communities.values() for node in group]
    assert len(members) == len(set(members)) == len(graph)
    assert set(members) == set(graph)
    assert set(communities) == set(labels) == set(descriptions)


def test_structure_keeps_objects_separate_despite_shared_automation_and_tests():
    graph = nx.DiGraph()
    first = node(graph, "object", "Alpha")
    second = node(graph, "object", "Beta")
    field = node(graph, "field", "Alpha.Beta__c")
    flow = node(graph, "flow", "SharedAutomation", trigger_object="Alpha")
    cls = node(graph, "apexclass", "SharedLogic")
    method = node(graph, "apexmethod", "SharedLogic.run()")
    graph.add_edge(first, field, relation="contains")
    graph.add_edge(field, second, relation="references")
    graph.add_edge(cls, method, relation="contains")
    for source in (flow, method):
        graph.add_edge(source, first, relation="reads")
        graph.add_edge(source, second, relation="writes")
    for index in range(20):
        test = node(graph, "apexclass", f"SharedTests{index}", is_test=True)
        graph.add_edge(test, first, relation="reads")
        graph.add_edge(test, second, relation="reads")
    result = grouping(graph)
    assert_partition(graph, result)
    assert member_group(result, first) == member_group(result, field)
    assert member_group(result, first) != member_group(result, second)
    assert member_group(result, cls) == member_group(result, method)
    assert member_group(result, flow)[1] == "Flows"
    assert len({member_group(result, value)[0] for value in (first, second, flow, cls)}) == 4


def test_qualified_referenced_members_find_existing_owners_without_inventing_edges():
    graph = nx.DiGraph()
    obj = node(graph, "object", "Sample")
    field = node(graph, "field", "Sample.Name")
    message = node(graph, "outboundmessage", "Sample.Notify")
    cls = node(graph, "apexclass", "Outer.Inner")
    method = node(graph, "apexmethod", "Outer.Inner.run(List<String>)")
    missing = node(graph, "field", "Absent.Name")
    result = grouping(graph)
    assert_partition(graph, result)
    assert member_group(result, field) == member_group(result, obj) == member_group(result, message)
    assert member_group(result, method) == member_group(result, cls)
    assert "without an unambiguous owner" in member_group(result, missing)[1]
    assert graph.number_of_edges() == 0


def test_conflicting_containment_remains_unassigned_instead_of_guessing_from_name():
    graph = nx.DiGraph()
    first = node(graph, "object", "Alpha")
    second = node(graph, "object", "Beta")
    field = node(graph, "field", "Alpha.Name")
    graph.add_edge(first, field, relationship_details=[{"relation": "contains"}])
    graph.add_edge(second, field, relation_counts={"contains": 1})
    result = grouping(graph)
    assert_partition(graph, result)
    assert "without an unambiguous owner" in member_group(result, field)[1]


@pytest.mark.parametrize("mode", ["structure", "connectivity"])
def test_test_members_are_separate_and_unknown_classification_is_retained(mode):
    graph = nx.DiGraph()
    cls = node(graph, "apexclass", "Mixed", is_test=False)
    production = node(graph, "apexmethod", "Mixed.run()", is_test=False)
    test = node(graph, "apexmethod", "Mixed.check()", is_test=True)
    unknown = node(graph, "apexmethod", "Mixed.unknown()", is_test=None)
    for child in (production, test, unknown):
        graph.add_edge(cls, child, relation="contains")
    result = grouping(graph, mode)
    assert_partition(graph, result)
    assert member_group(result, test)[1] == "Tests · Mixed"
    assert member_group(result, unknown)[1] != "Tests · Mixed"
    assert member_group(result, cls)[1] != "Tests · Mixed"


def test_connectivity_delegates_only_non_test_links_and_keeps_isolated_components(monkeypatch):
    graph = nx.DiGraph()
    first = node(graph, "object", "Alpha")
    second = node(graph, "object", "Beta")
    isolated = node(graph, "lwc", "alone")
    test = node(graph, "apexclass", "BridgeTest", is_test=True)
    graph.add_edge(first, second, relation="reads", evidence={"ids": ["synthetic"]})
    graph.add_edge(second, first, relation="writes")
    graph.add_edge(test, first, relation="references")
    graph.add_edge(test, isolated, relation="references")
    graph.add_edge(isolated, isolated, relation="calls")
    called = []

    def cluster(projection):
        called.append(True)
        assert set(projection) == {first, second}
        assert projection.number_of_edges() == 1
        assert list(projection.edges(data=True))[0][2] == {}
        return {99: [second, first]}

    monkeypatch.setattr("graphify.cluster.cluster", cluster)
    result = grouping(graph, "connectivity")
    assert called == [True]
    assert_partition(graph, result)
    assert member_group(result, first) == member_group(result, second)
    assert member_group(result, first)[1].startswith("Group ")
    assert member_group(result, isolated)[1] == "Unconnected components"
    assert member_group(result, test)[1] == "Tests · BridgeTest"
    assert graph.number_of_edges() == 5


@pytest.mark.parametrize("mode", ["structure", "connectivity"])
def test_grouping_is_order_independent_complete_and_does_not_mutate_input(mode):
    graph = nx.DiGraph(snapshot="fictional")
    obj = node(graph, "object", "Sample")
    field = node(graph, "field", "Sample.Name")
    flow = node(graph, "flow", "Automation")
    node(graph, "apexclass", "Tests", is_test=True)
    graph.add_edge(obj, field, relation="contains", evidence={"ids": ["one"]})
    graph.add_edge(flow, field, relation="reads")
    before = copy.deepcopy(nx.node_link_data(graph))
    reversed_graph = nx.DiGraph()
    reversed_graph.add_nodes_from(reversed(list(graph.nodes(data=True))))
    reversed_graph.add_edges_from(reversed(list(graph.edges(data=True))))
    result = grouping(graph, mode)
    assert_partition(graph, result)
    assert result == grouping(reversed_graph, mode)
    assert nx.node_link_data(graph) == before


def test_empty_graph_and_unknown_mode():
    assert grouping(nx.DiGraph()) == ({}, {}, {})
    assert grouping(nx.DiGraph(), "connectivity") == ({}, {}, {})
    with pytest.raises(ValueError, match="Unknown Salesforce grouping"):
        grouping(nx.DiGraph(), "guessed")
