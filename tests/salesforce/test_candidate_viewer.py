"""Candidate uncertainty survives aggregation, offline rendering and evidence lookup."""
from __future__ import annotations

import copy
import json
import re

from graphify.salesforce.graphify_adapter import export_graphify, projection_graph
from graphify.salesforce.standard_graph import standard_assets
from graphify.salesforce.viewer import SnapshotView


def candidate_bundle():
    nodes = ["Caller.run", "Helper.apply", "Helper.check"]
    ids = {name: "sf:apexmethod:" + name.lower() for name in nodes}
    candidate = {"resolution_status": "candidate", "uncertainty": "external_hierarchy",
                 "missing_ancestors": ["ExternalBase"], "invocation": "static_method"}
    rows = [("candidate", nodes[0], nodes[1], candidate),
            ("extracted", nodes[0], nodes[1], {"invocation": "static_method"}),
            ("ordinary", nodes[0], nodes[2], {"invocation": "static_method"})]
    return {"manifest": {"snapshot_id": "snapshot:synthetic", "source_revision": None},
            "facts": {"components": [{"id": ids[name], "kind": "apexmethod", "api_name": name,
                                      "definition_status": "defined", "definitions": []}
                                     for name in nodes],
                      "relationships": [{"id": key, "source": ids[source], "target": ids[target],
                                         "relation": "calls", "qualifiers": qualifiers,
                                         "evidence_ids": ["ev:" + key]}
                                        for key, source, target, qualifiers in rows],
                      "evidence": [{"id": "ev:" + key, "path": "classes/Caller.cls", "line": i,
                                    "locator": "synthetic", "relationship_id": key}
                                   for i, (key, *_rest) in enumerate(rows, 1)]},
            "diagnostics": []}


def test_projection_discloses_mixed_confidence_and_preserves_canonical_rows(tmp_path):
    bundle = candidate_bundle()
    original = copy.deepcopy(bundle)
    graph = projection_graph(bundle)
    mixed = graph["sf:apexmethod:caller.run"]["sf:apexmethod:helper.apply"]
    assert mixed["confidence"] == "AMBIGUOUS"
    assert mixed["resolution_status"] == "mixed"
    assert mixed["resolution_counts"] == {"candidate": 1, "extracted": 1}
    details = {item["id"]: item for item in mixed["relationship_details"]}
    assert details["candidate"]["resolution"]["missing_ancestors"] == ["ExternalBase"]
    assert details["extracted"]["resolution"]["confidence"] == "EXTRACTED"
    ordinary = graph["sf:apexmethod:caller.run"]["sf:apexmethod:helper.check"]
    assert ordinary["confidence"] == "EXTRACTED"
    assert ordinary["resolution_counts"] == {"candidate": 0, "extracted": 1}
    exported = json.loads(export_graphify(bundle, tmp_path / "projection").read_text())
    exported_mixed = next(item for item in exported["links"] if item["target"].endswith("helper.apply"))
    assert exported_mixed["confidence"] == "AMBIGUOUS"
    assert exported_mixed["relationship_details"] == mixed["relationship_details"]
    assert exported_mixed["canonical_relationship_ids"] == ["candidate", "extracted"]
    assert bundle == original


def test_standard_page_retains_candidates_and_renders_ambiguous_links_dashed():
    assets = standard_assets(candidate_bundle())
    script = next(payload.decode() for route, (payload, _) in assets.items()
                  if route.endswith(".js") and b"const RAW_NODES =" in payload)
    match = re.search(r"const RAW_EDGES = (.*);\n", script)
    assert match is not None
    edges = json.loads(match.group(1))
    mixed = next(item for item in edges if item["to"].endswith("helper.apply"))
    ordinary = next(item for item in edges if item["to"].endswith("helper.check"))
    assert mixed["dashes"] is True
    assert mixed["confidence"] == "AMBIGUOUS"
    assert mixed["resolution_counts"] == {"candidate": 1, "extracted": 1}
    assert ordinary["dashes"] is False
    assert len(edges) == 2


def test_focused_view_discloses_mixed_groups_and_exact_evidence_resolution():
    bundle = candidate_bundle()
    original = copy.deepcopy(bundle)
    model = SnapshotView(bundle)
    view = model.view("sf:apexmethod:helper.apply")
    edge, = view["edges"]
    assert edge["resolution_status"] == "mixed"
    assert edge["resolution_counts"] == {"candidate": 1, "extracted": 1}
    records = {item["id"]: item for item in edge["relationships"]}
    assert records["candidate"]["resolution"]["status"] == "candidate"
    assert records["extracted"]["resolution"]["status"] == "extracted"
    evidence = model.evidence("candidate")
    assert evidence["resolution"]["missing_ancestors"] == ["ExternalBase"]
    assert evidence["relationship"] == bundle["facts"]["relationships"][0]
    assert evidence["results"][0]["id"] == "ev:candidate"
    assert bundle == original
