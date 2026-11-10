"""Persistent-query parity, immutable generation and JSONL boundary contracts."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys

import pytest

from graphify.salesforce import cli, query, session, snapshot
from graphify.salesforce.schema import DataError, FactStore, SCHEMA_VERSION, stable_id


def synthetic_bundle():
    path = "force-app/flows/Configuración.xml"
    digest = hashlib.sha256(b"synthetic evidence").hexdigest()
    store = FactStore()
    owner = store.ensure_component("object", "Account")
    field = store.ensure_component("field", "Account.Name")
    setter = store.ensure_component("flow", "Set_Name")
    caller = store.ensure_component("flow", "Caller")
    store.ensure_component("flow", "Account")  # Unqualified Account is ambiguous.
    for index, (source, target, relation) in enumerate([
        (setter, field, "writes"), (setter, field, "writes"),
        (setter, field, "reads"), (caller, setter, "calls"),
        (setter, caller, "calls"), (owner, field, "contains"),
    ]):
        store.relation(source, target, relation, {
            "path": path, "locator": f"/Flow/assignments[{index}]",
            "line": index + 1, "column": 1, "content_hash": digest,
        }, "synthetic.session", resolution={"basis": "explicit_syntax"})
    store.diagnostic("synthetic_note", "Configuración de prueba: señal útil", {"path": path})
    value = {
        "manifest": {"schema_version": SCHEMA_VERSION, "inputs": {path: digest},
                     "scope": {}, "coverage": {"note": "Configuración: señal útil"},
                     "activity_policy": {"mode": "include_inactive"}},
        "facts": store.finish(), "diagnostics": store.diagnostics,
    }
    value["manifest"]["snapshot_id"] = stable_id("snapshot", value)
    return value


def run_requests(directory, requests, **kwargs):
    incoming = io.StringIO("\n".join(json.dumps(item, ensure_ascii=False) for item in requests) + "\n")
    output, errors = io.StringIO(), io.StringIO()
    code = session.run_session(directory, input_stream=incoming,
                               output_stream=output, error_stream=errors, **kwargs)
    return code, [json.loads(line) for line in output.getvalue().splitlines()], output.getvalue(), errors.getvalue()


def test_session_matches_all_queries_and_preserves_every_occurrence(monkeypatch, tmp_path):
    bundle = synthetic_bundle()
    original = copy.deepcopy(bundle)
    loads = []

    def load_once(path, **kwargs):
        assert kwargs == {"expect_snapshot": bundle["manifest"]["snapshot_id"]}
        loads.append(path)
        return bundle

    monkeypatch.setattr(snapshot, "load", load_once)
    write = next(item for item in bundle["facts"]["relationships"] if item["relation"] == "writes")
    cases = [
        ({"command": "status"}, query.status(bundle)),
        ({"command": "components", "name": "Name", "kind": "field"},
         query.components(bundle, "Name", kind="field")),
        ({"command": "refs", "name": "Account.Name"}, query.refs(bundle, "Account.Name")),
        ({"command": "deps", "name": "Set_Name", "limit": 1}, query.deps(bundle, "Set_Name", limit=1)),
        ({"command": "impact", "name": "Account.Name", "depth": 10},
         query.impact(bundle, "Account.Name", depth=10)),
        ({"command": "refs", "name": "Account.Name", "diagnostics": "full"},
         query.refs(bundle, "Account.Name", diagnostics="full")),
        ({"command": "deps", "name": "Set_Name", "diagnostics": "full"},
         query.deps(bundle, "Set_Name", diagnostics="full")),
        ({"command": "impact", "name": "Account.Name", "diagnostics": "full"},
         query.impact(bundle, "Account.Name", diagnostics="full")),
        ({"command": "explain", "name": write["id"]}, query.explain(bundle, write["id"])),
    ]
    requests = [{**request, "id": f"consulta-{index}-ñ"} for index, (request, _) in enumerate(cases)]
    code, replies, raw, errors = run_requests(tmp_path, requests,
                                             expect_snapshot=bundle["manifest"]["snapshot_id"])
    assert code == 0 and loads == [tmp_path]
    assert len(replies) == len(cases)
    for index, (reply, (_, expected)) in enumerate(zip(replies, cases)):
        assert reply == {"id": f"consulta-{index}-ñ", "exit_code": 0, "result": expected}
        assert reply["result"]["freshness"]["state"] == "unknown"
    assert len(replies[-1]["result"]["results"][0]["evidence"]) == 2
    assert "Configuración" in raw and "consulta-0-ñ" in raw
    assert "ready" not in raw and "ready" in errors
    assert bundle == original


def test_query_failures_keep_regular_error_codes_and_allow_following_requests(monkeypatch, tmp_path):
    bundle = synthetic_bundle()
    monkeypatch.setattr(snapshot, "load", lambda _: bundle)
    _, replies, _, _ = run_requests(tmp_path, [
        {"command": "refs", "name": "Missing", "id": "missing"},
        {"command": "refs", "name": "Account", "id": "ambiguous"},
        {"command": "impact", "name": "Account.Name", "depth": 0},
        {"command": "refs", "name": "Account.Name", "limit": 0},
        {"command": "status", "id": "after"},
    ])
    assert [reply["exit_code"] for reply in replies] == [4, 3, 2, 2, 0]
    assert replies[0]["result"]["error"] == "No matching component: Missing"
    with pytest.raises(query.QueryError) as ambiguous:
        query.refs(bundle, "Account")
    assert replies[1]["result"]["candidates"] == ambiguous.value.candidates
    assert replies[1]["result"]["error"] == str(ambiguous.value)
    assert replies[-1]["result"] == query.status(bundle)


@pytest.mark.parametrize("line", [
    "{", "[]", '{"command":"delete"}',
    '{"command":"status","unexpected":true}',
    '{"command":"status","source":"private/source"}',
    '{"command":"status","snapshot":"another/snapshot"}',
    '{"command":"components","limit":true}',
    '{"command":"components","limit":1.5}',
    '{"command":"components","name":42}',
    '{"command":"impact","name":"Account.Name","depth":false}',
    '{"command":"refs","name":"Account.Name","relation":"invented"}',
    '{"command":"refs","name":"Account.Name","diagnostics":"all"}',
    '{"command":"deps","name":"Set_Name","diagnostics":true}',
    '{"command":"impact","name":"Account.Name","diagnostics":[]}',
    '{"command":"status","diagnostics":"full"}',
    '{"command":"refs","name":""}',
    '{"command":"status","id":true}',
    '{"command":"status","command":"components"}',
    '[' * (sys.getrecursionlimit() + 100) + '0' + ']' * (sys.getrecursionlimit() + 100),
    r'{"command":"status","id":"\ud800"}',
    r'{"command":"refs","name":"\udfff"}',
    r'{"command":"status","\ud800":1}',
    r'{"command":"status","\ud800":1,"\ud800":2}',
])
def test_invalid_jsonl_request_does_not_desynchronize_the_session(monkeypatch, tmp_path, line):
    bundle = synthetic_bundle()
    monkeypatch.setattr(snapshot, "load", lambda _: bundle)
    output = io.StringIO()
    code = session.run_session(tmp_path, input_stream=io.StringIO(
        line + '\n{"command":"status","id":"following"}\n'),
        output_stream=output, error_stream=io.StringIO())
    replies = [json.loads(item) for item in output.getvalue().encode("utf-8").splitlines()]
    assert code == 0 and len(replies) == 2
    assert replies[0]["exit_code"] == 2 and replies[0]["result"]["error"]
    assert replies[1] == {"id": "following", "exit_code": 0, "result": query.status(bundle)}


def test_republishing_current_does_not_change_the_running_session(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    directory = tmp_path / "snapshot"
    first = synthetic_bundle()
    snapshot.publish(first, directory, source)
    second = copy.deepcopy(first)
    second["manifest"].pop("snapshot_id")
    second["manifest"]["coverage"]["note"] = "A later verified snapshot"
    second["manifest"]["snapshot_id"] = stable_id("snapshot", second)
    output = io.StringIO()
    original_load = snapshot.load
    loads = []

    def tracked_load(path):
        loads.append(path)
        return original_load(path)

    monkeypatch.setattr(snapshot, "load", tracked_load)

    class SwitchingRequests(io.StringIO):
        def __iter__(self):
            yield '{"command":"status","id":1}\n'
            assert len(output.getvalue().splitlines()) == 1
            snapshot.publish(second, directory, source)
            yield '{"command":"status","id":2}\n'

    assert session.run_session(directory, input_stream=SwitchingRequests(),
                               output_stream=output, error_stream=io.StringIO()) == 0
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert loads == [directory]
    assert [reply["result"]["snapshot_id"] for reply in replies] == [first["manifest"]["snapshot_id"]] * 2
    assert original_load(directory)["manifest"]["snapshot_id"] == second["manifest"]["snapshot_id"]


@pytest.mark.parametrize("failure", ["expected_id", "corruption"])
def test_startup_failure_rejects_before_consuming_requests(tmp_path, failure):
    source = tmp_path / "source"
    source.mkdir()
    directory = tmp_path / "snapshot"
    bundle = synthetic_bundle()
    generation = snapshot.publish(bundle, directory, source)
    if failure == "corruption":
        with (generation / "diagnostics.json").open("ab") as output:
            output.write(b" ")

    class MustNotRead(io.StringIO):
        def __iter__(self):
            raise AssertionError("Requests were consumed before startup validation")

    output, errors = io.StringIO(), io.StringIO()
    with pytest.raises(DataError, match="Expected snapshot|integrity check failed"):
        session.run_session(directory, expect_snapshot="snapshot:wrong" if failure == "expected_id" else None,
                            input_stream=MustNotRead(), output_stream=output, error_stream=errors)
    assert output.getvalue() == errors.getvalue() == ""


def test_cli_session_has_only_response_frames_on_stdout(monkeypatch, tmp_path, capsys):
    bundle = synthetic_bundle()
    monkeypatch.setattr(snapshot, "load", lambda _, **kwargs: bundle)
    monkeypatch.setattr("sys.stdin", io.StringIO('\n{"command":"status","id":"sesión"}\n'))
    assert cli.main(["session", "--snapshot", str(tmp_path),
                     "--expect-snapshot", bundle["manifest"]["snapshot_id"]]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"id": "sesión", "exit_code": 0, "result": query.status(bundle)}
    assert "Query session ready" in captured.err


def test_cli_jsonl_uses_utf8_even_when_inherited_pipe_encoding_differs(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    directory = tmp_path / "snapshot"
    bundle = synthetic_bundle()
    snapshot.publish(bundle, directory, source)
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.run(
        [sys.executable, "-m", "graphify.salesforce", "session", "--snapshot", str(directory)],
        input=(
            '{"command":"status","id":"sesión-ñ"}\n'.encode("utf-8")
            + b'{"command":"status","id":"\xff"}\n'
            + br'{"command":"status","id":"\ud83d\ude80"}' + b"\n"
        ),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30, shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.returncode == 0, process.stderr.decode("utf-8")
    before, invalid, after = map(json.loads, process.stdout.splitlines())
    assert before == {
        "id": "sesión-ñ", "exit_code": 0, "result": query.status(bundle),
    }
    assert invalid["id"] is None and invalid["exit_code"] == 2
    assert invalid["result"]["error"].startswith("Invalid JSON request:")
    assert after == {"id": "\N{ROCKET}", "exit_code": 0, "result": query.status(bundle)}
