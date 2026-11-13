"""Compact source locations for the Salesforce dependency CLI."""
from __future__ import annotations

import json
from collections import defaultdict
from itertools import groupby
from pathlib import PurePosixPath
from urllib.parse import unquote

from .query import assigned_write_value


def _value_text(value) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text[:117] + "…" if len(text) > 120 else text


def _component(identifier: str, path: str, *, omit_file_owner: bool) -> str:
    _, _, name = identifier.split(":", 2)
    name = unquote(name)
    if omit_file_owner:
        filename = PurePosixPath(path).name.casefold()
        stem = filename.removesuffix("-meta.xml").rsplit(".", 1)[0]
        if name.startswith(stem + "."):
            return name[len(stem) + 1:]
        if name == stem:
            return ""
    return name


def _description(edge: dict, occurrence: dict, focus: str | None, value_filtered: bool) -> str:
    path = occurrence["path"]
    source, target = edge["source"], edge["target"]
    qualifier = edge["qualifiers"]
    name = _component(source, path, omit_file_owner=True)
    other = _component(target, path, omit_file_owner=False)
    relation = edge["relation"]
    parts = [name, relation]
    if target != focus:
        parts.append(other)
    if effect := qualifier.get("effect"):
        parts.append(f"[{effect}]")
    if relation in {"triggered_by", "publishes", "subscribes"}:
        parts.extend(str(qualifier[key]) for key in ("timing", "local_status", "record_trigger_type", "trigger_order")
                     if qualifier.get(key) is not None)
        if events := qualifier.get("events"):
            parts.append(", ".join(events))
    if relation == "mentions":
        parts.append("[inferred]")
    if relation == "grants" and (permission := qualifier.get("permission")):
        parts.append(permission)
    if relation == "sets_value":
        parts.append(_value_text(qualifier["value"]))
    if edge.get("resolution", {}).get("status") == "candidate":
        reason = edge["resolution"].get("reason") or "unverified"
        missing = edge["resolution"].get("missing_ancestors", ())
        parts.append("[candidate: " + reason + (", " + ", ".join(missing) if missing else "") + "]")
    elif edge.get("traversal", {}).get("candidate_relationship_ids"):
        parts.append("[candidate path]")
    if literal := assigned_write_value(occurrence):
        if not value_filtered:
            parts.append(_value_text(literal["value"]))
        site = occurrence["resolution"].get("assigned_value_location", {})
        line = site.get("line")
        if line is not None and (site.get("path"), line) != (path, occurrence.get("line")):
            label = str(line) if site["path"] == path else f"{site['path']}:{line}"
            parts.append(f"(value at {label})")
    return " ".join(filter(None, parts))


def evidence_lines(result: dict) -> list[str]:
    focus = result.get("component", {}).get("id")
    rows = defaultdict(list)
    total = 0
    for edge in result.get("results", []):
        for occurrence in edge["evidence"]:
            path = occurrence["path"]
            label = _description(edge, occurrence, focus, "value_filter" in result)
            rows[path].append((occurrence.get("line"), label))
            total += 1
    if not rows:
        return []
    lines = [f"Indexed source sites for {focus or 'this query'} ({total} occurrences)."]
    lines.append("Use check-apex to classify supplemental Apex search hits before counting them as code.")
    for path, entries in sorted(rows.items()):
        lines.append(path)
        entries.sort(key=lambda entry: (entry[0] is None, entry[0] or 0, entry[1]))
        for label, group in groupby(entries, key=lambda entry: entry[1]):
            sites = ", ".join("?" if line is None else str(line) for line, _ in group)
            lines.append(f"  {sites} {label}")
    return lines
