"""Project snapshot dependencies for a documented process or one of its steps."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .schema import DataError, relationship_resolution

if TYPE_CHECKING:
    from .viewer import SnapshotView


PROCESS_NODE_LIMIT = 140
PROCESS_RELATIONSHIP_LIMIT = 280
PROCESS_CALL_DEPTH = 2
_EXECUTABLE_KINDS = {
    "flow", "apextrigger", "apexclass", "apexmethod", "lwc", "aura",
    "visualforcepage", "visualforcecomponent", "validationrule", "workflowrule",
    "approvalprocess", "outboundmessage",
}
_DEPENDENCIES = {"calls", "reads", "writes", "filters", "references", "triggered_by", "publishes", "subscribes"}
_SCHEMA = {"contains", "instance_of", "sets_value"}


def _citations(steps: list[dict]) -> tuple[set[str], set[tuple[str, int]]]:
    matches: set[str] = set()
    sites: set[tuple[str, int]] = set()
    for step in steps:
        for reference in step["evidence"] + [ref for effect in step["effects"] for ref in effect["evidence"]]:
            sites.add((reference["path"], reference["line"]))
            matches.update(match["relationship"] for match in reference.get("snapshot_matches", []))
    return matches, sites


def process_dependency_bundle(model: SnapshotView, process: dict, *, step_id: str | None = None) -> dict:
    """Return an ephemeral, bounded projection of the current snapshot."""
    if process.get("catalog_error"):
        raise DataError("The requested process map is unavailable.")
    steps = process["steps"]
    if step_id is not None:
        steps = [step for step in steps if step["id"] == step_id]
        if not steps:
            raise DataError("The requested process step does not exist.")

    components = model.after_components
    relationships = model.after_relations
    evidence = model.after_evidence
    authored = {item["id"] for step in steps for item in step["components"] if item["id"] in components}
    selected_nodes = set(sorted(authored)[:PROCESS_NODE_LIMIT])
    selected_edges: set[str] = set()
    truncated = len(selected_nodes) < len(authored)
    call_depth_limit = 0 if step_id is not None else PROCESS_CALL_DEPTH

    def include(edge: dict) -> bool:
        nonlocal truncated
        if edge["id"] in selected_edges:
            return True
        endpoints = {edge["source"], edge["target"]}
        if (len(selected_edges) >= PROCESS_RELATIONSHIP_LIMIT
                or len(selected_nodes | endpoints) > PROCESS_NODE_LIMIT):
            truncated = True
            return False
        selected_nodes.update(endpoints)
        selected_edges.add(edge["id"])
        return True

    def edges_at(ids: list[str]):
        return (relationships[rid] for rid in ids if rid in relationships)

    def on_cited_line(edge: dict) -> bool:
        return any((evidence[eid]["path"], evidence[eid].get("line")) in sites for eid in edge["evidence_ids"])

    cited, sites = _citations(steps)
    for rid in sorted(cited):
        if rid in relationships:
            include(relationships[rid])

    # Exact authored source locations can also identify a dependency on a field
    # which was not explicitly named in the catalog.
    for source in sorted(authored):
        for edge in edges_at(model.outgoing.get(source, [])):
            if edge["relation"] in _DEPENDENCIES and on_cited_line(edge):
                include(edge)

    if step_id is None:
        frontier = authored.copy()
        seen: set[str] = set()
        for depth in range(call_depth_limit + 1):
            next_frontier: set[str] = set()
            for source in sorted(frontier - seen):
                if source not in selected_nodes or components[source]["kind"] not in _EXECUTABLE_KINDS:
                    continue
                for edge in edges_at(model.outgoing.get(source, [])):
                    if edge["relation"] not in _DEPENDENCIES:
                        continue
                    target = edge["target"]
                    is_call = edge["relation"] == "calls"
                    if is_call and depth == call_depth_limit and target not in authored | seen | frontier:
                        truncated = True
                        continue
                    if (include(edge) and is_call and depth < call_depth_limit
                            and relationship_resolution(edge)["status"] == "extracted"):
                        next_frontier.add(target)
            seen.update(frontier)
            frontier = next_frontier
            if not frontier - seen:
                break

    # Give each used field or method its declared owner without pulling in the
    # owner's other fields, methods, or callers.
    for node in sorted(selected_nodes):
        if components[node]["kind"] not in {"field", "apexmethod", "apexclass"}:
            continue
        for edge in edges_at(model.incoming.get(node, [])):
            if edge["relation"] == "contains":
                include(edge)
        if step_id is None and components[node]["kind"] == "field":
            for edge in edges_at(model.outgoing.get(node, [])):
                if (edge["relation"] == "references"
                        and edge["qualifiers"].get("context") == "lookup_declaration"):
                    include(edge)

    for source in sorted(authored & selected_nodes):
        for edge in edges_at(model.outgoing.get(source, [])):
            if edge["relation"] in _SCHEMA and edge["target"] in selected_nodes:
                include(edge)

    chosen_edges = [relationships[rid] for rid in sorted(selected_edges)]
    occurrence_ids = {eid for edge in chosen_edges for eid in edge["evidence_ids"]}
    return {
        "manifest": model.bundle["manifest"],
        "facts": {
            "components": [components[node] for node in sorted(selected_nodes)],
            "relationships": chosen_edges,
            "evidence": [evidence[eid] for eid in sorted(occurrence_ids)],
        },
        "diagnostics": [],
        "process_graph": {
            "step_id": step_id,
            "truncated": truncated,
            "node_limit": PROCESS_NODE_LIMIT,
            "relationship_limit": PROCESS_RELATIONSHIP_LIMIT,
            "call_depth_limit": call_depth_limit,
        },
    }
