"""Synthetic checks for the per-checkout process catalog registration."""
from __future__ import annotations

import hashlib
import json

import pytest

from graphify.salesforce.process_registration import load_process_registration, save_process_registration
from graphify.salesforce.process_queries import format_process_brief, show_process
from graphify.salesforce.process_workspace import load_registered_catalog, require_registered_files
from graphify.salesforce.schema import DataError, EXTRACTOR_VERSION, SCHEMA_VERSION, stable_id
from graphify.salesforce.snapshot import local_project_directory, publish


@pytest.fixture
def process_files(tmp_path, monkeypatch):
    cache = tmp_path / "local cache"
    monkeypatch.setenv("LOCALAPPDATA", str(cache))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    project = tmp_path / "Salesforce checkout"
    project.mkdir()
    (project / "sfdx-project.json").write_text("{}", encoding="utf-8")
    revision = "1" * 40
    bundle = {
        "manifest": {
            "schema_version": SCHEMA_VERSION, "extractor_version": EXTRACTOR_VERSION,
            "source_revision": revision, "dirty": False, "input_mode": "working_tree",
            "inputs": {}, "scope": {"package_dirs": ["force-app"]},
        },
        "facts": {"components": [], "relationships": [], "evidence": []},
        "diagnostics": [],
    }
    bundle["manifest"]["snapshot_id"] = stable_id("snapshot", bundle)
    snapshot = tmp_path / "clean snapshot"
    publish(bundle, snapshot, project)
    catalog = tmp_path / "process catalog.json"
    process = {
        "id": "demonstrate", "title": "Demonstrate", "summary": "Synthetic process.",
        "steps": [
            {"id": "start", "label": "Start", "description": "Begin.",
             "kind": "start", "components": [], "effects": [], "evidence": []},
        ],
        "edges": [],
    }
    authored = {
        "source_revision": revision,
        "countries": [{"id": "ES", "name": "Spain",
                       "features": [{"id": "example", "name": "Example", "processes": [process]}]}],
    }
    catalog.write_text(json.dumps(authored), encoding="utf-8")
    return project, snapshot, catalog, bundle["manifest"]


def test_registration_is_local_to_the_checkout(process_files):
    project, snapshot, catalog, manifest = process_files
    other = project.with_name("Another checkout")
    other.mkdir()
    (other / "sfdx-project.json").write_text("{}", encoding="utf-8")
    assert load_process_registration(project) is None

    saved = save_process_registration(project, snapshot, catalog, port=8775)
    assert saved == {
        "snapshot": str(snapshot.resolve()), "catalog": str(catalog.resolve()), "port": 8775,
        "snapshot_id": manifest["snapshot_id"], "source_revision": manifest["source_revision"],
        "catalog_digest": hashlib.sha256(catalog.read_bytes()).hexdigest(),
    }
    assert load_process_registration(project) == saved
    setting = local_project_directory(project).parent / "snapshot-root.txt"
    setting.write_text(str(snapshot.parent / "new snapshot storage"), encoding="utf-8")
    assert load_process_registration(project) == saved
    assert load_process_registration(other) is None
    assert {item.name for item in project.iterdir()} == {"sfdx-project.json"}


def test_invalid_catalog_cannot_replace_the_last_valid_registration(process_files):
    project, snapshot, catalog, _ = process_files
    saved = save_process_registration(project, snapshot, catalog)
    broken = json.loads(catalog.read_bytes())
    broken["source_revision"] = "2" * 40
    catalog.write_text(json.dumps(broken), encoding="utf-8")

    with pytest.raises(DataError, match="clean snapshot"):
        save_process_registration(project, snapshot, catalog)

    assert load_process_registration(project) == saved


def test_registration_rejects_invalid_stored_port(process_files):
    project, snapshot, catalog, _ = process_files
    saved = save_process_registration(project, snapshot, catalog)
    path = local_project_directory(project) / "PROCESS_MAPS.json"
    path.write_text(json.dumps({**saved, "port": True}), encoding="utf-8")

    with pytest.raises(DataError, match="Invalid local process catalog registration"):
        load_process_registration(project)


def _split_catalog(catalog):
    authored = json.loads(catalog.read_bytes())
    feature = authored["countries"][0]["features"][0]
    process, = feature["processes"]
    process_dir = catalog.parent / "processes"
    process_dir.mkdir()
    child = process_dir / "demonstrate.json"
    child.write_text(json.dumps(process), encoding="utf-8")
    feature["processes"] = [{"id": "demonstrate", "file": "processes/demonstrate.json"}]
    catalog.write_text(json.dumps(authored), encoding="utf-8")
    return authored, feature, child


def test_registration_pins_process_file_contents_and_feature_entry_points(process_files):
    project, snapshot, catalog, _ = process_files
    authored, feature, child = _split_catalog(catalog)
    alternate = {"id": "alternate", "name": "Alternate",
                 "processes": [{**feature["processes"][0], "entry_step": "start"}]}
    authored["countries"][0]["features"].append(alternate)
    catalog.write_text(json.dumps(authored), encoding="utf-8")
    saved = save_process_registration(project, snapshot, catalog)
    require_registered_files(saved)
    _, loaded = load_registered_catalog(saved)
    default, scoped = loaded["countries"][0]["features"]
    assert "entry_step" not in default["processes"][0]
    assert scoped["processes"][0]["entry_step"] == "start"
    assert "catalog_errors" not in loaded

    child.write_bytes(child.read_bytes() + b"\n")
    with pytest.raises(DataError, match="register again"):
        require_registered_files(saved)


def test_broken_process_files_are_reported_without_hiding_valid_processes(process_files, monkeypatch, capsys):
    from graphify.salesforce import cli

    project, snapshot, catalog, _ = process_files
    authored, feature, child = _split_catalog(catalog)
    feature["processes"].extend([
        {"id": "missing", "file": "processes/missing.json"},
        {"id": "corrupt", "file": "processes/corrupt.json"},
        {"id": "wrong-id", "file": "processes/demonstrate.json"},
    ])
    child.with_name("corrupt.json").write_text("{", encoding="utf-8")
    catalog.write_text(json.dumps(authored), encoding="utf-8")

    saved = save_process_registration(project, snapshot, catalog)
    _, loaded = load_registered_catalog(saved)
    require_registered_files(saved)
    assert loaded["catalog_errors"] == 3
    assert show_process(loaded, "demonstrate")["process"]["steps"][0]["id"] == "start"
    expected = {
        "missing": "File is missing.",
        "corrupt": "Invalid process map JSON.",
        "wrong-id": "Process ID must match the catalog index.",
    }
    for identifier, error in expected.items():
        result = show_process(loaded, identifier)
        assert not result["process"]["steps"]
        assert result["process"]["review_status"] == "needs_review"
        assert error in format_process_brief(result)

    monkeypatch.chdir(project)
    assert cli.main(["processes", "show", "demonstrate"]) == 0
    assert json.loads(capsys.readouterr().out)["process"]["id"] == "demonstrate"
    assert cli.main(["processes", "show", "missing", "--format", "brief"]) == 1
    assert "processes/missing.json: File is missing." in capsys.readouterr().out
    assert cli.main(["processes", "check"]) == 1
    assert json.loads(capsys.readouterr().out)["summary"]["catalog_errors"] == 3

    child.unlink()
    with pytest.raises(DataError, match="No usable process maps"):
        save_process_registration(project, snapshot, catalog)
    assert load_process_registration(project) == saved


@pytest.mark.parametrize("file", ["../outside.json", "processes\\demonstrate.json",
                                  "C:/outside.json", "processes/con.json"])
def test_process_file_references_reject_unsafe_paths(process_files, file):
    project, snapshot, catalog, _ = process_files
    authored, feature, _ = _split_catalog(catalog)
    feature["processes"][0]["file"] = file
    catalog.write_text(json.dumps(authored), encoding="utf-8")
    with pytest.raises(DataError, match="relative JSON path"):
        save_process_registration(project, snapshot, catalog)
