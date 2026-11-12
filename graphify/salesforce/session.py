"""Local UTF-8 JSONL query session over one fully verified, pinned snapshot."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import TextIO

from . import query, snapshot
from .schema import DataError, RELATIONS


_OPTIONS = {
    "status": frozenset(),
    "components": frozenset({"name", "kind", "limit", "exclude_tests"}),
    "refs": frozenset({"name", "relation", "limit", "exclude_tests", "diagnostics", "value"}),
    "deps": frozenset({"name", "relation", "limit", "exclude_tests", "diagnostics"}),
    "impact": frozenset({"name", "relation", "limit", "depth", "exclude_tests", "diagnostics"}),
    "explain": frozenset({"name"}),
}
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_JSON_NESTING_LIMIT = 128
_JSON_TOKENS = re.compile(r'["{}\[\]]|\\.', re.DOTALL)


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate request key: {key!r}")
        result[key] = value
    return result


def _validate(request: object) -> tuple[str, dict]:
    if not isinstance(request, dict):
        raise query.QueryError("A session request must be a JSON object.")
    command = request.get("command")
    if not isinstance(command, str) or command not in _OPTIONS:
        raise query.QueryError("Command must be status, components, refs, deps, impact or explain.")
    unknown = request.keys() - (_OPTIONS[command] | {"command", "id"})
    if unknown:
        raise query.QueryError("Unsupported request options: " + ", ".join(sorted(unknown)))
    if "id" in request and request["id"] is not None and type(request["id"]) not in (str, int):
        raise query.QueryError("Request id must be a string, integer or null.")
    for key in ("name", "kind", "relation", "diagnostics", "value"):
        if key in request and not isinstance(request[key], str):
            raise query.QueryError(f"{key} must be a string.")
    for key in ("limit", "depth"):
        if key in request and type(request[key]) is not int:
            raise query.QueryError(f"{key} must be an integer, not a boolean or decimal.")
    if "exclude_tests" in request and type(request["exclude_tests"]) is not bool:
        raise query.QueryError("exclude_tests must be a boolean.")
    if command not in {"status", "components"} and not request.get("name"):
        raise query.QueryError(f"{command} requires a nonempty name.")
    if "relation" in request and request["relation"] not in RELATIONS:
        raise query.QueryError("Unknown relationship type.")
    if "diagnostics" in request and request["diagnostics"] not in {"summary", "full"}:
        raise query.QueryError("diagnostics must be summary or full.")
    return command, {key: value for key, value in request.items() if key in _OPTIONS[command]}


def parse_request(text: str) -> object:
    """Decode JSON with bounded nesting, duplicate-key and Unicode checks."""
    depth = 0
    in_string = False
    # Match escaped pairs together so quoted brackets and escaped quotes never
    # affect the depth. json.loads still validates the complete JSON grammar.
    for match in _JSON_TOKENS.finditer(text):
        token = match[0]
        if token == '"':
            in_string = not in_string
        elif not in_string:
            if token in ("[", "{"):
                depth += 1
                if depth > _JSON_NESTING_LIMIT:
                    raise ValueError("JSON nesting is too deep.")
            elif token in ("]", "}"):
                depth -= 1
    try:
        request = json.loads(text, object_pairs_hook=_unique_keys)
    except RecursionError as exc:
        raise ValueError("JSON nesting is too deep.") from exc
    # Requests are flat. Nested option values fail type validation without being
    # echoed, while keys and string values can appear in response IDs and errors.
    if isinstance(request, dict) and any(
        _SURROGATE.search(key) or (isinstance(value, str) and _SURROGATE.search(value))
        for key, value in request.items()
    ):
        raise ValueError("JSON request strings contain an unpaired Unicode surrogate.")
    return request


def answer_request(bundle: dict, request: object, *, index: query.QueryIndex | None = None) -> dict:
    """Shared HTTP/JSONL response contract; retain every returned occurrence."""
    request_id = (request.get("id") if isinstance(request, dict)
                  and type(request.get("id")) in (str, int) else None)
    try:
        command, options = _validate(request)
        result = query.dispatch(bundle, command, index=index, **options)
        code = 0
    except query.QueryError as exc:
        result = query.envelope(bundle, [])
        result.update({"error": str(exc), "candidates": exc.candidates})
        code = exc.code
    return {"id": request_id, "exit_code": code, "result": result}


def run_session(directory: Path, *, expect_snapshot: str | None = None,
                input_stream: TextIO | None = None, output_stream: TextIO | None = None,
                error_stream: TextIO | None = None) -> int:
    """Load once; emit one response per nonblank request until EOF.

    Per-request exit_code follows the regular CLI. Query failures do not close
    the session. Startup integrity or expected-ID failures do stop it. Source
    verification is deliberately separate; cached data never implies freshness.
    """
    # Decode each frame independently so invalid UTF-8 cannot interrupt iteration
    # over stdin or discard adjacent requests buffered by a text stream.
    incoming = input_stream if input_stream is not None else getattr(sys.stdin, "buffer", sys.stdin)
    outgoing = output_stream if output_stream is not None else sys.stdout
    errors = error_stream if error_stream is not None else sys.stderr
    bundle = (snapshot.load(directory, expect_snapshot=expect_snapshot) if expect_snapshot is not None
              else snapshot.load(directory))
    snapshot_id = bundle["manifest"]["snapshot_id"]
    if expect_snapshot is not None and expect_snapshot != snapshot_id:
        raise DataError(f"Expected snapshot {expect_snapshot}; loaded {snapshot_id}.")
    index = query.QueryIndex.build(bundle)
    print(f"Query session ready: {snapshot_id}. Snapshot pinned until EOF; source freshness is unknown.",
          file=errors, flush=True)
    for line in incoming:
        try:
            text = line.decode("utf-8") if isinstance(line, bytes) else line
            if not text.strip():
                continue
            request = parse_request(text)
        except ValueError as exc:
            result = query.envelope(bundle, [])
            result["error"] = f"Invalid JSON request: {exc}"
            response = {"id": None, "exit_code": 2, "result": result}
        else:
            response = answer_request(bundle, request, index=index)
        print(json.dumps(response, ensure_ascii=False, sort_keys=True, allow_nan=False),
              file=outgoing, flush=True)
    return 0
