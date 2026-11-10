"""Persistent HTTP queries reuse verified facts without weakening local boundaries."""
from __future__ import annotations

import copy
from http.client import HTTPConnection
import hashlib
import json
import os
import subprocess
import sys
import threading

import pytest

from graphify.salesforce import cli, query_server, session, snapshot
from graphify.salesforce.schema import DataError, FactStore, SCHEMA_VERSION, stable_id


def bundle_fixture():
    store = FactStore()
    path = "force-app/flows/Configuración.xml"
    digest = hashlib.sha256(b"synthetic HTTP evidence").hexdigest()
    owner = store.ensure_component("object", "Account")
    field = store.ensure_component("field", "Account.Name")
    setter = store.ensure_component("flow", "Set_Name")
    caller = store.ensure_component("flow", "Caller")
    store.ensure_component("flow", "Account")
    for index, (source, target, relation) in enumerate([
        (setter, field, "writes"), (setter, field, "writes"),
        (setter, field, "reads"), (caller, setter, "calls"),
        (setter, caller, "calls"), (owner, field, "contains"),
    ]):
        store.relation(source, target, relation, {
            "path": path, "locator": f"/Flow/assignments[{index}]",
            "line": index + 1, "column": 1, "content_hash": digest,
        }, "synthetic.http", resolution={"basis": "explicit_syntax"})
    for line in range(20, 60):
        store.diagnostic("SIBLING_GAP", f"Other source construct on line {line}.", {"path": path, "line": line})
    value = {
        "manifest": {"schema_version": SCHEMA_VERSION, "inputs": {path: digest},
                     "scope": {}, "coverage": {"note": "Configuración: señal útil"},
                     "activity_policy": {"mode": "include_inactive"}},
        "facts": store.finish(), "diagnostics": store.diagnostics,
    }
    value["manifest"]["snapshot_id"] = stable_id("snapshot", value)
    return value


@pytest.fixture
def published(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    directory = tmp_path / "snapshot"
    bundle = bundle_fixture()
    generation = snapshot.publish(bundle, directory, source)
    return source, directory, generation, bundle


@pytest.fixture
def service(published, monkeypatch):
    _, directory, _, bundle = published
    loads = []
    original_load = snapshot.load

    def tracked_load(path, **kwargs):
        loads.append((path, kwargs))
        return original_load(path, **kwargs)

    monkeypatch.setattr(snapshot, "load", tracked_load)
    server = query_server.create_query_server(
        directory, port=0, expect_snapshot=bundle["manifest"]["snapshot_id"])
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server, loads
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def address(server):
    return f"http://127.0.0.1:{server.server_port}"


def raw_request(server, *, method="POST", path="/query", body=b'{"command":"status"}', headers=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.putrequest(method, path, skip_host=True)
        selected = headers if headers is not None else [
            ("Host", f"127.0.0.1:{server.server_port}"),
            ("Content-Type", "application/json"), ("Content-Length", str(len(body))),
        ]
        for key, value in selected:
            connection.putheader(key, value)
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())
    finally:
        connection.close()


def test_separate_requests_load_once_match_jsonl_and_keep_all_evidence(service, published):
    server, loads = service
    _, directory, _, bundle = published
    original = copy.deepcopy(bundle)
    relationship = next(item for item in bundle["facts"]["relationships"] if item["relation"] == "writes")
    cases = [
        {"command": "status"}, {"command": "components", "kind": "field"},
        {"command": "refs", "name": "Account.Name"},
        {"command": "deps", "name": "Set_Name", "limit": 1},
        {"command": "impact", "name": "Account.Name", "depth": 10},
        {"command": "explain", "name": relationship["id"]},
        {"command": "components", "exclude_tests": True},
        {"command": "refs", "name": "Account.Name", "exclude_tests": True},
        {"command": "refs", "name": "Account.Name", "diagnostics": "full"},
        {"command": "deps", "name": "Set_Name", "diagnostics": "full"},
        {"command": "impact", "name": "Account.Name", "diagnostics": "full"},
    ]
    expected = bundle["manifest"]["snapshot_id"]
    for index, case in enumerate(cases):
        request = {**case, "id": f"petición-{index}-ñ"}
        result = query_server.request_query(address(server), request, expected_snapshot=expected)
        assert result == session.answer_request(bundle, request)
        assert result["exit_code"] == 0
        assert result["result"]["freshness"]["state"] == "unknown"
        assert str(directory) not in json.dumps(result)
        if case["command"] == "explain":
            assert len(result["result"]["results"][0]["evidence"]) == 2
        if case.get("diagnostics") == "full":
            context = result["result"]["diagnostic_context"]
            assert context["total"] == len(context["diagnostics"]) == 40
            assert context["omitted_count"] == 0 and not context["truncated"]
    assert loads == [(directory, {"expect_snapshot": expected})]
    assert bundle == original


def test_query_errors_preserve_codes_and_next_request_still_works(service):
    server, _ = service
    cases = [
        ({"command": "refs", "name": "Missing"}, 4),
        ({"command": "refs", "name": "Account"}, 3),
        ({"command": "status", "source": "private/source"}, 2),
        ({"command": "status", "snapshot": "other/snapshot"}, 2),
        ({"command": "build"}, 2),
        ({"command": "components", "exclude_tests": 1}, 2),
        ({"command": "refs", "name": "Account.Name", "diagnostics": "all"}, 2),
        ({"command": "deps", "name": "Set_Name", "diagnostics": False}, 2),
        ({"command": "status", "diagnostics": "full"}, 2),
        ({"command": "status"}, 0),
    ]
    for request, code in cases:
        assert query_server.request_query(address(server), request)["exit_code"] == code


def test_current_changes_do_not_change_pinned_server_and_expectation_is_checked_each_time(service, published):
    server, loads = service
    source, directory, _, first = published
    first_id = first["manifest"]["snapshot_id"]
    second = copy.deepcopy(first)
    second["manifest"].pop("snapshot_id")
    second["manifest"]["coverage"]["note"] = "Later complete generation"
    second["manifest"]["snapshot_id"] = stable_id("snapshot", second)
    second_id = second["manifest"]["snapshot_id"]
    assert query_server.request_query(address(server), {"command": "status"})["result"]["snapshot_id"] == first_id
    snapshot.publish(second, directory, source)
    assert json.loads((directory / "CURRENT.json").read_text("utf-8"))["snapshot_id"] == second_id
    assert query_server.request_query(address(server), {"command": "status"},
                                      expected_snapshot=first_id)["result"]["snapshot_id"] == first_id
    mismatch = query_server.request_query(address(server), {"command": "status"}, expected_snapshot=second_id)
    assert mismatch["exit_code"] == 6
    assert mismatch["result"]["snapshot_id"] == first_id
    assert len(loads) == 1


def test_status_exposes_readiness_without_source_mapping_or_cors(service, published):
    server, _ = service
    source, directory, _, bundle = published
    status, headers, result = raw_request(server, method="GET", path="/status", body=b"")
    assert status == 200 and result == session.answer_request(bundle, {"command": "status"})
    assert "Access-Control-Allow-Origin" not in headers
    assert headers["Cache-Control"] == "no-store"
    assert str(source) not in json.dumps(result) and str(directory) not in json.dumps(result)
    assert raw_request(server, method="GET", path="/CURRENT.json", body=b"")[0] == 404


@pytest.mark.parametrize("alteration,expected_status", [
    ("foreign_host", 403), ("duplicate_host", 403), ("foreign_origin", 403),
    ("null_origin", 403), ("duplicate_origin", 403), ("missing_length", 411),
    ("duplicate_length", 411), ("huge_length", 411), ("too_large", 413),
    ("chunked", 400), ("wrong_type", 415), ("duplicate_type", 415),
    ("duplicate_expected", 400),
])
def test_http_boundaries_reject_unsafe_or_ambiguous_requests(service, alteration, expected_status):
    server, _ = service
    host = f"127.0.0.1:{server.server_port}"
    headers = [("Host", host), ("Content-Type", "application/json"), ("Content-Length", "20")]
    if alteration == "foreign_host":
        headers[0] = ("Host", "attacker.example:8769")
    elif alteration == "duplicate_host":
        headers.append(("Host", host))
    elif alteration in {"foreign_origin", "null_origin", "duplicate_origin"}:
        headers.append(("Origin", "null" if alteration == "null_origin" else "https://attacker.example"))
        if alteration == "duplicate_origin":
            headers.extend([("Origin", "http://" + host), ("Origin", "http://" + host)])
    elif alteration == "missing_length":
        headers.pop()
    elif alteration == "duplicate_length":
        headers.append(headers[-1])
    elif alteration == "huge_length":
        headers[-1] = ("Content-Length", "9" * 5000)
    elif alteration == "too_large":
        headers[-1] = ("Content-Length", str(1024 * 1024 + 1))
    elif alteration == "chunked":
        headers.append(("Transfer-Encoding", "chunked"))
    elif alteration == "wrong_type":
        headers[1] = ("Content-Type", "text/plain")
    elif alteration == "duplicate_type":
        headers.append(("Content-Type", "application/json"))
    elif alteration == "duplicate_expected":
        headers.extend([("X-SFDX-Snapshot", "same"), ("X-SFDX-Snapshot", "same")])
    # Reject these headers before reading a body. Sending unread bytes can race
    # with socket closure and reset the connection before Windows delivers the response.
    status, response_headers, result = raw_request(server, body=b"", headers=headers)
    assert status == expected_status and result["exit_code"] == 2
    assert "Access-Control-Allow-Origin" not in response_headers


@pytest.mark.parametrize("body", [
    b"{",
    b"\xff",
    b'{"command":"status","command":"components"}',
    b"[" * 200 + b"0" + b"]" * 200,
    br'{"command":"status","id":"\ud800"}',
    br'{"command":"refs","name":"\udfff"}',
    br'{"command":"status","\ud800":1}',
    br'{"command":"status","\ud800":1,"\ud800":2}',
])
def test_invalid_json_is_rejected_without_desynchronizing_next_request(service, body):
    server, _ = service
    status, _, invalid = raw_request(server, body=body)
    assert status == 400 and invalid["id"] is None and invalid["exit_code"] == 2
    assert invalid["result"]["error"].startswith("Invalid JSON request:")
    request_id = "\N{ROCKET}" + r'\"' + "[{" * 200
    following = json.dumps({"command": "status", "id": request_id}).encode("utf-8")
    status, _, after = raw_request(server, body=following)
    assert status == 200 and after["id"] == request_id and after["exit_code"] == 0


def test_expected_id_guard_reads_only_current_before_failure(published, monkeypatch):
    _, directory, _, _ = published
    reads = []
    original_read = snapshot._read_limited

    def tracked_read(path, *args, **kwargs):
        reads.append(path.name)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(snapshot, "_read_limited", tracked_read)
    with pytest.raises(DataError, match="Expected snapshot"):
        query_server.create_query_server(directory, port=0, expect_snapshot="snapshot:wrong")
    assert reads == ["CURRENT.json"]


def test_matching_expected_id_does_not_skip_integrity_checks(published):
    _, directory, generation, bundle = published
    with (generation / "facts.json").open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(DataError, match="integrity check failed"):
        query_server.create_query_server(directory, port=0, expect_snapshot=bundle["manifest"]["snapshot_id"])


def test_load_uses_the_same_checked_pointer_if_current_changes(published, monkeypatch):
    source, directory, _, first = published
    second = copy.deepcopy(first)
    second["manifest"].pop("snapshot_id")
    second["manifest"]["coverage"]["note"] = "New generation published while loading"
    second["manifest"]["snapshot_id"] = stable_id("snapshot", second)
    original_read = snapshot._read_limited
    reads = []

    def switching_read(path, *args, **kwargs):
        content = original_read(path, *args, **kwargs)
        if path.name == "CURRENT.json":
            reads.append(path)
            snapshot.publish(second, directory, source)
        return content

    monkeypatch.setattr(snapshot, "_read_limited", switching_read)
    assert snapshot.load(directory, expect_snapshot=first["manifest"]["snapshot_id"]) == first
    assert reads == [directory / "CURRENT.json"]


@pytest.mark.parametrize("url", [
    "http://example.com:8769", "https://127.0.0.1:8769", "file:///snapshot",
    "http://0.0.0.0:8769", "http://192.168.1.2:8769", "http://2130706433:8769",
    "http://localhost", "http://localhost:0", "http://localhost:8769/query",
    "http://user@localhost:8769", "http://localhost:8769?x=1", "http://localhost:8769#x",
    "http://localhost:8769\n", "http://[::1%lo]:8769", "http://[::ffff:192.168.1.2]:8769",
])
def test_client_rejects_nonlocal_or_ambiguous_urls_before_connecting(monkeypatch, url):
    def must_not_connect(*args, **kwargs):
        raise AssertionError("Invalid URL reached network")

    monkeypatch.setattr(query_server, "HTTPConnection", must_not_connect)
    with pytest.raises(DataError):
        query_server.request_query(url, {"command": "status"})


def test_client_maps_localhost_without_dns_and_ignores_proxy_settings(service, monkeypatch):
    server, _ = service
    calls = []

    def tracked_connection(host, port, **kwargs):
        calls.append((host, port))
        return HTTPConnection(host, port, **kwargs)

    monkeypatch.setattr(query_server, "HTTPConnection", tracked_connection)
    monkeypatch.setenv("HTTP_PROXY", "http://invalid.example:9999")
    monkeypatch.setenv("ALL_PROXY", "http://invalid.example:9999")
    result = query_server.request_query(f"http://localhost:{server.server_port}", {"command": "status"})
    assert result["exit_code"] == 0
    assert calls == [("127.0.0.1", server.server_port)]


def test_client_does_not_follow_redirects(monkeypatch):
    calls = []

    class RedirectConnection:
        def __init__(self, *args, **kwargs):
            calls.append(args)

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return type("RedirectResponse", (), {"status": 302})()

        def close(self):
            pass

    monkeypatch.setattr(query_server, "HTTPConnection", RedirectConnection)
    with pytest.raises(DataError, match="redirects are not followed"):
        query_server.request_query("http://127.0.0.1:8769", {"command": "status"})
    assert len(calls) == 1


@pytest.mark.parametrize("location", ["snapshot", "server"])
def test_cli_queries_and_errors_are_utf8_despite_inherited_pipe_encoding(service, published, location):
    server, _ = service
    _, directory, _, bundle = published
    selected = ["--snapshot", str(directory)] if location == "snapshot" else ["--server", address(server)]
    expected_id = bundle["manifest"]["snapshot_id"]
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONDONTWRITEBYTECODE": "1"}
    for command, expected_code in [(["status"], 0), (["refs", "Inexistente_雪"], 4)]:
        process = subprocess.run(
            [sys.executable, "-m", "graphify.salesforce", *command, *selected,
             "--expect-snapshot", expected_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        result = json.loads(process.stdout.decode("utf-8"))
        error = process.stderr.decode("utf-8")
        assert process.returncode == expected_code
        assert result["snapshot_id"] == expected_id
        if expected_code:
            assert "Inexistente_雪" in result["error"] and "Inexistente_雪" in error
        else:
            assert result == session.answer_request(bundle, {"command": "status"})["result"]
            assert not error


def test_cli_empty_server_url_fails_cleanly_without_loading_snapshot(monkeypatch, capsys):
    def must_not_load(*args, **kwargs):
        raise AssertionError("A server query must not read a snapshot path")

    monkeypatch.setattr(snapshot, "load", must_not_load)
    assert cli.main(["status", "--server", ""]) == 1
    output = capsys.readouterr()
    assert "loopback server URL" in json.loads(output.out)["error"]
    assert "loopback server URL" in output.err


@pytest.mark.parametrize("command,name", [("refs", "Account.Name"), ("deps", "Set_Name"), ("impact", "Account.Name")])
@pytest.mark.parametrize("mode", ["summary", "full"])
@pytest.mark.parametrize("location", ["snapshot", "server"])
def test_cli_diagnostic_modes_match_local_jsonl_and_http(service, published, capsys, command, name, mode, location):
    server, _ = service
    _, directory, _, bundle = published
    selected = ["--snapshot", str(directory)] if location == "snapshot" else ["--server", address(server)]
    flags = [] if mode == "summary" else ["--diagnostics", "full"]
    assert cli.main([command, name, *selected, *flags]) == 0
    output = capsys.readouterr()
    actual = json.loads(output.out)
    expected = session.answer_request(bundle, {"command": command, "name": name, "diagnostics": mode})
    assert actual == expected["result"] and not output.err
    context = actual["diagnostic_context"]
    assert context["mode"] == mode and context["total"] == 40
    assert context["returned_count"] == (40 if mode == "full" else 0)
    assert context["omitted_count"] == (0 if mode == "full" else 40)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.2", "example.com", "::"])
def test_server_rejects_nonlocal_bind_before_loading(monkeypatch, tmp_path, host):
    def must_not_load(*args, **kwargs):
        raise AssertionError("Invalid bind reached snapshot load")

    monkeypatch.setattr(snapshot, "load", must_not_load)
    with pytest.raises(DataError, match="loopback"):
        query_server.create_query_server(tmp_path, host=host)
