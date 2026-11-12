"""Search the authored process catalog and select evidence for one step."""
from __future__ import annotations

import re
from urllib.parse import urlencode

from .query import QueryError


def _name(entry: dict) -> dict:
    return {"id": entry["id"], "name": entry["name"]}


def _scope(entries: list[dict], term: str | None, kind: str) -> list[dict]:
    if term is None:
        return entries
    value = term.strip().casefold()
    selected = ([entry for entry in entries if entry["id"].casefold() == value]
                or [entry for entry in entries if entry["name"].casefold() == value])
    if len(selected) > 1:
        raise QueryError(f"Ambiguous {kind}: {term}", 3, [_name(entry) for entry in selected])
    return selected


def _records(catalog: dict, country: str | None, feature: str | None):
    for country_entry in _scope(catalog["countries"], country, "country"):
        available = country_entry["features"]
        selected = _scope(available, feature, "feature") if feature is not None else []
        if feature is not None and not selected:
            continue
        feature_entry = selected[0] if selected else None
        memberships: dict[str, list[dict]] = {}
        variants: dict[str, dict] = {}
        for member in available:
            for process in member["processes"]:
                identifier = process["id"]
                memberships.setdefault(identifier, []).append(_name(member))
                # The viewer uses the last variant for a repeated ID in All features.
                if feature_entry is None or member is feature_entry:
                    variants[identifier] = process
        for identifier, process in variants.items():
            yield {"country": _name(country_entry), "feature": feature_entry,
                   "features": memberships[identifier], "process": process}


def _header(record: dict, step: str | None = None) -> dict:
    process = record["process"]
    params = {"country": record["country"]["id"]}
    feature = record["feature"]
    if feature is not None:
        params["feature"] = feature["id"]
        step = step or process.get("entry_step")
    params["process"] = process["id"]
    if step is not None:
        params["step"] = step
    header = {key: process[key] for key in ("id", "title", "summary")} | {
        "country": record["country"], "features": record["features"],
        "viewer_path": "/processes#" + urlencode(params),
    }
    header.update({key: process[key] for key in ("review_status", "catalog_error") if key in process})
    return header


def _words(value: str) -> list[str]:
    return re.findall(r"[^\W_]+", value.casefold())


def _matches(entry: dict, keys: tuple[str, ...], term: str) -> bool:
    words = _words(term)
    available = _words(" ".join(entry[key] for key in keys))
    return bool(words) and all(any(value.startswith(word) for value in available) for word in words)


def _find(entries: list[dict], term: str, label_key: str) -> list[dict]:
    value = term.strip().casefold()
    return ([entry for entry in entries if entry["id"].casefold() == value]
            or [entry for entry in entries if entry[label_key].casefold() == value]
            or [entry for entry in entries if _matches(entry, ("id", label_key), term)])


def _provenance(catalog: dict) -> dict:
    return {key: catalog[key] for key in ("source_revision", "snapshot_id", "catalog_errors") if key in catalog}


def _component_fields(component: dict) -> dict:
    return {key: component[key] for key in ("id", "api_name", "kind")}


def _find_component(catalog: dict, term: str) -> dict | None:
    components = {}
    for record in _records(catalog, None, None):
        for step in record["process"]["steps"]:
            for component in step["components"]:
                components[component["id"]] = component
    value = term.strip().casefold()
    by_id = [item for item in components.values() if item["id"].casefold() == value]
    matches = by_id or [item for item in components.values() if item["api_name"].casefold() == value]
    if len(matches) > 1:
        raise QueryError(f"Ambiguous component: {term}", 3,
                         [_component_fields(item) for item in sorted(matches, key=lambda item: item["id"])])
    return _component_fields(matches[0]) if matches else None


def list_processes(catalog: dict, *, country: str | None = None, feature: str | None = None,
                   match: str | None = None, component: str | None = None) -> dict:
    """List each process once per country, optionally with steps that use a component."""
    result = _provenance(catalog)
    target = _find_component(catalog, component) if component is not None else None
    if component is not None and target is None:
        return result | {"results": []}
    if target is not None:
        result["component"] = target
    results = []
    for record in _records(catalog, country, feature):
        process = record["process"]
        if match is not None and not _matches(process, ("id", "title", "summary"), match):
            continue
        header = _header(record)
        if target is not None:
            steps = [
                {key: step[key] for key in ("id", "label", "kind", "missing_components") if key in step} |
                {"viewer_path": _header(record, step["id"])["viewer_path"]}
                for step in process["steps"]
                if any(item["id"] == target["id"] for item in step["components"])
            ]
            if not steps:
                continue
            header["matching_steps"] = steps
        results.append(header)
    results.sort(key=lambda row: (row["country"]["id"].casefold(), row["title"].casefold(), row["id"]))
    return result | {"results": results}


def _compact_evidence(entries: list[dict]) -> list[dict]:
    def resolution(entry):
        matches = {match["resolution"] for match in entry.get("snapshot_matches", [])}
        return next((status for status in ("extracted", "inferred", "candidate") if status in matches), "documented")

    if not entries:
        return []
    priority = {"extracted": 0, "inferred": 1, "candidate": 2, "documented": 3}
    evidence = min(entries, key=lambda entry: priority[resolution(entry)])
    return [{"path": evidence["path"], "line": evidence["line"], "resolution": resolution(evidence)}]


def _compact_step(step: dict) -> dict:
    compact = {key: step[key] for key in ("id", "label", "kind", "description")}
    if step.get("missing_components"):
        compact["missing_components"] = step["missing_components"]
    names = {item["id"]: item["api_name"] for item in step["components"]}
    effects = []
    for effect in step["effects"]:
        summary = {key: effect[key] for key in ("kind", "summary")}
        if target := effect.get("component"):
            summary.update({"component": target, "component_name": names.get(target, target)})
        summary["evidence"] = _compact_evidence(effect["evidence"])
        effects.append(summary)
    compact["effects"] = effects
    return compact


def show_process(catalog: dict, process: str, *, country: str | None = None, feature: str | None = None,
                 step: str | None = None) -> dict:
    """Return a connected process outline and the full evidence for a selected step."""
    records = list(_records(catalog, country, feature))
    matches = _find([record["process"] for record in records], process, "title")
    if not matches:
        raise QueryError(f"No matching process: {process}. Use processes list to find a process ID.", 4)
    selected = [record for record in records if any(record["process"] is match for match in matches)]
    if len(selected) != 1:
        raise QueryError(f"Ambiguous process: {process}", 3, [_header(record) for record in selected])
    record = selected[0]
    steps = record["process"]["steps"]
    outline = [{key: item[key] for key in ("id", "label", "kind")} for item in steps]
    detail = None
    if step is not None:
        step_matches = _find(steps, step, "label")
        if not step_matches:
            raise QueryError(f"No matching process step: {step}", 4, outline)
        if len(step_matches) != 1:
            identifiers = {item["id"] for item in step_matches}
            raise QueryError(f"Ambiguous process step: {step}", 3,
                             [item for item in outline if item["id"] in identifiers])
        detail = step_matches[0]
    header = _header(record, detail["id"] if detail else None) | {
        "steps": [_compact_step(item) for item in steps], "edges": record["process"]["edges"],
    }
    response = _provenance(catalog) | {"process": header}
    if detail is not None:
        response["step"] = detail
    return response


def _view_link(result: dict, item: dict) -> str:
    return (result.get("viewer_url") or "") + item["viewer_path"]


def _brief_effect(effect: dict) -> str:
    summary = effect["summary"]
    component = effect.get("component_name")
    if component and component.casefold() not in summary.casefold():
        summary = f"{component} · {summary}"
    line = f"  - {effect['kind']} · {summary}"
    if effect["evidence"]:
        evidence = effect["evidence"][0]
        status = "" if evidence["resolution"] == "extracted" else f" · {evidence['resolution']}"
        line += f" [{evidence['path']}:{evidence['line']}{status}]"
    return line


def format_process_brief(result: dict) -> str:
    """Print a readable process list or outline with one citation per effect."""
    lines = [f"Source revision {result['source_revision']}", f"Snapshot {result['snapshot_id']}",
             *result.get("warnings", [])]
    if result.get("catalog_errors"):
        lines.append("Some process entries are unavailable. Run processes check for details.")
    if "results" in result:
        if not result["results"]:
            lines.append("No matching processes.")
        for process in result["results"]:
            status = " · Review needed" if process.get("review_status") == "needs_review" else ""
            lines.append(f"- {process['country']['name']} · {process['title']} [{process['id']}]{status}")
            if error := process.get("catalog_error"):
                lines.append(f"  {error}")
            for step in process.get("matching_steps", []):
                lines.append(f"  - {step['label']} [{step['id']}] · {_view_link(result, step)}")
        return "\n".join(lines)
    process = result["process"]
    lines.extend([f"{process['country']['name']} · {process['title']} [{process['id']}]",
                  process["summary"], f"View {_view_link(result, process)}"])
    if error := process.get("catalog_error"):
        lines.append(error)
    elif process.get("review_status") == "needs_review":
        lines.append("Review needed. Some components are missing from the snapshot.")
    steps = process["steps"]
    if detail := result.get("step"):
        steps = [step for step in steps if step["id"] == detail["id"]]
    for step in steps:
        lines.extend([f"- {step['label']} [{step['id']}, {step['kind']}]", f"  {step['description']}"])
        if missing := step.get("missing_components"):
            lines.append("  Missing components " + ", ".join(missing))
        lines.extend(_brief_effect(effect) for effect in step["effects"])
        for edge in process["edges"]:
            if edge["from"] == step["id"]:
                label = f" · {edge['label']}" if edge["label"] else ""
                lines.append(f"  → {edge['to']}{label}")
    return "\n".join(lines)
