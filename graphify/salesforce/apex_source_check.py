"""Classify Apex source citations using the pinned local grammar."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
import re
import stat

from .apex import _COMMENTS, _MAX_NODES, _MAX_SOURCE_BYTES, _parse_source
from .discovery import _capture, _linked, _safe_path
from .schema import DataError


_CITATION = re.compile(r"(.+):([1-9][0-9]{0,9})\Z")
_STRINGS = {"string_literal", "multi_line_string_literal"}
_CODE = 1
_COMMENT = 2
_LINE_STATES = {0: "blank", _CODE: "code", _COMMENT: "comment", _CODE | _COMMENT: "mixed"}


def _token_states(tree, lines: list[int]) -> dict[int, int] | None:
    """Mark physical lines touched by lexical tokens, including comment tokens."""
    states = dict.fromkeys(lines, 0)
    pending = [tree.root_node]
    seen = 0
    while pending:
        node = pending.pop()
        seen += 1
        if seen > _MAX_NODES:
            return None
        if node.type in _COMMENTS:
            bit = _COMMENT
        elif node.type in _STRINGS or not node.child_count:
            bit = _CODE
        else:
            pending.extend(node.children)
            continue
        if node.start_byte == node.end_byte:
            continue
        start = node.start_point.row + 1
        # A token ending at column zero does not touch that last line.
        end = node.end_point.row + bool(node.end_point.column)
        for index in range(bisect_left(lines, start), bisect_right(lines, end)):
            states[lines[index]] |= bit
    return states


def _check_file(parser, path: Path, requested: set[int]) -> dict[int, tuple[str, str | None]]:
    def unknown(reason: str) -> dict[int, tuple[str, str | None]]:
        return {line: ("unknown", reason) for line in requested}

    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return unknown("Not a regular source file.")
        if info.st_size > _MAX_SOURCE_BYTES:
            return unknown("Apex source exceeds the parser size limit.")
        data, _, _ = _capture(path, keep_bytes=True)
    except (OSError, DataError):
        return unknown("Apex source is unavailable or changed while being read.")
    if data is None or len(data) > _MAX_SOURCE_BYTES:
        return unknown("Apex source exceeds the parser size limit.")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return unknown("Apex source must be valid UTF-8.")
    tree, _ = _parse_source(parser, data)
    if tree.root_node.has_error:
        return unknown("The pinned Apex grammar could not parse this file.")
    line_count = data.count(b"\n") + int(bool(data) and not data.endswith(b"\n"))
    lines = sorted(line for line in requested if line <= line_count)
    states = _token_states(tree, lines)
    if states is None:
        return unknown("Apex source exceeds the parser node limit.")
    return {line: (_LINE_STATES[states[line]], None) if line in states
            else ("unknown", "Line is outside the source file.") for line in requested}


def check_apex_lines(source: Path, citations: Sequence[str]) -> list[dict]:
    """Classify relative Apex path:line citations, preserving input order.

    A code line contains a syntax token. That alone does not establish an
    assignment, a call path, or execution in Salesforce.
    """
    from tree_sitter_language_pack import get_parser

    root = Path(source).expanduser().absolute()
    if not root.is_dir():
        raise DataError("Source root must be an existing directory.")
    for path in (root, *root.parents):
        if _linked(path):
            raise DataError("Source root cannot contain symbolic links or junctions.")
    root = root.resolve()
    files: dict[str, set[int]] = defaultdict(set)
    ordered = []
    for citation in citations:
        match = _CITATION.fullmatch(citation)
        if not match or PurePosixPath(match[1]).suffix.casefold() not in {".cls", ".trigger"}:
            raise DataError("Expected a relative Apex .cls or .trigger path and a positive line number.")
        relative, line = match[1].replace("\\", "/"), int(match[2])
        files[relative].add(line)
        ordered.append((relative, line))

    parser = get_parser("apex")
    checked = {relative: _check_file(parser, _safe_path(root, relative), lines)
               for relative, lines in files.items()}
    rows = []
    for relative, line in ordered:
        state, reason = checked[relative][line]
        row = {"path": relative, "line": line, "state": state}
        if reason:
            row["reason"] = reason
        rows.append(row)
    return rows
