"""Check authored process citations against a local Salesforce working tree."""
from __future__ import annotations

from collections import Counter
from functools import cache
from pathlib import Path
import re
import stat

from .discovery import _MAX_FILE_BYTES, _capture, _git, _safe_path
from .schema import DataError


_REVISION = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
_STATES = ("matched", "relocated", "ambiguous", "missing", "changed", "unverified")
_CANDIDATE_LIMIT = 10


def project_revision(project_root: Path) -> str | None:
    """Return the checkout HEAD without refreshing or writing its index."""
    try:
        raw = _git(project_root, ["rev-parse", "--verify", "HEAD"], optional=True)
        revision = raw.decode("ascii").strip() if raw is not None else ""
    except (DataError, UnicodeError):
        return None
    return revision if _REVISION.fullmatch(revision) else None


def _working_lines(root: Path, relative: str) -> tuple[tuple[str, ...] | None, str | None]:
    try:
        path = _safe_path(root, relative)
        if not stat.S_ISREG(path.stat().st_mode):
            return None, "not_a_regular_file"
        data, _, _ = _capture(path, keep_bytes=True)
        assert data is not None
        return tuple(data.decode("utf-8-sig").splitlines()), None
    except FileNotFoundError:
        return None, "file_missing"
    except (OSError, DataError, UnicodeError, RuntimeError):
        return None, "file_unreadable"


def _original_lines(root: Path, revision: str, relative: str) -> tuple[str, ...] | None:
    if not _REVISION.fullmatch(revision):
        return None
    try:
        # Use Git's working-directory-relative path syntax for projects in subdirectories.
        object_name = f"{revision}:./{relative}"
        options = ("--no-replace-objects",)
        size = _git(root, ["cat-file", "-s", object_name], optional=True, options=options)
        if size is None or int(size) > _MAX_FILE_BYTES:
            return None
        data = _git(root, ["cat-file", "blob", object_name], optional=True, options=options)
        return tuple(data.decode("utf-8-sig").splitlines()) if data is not None else None
    except (DataError, UnicodeError, ValueError):
        return None


def _context(lines: tuple[str, ...], line: int) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    offset = line - 1
    before = tuple(item.strip() for item in lines[max(0, offset - 2):offset])
    after = tuple(item.strip() for item in lines[offset + 1:offset + 3])
    return before, (lines[offset].strip(),), after


def _candidates(status: str, reason: str, lines: list[int]) -> dict:
    return {"status": status, "reason": reason, "candidate_lines": lines[:_CANDIDATE_LIMIT],
            "candidate_count": len(lines)}


def _compare(ref: dict, current: tuple[str, ...], original: tuple[str, ...] | None) -> dict:
    snippet = ref.get("snippet", "").strip()
    if not snippet:
        return {"status": "unverified", "reason": "no_snippet"}

    line = ref["line"]
    needle = snippet.removesuffix("…").removesuffix("...").rstrip()
    if not needle:
        return {"status": "unverified", "reason": "no_usable_snippet"}
    candidates = [index for index, text in enumerate(current, 1) if text.lstrip().startswith(needle)]
    if not candidates:
        return {"status": "missing", "reason": "snippet_absent"}
    if original is None or line > len(original) or not original[line - 1].lstrip().startswith(needle):
        return _candidates("unverified", "original_context_unavailable", candidates)

    anchor = _context(original, line)
    same_context = [candidate for candidate in candidates if _context(current, candidate) == anchor]
    if line in same_context:
        return {"status": "matched", "checkout_line": line}
    if len(same_context) == 1:
        return {"status": "relocated", "checkout_line": same_context[0]}
    if len(same_context) > 1:
        return _candidates("ambiguous", "multiple_context_matches", same_context)
    if len(candidates) > 1:
        return _candidates("ambiguous", "no_unique_context_match", candidates)
    return _candidates("changed", "context_changed", candidates)


def _references(process: dict):
    for step in process["steps"]:
        for ref in step["evidence"]:
            yield step["id"], None, ref
        for effect in step["effects"]:
            for ref in effect["evidence"]:
                yield step["id"], effect["kind"], ref


def _summary(references: list[dict]) -> dict:
    states = Counter(reference["status"] for reference in references)
    return {"references": len(references), **{state: states[state] for state in _STATES}}


def check_processes(catalog: dict, project_root: Path) -> dict:
    """Locate citations in the worktree with context from the registered Git revision.

    Source file and line matches assess citation drift. They do not verify
    that the process still behaves as documented.
    """
    root = project_root.resolve()
    source_revision = catalog["source_revision"]
    checkout_revision = project_revision(root)
    processes = []
    references = []

    @cache
    def source(relative: str):
        current, error = _working_lines(root, relative)
        original = _original_lines(root, source_revision, relative) if current is not None else None
        return current, original, error

    for country in catalog["countries"]:
        # Match the variant selected by the viewer in its all-features view.
        variants = {process["id"]: process for feature in country["features"] for process in feature["processes"]}
        for process in variants.values():
            checked = []
            for step_id, effect_kind, ref in _references(process):
                relative = ref["path"]
                current, original, error = source(relative)
                if current is None:
                    state = "missing" if error == "file_missing" else "unverified"
                    result = {"status": state, "reason": error}
                else:
                    result = _compare(ref, current, original)
                file_changed = None if current is None or original is None else current != original
                entry = {key: ref[key] for key in ("path", "line", "label")}
                entry.update(result, step_id=step_id, file_changed=file_changed)
                if effect_kind is not None:
                    entry["effect_kind"] = effect_kind
                checked.append(entry)
            references.extend(checked)
            changed_files = sorted({ref["path"] for ref in checked if ref["file_changed"] is True})
            missing_components = sorted({
                component for step in process["steps"] for component in step.get("missing_components", [])
            })
            needs_review = (bool(changed_files or missing_components) or process.get("review_status") == "needs_review"
                            or any(ref["status"] != "matched" for ref in checked))
            result = {
                "id": process["id"], "title": process["title"],
                "country": {key: country[key] for key in ("id", "name")},
                "status": "needs_review" if needs_review else "matched",
                "summary": _summary(checked), "references": checked,
                "changed_files": changed_files, "missing_components": missing_components,
            }
            if "catalog_error" in process:
                result["catalog_error"] = process["catalog_error"]
            processes.append(result)

    result = {
        "source_revision": source_revision, "snapshot_id": catalog["snapshot_id"],
        "checkout_revision": checkout_revision,
        "revision_match": None if checkout_revision is None else checkout_revision == source_revision,
        "summary": {
            "processes": len(processes), "processes_needing_review": sum(p["status"] == "needs_review" for p in processes),
            "missing_components": sum(len(p["missing_components"]) for p in processes), **_summary(references),
        },
        "processes": processes,
    }
    if catalog.get("catalog_errors"):
        result["summary"]["catalog_errors"] = catalog["catalog_errors"]
    return result


def format_process_check(result: dict) -> str:
    """Summarize citation drift and identify the maps that need review."""
    summary = result["summary"]
    revision_match = {True: "same revision", False: "different revisions", None: "unknown"}
    lines = [
        f"Catalog revision {result['source_revision']}",
        f"Checkout revision {result['checkout_revision'] or 'unknown'}",
        f"Revision comparison · {revision_match[result['revision_match']]}",
        f"References checked {summary['references']} · Processes checked {summary['processes']}",
        " · ".join(f"{summary[state]} {state}" for state in _STATES),
        f"Processes needing review {summary['processes_needing_review']}",
    ]
    if errors := summary.get("catalog_errors"):
        lines.append(f"Unavailable process entries {errors}")
    for process in result["processes"]:
        if process["status"] != "needs_review":
            continue
        counts = process["summary"]
        details = [f"{counts[state]} {state}" for state in _STATES[1:] if counts[state]]
        if process["changed_files"]:
            details.append(f"changed source files {len(process['changed_files'])}")
        if process["missing_components"]:
            details.append(f"missing components {len(process['missing_components'])}")
        if error := process.get("catalog_error"):
            details.append(error)
        suffix = " · ".join(details) or "catalog review required"
        lines.append(f"- {process['country']['id']} / {process['id']} · {suffix}")
    return "\n".join(lines)
