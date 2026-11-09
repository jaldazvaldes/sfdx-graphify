"""Complete standard Graphify rendering contracts with fictional components."""
from __future__ import annotations

import copy
import base64
import hashlib
from importlib.resources import files
import json
import math
import re

from graphify.salesforce.standard_graph import standard_assets


def test_vendored_library_matches_rendered_integrity_pin():
    page = standard_assets(synthetic_bundle())["/"][0].decode()
    script = re.search(r'<script[^>]+src="/vis-network.min.js"[^>]*>', page)
    assert script is not None
    pin = re.search(r'integrity="sha384-([^"]+)"', script.group())
    assert pin is not None
    library = files("graphify.salesforce").joinpath("viewer_static/vis-network.min.js").read_bytes()
    assert base64.b64encode(hashlib.sha384(library).digest()).decode() == pin.group(1)


def synthetic_bundle(count=5):
    return {
        "manifest": {"snapshot_id": "snapshot:synthetic", "source_revision": None},
        "facts": {
            "components": [
                {"id": f"sf:object:sample{index}", "api_name": f"Sample{index}",
                 "kind": "object", "definition_status": "referenced_only", "definitions": []}
                for index in range(count)],
            "relationships": [
                {"id": "rel:first", "source": "sf:object:sample1", "target": "sf:object:sample0",
                 "relation": "reads", "evidence_ids": []},
                {"id": "rel:second", "source": "sf:object:sample1", "target": "sf:object:sample0",
                 "relation": "writes", "evidence_ids": []},
                {"id": "rel:reverse", "source": "sf:object:sample0", "target": "sf:object:sample1",
                 "relation": "calls", "evidence_ids": []},
            ],
            "evidence": [],
        },
        "diagnostics": [],
    }


def rendered_arrays(assets):
    script = next(payload.decode() for route, (payload, _) in assets.items()
                  if route.endswith(".js") and b"const RAW_NODES =" in payload)
    node_match = re.search(r"const RAW_NODES = (.*);\n", script)
    edge_match = re.search(r"const RAW_EDGES = (.*);\n", script)
    assert node_match is not None and edge_match is not None
    nodes = json.loads(node_match.group(1))
    edges = json.loads(edge_match.group(1))
    return nodes, edges, script


def test_large_standard_graph_keeps_every_node_and_ordered_pair():
    bundle = synthetic_bundle(5003)
    original = copy.deepcopy(bundle)
    assets = standard_assets(bundle)
    nodes, edges, script = rendered_arrays(assets)
    assert {node["id"] for node in nodes} == {node["id"] for node in bundle["facts"]["components"]}
    assert len(nodes) == 5003
    assert {(edge["from"], edge["to"]) for edge in edges} == {
        ("sf:object:sample1", "sf:object:sample0"), ("sf:object:sample0", "sf:object:sample1")}
    assert len(edges) == 2 and any(edge["label"] == "reads, writes" for edge in edges)
    assert all(math.isfinite(node[coordinate]) for node in nodes for coordinate in ("x", "y"))
    assert len({(node["x"], node["y"]) for node in nodes}) == 5003
    assert "physics: {\n    enabled: false," in script
    assert "smooth: false" in script and "x: n.x" in script and "y: n.y" in script
    assert not any(node.get("hidden") for node in nodes)
    assert "Unconnected components" in script
    assert bundle == original


def test_standard_assets_are_local_scripts_with_safe_names_and_evidence_link():
    bundle = synthetic_bundle()
    bundle["facts"]["components"][0]["api_name"] = '</script><script>alert("synthetic")</script>'
    assets = standard_assets(bundle)
    page = assets["/"][0].decode()
    nodes, _, script = rendered_arrays(assets)
    assert '<script>alert("synthetic")</script>' not in page + script
    assert "\\u003c" in script
    assert len(nodes) == 5
    assert 'src="/vis-network.min.js"' in page
    assert "unpkg.com" not in page
    assert not re.search(r"\bon\w+\s*=", page)
    for source in re.findall(r'<script[^>]+src="([^"]+)"', page):
        assert source == "/vis-network.min.js" or source in assets
    assert all(body.strip() == "" for body in re.findall(r"<script[^>]*>(.*?)</script>", page, re.DOTALL))
    assert "/explorer#' + encodeURIComponent(nodeId)" in script
    assert 'href="/explorer"' in page and 'id="fit-all"' in page
    assert "selectAllCb.addEventListener('change'" in script
    assert 'all nodes included' in page and '3 relationships' in page
    assert "Search visible nodes..." in page and 'id="groups-heading">Groups' in page
    assert "forceAtlas2Based" in script  # The standard renderer, with its simulation disabled.


def test_standard_layout_is_deterministic_and_empty_graph_is_supported():
    bundle = synthetic_bundle()
    assert standard_assets(bundle) == standard_assets(bundle)
    bundle["facts"]["components"] = []
    bundle["facts"]["relationships"] = []
    nodes, edges, _ = rendered_arrays(standard_assets(bundle))
    assert nodes == [] and edges == []


def test_standard_graph_retains_test_classification_and_triggers_for_presentation():
    bundle = synthetic_bundle()
    for name, classification in (("Marked", True), ("Unmarked", False), ("Unresolved", None)):
        bundle["facts"]["components"].append({
            "id": f"sf:apexclass:{name.lower()}", "api_name": name, "kind": "apexclass",
            "definition_status": "defined", "definitions": [], "is_test": classification,
        })
    bundle["facts"]["components"].append({
        "id": "sf:apextrigger:example", "api_name": "Example", "kind": "apextrigger",
        "definition_status": "defined", "definitions": [], "trigger_events": ["before insert"],
    })
    original = copy.deepcopy(bundle)
    assets = standard_assets(bundle)
    nodes, _, script = rendered_arrays(assets)
    by_id = {node["id"]: node for node in nodes}
    assert by_id["sf:apexclass:marked"]["is_test"] is True
    assert by_id["sf:apexclass:unmarked"]["is_test"] is False
    assert by_id["sf:apexclass:unresolved"]["is_test"] is None
    assert by_id["sf:apextrigger:example"]["kind"] == "apextrigger"
    assert all(not node.get("hidden") for node in nodes)
    assert 'id="show-tests-cb" type="checkbox" checked' in assets["/"][0].decode()
    assert "Test classification:" in script
    assert len(nodes) == len(bundle["facts"]["components"]) and bundle == original
