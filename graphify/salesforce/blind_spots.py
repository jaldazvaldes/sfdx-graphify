"""Index literal review hints in selected metadata and parsed Apex strings."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection
import re
from typing import TYPE_CHECKING

from .schema import DataError

if TYPE_CHECKING:
    from .apex import Unit
    from .discovery import Scan


_METADATA_SUFFIXES = (
    ".layout-meta.xml", ".flexipage-meta.xml", ".quickaction-meta.xml",
    ".permissionset-meta.xml", ".profile-meta.xml", ".reporttype-meta.xml",
    ".report-meta.xml", ".report", ".approvalprocess-meta.xml", ".email", ".email-meta.xml",
    ".listview-meta.xml", ".md-meta.xml", ".weblink-meta.xml", ".sharingrules-meta.xml",
    ".fieldset-meta.xml",
)
_AURA_SUFFIXES = (".app", ".cmp", ".design", ".evt", ".intf", ".js")
_LWC_SUFFIXES = (".html", ".js", ".js-meta.xml")
_TOKEN = re.compile(rb"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_SCAN_BYTES = 64 * 1024 * 1024
_EXAMPLE_LIMIT = 3


def _interface(path: str) -> bool:
    return (path.endswith((".page", ".component"))
            or ("/aura/" in f"/{path}" and path.endswith(_AURA_SUFFIXES))
            or ("/lwc/" in f"/{path}" and path.endswith(_LWC_SUFFIXES)))


def _count_field(counts, token, qualified_fields, field_names):
    if token in qualified_fields:
        counts["qualified", token] += 1
    else:
        leaf = token.rsplit(b".", 1)[-1]
        if leaf in field_names:
            counts["field_name", leaf] += 1


def _record_counts(summaries, counts, relative):
    for (kind, name), count in counts.items():
        summary = summaries[kind].setdefault(name.decode("utf-8"), {
            "occurrence_count": 0, "file_count": 0, "example_paths": [],
        })
        summary["occurrence_count"] += count
        summary["file_count"] += 1
        if len(summary["example_paths"]) < _EXAMPLE_LIMIT:
            summary["example_paths"].append(relative)


def _apex_string_index(scan, units, qualified_fields, field_names):
    from .apex import _walk

    summaries = {"qualified": {}, "field_name": {}}
    scanned = scanned_bytes = 0
    # Inner classes share their source file and syntax tree with the outer class.
    units_by_path = {unit.path: unit for unit in units}
    for path, unit in sorted(units_by_path.items()):
        if unit.parse_error or len(unit.data) > min(_MAX_FILE_BYTES, _MAX_SCAN_BYTES - scanned_bytes):
            continue
        scanned += 1
        scanned_bytes += len(unit.data)
        counts = Counter()
        for node in _walk(unit.tree.root_node):
            if node.type in {"string_literal", "multi_line_string_literal"}:
                for match in _TOKEN.finditer(unit.data, node.start_byte, node.end_byte):
                    _count_field(counts, match[0].lower(), qualified_fields, field_names)
        _record_counts(summaries, counts, path)
    return {
        "eligible_file_count": sum(path.casefold().endswith((".cls", ".trigger")) for path in scan.inputs),
        "scanned_file_count": scanned,
        **summaries,
    }


def build_literal_index(scan: Scan, components: list[dict], *, apex_units: list[Unit]) -> dict:
    from .discovery import _capture, _safe_path

    qualified_fields = {c["api_name"].casefold().encode("utf-8")
                        for c in components if c["kind"] == "field"}
    qualified_methods = {c["api_name"].partition("(")[0].casefold().encode("utf-8")
                         for c in components if c["kind"] == "apexmethod" and not c.get("is_constructor")}
    field_names = {name.rsplit(b".", 1)[-1] for name in qualified_fields}
    method_names = {name.rsplit(b".", 1)[-1] for name in qualified_methods}
    summaries: dict[str, dict[str, dict]] = {
        "qualified": {}, "field_name": {}, "qualified_method": {}, "method_name": {},
    }
    eligible = []
    for relative in sorted(scan.inputs):
        lower = relative.casefold()
        interface = _interface(lower)
        if interface or lower.endswith(_METADATA_SUFFIXES):
            eligible.append((relative, interface))

    scanned = 0
    scanned_interfaces = 0
    scanned_bytes = 0
    for relative, interface in eligible:
        limit = min(_MAX_FILE_BYTES, _MAX_SCAN_BYTES - scanned_bytes)
        data = scan.files.get(relative)
        if data is None:
            path = _safe_path(scan.root, relative)
            if path.stat().st_size > limit:
                continue
            data, digest, _ = _capture(path, keep_bytes=True)
            if data is None or digest != scan.inputs[relative]:
                raise DataError("Source changed while indexing literal mentions. Retry with a stable working tree.")
        if len(data) > limit:
            continue
        scanned += 1
        scanned_interfaces += int(interface)
        scanned_bytes += len(data)
        counts: Counter[tuple[str, bytes]] = Counter()
        for match in _TOKEN.finditer(data):
            token = match[0].lower()
            _count_field(counts, token, qualified_fields, field_names)
            if interface:
                leaf = token.rsplit(b".", 1)[-1]
                if token in qualified_methods:
                    counts["qualified_method", token] += 1
                elif leaf in method_names:
                    counts["method_name", leaf] += 1
        _record_counts(summaries, counts, relative)
    return {
        "eligible_file_count": len(eligible),
        "scanned_file_count": scanned,
        "qualified": summaries["qualified"],
        "field_name": summaries["field_name"],
        "methods": {
            "eligible_file_count": sum(interface for _, interface in eligible),
            "scanned_file_count": scanned_interfaces,
            "qualified": summaries["qualified_method"],
            "method_name": summaries["method_name"],
        },
        "apex_strings": _apex_string_index(scan, apex_units, qualified_fields, field_names),
    }


def _mentions(index, literal, short_key):
    scanned = index["scanned_file_count"]
    leaf = literal.rsplit(".", 1)[-1]
    empty = {"occurrence_count": 0, "file_count": 0, "example_paths": []}
    return {
        "scanned_file_count": scanned,
        "skipped_file_count": index["eligible_file_count"] - scanned,
        "qualified": {"literal": literal, **index["qualified"].get(literal.casefold(), empty)},
        short_key: {"literal": leaf, **index[short_key].get(leaf.casefold(), empty)},
    }


def literal_mentions(manifest: dict, component: dict) -> dict:
    index = manifest.get("literal_blind_spots")
    apex_strings = None
    literal = component["api_name"]
    if component["kind"] == "apexmethod":
        if component.get("is_constructor"):
            return {"state": "unavailable", "reason": "Literal method hints cover ordinary methods."}
        index = index.get("methods") if index is not None else None
        literal = literal.partition("(")[0]
        short_key = "method_name"
        interpretation = (
            "Literal matches in Aura, Visualforce and LWC are review hints. They can refer to "
            "other methods, markup or unrelated text. Overloads share literal hints."
        )
    else:
        short_key = "field_name"
        apex_strings = index.get("apex_strings") if index is not None else None
        interpretation = (
            "Literal matches are review hints. Field-name matches include standalone names and "
            "suffixes of other dotted tokens, which may refer to another object, markup, or unrelated text."
        )
    if index is None:
        return {"state": "unavailable", "reason": "This snapshot has no literal index for this component kind."}
    result = {"state": "indexed", **_mentions(index, literal, short_key),
              "interpretation": interpretation + " A zero count covers only scanned files."}
    if apex_strings is not None:
        result["apex_strings"] = _mentions(apex_strings, literal, short_key)
    return result


def _validate_group(index: dict, inputs: Collection[str], short_key: str) -> None:
    eligible = index["eligible_file_count"]
    scanned = index["scanned_file_count"]
    if type(eligible) is not int or type(scanned) is not int or not 0 <= scanned <= eligible <= len(inputs):
        raise DataError("Invalid literal metadata scan counts.")
    for kind in ("qualified", short_key):
        for summary in index[kind].values():
            count = summary["occurrence_count"]
            files = summary["file_count"]
            paths = summary["example_paths"]
            if (type(count) is not int or type(files) is not int or not 1 <= files <= min(scanned, count)
                    or not isinstance(paths, list) or not 1 <= len(paths) <= files
                    or paths != sorted(set(paths)) or any(path not in inputs for path in paths)):
                raise DataError("Invalid literal metadata match summary.")


def validate_literal_index(index: dict, inputs: dict) -> None:
    _validate_group(index, inputs, "field_name")
    if "methods" in index:
        methods = index["methods"]
        _validate_group(methods, inputs, "method_name")
        if (methods["eligible_file_count"] > index["eligible_file_count"]
                or methods["scanned_file_count"] > index["scanned_file_count"]):
            raise DataError("Invalid literal method scan counts.")
    if "apex_strings" in index:
        apex_inputs = {path for path in inputs if path.casefold().endswith((".cls", ".trigger"))}
        _validate_group(index["apex_strings"], apex_inputs, "field_name")
