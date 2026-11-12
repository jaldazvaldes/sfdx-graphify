"""Deterministic queries over canonical Salesforce facts; no model calls."""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from .schema import (CUSTOM_METADATA_RELATIONS, RELATED_EVIDENCE_SITES, SCHEMA_VERSION, DataError,
                     canonical_json, relationship_resolution, shared_activity_field)


class QueryError(DataError):
    """A query failure with a stable process exit status."""

    def __init__(self, message: str, code: int = 2, candidates: list | None = None):
        super().__init__(message)
        self.code = code
        self.candidates = candidates or []


def freshness(bundle: dict, source: str | Path | None = None) -> dict:
    """Check selected source bytes, not just the repository commit."""
    if source is None:
        return {"state": "unknown", "reason": "Source was not supplied for verification."}
    from .discovery import discover

    manifest = bundle["manifest"]
    scope = manifest.get("scope", {})
    try:
        scan = discover(Path(source), include_untracked=scope.get("include_untracked", False),
                        fingerprints_only=True)
        scan.assert_unchanged()
    except (DataError, OSError, ValueError) as exc:
        return {"state": "unknown", "reason": f"Source could not be verified: {exc}"}
    old = manifest.get("inputs", {})
    new = scan.inputs
    added = sorted(set(new) - set(old))
    deleted = sorted(set(old) - set(new))
    changed = sorted(path for path in old.keys() & new.keys() if old[path] != new[path])
    # Extraction options are pinned in the snapshot. Source freshness compares
    # selected project inputs and the settings that select those inputs.
    extraction_options = {"custom_metadata_accessors", "include_permissions"}
    scope_changed = scan.scope != {key: value for key, value in scope.items() if key not in extraction_options}
    return {
        "state": "stale" if added or deleted or changed or scope_changed else "current",
        "added": added, "deleted": deleted, "changed": changed,
        "scope_changed": scope_changed,
    }


def envelope(bundle: dict, results: list, *, source=None, truncated: bool = False) -> dict:
    manifest = bundle["manifest"]
    # The exclusion inventory remains in the snapshot for exact-name errors.
    # Repeating every inactive definition twice in every query would swamp the
    # actual evidence; the normal envelope carries the complete policy counts.
    policy = {key: value for key, value in manifest.get("activity_policy", {"mode": "legacy_all_statuses"}).items()
              if key != "excluded_components"}
    coverage = dict(manifest.get("coverage", {}))
    if "activity_policy" in coverage:
        coverage["activity_policy"] = {key: value for key, value in coverage["activity_policy"].items()
                                       if key != "excluded_components"}
    return {
        "schema_version": manifest.get("schema_version", SCHEMA_VERSION),
        "snapshot_id": manifest["snapshot_id"],
        "freshness": freshness(bundle, source),
        "coverage": coverage,
        "activity_policy": policy,
        "results": results,
        "truncated": truncated,
        "resolution_policy": {
            "candidate_calls_included": True,
            "interpretation": "Candidate relationships include unverified external hierarchies and conditional trigger dispatch. Inspect their reason and source evidence before relying on a path.",
        },
        "limitations": [
            "Missing relationships do not prove absence of dependencies.",
            "Check current source before drawing consequential conclusions.",
        ],
    }


def _components(bundle: dict) -> dict:
    return {item["id"]: item for item in bundle["facts"]["components"]}


def _evidence(bundle: dict) -> dict:
    return {item["id"]: item for item in bundle["facts"]["evidence"]}


@dataclass(frozen=True, slots=True)
class QueryIndex:
    """Lookup tables for a verified bundle pinned by a server or session."""

    components: dict
    evidence: dict

    @classmethod
    def build(cls, bundle: dict) -> QueryIndex:
        return cls(_components(bundle), _evidence(bundle))


def resolve_component(bundle: dict, name: str, *, indexed: dict | None = None) -> dict:
    """Resolve exact IDs or qualified API names, refusing ambiguous names."""
    def require_unique_definition(item):
        if item.get("definition_status") == "ambiguous":
            raise QueryError(f"Ambiguous component definitions: {name}", 3, [item])
        return item

    needle = name.casefold()
    components = _components(bundle) if indexed is None else indexed
    if name in components:
        return require_unique_definition(components[name])
    by_id = [item for key, item in components.items() if key.casefold() == needle]
    if len(by_id) == 1:
        return require_unique_definition(by_id[0])
    matches = [item for item in components.values() if (
        str(item.get("api_name", "")).casefold() == needle
        or str(item.get("qualified_name", "")).casefold() == needle
        or item["id"].split(":", 2)[-1].casefold() == needle
    )]
    if not matches and "." not in name and ":" not in name:
        matches = [item for item in components.values() if item["kind"] == "field"
                   and item.get("api_name", "").rsplit(".", 1)[-1].casefold() == needle]
    if len(matches) > 1:
        raise QueryError(f"Ambiguous component: {name}", 3,
                         sorted(matches, key=lambda item: item["id"]))
    if not matches:
        owner, _, field = needle.removeprefix("sf:field:").partition(".")
        activity = shared_activity_field(components, owner, field)
        if activity is not None:
            return activity
        excluded = bundle["manifest"].get("activity_policy", {}).get("excluded_components", [])
        omitted = [item for item in excluded if name.casefold() in
                   {item["id"].casefold(), item["api_name"].casefold()}]
        if omitted:
            states = ", ".join(sorted({item["local_status"] for item in omitted}))
            raise QueryError(f"Component excluded by local inactive status ({states}): {name}", 4, omitted)
        raise QueryError(f"No matching component: {name}", 4)
    return require_unique_definition(matches[0])


_DIAGNOSTIC_SUMMARY_LIMIT = 20


def _diagnostic_mode(mode):
    if not isinstance(mode, str) or mode not in {"summary", "full"}:
        raise QueryError("diagnostics must be summary or full.")


def diagnostic_context(bundle: dict, target: dict, results: list, *, mode: str = "summary") -> dict:
    """Summarize consulted files, with bounded focused records or explicit full detail.

    File counts include sibling methods because unresolved references cannot
    always be assigned to a component. Detailed summary records require an
    explicit component association or overlap with a known source span. A
    missing end line establishes only the anchor line, never the whole file.
    """
    _diagnostic_mode(mode)
    paths = {item["path"] for item in target.get("definitions", [])}
    paths.update(item["path"] if isinstance(item, dict) else item
                 for item in target.get("source_files", []))
    locations = list(target.get("definitions", []))
    for result in results:
        occurrences = result.get("evidence", [])
        locations.extend(occurrences)
        for occurrence in occurrences:
            proof = occurrence.get("resolution")
            if not isinstance(proof, dict):
                continue  # Older snapshots can store the resolution as text.
            for key, _ in RELATED_EVIDENCE_SITES:
                site = proof.get(key)
                if isinstance(site, dict) and isinstance(site.get("path"), str):
                    locations.append(site)
    paths.update(item["path"] for item in locations)
    spans = defaultdict(list)
    for location in locations:
        start = location.get("line")
        if type(start) is int and start > 0:
            end = location.get("end_line")
            spans[location["path"]].append((start, end if type(end) is int and end >= start else start))
    for path, intervals in spans.items():
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        spans[path] = merged
    span_starts = {path: [start for start, _ in intervals] for path, intervals in spans.items()}
    selected = [item for item in bundle.get("diagnostics", [])
                if (item.get("location") or {}).get("path") in paths
                or item.get("source_component") == target["id"]
                or item.get("target_component") == target["id"]]
    relevant = []
    by_file = defaultdict(Counter)
    unlocated = 0
    for item in selected:
        location = item.get("location") or {}
        path, start = location.get("path"), location.get("line")
        if path is not None:
            by_file[path][item["code"]] += 1
        else:
            unlocated += 1
        associated = item.get("source_component") == target["id"] or item.get("target_component") == target["id"]
        overlaps = False
        if type(start) is int and start > 0:
            end = location.get("end_line")
            end = end if type(end) is int and end >= start else start
            index = bisect_right(span_starts.get(path, []), end) - 1
            overlaps = index >= 0 and spans[path][index][1] >= start
        if associated or overlaps:
            relevant.append(item)
    relevant.sort(key=lambda item: (
        (item.get("location") or {}).get("path") or "",
        (item.get("location") or {}).get("line") or 0,
        item["code"], item.get("id", ""), item.get("message", ""),
    ))
    returned = selected if mode == "full" else relevant[:_DIAGNOSTIC_SUMMARY_LIMIT]
    omitted = len(selected) - len(returned)
    return {
        "scope": "queried component and returned evidence source files",
        "mode": mode, "paths": sorted(paths), "total": len(selected), "truncated": bool(omitted),
        "code_counts": dict(sorted(Counter(item["code"] for item in selected).items())),
        "file_counts": {path: {"total": sum(counts.values()), "code_counts": dict(sorted(counts.items()))}
                        for path, counts in sorted(by_file.items())},
        "unlocated_count": unlocated, "relevant_count": len(relevant),
        "returned_count": len(returned), "omitted_count": omitted,
        "omitted_outside_focus_count": len(selected) - len(relevant) if mode == "summary" else 0,
        "omitted_by_limit_count": max(0, len(relevant) - _DIAGNOSTIC_SUMMARY_LIMIT) if mode == "summary" else 0,
        "record_limit": _DIAGNOSTIC_SUMMARY_LIMIT if mode == "summary" else None,
        "diagnostics": returned,
        "other_snapshot_diagnostic_count": len(bundle.get("diagnostics", [])) - len(selected),
        "full_details": "Repeat this query with --diagnostics full (JSONL/HTTP: diagnostics=full) for every consulted-file diagnostic; snapshot diagnostics are unchanged.",
        "interpretation": "File-scoped extraction diagnostics, not proof of complete dependency coverage; unknown consumers may be in other files.",
    }


def _expanded(relationship: dict, evidence: dict, *, evidence_ids: list[str] | None = None) -> dict:
    selected = relationship["evidence_ids"] if evidence_ids is None else evidence_ids
    return {**relationship, "evidence_ids": selected, "resolution": relationship_resolution(relationship),
            "evidence": [evidence[key] for key in selected]}


def assigned_write_value(occurrence: dict) -> dict | None:
    """Read the typed value attached to this occurrence, if present."""
    proof = occurrence.get("resolution")
    value = proof.get("assigned_value") if isinstance(proof, dict) else None
    return value if isinstance(value, dict) and "value" in value else None


def _matches_assigned_value(occurrence: dict, value: str) -> bool:
    assignment = assigned_write_value(occurrence)
    if assignment is None:
        return False
    literal = assignment["value"]
    return value == (literal if isinstance(literal, str) else canonical_json(literal))


def _limit(limit: int) -> None:
    if limit < 1:
        raise QueryError("Limit must be at least 1.")


def _scope(bundle: dict, exclude_tests: bool, index: QueryIndex | None) -> tuple[dict, list, dict]:
    if type(exclude_tests) is not bool:
        raise QueryError("exclude_tests must be a boolean.")
    components = _components(bundle) if index is None else index.components
    tests = {cid for cid, item in components.items() if item.get("is_test") is True}
    relationships = bundle["facts"]["relationships"]
    retained = ([item for item in relationships if item["source"] not in tests and item["target"] not in tests]
                if exclude_tests else relationships)
    return components, retained, {
        "exclude_tests": exclude_tests,
        "known_test_components_in_snapshot": len(tests),
        "excluded_components_in_snapshot": len(tests) if exclude_tests else 0,
        "excluded_relationships_in_snapshot": len(relationships) - len(retained),
        "unknown_apex_components_retained": sum(
            item["kind"] in {"apexclass", "apexmethod"} and item.get("is_test") is None
            for item in components.values()),
        "classification": "explicit_ast_metadata; unknown classification is retained",
        "ordering": "production before tests, then relationship type and qualified names within traversal depth",
    }


def _relationship_key(item: dict, components: dict) -> tuple:
    source, target = components[item["source"]], components[item["target"]]
    order = {"calls": 0, "publishes": 1, "subscribes": 2, "triggered_by": 3, "writes": 4, "filters": 5,
             "reads": 6, "references": 7, "contains": 8, "grants": 9, "instance_of": 10, "sets_value": 11,
             "mentions": 12}
    return (source.get("is_test") is True or target.get("is_test") is True,
            order.get(item["relation"], len(order)), str(source.get("api_name", source["id"])).casefold(),
            str(target.get("api_name", target["id"])).casefold(), item["id"])


def _scoped_target(bundle: dict, name: str, exclude_tests: bool, indexed: dict) -> dict:
    target = resolve_component(bundle, name, indexed=indexed)
    if exclude_tests and target.get("is_test") is True:
        raise QueryError(f"Component is excluded by the test filter: {name}", 4)
    return target


def _owned_methods(target: dict, relationships: list, components: dict) -> set[str]:
    if target["kind"] != "apexclass":
        return set()
    return {item["target"] for item in relationships
            if item["relation"] == "contains" and item["source"] == target["id"]
            and components[item["target"]]["kind"] == "apexmethod"
            and components[item["target"]].get("definition_status") != "ambiguous"}


def _class_reference_summary(methods: set[str], matches: list[dict]) -> dict:
    indirect = [item for item in matches if item["target"] in methods]
    return {
        "method_scope": "directly_owned",
        "method_count": len(methods),
        "referenced_method_count": len({item["target"] for item in indirect}),
        "method_relationship_count": len(indirect),
        "class_relationship_count": len(matches) - len(indirect),
    }


def _custom_metadata_value_summary(matches: list[dict], components: dict) -> dict:
    groups = defaultdict(set)
    records = set()
    for item in matches:
        if item["relation"] != "sets_value":
            continue
        record = components[item["source"]]["api_name"]
        qualifiers = item["qualifiers"]
        groups[qualifiers["value_type"], qualifiers["value"]].add(record)
        records.add(record)
    ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0][0], repr(item[0][1])))
    return {"record_count": len(records),
            "groups": [{"value_type": kind, "value": value, "record_count": len(names),
                        "records": sorted(names, key=str.casefold)} for (kind, value), names in ordered]}


def _references(bundle: dict, name: str, endpoint: str, *, relation: str | None = None,
                limit: int = 1000, source=None, exclude_tests: bool = False, diagnostics: str = "summary",
                value: str | None = None, index: QueryIndex | None = None) -> dict:
    _limit(limit)
    _diagnostic_mode(diagnostics)
    indexed, relationships, filtering = _scope(bundle, exclude_tests, index)
    target = _scoped_target(bundle, name, exclude_tests, indexed)
    if value is not None:
        if not isinstance(value, str):
            raise QueryError("value must be a string.")
        if relation not in {None, "writes"}:
            raise QueryError("The value filter requires writes.")
        relation = "writes"
    methods = _owned_methods(target, relationships, indexed) if endpoint == "target" else set()
    evidence = _evidence(bundle) if index is None else index.evidence
    matches = sorted((item for item in relationships
                      if (item[endpoint] == target["id"]
                          or (item["relation"] != "contains" and item[endpoint] in methods))
                      and (relation is None or item["relation"] == relation)),
                     key=lambda item: _relationship_key(item, indexed))
    matching_evidence = {}
    if value is not None:
        for item in matches:
            selected = [key for key in item["evidence_ids"] if _matches_assigned_value(evidence[key], value)]
            if selected:
                matching_evidence[item["id"]] = selected
        matches = [item for item in matches if item["id"] in matching_evidence]
    result = envelope(bundle, [_expanded(item, evidence, evidence_ids=matching_evidence.get(item["id"]))
                               for item in matches[:limit]],
                      source=source, truncated=len(matches) > limit)
    result["component"] = target
    result["total"] = len(matches)
    result["test_filter"] = filtering
    if value is not None:
        result["value_filter"] = {"literal": value, "occurrence_count": sum(map(len, matching_evidence.values()))}
        if bundle["manifest"].get("schema_version", SCHEMA_VERSION) < 13:
            result["limitations"].append("This snapshot predates assigned literal write values. Rebuild to use this filter.")
    if endpoint == "target" and target["kind"] == "apexclass":
        result["class_references"] = _class_reference_summary(methods, matches)
    if relation == "grants" and result["coverage"].get("permission_selection", {}).get("included") is False:
        result["limitations"].append("Permission extraction was disabled for this snapshot. Use build --include-permissions to extract declared grants.")
    if (endpoint == "target" and relation in {None, "sets_value"} and target["kind"] == "field"
            and target["api_name"].split(".", 1)[0].casefold().endswith("__mdt")
            and bundle["manifest"]["schema_version"] >= 11):
        from .custom_metadata import field_default_summary
        result["custom_metadata_values"] = _custom_metadata_value_summary(matches, indexed)
        result["custom_metadata_values"].update(field_default_summary(target, relationships, indexed))
    if endpoint == "target":
        from .automation import reference_context
        result.update(reference_context(bundle, target, relationships, indexed))
    result["diagnostic_context"] = diagnostic_context(bundle, target, result["results"], mode=diagnostics)
    return result


def refs(bundle: dict, name: str, *, relation: str | None = None,
         limit: int = 1000, source=None, exclude_tests: bool = False, diagnostics: str = "summary",
         value: str | None = None, index: QueryIndex | None = None) -> dict:
    """List incoming relationships, retaining all occurrences per result."""
    result = _references(bundle, name, "target", relation=relation, limit=limit, source=source,
                         exclude_tests=exclude_tests, diagnostics=diagnostics, value=value, index=index)
    component = result["component"]
    if component["kind"] in {"field", "apexmethod"}:
        from .blind_spots import literal_mentions
        result["literal_blind_spots"] = literal_mentions(bundle["manifest"], component)
    return result


def deps(bundle: dict, name: str, *, relation: str | None = None,
         limit: int = 1000, source=None, exclude_tests: bool = False, diagnostics: str = "summary",
         index: QueryIndex | None = None) -> dict:
    """List a component's outgoing dependencies with all supporting occurrences."""
    return _references(bundle, name, "source", relation=relation, limit=limit, source=source,
                       exclude_tests=exclude_tests, diagnostics=diagnostics, index=index)


def impact(bundle: dict, name: str, *, depth: int = 2, limit: int = 1000,
           relation: str | None = None, source=None, exclude_tests: bool = False, diagnostics: str = "summary",
           index: QueryIndex | None = None) -> dict:
    """Walk incoming dependency edges; this is candidate impact, not execution."""
    _limit(limit)
    _diagnostic_mode(diagnostics)
    if depth < 1 or depth > 100:
        raise QueryError("Depth must be between 1 and 100.")
    indexed, relationships, filtering = _scope(bundle, exclude_tests, index)
    target = _scoped_target(bundle, name, exclude_tests, indexed)
    methods = _owned_methods(target, relationships, indexed)
    evidence = _evidence(bundle) if index is None else index.evidence
    incoming = defaultdict(list)
    call_ids = set()
    for item in relationships:
        if item["relation"] == "contains" or (item["relation"] == "grants" and relation != "grants"):
            continue
        if relation is None and item["relation"] in CUSTOM_METADATA_RELATIONS:
            continue
        if relation is None or item["relation"] == relation:
            incoming[item["target"]].append(item)
            if item["relation"] == "calls":
                call_ids.add(item["id"])
    for items in incoming.values():
        items.sort(key=lambda item: _relationship_key(item, indexed))
    roots = [target["id"], *sorted(methods)]
    queue = deque((component_id, 0) for component_id in roots)
    # One deterministic shortest witness per visited node. It does not assert
    # that all possible paths have the same certainty, nor describe execution.
    witness_paths = {component_id: ([], []) for component_id in roots}
    visited_nodes = set(roots)
    visited_edges = set()
    results = []
    depth_limited = False
    limit_limited = False
    while queue:
        distance = queue[0][1]
        frontier = {}
        while queue and queue[0][1] == distance:
            component_id, _ = queue.popleft()
            for item in incoming[component_id]:
                if item["id"] not in visited_edges:
                    if distance >= depth:
                        depth_limited = True
                    else:
                        frontier[item["id"]] = item
        for item in sorted(frontier.values(), key=lambda edge: _relationship_key(edge, indexed)):
            if len(results) >= limit:
                limit_limited = True
                break
            visited_edges.add(item["id"])
            path, candidate_ids = witness_paths[item["target"]]
            path = [item["id"], *path]
            candidate_ids = ([item["id"], *candidate_ids]
                             if relationship_resolution(item)["status"] == "candidate"
                             else list(candidate_ids))
            traversal = {"relationship_ids": path, "candidate_relationship_ids": candidate_ids,
                         "includes_candidate_calls": any(key in call_ids for key in candidate_ids)}
            if any(key not in call_ids for key in candidate_ids):
                traversal["includes_candidate_relationships"] = True
            results.append({**_expanded(item, evidence), "depth": distance + 1, "traversal": traversal})
            if item["source"] not in visited_nodes:
                visited_nodes.add(item["source"])
                witness_paths[item["source"]] = (path, candidate_ids)
                queue.append((item["source"], distance + 1))
        if limit_limited:
            break
    result = envelope(bundle, results, source=source,
                      truncated=depth_limited or limit_limited)
    result.update({"component": target, "interpretation": "declared_permissions" if relation == "grants" else "candidate_impact",
                   "max_depth": depth, "depth_limited": depth_limited,
                   "limit_limited": limit_limited, "test_filter": filtering,
                   "returned_count": len(results)})
    result["traversal_policy"] = "Each witness starts with the returned relationship, then follows a deterministic shortest route from its target to the focus; it may revisit nodes. Candidate flags describe that witness, not all possible paths or runtime execution."
    if target["kind"] == "apexclass":
        result["class_references"] = _class_reference_summary(methods, [item for root in roots for item in incoming[root]])
        result["traversal_policy"] += " The class and its directly owned methods each start at depth zero."
    result["diagnostic_context"] = diagnostic_context(bundle, target, results, mode=diagnostics)
    return result


def explain(bundle: dict, relationship_id: str, *, source=None, index: QueryIndex | None = None) -> dict:
    evidence = _evidence(bundle) if index is None else index.evidence
    for item in bundle["facts"]["relationships"]:
        if item["id"] == relationship_id:
            return envelope(bundle, [_expanded(item, evidence)], source=source)
    raise QueryError(f"No matching relationship: {relationship_id}", 4)


def components(bundle: dict, term: str = "", *, kind: str | None = None,
               limit: int = 1000, source=None, exclude_tests: bool = False, index: QueryIndex | None = None) -> dict:
    _limit(limit)
    indexed, _, filtering = _scope(bundle, exclude_tests, index)
    needle = term.casefold()
    matches = sorted((item for item in indexed.values()
                      if (kind is None or item["kind"] == kind)
                      and (not exclude_tests or item.get("is_test") is not True)
                      and (needle in item["id"].casefold()
                           or needle in str(item.get("api_name", "")).casefold())),
                     key=lambda item: (item.get("is_test") is True, str(item.get("api_name", item["id"])).casefold(), item["id"]))
    result = envelope(bundle, matches[:limit], source=source, truncated=len(matches) > limit)
    scope = bundle["manifest"].get("scope", {})
    if not matches and scope.get("selection") == "git_tracked" and not scope.get("include_untracked"):
        result["limitations"].append(
            "This snapshot uses tracked source files. If the component is untracked, rebuild with --include-untracked."
        )
    result["total"] = len(matches)
    result["test_filter"] = filtering
    return result


def status(bundle: dict, *, source=None) -> dict:
    return envelope(bundle, [], source=source)


def dispatch(bundle: dict, command: str, name: str = "", *, index: QueryIndex | None = None, **options) -> dict:
    """Shared single-query and session dispatch; preserve complete query results."""
    handlers = {"components": components, "refs": refs, "deps": deps,
                "impact": impact, "explain": explain, "status": status}
    if command not in handlers:
        raise QueryError(f"Unknown query command: {command}")
    if command == "status":
        return status(bundle, **options)
    return handlers[command](bundle, name, index=index, **options)
