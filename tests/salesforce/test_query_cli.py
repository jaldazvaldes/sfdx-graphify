"""Contract tests for exact queries, traversal, local CLI, and optional projection."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket

import pytest

from graphify.salesforce import cli, query
from graphify.salesforce.graphify_adapter import export_graphify
from graphify.salesforce.schema import DataError


@pytest.fixture
def bundle():
    names = [("field", "Account.Name"), ("flow", "Set_Name"),
             ("flow", "Caller"), ("object", "Account")]
    components = [{"id": f"sf:{kind}:{name.lower()}", "kind": kind,
                   "api_name": name, "namespace": None, "definition_status": "defined",
                   "definitions": []} for kind, name in names]
    ids = [component["id"] for component in components]
    relationships = []
    evidence = []
    for number, (source, target, relation, occurrences) in enumerate([
        (ids[1], ids[0], "writes", 2), (ids[1], ids[0], "reads", 1),
        (ids[2], ids[1], "calls", 1), (ids[1], ids[2], "calls", 1),
        (ids[3], ids[0], "contains", 1),
    ]):
        relationship_id = f"relationship:{number}"
        occurrence_ids = []
        for occurrence in range(occurrences):
            occurrence_id = f"evidence:{number}:{occurrence}"
            occurrence_ids.append(occurrence_id)
            evidence.append({"id": occurrence_id, "relationship_id": relationship_id,
                             "source_component": source, "path": "flows/Sample.flow-meta.xml",
                             "locator": f"/Flow/assignments[{number}]/value[{occurrence}]",
                             "line": number + 1, "column": None,
                             "end_line": None, "end_column": None,
                             "content_hash": "0" * 64, "rule": "test",
                             "extractor_version": "0.1.0", "resolution": "explicit"})
        relationships.append({"id": relationship_id, "source": source, "target": target,
                              "relation": relation, "qualifiers": {},
                              "evidence_ids": occurrence_ids})
    return {"manifest": {"schema_version": 1, "snapshot_id": "snapshot:example",
                          "coverage": {"unsupported_constructs": 1},
                          "inputs": {"a.xml": "hash"},
                          "scope": {"include_untracked": False}, "source_revision": "a" * 40},
            "facts": {"components": components, "relationships": relationships,
                      "evidence": evidence}, "diagnostics": []}


def test_refs_preserves_parallel_relations_and_occurrences(bundle):
    result = query.refs(bundle, "Account.Name")
    assert {row["relation"] for row in result["results"]} == {"reads", "writes", "contains"}
    write = next(row for row in result["results"] if row["relation"] == "writes")
    assert len(write["evidence"]) == 2
    assert result["freshness"]["state"] == "unknown"
    assert result["coverage"]["unsupported_constructs"] == 1
    filtered = query.refs(bundle, "SF:FIELD:ACCOUNT.NAME", relation="writes")
    assert filtered["total"] == 1


def test_impact_is_incoming_bounded_and_cycle_safe(bundle):
    first = query.impact(bundle, "Account.Name", depth=1)
    assert {row["relation"] for row in first["results"]} == {"reads", "writes"}
    assert first["depth_limited"]
    complete = query.impact(bundle, "Account.Name", depth=10)
    assert len(complete["results"]) == 4
    assert not complete["truncated"]
    assert complete["interpretation"] == "candidate_impact"
    assert all(row["evidence"] for row in complete["results"])
    limited = query.impact(bundle, "Account.Name", limit=1)
    assert len(limited["results"]) == 1
    assert limited["limit_limited"]


def test_deps_returns_outgoing_facts_and_full_evidence(bundle):
    result = query.deps(bundle, "Set_Name")
    assert {item["relation"] for item in result["results"]} == {"reads", "writes", "calls"}
    assert all(item["source"] == "sf:flow:set_name" for item in result["results"])
    write = query.deps(bundle, "Set_Name", relation="writes")["results"][0]
    assert len(write["evidence"]) == 2
    assert query.deps(bundle, "Set_Name", limit=1)["truncated"]


@pytest.mark.parametrize("name", ["Set_Name", "sf:flow:set_name", "SF:FLOW:SET_NAME"])
def test_duplicate_definition_is_ambiguous_even_for_exact_id(bundle, name):
    item = next(row for row in bundle["facts"]["components"] if row["kind"] == "flow")
    item["definition_status"] = "ambiguous"
    item["definitions"] = [{"path": "one/Sample.flow-meta.xml"},
                           {"path": "two/Sample.flow-meta.xml"}]
    with pytest.raises(query.QueryError) as ambiguous:
        query.deps(bundle, name)
    assert ambiguous.value.code == 3
    assert len(ambiguous.value.candidates[0]["definitions"]) == 2


def test_exact_resolution_ambiguity_empty_results_and_search(bundle):
    assert query.refs(bundle, "Account")["results"] == []
    with pytest.raises(query.QueryError) as missing:
        query.refs(bundle, "Nonexistent")
    assert missing.value.code == 4
    bundle["facts"]["components"].append({"id": "sf:flow:account", "kind": "flow",
                                           "api_name": "Account"})
    with pytest.raises(query.QueryError) as ambiguous:
        query.refs(bundle, "Account")
    assert ambiguous.value.code == 3
    assert len(ambiguous.value.candidates) == 2
    assert query.refs(bundle, "sf:object:account")["results"] == []
    found = query.components(bundle, "Account", kind="field")
    assert [row["id"] for row in found["results"]] == ["sf:field:account.name"]


def test_explain_and_invalid_bounds(bundle):
    assert len(query.explain(bundle, "relationship:0")["results"][0]["evidence"]) == 2
    with pytest.raises(query.QueryError):
        query.impact(bundle, "Account.Name", depth=0)
    with pytest.raises(query.QueryError):
        query.refs(bundle, "Account.Name", limit=0)


def test_freshness_compares_actual_inputs_and_scope(bundle, monkeypatch, tmp_path):
    from graphify.salesforce import discovery
    from types import SimpleNamespace
    scan = SimpleNamespace(inputs=dict(bundle["manifest"]["inputs"]),
                           scope=dict(bundle["manifest"]["scope"]), assert_unchanged=lambda: None)
    monkeypatch.setattr(discovery, "discover", lambda *args, **kwargs: scan)
    assert query.freshness(bundle, tmp_path)["state"] == "current"
    scan.inputs["a.xml"] = "changed"
    assert query.freshness(bundle, tmp_path)["changed"] == ["a.xml"]
    scan.inputs = {}
    assert query.freshness(bundle, tmp_path)["deleted"] == ["a.xml"]


def test_cli_outputs_one_json_document_and_preserves_exit_status(bundle, monkeypatch, capsys):
    from graphify.salesforce import snapshot
    monkeypatch.setattr(snapshot, "load", lambda path: bundle)
    code = cli.main(["refs", "Account.Name", "--snapshot", "unused", "--relation", "writes"])
    output = capsys.readouterr()
    assert code == 0 and output.err == ""
    assert json.loads(output.out)["total"] == 1
    assert cli.main(["refs", "Account.Name", "--snapshot", "unused", "--format", "text"]) == 0
    assert "flows/Sample.flow-meta.xml:1" in capsys.readouterr().out
    assert cli.main(["refs", "Missing", "--snapshot", "unused"]) == 4
    output = capsys.readouterr()
    assert json.loads(output.out)["error"] == "No matching component: Missing"
    assert output.err
    assert cli.main(["status", "--snapshot", "unused", "--require-current"]) == 5
    assert json.loads(capsys.readouterr().out)["freshness"]["state"] == "unknown"


def test_cli_guide_writes_only_stdout(capsys):
    assert cli.main(["guide"]) == 0
    output = capsys.readouterr()
    assert "absent edge" in output.out
    assert output.err == ""


def test_cli_deps_and_ambiguous_definitions_are_visible(bundle, monkeypatch, capsys):
    from graphify.salesforce import snapshot
    monkeypatch.setattr(snapshot, "load", lambda path: bundle)
    assert cli.main(["deps", "Set_Name", "--snapshot", "unused"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 3
    item = next(row for row in bundle["facts"]["components"] if row["kind"] == "flow")
    item["definition_status"] = "ambiguous"
    item["definitions"] = [{"path": "one/Sample.flow-meta.xml"},
                           {"path": "two/Sample.flow-meta.xml"}]
    assert cli.main(["deps", "sf:flow:set_name", "--snapshot", "unused", "--format", "text"]) == 3
    text = capsys.readouterr().out
    assert "one/Sample.flow-meta.xml" in text and "two/Sample.flow-meta.xml" in text


def test_cli_export_uses_local_source_mapping_for_containment(bundle, monkeypatch, tmp_path, capsys):
    from graphify.salesforce import snapshot
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(snapshot, "load", lambda path: bundle)
    monkeypatch.setattr(snapshot, "source_root", lambda path: source)
    output = source / "must-not-be-created"
    assert cli.main(["export", "graphify", "--snapshot", str(tmp_path / "snapshot"),
                     "--out", str(output)]) == 1
    assert json.loads(capsys.readouterr().out)["error"]
    assert not output.exists()


def test_projection_preserves_fact_ids_and_does_not_mutate_snapshot(bundle, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network call")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    original = copy.deepcopy(bundle)
    output = tmp_path / "projection"
    path = export_graphify(bundle, output)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert bundle == original
    assert data["directed"]
    assert data["graph"]["canonical_snapshot_id"] == bundle["manifest"]["snapshot_id"]
    assert data["built_at_commit"] == bundle["manifest"]["source_revision"]
    pair = next(row for row in data["links"] if row["source"] == "sf:flow:set_name"
                and row["target"] == "sf:field:account.name")
    assert pair["canonical_relationship_ids"] == ["relationship:0", "relationship:1"]
    assert pair["relation_counts"] == {"reads": 1, "writes": 1}
    assert pair["relation"] == "reads, writes"
    assert {node["id"] for node in data["nodes"]} == {
        component["id"] for component in bundle["facts"]["components"]}
    bundle["facts"] = {"components": [], "relationships": [], "evidence": []}
    export_graphify(bundle, output)
    assert json.loads(path.read_text(encoding="utf-8"))["nodes"] == []


def test_projection_cannot_overwrite_canonical_bundle(bundle, tmp_path):
    with pytest.raises(DataError):
        export_graphify(bundle, tmp_path / "snapshot", snapshot_dir=tmp_path / "snapshot")
    with pytest.raises(DataError):
        export_graphify(bundle, tmp_path / "snapshot" / "nested",
                        snapshot_dir=tmp_path / "snapshot")


def test_projection_rejects_junction_or_symlink_before_resolving(bundle, tmp_path):
    target = tmp_path / "external"
    target.mkdir()
    link = tmp_path / "output-link"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("Directory symlink creation is unavailable")
    with pytest.raises(DataError, match="links or junctions"):
        export_graphify(bundle, link / "generated")
    assert not (target / "generated").exists()


def test_query_text_coverage_is_compact_and_full_output_is_opt_in(bundle, monkeypatch, capsys):
    from graphify.salesforce import snapshot

    coverage = bundle["manifest"]["coverage"]
    coverage.update(status="partial", analyzed_files=12, diagnostic_counts={"many_details": 3})
    monkeypatch.setattr(snapshot, "load", lambda path: bundle)
    command = ["refs", "Account.Name", "--snapshot", "unused"]
    assert cli.main([*command, "--format", "text"]) == 0
    output = capsys.readouterr().out
    assert "Coverage: partial, 12 files analyzed" in output
    assert "many_details" not in output

    assert cli.main([*command, "--format", "text", "--full-coverage"]) == 0
    assert '"many_details": 3' in capsys.readouterr().out

    assert cli.main(command) == 0
    assert json.loads(capsys.readouterr().out)["coverage"] == coverage
