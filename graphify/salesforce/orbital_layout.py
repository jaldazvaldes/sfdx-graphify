"""Bounded, offline force layout with owned members orbiting their own anchor.

Geometry is a navigation aid, independent of the viewer's grouping/filter modes.
Neither the canonical graph nor its attributes are changed by this module.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import math

import networkx as nx
import numpy as np

from .grouping import _owners


_FORCE_LIMIT = 180
_GAP = 42.0
_ORBIT_START = 72.0
_ORBIT_STEP = 42.0
_NODE_PITCH = 36.0
_TAU = 2 * math.pi


def _angle(identity):
    digest = hashlib.sha256(str(identity).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 * _TAU


def _orbit_radius(count):
    if count == 0:
        return 32.0
    radius = _ORBIT_START
    while count > 0:
        count -= max(1, int(_TAU * radius / _NODE_PITCH))
        if count > 0:
            radius += _ORBIT_STEP
    return radius + 22


def _separate(centers, radii):
    """Resolve bubble collisions on a bounded batch, keeping the force shape."""
    count = len(centers)
    if count < 2:
        return centers
    required = radii[:, None] + radii[None, :] + _GAP
    for _ in range(100):
        delta = centers[:, None, :] - centers[None, :, :]
        distance = np.sqrt((delta * delta).sum(axis=-1))
        np.fill_diagonal(distance, 1)
        overlap = np.maximum(required - distance, 0)
        np.fill_diagonal(overlap, 0)
        if float(overlap.max()) < 0.25:
            break
        direction = delta / np.maximum(distance[:, :, None], 0.001)
        collisions = np.maximum((overlap > 0).sum(axis=1), 1)
        centers += (direction * overlap[:, :, None]).sum(axis=1) * (
            0.55 / np.sqrt(collisions))[:, None]
    centers -= centers.mean(axis=0)
    return centers


def _force_bubbles(graph, radii):
    """The only quadratic force calculation, strictly capped at 180 bubbles."""
    nodes = sorted(graph)
    if not nodes:
        return {}
    if len(nodes) == 1:
        return {nodes[0]: (0.0, 0.0)}
    if len(nodes) > _FORCE_LIMIT:
        raise ValueError("Force layout batch exceeds its bounded size")
    stable = nx.Graph()
    stable.add_nodes_from(nodes)
    for source, target in sorted(tuple(sorted(edge)) for edge in graph.edges()):
        stable.add_edge(source, target, weight=graph[source][target].get("weight", 1.0))
    # Below NetworkX's sparse threshold (500), this remains the dense force
    # solver across supported versions without the newer ``method`` keyword.
    units = nx.spring_layout(stable, seed=42, iterations=70, weight="weight")
    sizes = np.array([radii[node] for node in nodes], dtype=float)
    scale = max(float(sizes.max()) * 2.5, math.sqrt(float(((sizes + _GAP) ** 2).sum())) * 1.5)
    centers = np.array([units[node] for node in nodes], dtype=float) * scale
    centers = _separate(centers, sizes)
    return {node: tuple(map(float, centers[index])) for index, node in enumerate(nodes)}


def _chunks(graph, members):
    """Bound large communities while retaining neighboring modules together."""
    remaining = set(members)
    while remaining:
        seed = min(remaining, key=lambda node: (-graph.degree(node), node))
        pending = [seed]
        queued = {seed}
        chunk = []
        while pending and len(chunk) < _FORCE_LIMIT:
            current = pending.pop()
            if current not in remaining:
                continue
            remaining.remove(current)
            chunk.append(current)
            neighbors = sorted((node for node in graph[current]
                                if node in remaining and node not in queued),
                               key=lambda node: (graph.degree(node), node))
            queued.update(neighbors)
            pending.extend(neighbors)
        yield sorted(chunk)


def _bubble_layout(graph, radii):
    """Multilevel force layout; no level allocates an all-project force matrix."""
    if len(graph) <= _FORCE_LIMIT:
        return _force_bubbles(graph, radii)
    communities = (nx.community.louvain_communities(graph, seed=42, weight="weight")
                   if graph.number_of_edges() else [set(graph)])
    groups = [chunk for community in sorted(communities, key=lambda group: min(group))
              for chunk in _chunks(graph, community)]
    # Very fragmented inputs must still get smaller at each level. Joining these
    # layout batches creates no graph edge or displayed community membership.
    if len(groups) > len(graph) / 2:
        nodes = sorted(graph)
        groups = [nodes[index:index + _FORCE_LIMIT]
                  for index in range(0, len(nodes), _FORCE_LIMIT)]
    macro = nx.Graph()
    local = {}
    macro_radii = {}
    membership = {}
    for index, members in enumerate(groups):
        key = str(index)
        macro.add_node(key)
        local[key] = _force_bubbles(graph.subgraph(members), radii)
        macro_radii[key] = max(math.hypot(*point) + radii[node]
                              for node, point in local[key].items())
        membership.update((node, key) for node in members)
    for source, target, data in graph.edges(data=True):
        first, second = membership[source], membership[target]
        if first != second:
            old = macro.get_edge_data(first, second, {}).get("weight", 0.0)
            macro.add_edge(first, second, weight=old + data.get("weight", 1.0))
    for _, _, data in macro.edges(data=True):
        data["weight"] = 1 + math.log1p(data["weight"])
    centers = _bubble_layout(macro, macro_radii)
    return {node: (centers[key][0] + point[0], centers[key][1] + point[1])
            for key, members in local.items() for node, point in members.items()}


def _modules(graph):
    owners = _owners(graph)
    modules = defaultdict(list)
    for node in sorted(graph):
        modules[owners.get(node, node)].append(node)
    return dict(modules)


def _module_graph(graph, modules):
    membership = {node: owner for owner, members in modules.items() for node in members}
    result = nx.Graph()
    result.add_nodes_from(sorted(modules))
    counts = defaultdict(int)
    for source, target in graph.edges():
        first, second = membership[source], membership[target]
        if first != second:
            counts[tuple(sorted((first, second)))] += 1
    for (source, target), count in sorted(counts.items()):
        result.add_edge(source, target, weight=1 + math.log1p(count))
    return result


def _periphery(keys, radii, extent, preferred=None):
    """Loose outer orbit for isolates/tests, without moving production anchors."""
    if not keys:
        return {}
    keys = sorted(keys, key=lambda key: ((preferred or {}).get(key, _angle(key)), key))
    largest = max(radii[key] for key in keys)
    radius = max(extent + largest + 120, sum(2 * radii[key] + _GAP for key in keys) / _TAU * 1.1)
    arcs = [2 * math.asin(min(0.95, (radii[key] + _GAP / 2) / radius)) for key in keys]
    while sum(arcs) > _TAU:
        radius *= 1.05
        arcs = [2 * math.asin(min(0.95, (radii[key] + _GAP / 2) / radius)) for key in keys]
    slack = (_TAU - sum(arcs)) / len(keys)
    angle = -math.pi
    positions = {}
    for key, arc in zip(keys, arcs):
        angle += (arc + slack) / 2
        distance = radius + (0.5 + 0.5 * math.sin(_angle(key))) * min(radius * 0.06, 150)
        positions[key] = (distance * math.cos(angle), distance * math.sin(angle))
        angle += (arc + slack) / 2
    return positions


class _OccupiedBubbles:
    """Spatial collision index for incremental test placement, never a force pass."""
    def __init__(self):
        self.cells = defaultdict(list)
        self.cell_size = 256.0

    def _cells(self, x, y, radius):
        for first in range(math.floor((x - radius) / self.cell_size),
                           math.floor((x + radius) / self.cell_size) + 1):
            for second in range(math.floor((y - radius) / self.cell_size),
                                math.floor((y + radius) / self.cell_size) + 1):
                yield first, second

    def add(self, key, point, radius):
        bubble = (key, point[0], point[1], radius)
        for cell in self._cells(point[0], point[1], radius):
            self.cells[cell].append(bubble)

    def free(self, point, radius):
        checked = set()
        for cell in self._cells(point[0], point[1], radius + _GAP):
            for key, x, y, other_radius in self.cells[cell]:
                if key in checked:
                    continue
                checked.add(key)
                if math.hypot(point[0] - x, point[1] - y) < radius + other_radius + _GAP:
                    return False
        return True


def _test_centers(graph, test_modules, test_radii, modules, centers, radii):
    """Place tests near their dependencies without allowing them to pull production."""
    membership = {node: owner for owner, members in modules.items() for node in members}
    occupied = _OccupiedBubbles()
    for owner, point in centers.items():
        occupied.add(("production", owner), point, radii[owner])
    result = {}
    disconnected = []
    for owner, members in sorted(test_modules.items()):
        weights = defaultdict(int)
        for member in members:
            for target in set(graph.predecessors(member)) | set(graph.successors(member)):
                if target in membership:
                    weights[membership[target]] += 1
        if not weights:
            disconnected.append(owner)
            continue
        scaled = {key: 1 + math.log1p(count) for key, count in sorted(weights.items())}
        total = sum(scaled.values())
        origin = (sum(centers[key][0] * weight for key, weight in scaled.items()) / total,
                  sum(centers[key][1] * weight for key, weight in scaled.items()) / total)
        phase = _angle(owner)
        pitch = max(40.0, test_radii[owner] * 0.65)
        for attempt in range(2048):
            distance = pitch * math.sqrt(attempt)
            angle = phase + attempt * 2.399963229728653
            point = (origin[0] + distance * math.cos(angle), origin[1] + distance * math.sin(angle))
            if occupied.free(point, test_radii[owner]):
                result[owner] = point
                occupied.add(("test", owner), point, test_radii[owner])
                break
        else:
            disconnected.append(owner)
    extent = max((math.hypot(*point) + radii[key] for key, point in centers.items()), default=0.0)
    extent = max(extent, max((math.hypot(*point) + test_radii[key]
                             for key, point in result.items()), default=0.0))
    result.update(_periphery(disconnected, test_radii, extent))
    return result


def _expand_modules(graph, modules, centers):
    positions = {}
    membership = {node: owner for owner, members in modules.items() for node in members}
    for owner, members in modules.items():
        center_x, center_y = centers[owner]
        anchor = owner if owner in members else min(members, key=lambda node: (-graph.degree(node), node))
        positions[anchor] = (center_x, center_y)
        satellites = sorted((node for node in members if node != anchor),
                            key=lambda node: (-graph.degree(node), node))
        preferred = {}
        for node in satellites:
            targets = {membership[other] for other in set(graph.predecessors(node)) | set(graph.successors(node))
                       if other in membership and membership[other] != owner}
            if targets:
                dx = sum(centers[target][0] - center_x for target in sorted(targets))
                dy = sum(centers[target][1] - center_y for target in sorted(targets))
                preferred[node] = math.atan2(dy, dx)
            else:
                preferred[node] = _angle(node)
        radius = _ORBIT_START
        while satellites:
            capacity = max(1, int(_TAU * radius / _NODE_PITCH))
            ring, satellites = satellites[:capacity], satellites[capacity:]
            ring.sort(key=lambda node: (preferred[node], node))
            phase = math.atan2(sum(math.sin(preferred[node] - _TAU * index / len(ring))
                                   for index, node in enumerate(ring)),
                               sum(math.cos(preferred[node] - _TAU * index / len(ring))
                                   for index, node in enumerate(ring)))
            for index, node in enumerate(ring):
                angle = phase + _TAU * index / len(ring)
                positions[node] = (center_x + radius * math.cos(angle),
                                   center_y + radius * math.sin(angle))
            radius += _ORBIT_STEP
    return positions


def orbital_positions(graph):
    """Return finite, deterministic coordinates for every existing component.

    Known tests cannot attract production components or change their placement.
    Fields/methods remain satellites of their own object/class. Independent Flows,
    triggers and UI components each participate in the dependency force graph.
    """
    if not graph:
        return {}
    production = graph.subgraph(sorted(node for node, data in graph.nodes(data=True)
                                       if data.get("is_test") is not True))
    modules = _modules(production)
    radii = {owner: _orbit_radius(len(members) - 1) for owner, members in modules.items()}
    module_graph = _module_graph(production, modules)
    isolated = sorted(nx.isolates(module_graph))
    connected = module_graph.subgraph(sorted(set(module_graph) - set(isolated)))
    centers = _bubble_layout(connected, radii)
    extent = max((math.hypot(*point) + radii[key] for key, point in centers.items()), default=0.0)
    centers.update(_periphery(isolated, radii, extent))
    positions = _expand_modules(production, modules, centers)

    test_nodes = sorted(set(graph) - set(production))
    tests = graph.subgraph(test_nodes)
    test_owners = _owners(graph)
    test_modules = defaultdict(list)
    for node in test_nodes:
        test_modules[test_owners.get(node, node)].append(node)
    test_radii = {owner: _orbit_radius(len(members) - 1) for owner, members in test_modules.items()}
    test_centers = _test_centers(graph, test_modules, test_radii, modules, centers, radii)
    positions.update(_expand_modules(tests, test_modules, test_centers))
    # Defensive uniqueness for tiny/degenerate input after rounding, preserving
    # all IDs instead of losing coincident components in a collapsed dot.
    output, used = {}, set()
    for node in sorted(positions):
        x, y = positions[node]
        point = (round(x, 3), round(y, 3))
        while point in used:
            point = (round(point[0] + 0.01, 3), round(point[1] + 0.01, 3))
        used.add(point)
        output[node] = point
    return output
