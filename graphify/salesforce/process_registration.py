"""Register a local process catalog and snapshot for one Salesforce checkout."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .process_maps import load_process_maps
from .schema import DataError
from .snapshot import _no_links, _read_limited, _replace_json, load, local_project_directory
from .viewer import SnapshotView

_REGISTRATION_KEYS = {"snapshot", "catalog", "port", "snapshot_id", "source_revision", "catalog_digest"}
_MAX_REGISTRATION_BYTES = 1024 * 1024


def _registration_path(project_root: Path) -> Path:
    root = project_root.expanduser().resolve()
    if not (root / "sfdx-project.json").is_file():
        raise DataError("Open the project folder containing sfdx-project.json to use a process catalog.")
    path = local_project_directory(root) / "PROCESS_MAPS.json"
    _no_links(path)
    return path


def _valid_port(port) -> bool:
    return type(port) is int and 1 <= port <= 65535


def _valid_absolute_path(value) -> bool:
    return isinstance(value, str) and "\0" not in value and Path(value).is_absolute()


def save_process_registration(project_root: Path, snapshot: Path, catalog: Path, port: int = 8767) -> dict:
    """Validate the catalog before replacing the checkout's local registration."""
    path = _registration_path(project_root)
    if not _valid_port(port):
        raise DataError("The registered process viewer port must be an integer from 1 to 65535.")
    snapshot = snapshot.expanduser().absolute()
    catalog = catalog.expanduser().absolute()
    resolved = load_process_maps(catalog, SnapshotView(load(snapshot)))
    if resolved.get("catalog_errors") and not any(
        "catalog_error" not in process for country in resolved["countries"]
        for feature in country["features"] for process in feature["processes"]
    ):
        raise DataError("No usable process maps in the catalog. Repair at least one process file before registering.")
    registration = {
        "snapshot": str(snapshot.resolve()),
        "catalog": str(catalog.resolve()),
        "port": port,
        "snapshot_id": resolved["snapshot_id"],
        "source_revision": resolved["source_revision"],
        "catalog_digest": resolved["catalog_digest"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    _replace_json(path, registration)
    return registration


def load_process_registration(project_root: Path) -> dict | None:
    """Read a registration without loading its snapshot or catalog."""
    path = _registration_path(project_root)
    try:
        registration = json.loads(_read_limited(path, _MAX_REGISTRATION_BYTES))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as exc:
        raise DataError("Cannot read the local process catalog registration.") from exc
    if not isinstance(registration, dict) or set(registration) != _REGISTRATION_KEYS:
        raise DataError("Invalid local process catalog registration.")
    revision = registration["source_revision"]
    digest = registration["catalog_digest"]
    snapshot_id = registration["snapshot_id"]
    if (not _valid_absolute_path(registration["snapshot"]) or not _valid_absolute_path(registration["catalog"])
            or not _valid_port(registration["port"]) or not isinstance(revision, str) or not revision.strip()
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(snapshot_id, str) or not re.fullmatch(r"snapshot:[0-9a-f]{64}", snapshot_id)):
        raise DataError("Invalid local process catalog registration.")
    return registration
