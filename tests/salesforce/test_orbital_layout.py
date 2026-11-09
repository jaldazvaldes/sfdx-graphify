"""Offline orbital geometry preserves graph facts and avoids browser-scale force work."""
from __future__ import annotations

import copy
import math

import networkx as nx
import pytest

from graphify.salesforce.orbital_layout import _FORCE_LIMIT, _orbit_radius, orbital_positions


def example_graph():
    graph = nx.DiGraph(snapshot="synthetic")
    for identity, kind, name in (
        ("first", "object", "First"), ("first_name", "field", "First.Name"),
        ("second", "object", "Second"), ("second_name", "field", "Second.Name"),
        ("class", "apexclass", "Logic"), ("method", "apexmethod", "Logic.run()"),
        ("flow", "flow", "SharedFlow"), ("lwc", "lwc", "Panel"),
    ):
        graph.add_node(identity, kind=kind, label=name, is_test=None)
    for source, target, relation in (
        ("first", "first_name", "contains"), ("second", "second_name", "contains"),
        ("class", "method", "contains"), ("method", "first_name", "reads"),
        ("flow", "first", "reads"), ("flow", "second", "writes"),
        ("lwc", "method", "calls"),
    ):
        graph.add_edge(source, target, relation=relation, evidence={"ids": ["synthetic"]})
    return graph


def assert_complete(graph, positions):
    assert set(positions) == set(graph)
    assert len(set(positions.values())) == len(graph)
    assert all(math.isfinite(value) for xy in positions.values() for value in xy)


def test_orbits_preserve_distinct_owner_identity_and_all_relationships():
    graph = example_graph()
    before = copy.deepcopy(nx.node_link_data(graph))
    positions = orbital_positions(graph)
    assert_complete(graph, positions)
    for owner, member in (("first", "first_name"), ("second", "second_name"), ("class", "method")):
        assert math.dist(positions[owner], positions[member]) == pytest.approx(72, abs=0.002)
    assert positions["first"] != positions["second"]
    assert positions["flow"] not in {positions["first"], positions["second"]}
    assert nx.node_link_data(graph) == before


def test_test_bridges_cannot_move_production_layout():
    graph = example_graph()
    production = orbital_positions(graph)
    for index in range(20):
        cls, method = f"test:{index}", f"test:{index}:method"
        graph.add_node(cls, kind="apexclass", label=f"Tests{index}", is_test=True)
        graph.add_node(method, kind="apexmethod", label=f"Tests{index}.run()", is_test=True)
        graph.add_edge(cls, method, relation="contains")
        graph.add_edge(method, "first", relation="reads")
        graph.add_edge(method, "second", relation="reads")
    positions = orbital_positions(graph)
    assert_complete(graph, positions)
    assert {node: positions[node] for node in production} == production


def test_input_order_does_not_change_geometry():
    graph = example_graph()
    reversed_graph = nx.DiGraph()
    reversed_graph.add_nodes_from(reversed(list(graph.nodes(data=True))))
    reversed_graph.add_edges_from(reversed(list(graph.edges(data=True))))
    assert orbital_positions(graph) == orbital_positions(reversed_graph)


def test_connected_tests_stay_near_dependencies_instead_of_a_global_test_halo():
    graph = example_graph()
    for index in range(300):
        graph.add_node(f"isolated:{index}", kind="lwc", label=f"Isolated{index}")
    graph.add_node("test", kind="apexclass", label="TestFirst", is_test=True)
    graph.add_edge("test", "first_name", relation="reads")
    positions = orbital_positions(graph)
    assert math.dist(positions["test"], positions["first"]) < 500
    assert_complete(graph, positions)


def test_every_quadratic_force_batch_is_bounded(monkeypatch):
    graph = nx.DiGraph()
    for index in range(550):
        graph.add_node(f"module:{index:04}", kind="flow", label=f"Flow{index}")
        if index:
            graph.add_edge(f"module:{index-1:04}", f"module:{index:04}", relation="calls")
    original = nx.spring_layout
    sizes = []

    def bounded_projection(projection, **kwargs):
        sizes.append(len(projection))
        assert len(projection) <= _FORCE_LIMIT
        return original(projection, **kwargs)

    monkeypatch.setattr(nx, "spring_layout", bounded_projection)
    assert_complete(graph, orbital_positions(graph))
    assert sizes and len(sizes) > 1


def test_exact_full_orbit_keeps_its_true_extent():
    assert _orbit_radius(0) == 32
    assert _orbit_radius(12) >= 94
    assert _orbit_radius(13) > _orbit_radius(12)


def test_empty_isolated_and_all_test_inputs_are_complete():
    assert orbital_positions(nx.DiGraph()) == {}
    for classification in (None, True):
        graph = nx.DiGraph()
        for index in range(20):
            graph.add_node(f"node:{index}", kind="apexclass", label=f"Item{index}", is_test=classification)
        assert_complete(graph, orbital_positions(graph))


def test_unknown_test_classification_remains_in_production_force_graph():
    graph = example_graph()
    unknown = orbital_positions(graph)
    graph.nodes["method"]["is_test"] = False
    assert orbital_positions(graph) == unknown
