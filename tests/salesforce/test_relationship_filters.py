"""Schema layers preserve canonical facts and distinguish lookup declarations."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from graphify.salesforce.graphify_adapter import projection_graph
from graphify.salesforce.standard_graph import standard_assets


def layered_bundle():
    components = [
        ("owner", "object", False), ("field", "field", False),
        ("target", "object", False), ("flow", "flow", False),
        ("test", "apexmethod", True), ("unknown", "apexmethod", None),
    ]
    rows = [
        ("contains", "owner", "field", "contains", {}),
        ("mixed_read", "owner", "field", "reads", {}),
        ("lookup", "field", "target", "references", {"context": "lookup_declaration"}),
        ("reference", "field", "target", "references", {"context": "typed_reference"}),
        ("reverse_write", "field", "owner", "writes", {}),
        ("flow_read", "flow", "field", "reads", {}),
        ("test_read", "test", "field", "reads", {}),
        ("unknown_read", "unknown", "field", "reads", {}),
    ]
    return {
        "manifest": {"snapshot_id": "snapshot:layers", "source_revision": None},
        "facts": {
            "components": [
                {"id": name, "api_name": name, "kind": kind, "is_test": is_test,
                 "definition_status": "referenced_only", "definitions": []}
                for name, kind, is_test in components
            ],
            "relationships": [
                {"id": name, "source": source, "target": target, "relation": relation,
                 "qualifiers": qualifiers, "evidence_ids": []}
                for name, source, target, relation, qualifiers in rows
            ],
            "evidence": [],
        },
        "diagnostics": [],
    }


def rendered_arrays(bundle):
    assets = standard_assets(bundle)
    script = next(payload.decode() for route, (payload, _) in assets.items()
                  if route.endswith(".js") and b"const RAW_NODES =" in payload)
    nodes_match = re.search(r"const RAW_NODES = (.*);\n", script)
    edges_match = re.search(r"const RAW_EDGES = (.*);\n", script)
    assert nodes_match is not None and edges_match is not None
    return json.loads(nodes_match.group(1)), json.loads(edges_match.group(1)), assets


def test_projection_keeps_parallel_layers_and_real_reference_context():
    bundle = layered_bundle()
    original = copy.deepcopy(bundle)
    graph = projection_graph(bundle)
    details = {row["id"]: row for _, _, data in graph.edges(data=True)
               for row in data["relationship_details"]}
    assert set(details) == {row["id"] for row in bundle["facts"]["relationships"]}
    assert {rid for rid, detail in details.items() if detail["layer"] == "schema"} == {
        "contains", "lookup"}
    assert details["lookup"]["context"] == "lookup_declaration"
    assert details["reference"]["layer"] == "dependencies"
    assert details["reference"]["relation"] == details["lookup"]["relation"] == "references"
    assert graph["owner"]["field"]["canonical_relationship_ids"] == ["contains", "mixed_read"]
    assert graph["field"]["owner"]["canonical_relationship_ids"] == ["reverse_write"]
    assert graph["field"]["target"]["relation_counts"] == {"references": 2}
    assert bundle == original


def test_standard_renderer_retains_all_layer_details_and_reverse_pairs():
    bundle = layered_bundle()
    original = copy.deepcopy(bundle)
    nodes, edges, assets = rendered_arrays(bundle)
    projected = projection_graph(bundle)
    assert {node["id"] for node in nodes} == set(projected.nodes)
    assert {(edge["from"], edge["to"]) for edge in edges} == set(projected.edges)
    for edge in edges:
        assert edge["relationship_details"] == projected[edge["from"]][edge["to"]]["relationship_details"]
    assert len(edges) == 6
    assert all(not node.get("hidden", False) for node in nodes)
    assert 'all nodes included' in assets["/"][0].decode()
    assert bundle == original


@pytest.mark.parametrize("scenario", [
    "layers", "direction", "combined_filters", "focus_styles", "focus_filters", "validation_empty", "validation_rules", "candidates",
])
def test_browser_layer_filter_behavior(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the browser filter regression harness")
    bundle = layered_bundle()
    if scenario == "candidates":
        bundle["facts"]["relationships"].append({
            "id": "candidate", "source": "owner", "target": "field", "relation": "calls",
            "qualifiers": {"resolution_status": "candidate", "uncertainty": "external_hierarchy",
                           "missing_ancestors": ["ExternalBase"]}, "evidence_ids": [],
        })
    if scenario == "validation_rules":
        bundle["facts"]["components"].append({
            "id": "rule", "api_name": "owner.Require_Name", "kind": "validationrule",
            "local_status": "Active", "definition_status": "referenced_only", "definitions": [],
        })
        bundle["facts"]["relationships"].extend([
            {"id": "rule_owner", "source": "owner", "target": "rule", "relation": "contains",
             "qualifiers": {}, "evidence_ids": []},
            {"id": "rule_read", "source": "rule", "target": "field", "relation": "reads",
             "qualifiers": {"context": "validation_formula"}, "evidence_ids": []},
        ])
    nodes, edges, _ = rendered_arrays(bundle)
    # Deliberate community boundary, independent of the clustering algorithm.
    for item in nodes:
        item["community"] = 1 if item["id"] in {"target", "flow", "test"} else 0
    directory = Path(__file__).parent
    filters = directory.parents[1] / "graphify/salesforce/viewer_static/standard_filters.js"
    done = subprocess.run(
        [node, str(directory / "standard_filters_harness.cjs"), str(filters), scenario],
        input=json.dumps({"nodes": nodes, "edges": edges}), encoding="utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"
