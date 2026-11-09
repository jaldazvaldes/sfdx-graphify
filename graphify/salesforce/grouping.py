"""Presentation-only Salesforce groups; canonical nodes and relationships are untouched."""
from __future__ import annotations

from collections import defaultdict

import networkx as nx

from .schema import DataError, component_id


_PARENT_KINDS = {"field": "object", "outboundmessage": "object",
                 "validationrule": "object", "apexmethod": "apexclass"}
_TYPE_NAMES = {"object": "Object", "field": "Field", "apexclass": "Apex class",
               "apexmethod": "Apex method", "apextrigger": "Trigger", "flow": "Flow",
               "lwc": "Lightning component", "outboundmessage": "Outbound message",
               "validationrule": "Validation rule"}
_TYPE_GROUPS = {"flow": "Flows", "apextrigger": "Triggers", "lwc": "Lightning components",
                "field": "Fields without an unambiguous owner",
                "apexmethod": "Apex methods without an unambiguous owner",
                "outboundmessage": "Outbound messages without an unambiguous owner",
                "validationrule": "Validation rules without an unambiguous owner"}


def _name(graph, node):
    return str(graph.nodes[node].get("label") or node)


def _owners(graph):
    """Use declared containment, with qualified identity for referenced-only members.

    Dependency edges, trigger objects and naming resemblance never establish
    ownership. Conflicting declared parents remain unresolved rather than being
    settled by the member's qualified spelling.
    """
    declared = defaultdict(set)
    for source, target, data in graph.edges(data=True):
        expected = _PARENT_KINDS.get(graph.nodes[target].get("kind"))
        if expected is None or graph.nodes[source].get("kind") != expected:
            continue
        contains = (data.get("relation") == "contains"
                    or "contains" in data.get("relation_counts", {})
                    or any(item.get("relation") == "contains"
                           for item in data.get("relationship_details", [])))
        if contains:
            declared[target].add(source)

    owners = {}
    for node, data in graph.nodes(data=True):
        expected = _PARENT_KINDS.get(data.get("kind"))
        if expected is None:
            continue
        candidates = declared.get(node, set())
        if candidates:
            if len(candidates) == 1:
                owners[node] = next(iter(candidates))
            continue
        qualified = _name(graph, node).split("(", 1)[0]
        if "." not in qualified:
            continue
        parent_name = qualified.rsplit(".", 1)[0]
        try:
            parent = component_id(expected, parent_name)
        except DataError:
            continue
        if parent in graph and graph.nodes[parent].get("kind") == expected:
            owners[node] = parent
    return owners


def _test_groups(graph, owners):
    groups = defaultdict(list)
    for node, data in graph.nodes(data=True):
        if data.get("is_test") is True:
            owner = node if data.get("kind") == "apexclass" else owners.get(node)
            groups[owner].append(node)
    return [(members, f"Tests · {_name(graph, owner)}" if owner is not None else "Other test components",
             "Components explicitly marked as tests. Their links remain available, "
             "but do not determine production connectivity groups.")
            for owner, members in groups.items()]


def _structure_groups(graph, owners):
    groups = defaultdict(list)
    for node, data in graph.nodes(data=True):
        if data.get("is_test") is True:
            continue
        kind = data.get("kind", "unknown")
        if kind in {"object", "apexclass"}:
            key = ("owner", node)
        elif node in owners:
            key = ("owner", owners[node])
        else:
            key = ("type", kind)
        groups[key].append(node)

    result = []
    for (category, key), members in groups.items():
        if category == "owner":
            kind = graph.nodes[key]["kind"]
            label = f"{_TYPE_NAMES.get(kind, kind)} · {_name(graph, key)}"
            description = ("Groups an object with its qualified fields and metadata members. "
                           "References to other objects remain separate links." if kind == "object" else
                           "Groups a declaring Apex class with its methods. Explicit test members "
                           "are placed in separate test groups.")
        else:
            label = _TYPE_GROUPS.get(key, str(key))
            description = ("Grouped by component type. These components remain independent; "
                           "sharing this group does not imply ownership or a direct dependency.")
        result.append((members, label, description))
    return result


def _connectivity_groups(graph):
    from graphify.cluster import cluster

    # A small, attribute-free projection prevents an upstream clustering change
    # from mutating canonical relationship metadata. One link per unordered pair
    # has equal influence, independent of evidence volume and reciprocal calls.
    connected = nx.Graph()
    connected.add_nodes_from(sorted(node for node, data in graph.nodes(data=True)
                                    if data.get("is_test") is not True))
    connected.add_edges_from(sorted({tuple(sorted((source, target)))
                                     for source, target in graph.edges()
                                     if source in connected and target in connected
                                     and source != target}))
    isolated = sorted(nx.isolates(connected))
    connected.remove_nodes_from(isolated)
    result = []
    for members in cluster(connected).values():
        anchors = sorted(members, key=lambda node: (-connected.degree(node), node))[:2]
        names = ", ".join(f"{_name(graph, node)} ({_TYPE_NAMES.get(graph.nodes[node].get('kind'), 'component')})"
                          for node in anchors)
        result.append((members, None, "Connectivity excluding explicit tests. Most connected: " + names
                       + ". Group membership is a navigation hint, not shared object identity."))
    if isolated:
        result.append((isolated, "Unconnected components",
                       "No links to other components after excluding explicit tests from grouping. "
                       "Any test links and self-references remain in the graph."))
    return result


def grouping(graph, mode="structure"):
    """Return a stable complete partition, labels and descriptions for a view.

    ``structure`` uses explicit ownership and qualified identities; standalone
    automation and UI components are grouped by kind. ``connectivity`` delegates
    production connectivity to the upstream cluster API. In either view only
    explicitly classified tests receive separate test groups. No nodes, edges,
    classifications or facts in ``graph`` are changed.
    """
    if mode not in {"structure", "connectivity"}:
        raise ValueError(f"Unknown Salesforce grouping mode: {mode}")
    owners = _owners(graph)
    groups = (_structure_groups(graph, owners) if mode == "structure" else _connectivity_groups(graph))
    groups.extend(_test_groups(graph, owners))
    groups = [(sorted(members), label, description) for members, label, description in groups]
    groups.sort(key=lambda item: (-len(item[0]), item[0]))
    communities, labels, descriptions = {}, {}, {}
    for cid, (members, label, description) in enumerate(groups):
        communities[cid] = members
        labels[cid] = label or f"Group {cid + 1}"
        descriptions[cid] = description
    return communities, labels, descriptions
