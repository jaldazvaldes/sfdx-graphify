"""Optional local Graphify navigation projection; canonical facts stay separate."""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import redirect_stdout
from pathlib import Path
import os
import sys
import tempfile

from .schema import DataError, relationship_resolution


def projection_graph(bundle: dict):
    """Keep every component and aggregate only relationships sharing an ordered pair."""
    import networkx as nx

    graph = nx.DiGraph()
    snapshot_id = bundle["manifest"]["snapshot_id"]
    graph.graph.update({"projection": "salesforce_dependency_navigation",
                        "canonical_snapshot_id": snapshot_id,
                        "aggregation": "one edge per ordered component pair",
                        "canonical_evidence_required": True})
    for component in bundle["facts"]["components"]:
        definitions = component.get("definitions", [])
        definition = definitions[0] if len(definitions) == 1 else {}
        graph.add_node(component["id"], label=component["api_name"],
                       kind=component["kind"], file_type="code",
                       source_file=definition.get("path"),
                       source_location=(f"L{definition['line']}" if definition.get("line") else None),
                       candidate_definitions=definitions if len(definitions) > 1 else [],
                       definition_status=component["definition_status"],
                       local_status=component.get("local_status"),
                       process_type=component.get("process_type"),
                       trigger_type=component.get("trigger_type"),
                       record_trigger_type=component.get("record_trigger_type"),
                       trigger_object=component.get("trigger_object"),
                       trigger_events=component.get("trigger_events"),
                       is_test=component.get("is_test"))
    pairs = defaultdict(list)
    for relationship in bundle["facts"]["relationships"]:
        pairs[(relationship["source"], relationship["target"])].append(relationship)
    for (source, target), relationships in sorted(pairs.items()):
        verbs = sorted({item["relation"] for item in relationships})
        details = []
        for item in sorted(relationships, key=lambda item: item["id"]):
            context = item.get("qualifiers", {}).get("context")
            structural = (item["relation"] == "contains"
                          or (item["relation"] == "references" and context == "lookup_declaration"))
            details.append({"id": item["id"], "relation": item["relation"],
                            "layer": "schema" if structural else "dependencies", "context": context,
                            "resolution": relationship_resolution(item)})
        candidate_count = sum(item["resolution"]["status"] == "candidate" for item in details)
        graph.add_edge(source, target, relation=", ".join(verbs),
                       confidence="AMBIGUOUS" if candidate_count else "EXTRACTED",
                       resolution_counts={"candidate": candidate_count, "extracted": len(details) - candidate_count},
                       resolution_status=("mixed" if 0 < candidate_count < len(details)
                                          else "candidate" if candidate_count else "extracted"),
                       canonical_relationship_ids=sorted(item["id"] for item in relationships),
                       relationship_details=details,
                       relation_counts=dict(sorted(Counter(item["relation"]
                                                          for item in relationships).items())))
    return graph


def export_graphify(bundle: dict, output: str | Path, *, source_root=None,
                    snapshot_dir=None) -> Path:
    """Aggregate ordered pairs without passing facts through upstream deduplication."""
    from .snapshot import validate_output
    from graphify.export import to_json

    out = validate_output(Path(output), source_root=source_root)
    if snapshot_dir is not None:
        snapshot_path = Path(snapshot_dir).resolve()
        if out == snapshot_path or snapshot_path in out.parents or out in snapshot_path.parents:
            raise DataError("Graphify output must be separate from the canonical snapshot.")
    graph = projection_graph(bundle)
    out.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".graphify-", suffix=".json", dir=out)
    os.close(fd)
    temporary = Path(temporary_name)
    target = out / "graph.json"
    try:
        revision = bundle["manifest"].get("source_revision")
        commit = revision if isinstance(revision, str) else None
        with redirect_stdout(sys.stderr):
            # This is a newly created staging file, never an existing snapshot.
            # Skip the saved-graph reconciliation path and its generic extractors.
            wrote = to_json(graph, {}, str(temporary), force=True,
                            built_at_commit=commit or "")
        if not wrote:
            raise DataError("Graphify projection export refused the new snapshot.")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
