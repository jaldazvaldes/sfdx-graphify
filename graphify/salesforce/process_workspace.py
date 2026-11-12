"""Read the project process catalog and reuse the exact registered local viewer."""
from __future__ import annotations

from http.client import HTTPConnection, HTTPException
import json
from pathlib import Path

from .process_maps import load_process_maps, process_catalog_digest, require_process_identity
from .schema import DataError
from .snapshot import current_snapshot_id, load
from .viewer import SnapshotView

_VIEWER_RESPONSE_LIMIT = 16 * 1024 * 1024


def _viewer_catalog(registration: dict) -> dict | None:
    connection = HTTPConnection("127.0.0.1", registration["port"], timeout=2)
    try:
        connection.request("GET", "/api/processes")
        response = connection.getresponse()
        if response.status != 200:
            return None
        data = response.read(_VIEWER_RESPONSE_LIMIT + 1)
        if len(data) > _VIEWER_RESPONSE_LIMIT:
            return None
        catalog = json.loads(data)
        if not isinstance(catalog, dict) or not isinstance(catalog.get("countries"), list):
            return None
        require_process_identity(catalog, registration)
        return catalog
    except (OSError, HTTPException, ValueError, RecursionError):
        return None
    finally:
        connection.close()


def require_registered_files(registration: dict) -> None:
    snapshot = Path(registration["snapshot"])
    path = Path(registration["catalog"])
    if (current_snapshot_id(snapshot) != registration["snapshot_id"]
            or process_catalog_digest(path) != registration["catalog_digest"]):
        raise DataError("The registered snapshot or catalog changed. Run sfdx-graphify processes register again.")


def load_registered_catalog(registration: dict) -> tuple[dict, dict]:
    bundle = load(Path(registration["snapshot"]), expect_snapshot=registration["snapshot_id"])
    catalog = load_process_maps(Path(registration["catalog"]), SnapshotView(bundle))
    require_process_identity(catalog, registration)
    return bundle, catalog


def registered_catalog(registration: dict) -> tuple[dict, str | None]:
    require_registered_files(registration)
    live = _viewer_catalog(registration)
    if live is not None:
        return live, f"http://127.0.0.1:{registration['port']}"
    _, catalog = load_registered_catalog(registration)
    return catalog, None
