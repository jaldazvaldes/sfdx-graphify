"""Compact source locations for the Salesforce dependency CLI."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from itertools import groupby
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from .automation import publication_labels
from .discovery import _capture, _safe_path
from .query import assigned_write_value
from .schema import DataError
from .xml_source import parse_xml


_UPDATE_LOCATOR = re.compile(r"^/Flow/recordUpdates\[@name=([A-Za-z_][A-Za-z0-9_]*)\]/inputAssignments(?:\[\d+\])?/field\Z")
_PUBLICATION_ORDER = {"indexed_and_candidate_production_callers": 0, "indexed_production_caller": 0,
                      "candidate_production_caller": 1, "not_applicable": 2, "no_indexed_production_callers": 3}
_CALL_TARGET_LABELS = {"outboundmessage": "outbound message", "flow": "Flow", "apexclass": "Apex class"}


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


def record_update_text(occurrence: dict, *, relative: bool = False) -> str | None:
    proof = occurrence.get("resolution")
    update = proof.get("record_update") if isinstance(proof, dict) else None
    if update is None:
        if occurrence.get("rule") == "flow.record_input_field":
            match = _UPDATE_LOCATOR.fullmatch(occurrence.get("locator", ""))
            if match:
                return f"Record update {match[1]}"
        return None
    site = update["site"]
    line = site.get("line") or "?"
    location = f"line {line}" if relative else f"{site['path']}:{line}"
    return f"Update attempt {update['element']} at {location} (collection {update['collection']})"


def event_rows(result: dict) -> tuple[list[dict], str | None]:
    rows = result.get("results", [])
    event = result.get("platform_event")
    if event is None or "relation_filter" not in event:
        return rows, None
    rows = sorted(rows, key=_publication_rank)
    if event["relation_filter"] is None:
        grants = sum(row["relation"] == "grants" for row in rows)
        if grants:
            noun = "relationship" if grants == 1 else "relationships"
            return [row for row in rows if row["relation"] != "grants"], (
                f"Permission grants summarized ({grants} returned {noun}). Use --relation grants for their source sites."
            )
    return rows, None


def _publication_rank(edge: dict) -> int:
    return _PUBLICATION_ORDER.get(edge.get("publication_context", {}).get("caller_status"), 4)


def relationship_labels(edge: dict) -> tuple[str, ...]:
    if edge["relation"] == "contains" and edge["target"].startswith("sf:apexmethod:"):
        return ("method declaration",)
    if edge["relation"] == "calls":
        kind = edge["target"].split(":", 2)[1]
        label = _CALL_TARGET_LABELS.get(kind)
        if label:
            return (label,)
    return publication_labels(edge)


def _description(edge: dict, occurrence: dict, focus: str | None, value_filtered: bool,
                 *, representative: bool = False) -> str:
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
    parts.extend(f"[{label}]" for label in relationship_labels(edge))
    if edge.get("resolution", {}).get("status") == "candidate":
        reason = edge["resolution"].get("reason") or "unverified"
        missing = edge["resolution"].get("missing_ancestors", ())
        parts.append("[candidate: " + reason + (", " + ", ".join(missing) if missing else "") + "]")
    elif edge.get("traversal", {}).get("candidate_relationship_ids"):
        parts.append("[candidate path]")
    if not representative and (literal := assigned_write_value(occurrence)):
        if not value_filtered:
            parts.append(_value_text(literal["value"]))
        site = occurrence["resolution"].get("assigned_value_location", {})
        line = site.get("line")
        if line is not None and (site.get("path"), line) != (path, occurrence.get("line")):
            label = str(line) if site["path"] == path else f"{site['path']}:{line}"
            parts.append(f"(value at {label})")
    if not representative and (update := record_update_text(occurrence, relative=True)):
        parts.append(f"[{update}]")
    return " ".join(filter(None, parts))


def compact_literal_hints(mentions: dict) -> list[str]:
    """Summarize supplemental literal searches separately from a selected relationship."""
    short = "method_name" if "method_name" in mentions else "field_name"
    lines = ["Unverified literal hints in scanned files (matches/files):"]
    groups = [("Metadata", mentions)]
    if "apex_strings" in mentions:
        groups.append(("Apex strings", mentions["apex_strings"]))
    for label, group in groups:
        counts = ", ".join(f"{group[key]['occurrence_count']}/{group[key]['file_count']} {key.replace('_', ' ')}"
                           for key in ("qualified", short))
        lines.append(f"  {label} ({group['scanned_file_count']} scanned, {group['skipped_file_count']} skipped): {counts}.")
    ambiguity = ("Methods may match unrelated text and overloads share hints." if short == "method_name"
                 else "Field names may match other objects or unrelated text.")
    lines.append(f"{ambiguity} Use --format text for example paths.")
    return lines


def evidence_lines(result: dict) -> list[str]:
    focus = result.get("component", {}).get("id")
    rows = defaultdict(list)
    path_ranks: dict[str, int] = {}
    aura_markup_sites: set[tuple[str, int]] = set()
    total = 0
    edges, grants = event_rows(result)
    for edge in edges:
        for occurrence in edge["evidence"]:
            path = occurrence["path"]
            rank = _publication_rank(edge)
            path_ranks[path] = min(path_ranks.get(path, rank), rank)
            label = _description(edge, occurrence, focus, "value_filter" in result)
            if occurrence.get("rule") == "aura.apex_action":
                proof = occurrence.get("resolution") or {}
                markup = proof.get("controller_declaration") or {}
                if markup.get("path") and markup.get("line"):
                    aura_markup_sites.add((markup["path"], markup["line"]))
                enqueue = proof.get("enqueue") or {}
                if enqueue.get("path") and enqueue.get("line"):
                    site = (f"line {enqueue['line']}" if enqueue["path"] == path
                            else f"{enqueue['path']}:{enqueue['line']}")
                    label += f" [lookup, enqueue at {site}]"
            rows[path].append((occurrence.get("line"), label))
            total += 1
    if not rows:
        return [grants] if grants else []
    lines = [f"Indexed source sites for {focus or 'this query'} ({total} occurrences)."]
    apex_hints = result.get("literal_blind_spots", {}).get("apex_strings")
    if apex_hints is None or any(apex_hints[key]["occurrence_count"] for key in ("qualified", "field_name")):
        lines.append("Use check-apex to classify supplemental Apex search hits before counting them as code.")
    for path, line in sorted(aura_markup_sites):
        lines.append(f"Aura controller markup root: {path}:{line}")
    for path in sorted(rows, key=lambda path: (path_ranks[path], path)):
        entries = rows[path]
        lines.append(path)
        entries.sort(key=lambda entry: (entry[0] is None, entry[0] or 0, entry[1]))
        for label, group in groupby(entries, key=lambda entry: entry[1]):
            sites = ", ".join("?" if line is None else str(line) for line, _ in group)
            lines.append(f"  {sites} {label}")
    return [*lines, grants] if grants else lines


def writer_evidence_lines(result: dict, inputs: dict[str, str], source: Path, *, full_coverage: bool) -> list[str]:
    """Cite indexed field writes only where the source still matches the snapshot."""
    root = source.resolve(strict=True)
    if not root.is_dir():
        raise DataError("The writer source must be a checkout directory.")
    focus = result["component"]
    sites = defaultdict(list)
    for edge in result["results"]:
        for site in edge["evidence"]:
            label = _description(edge, site, focus["id"], "value_filter" in result)
            sites[site["path"]].append((site.get("line"), label))

    coverage = result["coverage"]
    coverage_text = (json.dumps(coverage, ensure_ascii=False, sort_keys=True) if full_coverage else
                     coverage.get("status", "unknown"))
    lines = [f"Indexed writers of {focus['api_name']}",
             f"Snapshot: {result['snapshot_id']}", f"Coverage: {coverage_text}"]
    if result["activity_policy"].get("mode") == "exclude_explicitly_inactive":
        lines.append("Snapshot excludes explicitly inactive Flows.")
    lines.append("Source hashes checked for the listed files. Flow statuses are local XML declarations.")
    for path, rows in sorted(sites.items()):
        is_flow = path.casefold().endswith(".flow-meta.xml")
        try:
            data, digest, _ = _capture(_safe_path(root, path), keep_bytes=is_flow)
        except (OSError, DataError):
            lines.append(f"{path} — indexed writes unverified because the source file could not be read.")
            continue
        if digest != inputs.get(path):
            lines.append(f"{path} — indexed writes unverified because source differs from the snapshot.")
            continue
        status_cell = ""
        if is_flow:
            try:
                document = parse_xml(path, data)
            except DataError:
                status_cell = " | Declared Flow status: unknown (invalid XML)"
            else:
                statuses = document.root.all("status") if document.root.tag == "Flow" else []
                if len(statuses) == 1 and statuses[0].text and not statuses[0].children:
                    status = statuses[0]
                    value = json.dumps(status.text, ensure_ascii=False)
                    status_cell = f" | Declared Flow status: {value} ({path}:{status.line})"
                else:
                    status_cell = " | Declared Flow status: unknown (no unique, nonempty top-level status)"
        for line, label in sorted(rows, key=lambda row: (row[0] is None, row[0] or 0, row[1])):
            lines.append(f"{path}:{line if line is not None else '?'} {label}{status_cell}")
    if not sites:
        lines.append("No indexed writer sites in this result.")
    if result["truncated"]:
        lines.append("Result limit reached. Increase --limit to include more indexed writers.")
    return lines


def summary_lines(result: dict) -> list[str]:
    """Return representative citations from the returned relationships."""
    if "value_filter" in result:
        return evidence_lines(result)
    focus = result.get("component", {}).get("id")
    edges, grants = event_rows(result)
    save_inventory = "save_automation" in result and all(edge["relation"] == "triggered_by" for edge in edges)
    metadata_values = "custom_metadata_values" in result and any(edge["relation"] != "sets_value" for edge in edges)
    rows: defaultdict[tuple, list[tuple[str, int | None, str]]] = defaultdict(list)
    directory_ranks: dict[PurePosixPath, int] = {}
    path_ranks: dict[str, int] = {}
    skipped_values = 0
    for edge in edges:
        if metadata_values and edge["relation"] == "sets_value":
            skipped_values += len(edge["evidence"])
            continue
        kind = edge["source"].split(":", 2)[1]
        for occurrence in edge["evidence"]:
            path = occurrence["path"]
            if "publication_context" in edge:
                rank = _publication_rank(edge)
                parent = PurePosixPath(path).parent
                directory_ranks[parent] = min(directory_ranks.get(parent, rank), rank)
                path_ranks[path] = min(path_ranks.get(path, rank), rank)
            label = _description(edge, occurrence, focus, False, representative=True)
            if save_inventory:
                qualifiers = edge["qualifiers"]
                group = kind, qualifiers.get("timing"), qualifiers.get("local_status")
            else:
                group = path, edge["source"], edge["target"], label
            rows[group].append((path, occurrence.get("line"), label))
    if not rows:
        return [grants] if grants else []
    total = sum(map(len, rows.values()))
    scope = "save automation kind, phase and status" if save_inventory else "source component, file and dependency"
    lines = [f"Representative sites by {scope} ({total} occurrences across {len(rows)} groups).",
             "Use --format evidence for every returned location."]
    if skipped_values:
        lines.append(f"Custom metadata record sites omitted from these citations: {skipped_values}.")
    representatives: list[tuple[str, int | None, str, int]] = []
    for group in rows.values():
        path, line, label = min(group, key=lambda row: (row[0], row[1] is None, row[1] or 0, row[2]))
        representatives.append((path, line, label, len(group)))
    def order(row):
        path, line, label, _ = row
        parent = PurePosixPath(path).parent
        return directory_ranks.get(parent, 4), str(parent), path_ranks.get(path, 4), path, line or 0, label

    representatives.sort(key=order)
    for parent, group in groupby(representatives, key=lambda row: PurePosixPath(row[0]).parent):
        lines.append(f"{parent}/")
        for path, line, label, count in group:
            name = PurePosixPath(path).name
            location = f"{name}:{line}" if line is not None else name
            more = f" (+{count - 1} more)" if count > 1 else ""
            lines.append(f"  {location} {label}{more}")
    return [*lines, grants] if grants else lines
