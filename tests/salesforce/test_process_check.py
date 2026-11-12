"""Process evidence drift in a real Git worktree."""
from __future__ import annotations

from pathlib import Path
import subprocess

from graphify.salesforce.process_check import check_processes, format_process_check, project_revision


SOURCE = "run() {\n    if (ready) {\n        setStatus();\n    }\n}\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Process test", "-c", "user.email=process@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
         "-c", "core.autocrlf=false", "-C", str(root), *args],
        check=True, capture_output=True,
    )


def _citation(path: str, snippet: str | None = "setStatus();") -> dict:
    ref = {"path": path, "line": 3, "label": path}
    if snippet is not None:
        ref["snippet"] = snippet
    return ref


def _catalog(revision: str, references: list[dict], *, missing_component: bool = False) -> dict:
    step = {
        "id": "apply",
        "evidence": references[:-1],
        "effects": [{"kind": "assign", "evidence": references[-1:]}],
    }
    if missing_component:
        step["missing_components"] = ["sf:field:order.status"]
    process = {"id": "update", "title": "Update", "steps": [step]}
    features = [
        {"id": "original", "processes": [process]},
        {"id": "alternative", "processes": [{**process, "entry_step": "apply"}]},
    ]
    return {"source_revision": revision, "snapshot_id": "snapshot:fixture",
            "countries": [{"id": "ES", "name": "Spain", "features": features}]}


def test_check_reports_context_and_line_drift_in_a_project_subdirectory(tmp_path):
    _git(tmp_path, "init", "--template=")
    project = tmp_path / "salesforce"
    project.mkdir()
    names = ("matched", "moved", "changed", "repeated", "deleted", "no_snippet", "changed_file")
    for name in names:
        (project / f"{name}.cls").write_bytes(SOURCE.encode())
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Record source references")
    revision = project_revision(project)
    assert revision is not None

    (project / "moved.cls").write_text("// a new line\n" + SOURCE, encoding="utf-8")
    (project / "changed.cls").write_text(SOURCE.replace("if (ready)", "if (failed)"), encoding="utf-8")
    (project / "repeated.cls").write_text("// prefix\n" + SOURCE + SOURCE, encoding="utf-8")
    (project / "deleted.cls").unlink()
    (project / "changed_file.cls").write_text(SOURCE + "// later code\n", encoding="utf-8")
    refs = [_citation(f"{name}.cls", None if name == "no_snippet" else "setStatus();") for name in names]

    report = check_processes(_catalog(revision, refs, missing_component=True), project)
    assert report["revision_match"] is True
    assert report["summary"] == {
        "processes": 1, "processes_needing_review": 1, "missing_components": 1,
        "references": 7, "matched": 2, "relocated": 1, "ambiguous": 1, "missing": 1,
        "changed": 1, "unverified": 1,
    }
    process, = report["processes"]
    assert process["status"] == "needs_review"
    assert process["missing_components"] == ["sf:field:order.status"]
    checked = {ref["path"]: ref for ref in process["references"]}
    assert checked["moved.cls"]["checkout_line"] == 4
    assert checked["changed.cls"]["reason"] == "context_changed"
    assert "checkout_line" not in checked["changed.cls"]
    assert checked["repeated.cls"]["candidate_lines"] == [4, 9]
    assert checked["deleted.cls"]["reason"] == "file_missing"
    assert checked["changed_file.cls"]["file_changed"] is True
    assert checked["changed_file.cls"]["effect_kind"] == "assign"
    brief = format_process_check(report)
    assert "References checked 7 · Processes checked 1" in brief
    assert "- ES / update · 1 relocated · 1 ambiguous · 1 missing · 1 changed · 1 unverified" in brief


def test_check_requires_original_context_and_keeps_citations_inside_project(tmp_path):
    project = tmp_path / "salesforce"
    project.mkdir()
    assert project_revision(project) is None
    (project / "source.cls").write_text("\n" + SOURCE, encoding="utf-8")
    (tmp_path / "external.cls").write_text(SOURCE, encoding="utf-8")
    report = check_processes(_catalog("1" * 40, [_citation("source.cls"), _citation("../external.cls")]), project)

    assert report["revision_match"] is None
    actual, outside = report["processes"][0]["references"]
    assert actual["status"] == "unverified"
    assert actual["candidate_lines"] == [4]
    assert "checkout_line" not in actual
    assert outside["status"] == "unverified"
    assert outside["reason"] == "file_unreadable"
