"""Bounded, source-located outline of top-level Salesforce Flow XML elements."""
import json
import os
from pathlib import Path
import re
from xml.parsers import expat

from .discovery import EXCLUDED_DIRS, _bytes, _linked, _package_directories, _safe_path

_METADATA = "http://soap.sforce.com/2006/04/metadata"
_SUFFIX = ".flow-meta.xml"
_BASENAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.flow-meta\.xml)?", flags=re.IGNORECASE)
# Only these direct leaf values identify a named element or its execution context.
_FIELDS = {"name", "label", "object", "triggerType", "recordTriggerType", "triggerOrder",
           "actionName", "actionType", "flowName", "subflowName", "eventType"}
_START_FIELDS = {"filterFormula", "filterLogic", "doesRequireRecordChangedToMeetCriteria"}
_MAX_VALUE = 240
_MAX_FORMULA = 2048
_MAX_FILTERS = 16
_MAX_TERMS = 16
_MAX_FILE_BYTES = 32 * 1024 * 1024
_MAX_ELEMENTS = 100_000
_MAX_DEPTH = 128


def resolve_flow_path(source: str) -> Path:
    """Resolve a Flow API name or basename within the nearest Salesforce project."""
    path = Path(source)
    if not _BASENAME.fullmatch(source):
        return path
    has_suffix = source.casefold().endswith(_SUFFIX)
    if has_suffix and path.is_file():
        if _linked(path):
            raise ValueError("An explicit path is required for a linked Flow file")
        return path
    cwd = Path.cwd().resolve()
    root = next((parent for parent in (cwd, *cwd.parents) if (parent / "sfdx-project.json").is_file()), None)
    if root is None:
        raise ValueError("Run Flow name lookup inside a Salesforce DX project or pass the XML path")
    config = _safe_path(root, "sfdx-project.json")
    try:
        packages = _package_directories(root, json.loads(_bytes(config).decode("utf-8-sig")))
    except (UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"Invalid sfdx-project.json: {exc}") from exc

    filename = source if has_suffix else source + _SUFFIX
    matches = set()

    def walk_error(exc):
        raise exc

    for package in packages:
        base = _safe_path(root, package)
        if not base.is_dir():
            continue
        for current, dirs, names in os.walk(base, followlinks=False, onerror=walk_error):
            directory = Path(current)
            # os.walk does not reliably recognize Windows junctions as links.
            dirs[:] = [name for name in dirs if name.casefold() not in EXCLUDED_DIRS and not _linked(directory / name)]
            for name in names:
                if name.casefold() == filename.casefold():
                    match = directory / name
                    if not _linked(match) and match.is_file():
                        matches.add(match.relative_to(root).as_posix())
    if not matches:
        raise ValueError(f"No Flow XML named {filename} found in the configured package directories")
    if len(matches) > 1:
        paths = ", ".join(sorted(matches))
        raise ValueError(f"Ambiguous Flow name {source}; pass an explicit XML path. Matches: {paths}")
    match = _safe_path(root, matches.pop())
    return Path(os.path.relpath(match, cwd))


def flow_outline(path: Path, *, element: str | list[str] | tuple[str, ...] = "",
                 start_line: int = 1, offset: int = 0, limit: int = 32) -> dict:
    """Return rows [tag, opening line, closing line, [[field, value line, text], ...]].

    An element name or sequence selects matching direct children in document order. Scalar root children use
    '#text'. Start filters are fields of their enclosing start row. Each
    structured filter carries its closing line and source-located terms.
    These declarations describe entry criteria without evaluating them.
    Repeat the same filters with next_offset for the next page, even for
    minified XML. A final ellipsis marks a clipped field value.
    """
    if start_line < 1 or offset < 0 or not 1 <= limit <= 128:
        raise ValueError("start_line must be positive, offset non-negative, and limit between 1 and 128.")
    if Path(path).stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("Flow XML exceeds the 32 MiB outline limit. Use numbered source lines.")
    if isinstance(element, str):
        element = (element,) if element else ()
    elements = frozenset(element)
    parser = expat.ParserCreate(namespace_separator="|")
    stack = []
    rows = []
    matched = 0
    element_count = 0
    omitted_fields = 0
    current = None
    scalar = None
    scalar_line = 0
    condition = None
    condition_count = 0
    field_count = 0
    omitted_filters = 0
    clipped_formulas = []
    prefix = ""

    def open_tag(qualified, _attrs):
        nonlocal current, scalar, scalar_line, prefix, matched, element_count
        nonlocal condition, condition_count, field_count, omitted_filters
        element_count += 1
        if element_count > _MAX_ELEMENTS or len(stack) >= _MAX_DEPTH:
            raise ValueError("Flow XML exceeds the outline element or nesting limit. Use numbered source lines.")
        uri, _, name = qualified.rpartition("|")
        if not stack:
            if name != "Flow" or uri not in ("", _METADATA):
                raise ValueError("Expected Salesforce Flow metadata as the XML root.")
            prefix = uri
        depth = len(stack)
        stack.append(name if uri == prefix else None)
        line = parser.CurrentLineNumber
        if depth == 1:
            current = None
            scalar = None
            if uri != prefix or line < start_line or (elements and name not in elements):
                return
            matched += 1
            if matched <= offset or len(rows) >= limit:
                return
            current = [name, line, line, []]
            scalar = ["#text", None, ""]
            scalar_line = line
            condition_count = field_count = 0
        elif current is not None:
            # Opening a child invalidates its parent's scalar text.
            scalar = None
            scalar_line = line
            if depth == 2 and uri == prefix:
                if current[0] == "start" and name == "filters":
                    if condition_count < _MAX_FILTERS:
                        condition = {"through": line, "terms": []}
                        current[3].append(["filters", line, condition])
                    else:
                        omitted_filters += 1
                    condition_count += 1
                elif name in _FIELDS or (current[0] == "start" and name in _START_FIELDS):
                    scalar = [name, None, ""]
            elif condition is not None:
                if uri == prefix and depth == 3:
                    scalar = [name, None, ""]
                elif uri == prefix and depth == 4 and stack[-2] == "value":
                    scalar = ["value." + name, None, ""]
                else:
                    # Compound or foreign values remain available in the XML range.
                    condition["partial"] = True

    def characters(data):
        if scalar is None:
            return
        cap = _MAX_FORMULA if scalar[0] == "filterFormula" else _MAX_VALUE
        if len(scalar[2]) > cap:
            return
        if scalar[1] is None:
            leading = "" if scalar[0] == "value.stringValue" else data[:len(data) - len(data.lstrip())]
            if len(leading) == len(data):
                return
            scalar[1] = parser.CurrentLineNumber + leading.count("\n")
            data = data[len(leading):]
        scalar[2] += data[:cap + 1 - len(scalar[2])]

    def close_tag(_qualified):
        nonlocal current, scalar, condition, field_count, omitted_fields
        depth = len(stack)
        if current is not None:
            if scalar is not None:
                # An explicitly empty typed operand (e.g. stringValue) is evidence too.
                if scalar[1] is not None or condition is not None or scalar[0] in _START_FIELDS:
                    cap = _MAX_FORMULA if scalar[0] == "filterFormula" else _MAX_VALUE
                    value = scalar[2][:cap]
                    if scalar[0] != "value.stringValue":
                        value = value.strip()
                    if len(scalar[2]) > cap:
                        value += "…"
                        if condition is not None:
                            condition["partial"] = True
                        if scalar[0] == "filterFormula" and field_count < 16:
                            clipped_formulas.append([scalar_line, parser.CurrentLineNumber])
                    field = [scalar[0], scalar[1] or scalar_line, value]
                    if condition is not None:
                        if len(condition["terms"]) < _MAX_TERMS:
                            condition["terms"].append(field)
                        else:
                            condition["partial"] = True
                    elif field_count < 16:
                        current[3].append(field)
                        field_count += 1
                    else:
                        omitted_fields += 1
                scalar = None
            if depth == 3 and condition is not None:
                condition["through"] = parser.CurrentLineNumber
                condition = None
            if depth == 2:
                current[2] = parser.CurrentLineNumber
                rows.append(current)
                current = None
        stack.pop()

    def reject_doctype(*_):
        raise ValueError("Flow metadata with a DOCTYPE is unsupported.")

    parser.StartElementHandler, parser.CharacterDataHandler, parser.EndElementHandler = open_tag, characters, close_tag
    parser.StartDoctypeDeclHandler = reject_doctype
    try:
        with Path(path).open("rb") as stream:
            parser.ParseFile(stream)
    except expat.ExpatError as exc:
        raise ValueError(f"Invalid Flow XML at line {exc.lineno}: {exc}") from exc
    page = {"outline": rows}
    if matched > offset + limit:
        page["next_offset"] = offset + limit
    if clipped_formulas:
        page["clipped_formula_lines"] = clipped_formulas
    if omitted_filters:
        page["omitted_entry_filters"] = omitted_filters
    if omitted_fields:
        page["omitted_fields"] = omitted_fields
    return page


def flow_outline_evidence(path: Path, page: dict, *, elements: tuple[str, ...] | list[str], offset: int) -> str:
    """Render each returned value together with its XML citation."""
    location = path.as_posix()
    lines = []
    warnings = []
    for tag, opening, _, fields in page["outline"]:
        if not fields:
            lines.append(f"{location}:{opening} {tag}")
        for field, line, value in fields:
            key = tag if field == "#text" else f"{tag}.{field}"
            if isinstance(value, dict):
                lines.append(f"{location}:{line} {key} (through line {value['through']})")
                if value.get("partial"):
                    warnings.append(f"Partial entry filter at {location}:{line}-{value['through']}.")
                for term, term_line, operand in value["terms"]:
                    lines.append(f"{location}:{term_line} {term} = {json.dumps(operand, ensure_ascii=False)}")
            else:
                lines.append(f"{location}:{line} {key} = {json.dumps(value, ensure_ascii=False)}")
                if value.endswith("…") and field != "filterFormula":
                    warnings.append(f"Possible clipped value at {location}:{line}.")

    for start, end in page.get("clipped_formula_lines", []):
        warnings.append(f"Clipped formula at {location}:{start}-{end}.")
    for field, label in (("omitted_entry_filters", "entry filters"), ("omitted_fields", "fields")):
        if amount := page.get(field):
            warnings.append(f"{amount} {label} omitted from this outline.")
    if "next_offset" in page:
        warnings.append(f"More selected elements. Repeat with --offset {page['next_offset']}.")
    elif not offset:
        tags = {row[0] for row in page["outline"]}
        if missing := sorted(set(elements) - tags):
            lines.append(f"{location}: no top-level element(s): {', '.join(missing)}.")
        if not warnings:
            lines.append("End of selected evidence.")
    return "\n".join([*lines, *warnings])
