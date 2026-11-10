"""Index literal field mentions in selected metadata outside extraction coverage."""

from __future__ import annotations

from collections import Counter
import re
from typing import TYPE_CHECKING

from .schema import DataError

if TYPE_CHECKING:
    from .discovery import Scan


_METADATA_SUFFIXES = (
    ".layout-meta.xml", ".flexipage-meta.xml", ".quickaction-meta.xml",
    ".permissionset-meta.xml", ".profile-meta.xml", ".reporttype-meta.xml",
    ".report-meta.xml", ".report", ".approvalprocess-meta.xml",
    ".page", ".component", ".email", ".email-meta.xml",
)
_AURA_SUFFIXES = (".app", ".cmp", ".design", ".evt", ".intf", ".js")
_TOKEN = re.compile(rb"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_SCAN_BYTES = 64 * 1024 * 1024
_EXAMPLE_LIMIT = 3


def _candidate(path: str) -> bool:
    path = path.casefold()
    return path.endswith(_METADATA_SUFFIXES) or ("/aura/" in f"/{path}" and path.endswith(_AURA_SUFFIXES))


def build_literal_index(scan: Scan, components: list[dict]) -> dict:
    from .discovery import _capture, _safe_path

    qualified = {c["api_name"].casefold().encode("utf-8") for c in components if c["kind"] == "field"}
    field_names = {name.rsplit(b".", 1)[-1] for name in qualified}
    summaries: dict[str, dict[str, dict]] = {"qualified": {}, "field_name": {}}
    eligible = [path for path in scan.scope["unsupported_paths"] if _candidate(path)]
    scanned = 0
    scanned_bytes = 0
    for relative in eligible:
        path = _safe_path(scan.root, relative)
        limit = min(_MAX_FILE_BYTES, _MAX_SCAN_BYTES - scanned_bytes)
        if path.stat().st_size > limit:
            continue
        data, digest, _ = _capture(path, keep_bytes=True)
        if data is None or digest != scan.inputs[relative]:
            raise DataError("Source changed while indexing literal mentions. Retry with a stable working tree.")
        if len(data) > limit:
            continue
        scanned += 1
        scanned_bytes += len(data)
        counts: Counter[tuple[str, bytes]] = Counter()
        for match in _TOKEN.finditer(data):
            token = match[0].lower()
            if token in qualified:
                counts["qualified", token] += 1
            else:
                leaf = token.rsplit(b".", 1)[-1]
                if leaf in field_names:
                    counts["field_name", leaf] += 1
        for (kind, name), count in counts.items():
            summary = summaries[kind].setdefault(name.decode("utf-8"), {
                "occurrence_count": 0, "file_count": 0, "example_paths": [],
            })
            summary["occurrence_count"] += count
            summary["file_count"] += 1
            if len(summary["example_paths"]) < _EXAMPLE_LIMIT:
                summary["example_paths"].append(relative)
    return {"eligible_file_count": len(eligible), "scanned_file_count": scanned, **summaries}


def field_mentions(manifest: dict, api_name: str) -> dict:
    index = manifest.get("literal_blind_spots")
    if index is None:
        return {"state": "unavailable", "reason": "This snapshot has no literal index."}
    scanned = index["scanned_file_count"]
    leaf = api_name.rsplit(".", 1)[-1]
    empty = {"occurrence_count": 0, "file_count": 0, "example_paths": []}
    return {
        "state": "indexed",
        "scanned_file_count": scanned,
        "skipped_file_count": index["eligible_file_count"] - scanned,
        "qualified": {"literal": api_name, **index["qualified"].get(api_name.casefold(), empty)},
        "field_name": {"literal": leaf, **index["field_name"].get(leaf.casefold(), empty)},
        "interpretation": (
            "Literal matches are review hints. Field-name matches include standalone names and "
            "suffixes of other dotted tokens, which may refer to another object, markup, or unrelated text. "
            "A zero count covers only the scanned metadata."
        ),
    }


def validate_literal_index(index: dict, inputs: dict) -> None:
    eligible = index["eligible_file_count"]
    scanned = index["scanned_file_count"]
    if type(eligible) is not int or type(scanned) is not int or not 0 <= scanned <= eligible <= len(inputs):
        raise DataError("Invalid literal metadata scan counts.")
    for kind in ("qualified", "field_name"):
        for summary in index[kind].values():
            count = summary["occurrence_count"]
            files = summary["file_count"]
            paths = summary["example_paths"]
            if (type(count) is not int or type(files) is not int or not 1 <= files <= min(scanned, count)
                    or not isinstance(paths, list) or not 1 <= len(paths) <= files
                    or paths != sorted(set(paths)) or any(path not in inputs for path in paths)):
                raise DataError("Invalid literal metadata match summary.")
