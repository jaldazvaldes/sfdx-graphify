"""Synthetic races exercise the actual focused explorer JavaScript."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("scenario", ["snapshot", "page", "new-view", "diagnostics", "diagnostic-snapshot", "candidate"])
def test_explorer_ignores_obsolete_responses(scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser JavaScript harness")
    directory = Path(__file__).parent
    source = directory.parents[1] / "graphify" / "salesforce" / "viewer_static" / "viewer.js"
    result = subprocess.run(
        [node, str(directory / "viewer_async_harness.cjs"), str(source), scenario],
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "ok"
