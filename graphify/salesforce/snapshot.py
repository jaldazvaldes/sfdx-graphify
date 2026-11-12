"""Deterministic local snapshots with an atomic pointer to immutable artifacts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Callable

from .discovery import _linked
from .schema import DataError, SUPPORTED_SCHEMA_VERSIONS, canonical_json, validate_bundle


_ARTIFACTS = ("manifest.json", "facts.json", "diagnostics.json", "references.tsv", "SUMMARY.txt")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_ARTIFACT_LIMIT_ENV = "SFDX_GRAPHIFY_MAX_ARTIFACT_MIB"
_MAX_POINTER_BYTES = 1024 * 1024


def _artifact_limit_bytes() -> int:
    """Resolve the local resource guard once per operation; never truncate data."""
    configured = os.environ.get(_ARTIFACT_LIMIT_ENV)
    if configured is None:
        return _MAX_ARTIFACT_BYTES
    value = configured.strip()
    try:
        mib = int(value) if re.fullmatch(r"[0-9]+", value) else 0
    except ValueError:
        mib = 0
    if not 0 < mib <= (sys.maxsize - 1) // (1024 * 1024):
        raise DataError(f"{_ARTIFACT_LIMIT_ENV} must be a positive integer number of MiB "
                        "within the platform's readable file-size range.")
    return mib * 1024 * 1024


def _artifact_size_error(name: str, limit: int) -> DataError:
    return DataError(
        f"Snapshot artifact exceeds the configured size limit of {limit / (1024 * 1024):g} MiB: {name}. "
        f"Set {_ARTIFACT_LIMIT_ENV} to a larger positive integer to retain the complete artifact."
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _read_limited(path: Path, limit: int, *, artifact: bool = False) -> bytes:
    if path.stat().st_size > limit:
        if artifact:
            raise _artifact_size_error(path.name, limit)
        raise DataError(f"Snapshot file exceeds the size limit: {path.name}")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        if artifact:
            raise _artifact_size_error(path.name, limit)
        raise DataError(f"Snapshot file exceeds the size limit: {path.name}")
    return content


def _no_links(path: Path) -> None:
    for part in (path, *path.parents):
        try:
            if _linked(part):
                raise DataError("Snapshot paths cannot contain symbolic links or junctions")
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DataError("Cannot inspect snapshot path") from exc


def _local_storage() -> Path:
    base = (Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
            if os.name == "nt" else Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))))
    return base / "sfdx-graphify"


def _checkout_directory(base: Path, root: Path) -> Path:
    identity = _sha(os.path.normcase(str(Path(root).expanduser().resolve())).encode("utf-8"))
    return validate_output(base / identity, source_root=root)


def local_project_directory(root: Path) -> Path:
    """Resolve the local directory for per-checkout settings."""
    return _checkout_directory(_local_storage(), root)


def default_output(root: Path) -> Path:
    """Keep private derived data outside the source and the tool checkout."""
    setting = _local_storage() / "snapshot-root.txt"
    _no_links(setting)
    try:
        configured = _read_limited(setting, _MAX_POINTER_BYTES).decode("utf-8-sig").strip()
    except FileNotFoundError:
        return local_project_directory(root)
    except (OSError, UnicodeError) as exc:
        raise DataError("Cannot read the snapshot root from snapshot-root.txt") from exc
    base = Path(configured).expanduser()
    if not base.is_absolute():
        raise DataError("snapshot-root.txt must contain an absolute snapshot directory")
    return _checkout_directory(base, root)


def validate_output(out: Path, source_root: Path | None = None) -> Path:
    path = Path(out).expanduser().absolute()
    _no_links(path)
    path = path.resolve()
    forbidden = [Path(__file__).resolve().parents[2]]
    if source_root is not None:
        forbidden.append(Path(source_root).expanduser().resolve())
    for root in forbidden:
        if path.is_relative_to(root) or root.is_relative_to(path):
            raise DataError("Output must be outside and must not contain the source project or tool repository")
    if path.exists() and not path.is_dir():
        raise DataError("Output must be a directory")
    return path


def _artifacts(bundle: dict) -> dict[str, bytes]:
    facts = bundle["facts"]
    evidence = {entry["id"]: entry for entry in facts.get("evidence", [])}
    text = io.StringIO(newline="")
    writer = csv.writer(text, delimiter="\t", lineterminator="\n")
    writer.writerow(("source", "relation", "target", "relationship_id", "evidence_id", "path", "locator"))
    for relation in sorted(facts.get("relationships", []), key=lambda item: item["id"]):
        for evidence_id in sorted(relation["evidence_ids"]):
            occurrence = evidence[evidence_id]
            writer.writerow((relation["source"], relation["relation"], relation["target"],
                             relation["id"], evidence_id, occurrence.get("path", occurrence.get("source_file", "")),
                             occurrence.get("locator", "")))
    summary = (
        "sfdx-graphify local dependency snapshot\n"
        f"Snapshot: {bundle['manifest']['snapshot_id']}\n"
        f"Components: {len(facts.get('components', []))}\n"
        f"Relationships: {len(facts.get('relationships', []))}\n"
        f"Evidence occurrences: {len(facts.get('evidence', []))}\n"
        f"Diagnostics: {len(bundle['diagnostics'])}\n"
        "This is a partial navigation and candidate-impact aid, not a complete dependency inventory.\n"
        "Missing relationships do not establish that dependencies are absent.\n"
        "Verify freshness, coverage, diagnostics, and cited current source before consequential decisions.\n"
        "Dependency relationships do not describe Flow execution order or deployed behavior.\n"
        "Coverage: " + canonical_json(bundle["manifest"].get("coverage", {})) + "\n"
    )
    return {
        "manifest.json": _json_bytes(bundle["manifest"]),
        "facts.json": _json_bytes(facts),
        "diagnostics.json": _json_bytes(bundle["diagnostics"]),
        "references.tsv": text.getvalue().encode("utf-8"), "SUMMARY.txt": summary.encode("utf-8"),
    }


def _write(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _replace_json(path: Path, value: object) -> None:
    temporary = path.with_name(".tmp-" + uuid.uuid4().hex + ".json")
    _write(temporary, _json_bytes(value))
    os.replace(temporary, path)


def _read_source_root(out: Path) -> Path | None:
    path = Path(out).expanduser().absolute() / "SOURCE.json"
    _no_links(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise DataError("Invalid snapshot source mapping file")
    try:
        value = json.loads(_read_limited(path, _MAX_POINTER_BYTES).decode("utf-8"))
        value = value["source_root"]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise DataError("Invalid source mapping in snapshot sidecar")
        return Path(value).resolve()
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError("Cannot read snapshot source mapping") from exc


def source_root(out: Path) -> Path | None:
    """Read local source mapping without adding absolute paths to canonical data."""
    return _read_source_root(out)


def publish(bundle: dict, out: Path, source_root: Path,
            *, source_guard: Callable[[], None] | None = None) -> Path:
    """Validate all data before switching CURRENT; failures preserve its old value.

    Generation directory names hash the logical snapshot ID so identifiers with
    colons are portable to Windows. Absolute source mapping is local sidecar data.
    """
    artifact_limit = _artifact_limit_bytes()
    validate_bundle(bundle)
    destination = validate_output(out, source_root)
    stored_root = _read_source_root(destination)
    resolved_source = Path(source_root).resolve()
    if stored_root is not None and stored_root != resolved_source:
        raise DataError("Output already belongs to a different source project; choose another directory")
    artifacts = _artifacts(bundle)
    for name, content in artifacts.items():
        if len(content) > artifact_limit:
            raise _artifact_size_error(name, artifact_limit)
    snapshot_id = bundle["manifest"]["snapshot_id"]
    generation = _sha(snapshot_id.encode("utf-8"))
    pointer = {"schema_version": bundle["manifest"]["schema_version"], "snapshot_id": snapshot_id, "generation": generation,
               "sha256": {name: _sha(content) for name, content in sorted(artifacts.items())}}
    try:
        destination.mkdir(parents=True, exist_ok=True)
        generations = destination / "generations"
        _no_links(generations)
        generations.mkdir(exist_ok=True)
        final = generations / generation
        _no_links(final)
        if final.exists():
            for name in artifacts:
                _no_links(final / name)
            if not final.is_dir() or any(_read_limited(final / name, artifact_limit, artifact=True) != content
                                        for name, content in artifacts.items()):
                raise DataError("Existing immutable generation conflicts with this snapshot")
        else:
            stage = generations / (".tmp-" + uuid.uuid4().hex)
            stage.mkdir()
            for name, content in artifacts.items():
                _write(stage / name, content)
            os.replace(stage, final)
        # One authoritative guard after immutable data is durable and immediately
        # before publishing its pointer. A failed guard may leave an unreferenced
        # generation, but cannot replace CURRENT or create a source association.
        # Validate the mapping path too: do not follow an existing sidecar link.
        _no_links(destination / "SOURCE.json")
        _no_links(destination / "CURRENT.json")
        if source_guard is not None:
            source_guard()
        if stored_root is None:
            _replace_json(destination / "SOURCE.json", {"source_root": str(resolved_source)})
        _replace_json(destination / "CURRENT.json", pointer)
        return final
    except (OSError, UnicodeError) as exc:
        raise DataError(f"Cannot publish snapshot: {exc}") from exc


def _read_pointer(destination: Path) -> dict:
    path = destination / "CURRENT.json"
    _no_links(path)
    if not path.is_file():
        raise DataError("Snapshot pointer is missing or is not a file")
    try:
        pointer = json.loads(_read_limited(path, _MAX_POINTER_BYTES).decode("utf-8"))
    except RecursionError as exc:
        raise DataError("Snapshot pointer nesting is too deep") from exc
    version = pointer.get("schema_version") if isinstance(pointer, dict) else None
    if type(version) is not int or version not in SUPPORTED_SCHEMA_VERSIONS:
        raise DataError("Unsupported snapshot pointer schema")
    generation = pointer.get("generation")
    if not isinstance(generation, str) or not _HEX.fullmatch(generation):
        raise DataError("Invalid snapshot generation identifier")
    snapshot_id = pointer.get("snapshot_id")
    if not isinstance(snapshot_id, str) or generation != _sha(snapshot_id.encode("utf-8")):
        raise DataError("Snapshot ID does not match its generation")
    hashes = pointer.get("sha256")
    if not isinstance(hashes, dict) or set(hashes) != set(_ARTIFACTS):
        raise DataError("Invalid snapshot artifact manifest")
    if any(not isinstance(digest, str) or not _HEX.fullmatch(digest) for digest in hashes.values()):
        raise DataError("Invalid snapshot artifact checksum")
    return pointer


def current_snapshot_id(out: Path) -> str:
    """Read the checked CURRENT identity without loading generation artifacts."""
    try:
        return _read_pointer(Path(out).expanduser().absolute())["snapshot_id"]
    except (OSError, ValueError) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError(f"Cannot read snapshot pointer: {exc}") from exc


def load(out: Path, *, expect_snapshot: str | None = None) -> dict:
    """Read one generation, verify every artifact, then validate canonical facts."""
    artifact_limit = _artifact_limit_bytes()
    destination = Path(out).expanduser().absolute()
    _no_links(destination)
    try:
        pointer = _read_pointer(destination)
        generation, snapshot_id, hashes = pointer["generation"], pointer["snapshot_id"], pointer["sha256"]
        if expect_snapshot is not None and snapshot_id != expect_snapshot:
            raise DataError(f"Expected snapshot {expect_snapshot}; loaded {snapshot_id}.")
        directory = destination / "generations" / generation
        _no_links(directory)
        contents = {}
        for name in _ARTIFACTS:
            path = directory / name
            _no_links(path)
            contents[name] = _read_limited(path, artifact_limit, artifact=True)
            if _sha(contents[name]) != hashes[name]:
                raise DataError(f"Snapshot integrity check failed: {name}")
        bundle = {key: json.loads(contents[f"{key}.json"].decode("utf-8"))
                  for key in ("manifest", "facts", "diagnostics")}
        if (bundle["manifest"].get("snapshot_id") != snapshot_id
                or bundle["manifest"].get("schema_version") != pointer["schema_version"]):
            raise DataError("Snapshot manifest does not match CURRENT")
        validate_bundle(bundle)
        if _artifacts(bundle) != contents:
            raise DataError("Snapshot artifacts are not canonical or disagree with the facts")
        return bundle
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError(f"Cannot load snapshot: {exc}") from exc
