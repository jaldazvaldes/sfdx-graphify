"""Load authored process maps against the current local Salesforce snapshot."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re

from .schema import DataError, relationship_resolution
from .snapshot import _no_links

_MAX_BYTES = 2 * 1024 * 1024
_MAX_CATALOG_BYTES = 16 * 1024 * 1024
_MAX_PROCESS_FILES = 1024
_FILE_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_DEVICE_NAMES = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
_STEP_KINDS = {"start", "action", "decision", "outcome"}
_EFFECT_KINDS = {"read", "assign", "create", "update", "upsert", "delete", "call", "condition"}


def _entries(value, label):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise DataError(f"Process maps require a list of {label}.")
    return value


def _text(item, key):
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DataError(f"Process maps require a non-empty {key}.")
    return value


def _identified(value, label):
    entries = _entries(value, label)
    identifiers = [_text(item, "id") for item in entries]
    if not identifiers or len(set(identifiers)) != len(identifiers):
        raise DataError(f"Process maps require unique IDs for {label}.")
    return entries


def _describes_effect(relationship, kind, target):
    if relationship["target"] != target:
        return False
    relation, qualifiers = relationship["relation"], relationship["qualifiers"]
    if kind in {"read", "condition"}:
        return relation in {"reads", "filters"}
    if kind == "call":
        return relation == "calls"
    if kind == "assign":
        return relation == "writes" and qualifiers.get("effect") == "memory"
    operation = qualifiers.get("operation")
    return (relation == "writes" and qualifiers.get("effect") == "database"
            and (operation == kind or (kind == "create" and operation == "insert")))


def _evidence(entries, inputs, relations, evidence, effect=None):
    records = []
    for entry in _entries(entries, "evidence references"):
        path = _text(entry, "path")
        line = entry.get("line")
        if path not in inputs or type(line) is not int or line < 1:
            raise DataError(f"Invalid process evidence location: {path}.")
        record = {"path": path, "line": line, "label": _text(entry, "label")}
        if "snippet" in entry:
            record["snippet"] = _text(entry, "snippet")
        matches = []
        for relation in relations:
            if effect and not _describes_effect(relation, effect["kind"], effect.get("component")):
                continue
            for evidence_id in relation["evidence_ids"]:
                item = evidence[evidence_id]
                if item["path"] == path and item.get("line") == line:
                    matches.append({"relationship": relation["id"], "relation": relation["relation"],
                                    "rule": item["rule"], "resolution": relationship_resolution(relation)["status"]})
        if matches:
            record["snapshot_matches"] = matches
        records.append(record)
    return records


def _step(entry, manifest, model):
    kind = _text(entry, "kind")
    if kind not in _STEP_KINDS:
        raise DataError(f"Unknown process step kind: {kind}.")
    ids = entry.get("components")
    if (not isinstance(ids, list) or
            any(not isinstance(cid, str) or not cid.startswith("sf:") for cid in ids)):
        raise DataError(f"Process step {_text(entry, 'id')} requires typed snapshot component IDs.")
    components = {cid: model.after_components[cid] for cid in ids if cid in model.after_components}
    missing = sorted(set(ids) - components.keys())
    relations = [
        model.after_relations[rid]
        for cid in components for rid in model.outgoing[cid]
        if rid in model.after_relations and model.after_relations[rid]["target"] in components
    ]
    inputs, evidence = manifest["inputs"], model.after_evidence
    effects = []
    for item in _entries(entry.get("effects"), "effects"):
        effect_kind = _text(item, "kind")
        target = item.get("component")
        if effect_kind not in _EFFECT_KINDS:
            raise DataError(f"Unknown process effect kind: {effect_kind}.")
        if "component" in item and (not isinstance(target, str) or target not in ids):
            raise DataError(f"Process effect names a component outside step {_text(entry, 'id')}.")
        effect: dict = {"kind": effect_kind, "summary": _text(item, "summary")}
        if target is not None:
            effect["component"] = target
        effect["evidence"] = _evidence(item.get("evidence"), inputs, relations, evidence, effect)
        if not effect["evidence"]:
            raise DataError(f"Process effect in step {_text(entry, 'id')} needs a source reference.")
        effects.append(effect)
    result = {
        "id": _text(entry, "id"), "label": _text(entry, "label"),
        "description": _text(entry, "description"), "kind": kind,
        "components": [{key: component[key] for key in ("id", "api_name", "kind", "definitions")}
                       for component in components.values()],
        "effects": effects,
        "evidence": _evidence(entry.get("evidence"), inputs, relations, evidence),
    }
    if missing:
        result["missing_components"] = missing
    return result


def _process(item, manifest, model):
    steps = [_step(entry, manifest, model) for entry in _identified(item.get("steps"), "steps")]
    step_ids = {step["id"] for step in steps}
    edges = []
    for entry in _entries(item.get("edges"), "step connections"):
        source, target = _text(entry, "from"), _text(entry, "to")
        label = entry.get("label")
        if source not in step_ids or target not in step_ids or not isinstance(label, str):
            raise DataError("Process connection must name two existing steps and a label.")
        edges.append({"from": source, "to": target, "label": label})
    process = {"id": _text(item, "id"), "title": _text(item, "title"), "summary": _text(item, "summary"),
               "steps": steps, "edges": edges}
    if any(step.get("missing_components") for step in steps):
        process["review_status"] = "needs_review"
    if "entry_step" in item:
        entry = _text(item, "entry_step")
        if entry not in step_ids:
            raise DataError("Process entry step must name an existing step.")
        process["entry_step"] = entry
    return process


def _catalog_bytes(path: Path) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise DataError("Process map file exceeds 2 MiB.")
    return data


def _decode_catalog(data: bytes) -> dict:
    try:
        catalog = json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise DataError("Invalid process map JSON.") from exc
    if not isinstance(catalog, dict):
        raise DataError("Process map JSON must be an object.")
    return catalog


def _process_path(index: Path, relative: str) -> Path:
    parts = relative.split("/")
    if (not relative.endswith(".json") or any(
        not _FILE_PART.fullmatch(part) or part.endswith(".")
        or part.split(".", 1)[0].casefold() in _DEVICE_NAMES for part in parts
    )):
        raise DataError(f"Process file must be a relative JSON path inside the catalog: {relative!r}.")
    path = index.parent.joinpath(*parts)
    _no_links(path)
    if path.resolve() == index.resolve():
        raise DataError("A process file cannot reference the catalog index.")
    return path


def _read_catalog(path: Path) -> tuple[dict, dict[str, bytes | None], str]:
    index = path.expanduser().absolute()
    data = _catalog_bytes(index)
    catalog = _decode_catalog(data)
    files: dict[str, bytes | None] = {}
    catalog_bytes = len(data)
    for country in _identified(catalog.get("countries"), "countries"):
        for feature in _identified(country.get("features"), "features"):
            for item in _identified(feature.get("processes"), "processes"):
                if "file" not in item:
                    continue
                relative = _text(item, "file")
                if item.keys() - {"id", "file", "entry_step"}:
                    raise DataError(f"Process file reference has unexpected fields: {relative!r}.")
                if relative not in files:
                    if len(files) >= _MAX_PROCESS_FILES:
                        raise DataError(f"Process catalog exceeds {_MAX_PROCESS_FILES} process files.")
                    if not files:
                        _no_links(index)
                    child = _process_path(index, relative)
                    try:
                        files[relative] = _catalog_bytes(child)
                    except FileNotFoundError:
                        files[relative] = None
                catalog_bytes += len(files[relative] or b"")
                if catalog_bytes > _MAX_CATALOG_BYTES:
                    raise DataError("Process catalog exceeds 16 MiB including referenced process contents.")
    index_digest = sha256(data)
    if not files:
        return catalog, files, index_digest.hexdigest()
    # The index binds all reference names and their uses. Include a separate
    # digest for each resolved file, with an explicit marker for a missing file.
    digest = sha256(b"sfdx-graphify-process-catalog-v1\0" + index_digest.digest())
    for relative in sorted(files):
        content = files[relative]
        digest.update(b"\0" if content is None else b"\1" + sha256(content).digest())
    return catalog, files, digest.hexdigest()


def process_catalog_digest(path: Path) -> str:
    return _read_catalog(path)[2]


def require_process_identity(actual: dict, expected: dict) -> None:
    if any(actual.get(key) != expected[key] for key in ("snapshot_id", "source_revision", "catalog_digest")):
        raise DataError("The registered snapshot or catalog changed. Run sfdx-graphify processes register again.")


def _process_from_file(item: dict, data: bytes | None, manifest: dict, model) -> dict:
    identifier, relative = item["id"], item["file"]
    try:
        if data is None:
            raise DataError("File is missing.")
        authored = _decode_catalog(data)
        if authored.get("id") != identifier:
            raise DataError("Process ID must match the catalog index.")
        if "entry_step" in item:
            authored["entry_step"] = _text(item, "entry_step")
        return _process(authored, manifest, model)
    except DataError as exc:
        return {
            "id": identifier, "title": identifier, "summary": "This process map is unavailable.",
            "steps": [], "edges": [], "review_status": "needs_review",
            "catalog_error": f"{relative}: {exc}",
        }


def load_process_maps(path: Path, model) -> dict:
    """Resolve explicit components and evidence from the current snapshot only."""
    catalog, files, digest = _read_catalog(path)
    manifest = model.bundle["manifest"]
    revision = manifest.get("source_revision")
    if not revision or catalog.get("source_revision") != revision or manifest.get("dirty") is not False:
        raise DataError("Process maps require a clean snapshot at the source revision named in the map file.")
    countries = []
    errors = 0
    for country in _identified(catalog.get("countries"), "countries"):
        features = []
        for feature in _identified(country.get("features"), "features"):
            processes = [_process_from_file(item, files[item["file"]], manifest, model) if "file" in item
                         else _process(item, manifest, model)
                         for item in _identified(feature.get("processes"), "processes")]
            errors += sum("catalog_error" in process for process in processes)
            features.append({"id": _text(feature, "id"), "name": _text(feature, "name"), "processes": processes})
        countries.append({"id": _text(country, "id"), "name": _text(country, "name"), "features": features})
    result = {"source_revision": revision, "snapshot_id": model.manifest_id,
              "catalog_digest": digest, "countries": countries}
    if errors:
        result["catalog_errors"] = errors
    if "country_rules" in catalog:
        if not isinstance(catalog["country_rules"], dict):
            raise DataError("Process country rules must be a JSON object.")
        result["country_rules"] = catalog["country_rules"]
    return result
