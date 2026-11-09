"""Exercise grouping navigation against the real Salesforce browser scripts."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from graphify.salesforce.standard_graph import standard_assets


def grouping_bundle():
    """Two independently owned objects, a shared Flow, and explicit/unknown tests."""
    components = [
        ("first", "First", "object", False),
        ("first_field", "First.Name", "field", False),
        ("second", "Second", "object", False),
        ("second_field", "Second.Name", "field", False),
        ("flow", "SharedFlow", "flow", False),
        ("test", "Test.helper", "apexmethod", True),
        ("unknown", "Unknown.helper", "apexmethod", None),
        ("hostile", '</script><img src=x onerror="bad()">', "object", False),
    ]
    relationships = [
        ("first", "first_field", "contains"),
        ("second", "second_field", "contains"),
        ("flow", "first_field", "reads"),
        ("flow", "second_field", "writes"),
        ("test", "first_field", "reads"),
        ("test", "second_field", "reads"),
        ("unknown", "first_field", "reads"),
    ]
    return {
        "manifest": {"snapshot_id": "snapshot:grouping-controls", "source_revision": None},
        "facts": {
            "components": [
                {"id": key, "api_name": label, "kind": kind, "is_test": test,
                 "definition_status": "referenced_only", "definitions": []}
                for key, label, kind, test in components
            ],
            "relationships": [
                {"id": f"rel:{index}", "source": source, "target": target,
                 "relation": relation, "evidence_ids": []}
                for index, (source, target, relation) in enumerate(relationships)
            ],
            "evidence": [],
        },
        "diagnostics": [],
    }


def rendered_data():
    bundle = grouping_bundle()
    original = copy.deepcopy(bundle)
    assets = standard_assets(bundle)
    scripts = "\n".join(body.decode() for route, (body, _) in assets.items()
                        if route.endswith(".js"))
    data = {}
    for name, declaration in (("nodes", "RAW_NODES"), ("edges", "RAW_EDGES"),
                              ("modes", "GROUPING_MODES")):
        match = re.search(rf"const {declaration} = (.*);\n", scripts)
        assert match, f"Missing {declaration} browser payload"
        data[name] = json.loads(match.group(1))
    assert bundle == original, "Rendering must not change the canonical facts"
    return data, assets


def test_grouping_payload_is_complete_and_has_separate_object_groups():
    data, assets = rendered_data()
    node_ids = {node["id"] for node in data["nodes"]}
    assert node_ids == {row["id"] for row in grouping_bundle()["facts"]["components"]}
    assert {(edge["from"], edge["to"]) for edge in data["edges"]} == {
        (row["source"], row["target"])
        for row in grouping_bundle()["facts"]["relationships"]
    }
    assert set(data["modes"]) == {"structure", "connectivity"}
    for mode in data["modes"].values():
        assert set(mode["placements"]) == node_ids
        assert len({tuple(row[1:]) for row in mode["placements"].values()}) == len(node_ids)
        assert sum(row["count"] for row in mode["legend"]) == len(node_ids)
    structure = data["modes"]["structure"]["placements"]
    connectivity = data["modes"]["connectivity"]["placements"]
    assert all(structure[node][1:] == connectivity[node][1:] for node in node_ids)
    assert structure["first"][0] == structure["first_field"][0]
    assert structure["second"][0] == structure["second_field"][0]
    assert structure["first"][0] != structure["second"][0]
    assert structure["flow"][0] not in {structure["first"][0], structure["second"][0]}
    scripts = "\n".join(body.decode() for route, (body, _) in assets.items()
                        if route.endswith(".js"))
    assert '</script><img src=x onerror="bad()">' not in scripts


@pytest.mark.parametrize("scenario", ["switching_filters", "legend_focus", "literal_labels"])
def test_browser_grouping_controls(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the browser grouping regression harness")
    data, _ = rendered_data()
    directory = Path(__file__).parent
    scripts = directory.parents[1] / "graphify/salesforce/viewer_static"
    done = subprocess.run(
        [node, str(directory / "standard_groups_harness.cjs"),
         str(scripts / "standard_filters.js"), str(scripts / "standard_groups.js"), scenario],
        input=json.dumps(data), encoding="utf-8", stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=20, shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"
