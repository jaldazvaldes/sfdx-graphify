"""Persistent, read-only loopback queries over one fully verified snapshot."""
from __future__ import annotations

from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import socket
from typing import cast
from urllib.parse import urlsplit

from . import query, snapshot
from .listener import LoopbackHTTPServer
from .schema import DataError, canonical_json
from .session import answer_request, parse_request


_REQUEST_LIMIT = 1024 * 1024
_EXPECTED_HEADER = "X-SFDX-Snapshot"


def _local_address(host: str) -> tuple[str, int]:
    address = "127.0.0.1" if host.casefold() == "localhost" else host
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise DataError("Query service requires localhost or a literal loopback IP address.") from exc
    if not parsed.is_loopback or "%" in address:
        raise DataError("Query service requires a loopback address.")
    return str(parsed), parsed.version


def _error(bundle: dict, message: str, *, code: int = 2) -> dict:
    result = query.envelope(bundle, [])
    result["error"] = message
    return {"id": None, "exit_code": code, "result": result}


def create_query_server(directory: Path, host: str = "127.0.0.1", port: int = 8769,
                        *, expect_snapshot: str | None = None) -> ThreadingHTTPServer:
    """Validate once before binding. Each request uses the same in-memory bundle."""
    address, version = _local_address(host)
    if type(port) is not int or not 0 <= port <= 65535:
        raise DataError("Query service requires a valid local port.")
    bundle = (snapshot.load(directory, expect_snapshot=expect_snapshot) if expect_snapshot is not None
              else snapshot.load(directory))
    snapshot_id = bundle["manifest"]["snapshot_id"]
    index = query.QueryIndex.build(bundle)

    class QueryServer(LoopbackHTTPServer):
        address_family = socket.AF_INET6 if version == 6 else socket.AF_INET

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            pass

        def respond(self, status: int, response: dict):
            payload = canonical_json(response).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

        def authorize(self) -> bool:
            actual_port = cast(tuple[str, int], self.server.server_address)[1]
            literal = f"[{address}]" if version == 6 else address
            hosts = {f"{literal}:{actual_port}", f"localhost:{actual_port}"}
            host_headers = self.headers.get_all("Host", [])
            origin_headers = self.headers.get_all("Origin", [])
            if len(host_headers) != 1 or host_headers[0].lower() not in hosts:
                self.respond(403, {"id": None, "exit_code": 2, "result": {"error": "Local host required."}})
                return False
            if len(origin_headers) > 1 or (origin_headers and origin_headers[0] not in {"http://" + h for h in hosts}):
                self.respond(403, {"id": None, "exit_code": 2, "result": {"error": "Same-origin requests only."}})
                return False
            expected = self.headers.get_all(_EXPECTED_HEADER, [])
            if len(expected) > 1:
                self.respond(400, _error(bundle, "Duplicate expected-snapshot headers are not supported."))
                return False
            if expected and expected[0] != snapshot_id:
                self.respond(409, _error(bundle, f"Expected snapshot {expected[0]}; service is pinned to {snapshot_id}.", code=6))
                return False
            return True

        def do_GET(self):
            if not self.authorize():
                return
            if self.path != "/status":
                return self.respond(404, _error(bundle, "Route not found."))
            return self.respond(200, answer_request(bundle, {"command": "status"}, index=index))

        def do_POST(self):
            if not self.authorize():
                return
            if self.path != "/query":
                return self.respond(404, _error(bundle, "Route not found."))
            if self.headers.get_all("Transfer-Encoding"):
                return self.respond(400, _error(bundle, "Transfer encoding is not supported."))
            content_types = self.headers.get_all("Content-Type", [])
            if (len(content_types) != 1
                    or content_types[0].split(";", 1)[0].strip().lower() != "application/json"):
                return self.respond(415, _error(bundle, "Content-Type must be application/json with UTF-8 data."))
            lengths = self.headers.get_all("Content-Length", [])
            if (len(lengths) != 1 or not lengths[0].isascii()
                    or not lengths[0].isdigit() or len(lengths[0]) > 20):
                return self.respond(411, _error(bundle, "One valid Content-Length is required."))
            size = int(lengths[0])
            if not 0 < size <= _REQUEST_LIMIT:
                return self.respond(413, _error(bundle, "Query request must be between 1 byte and 1 MiB; no response data is truncated."))
            try:
                body = self.rfile.read(size)
                if len(body) != size:
                    return self.respond(400, _error(bundle, "Incomplete query request."))
                request = parse_request(body.decode("utf-8"))
            except (ValueError, TimeoutError) as exc:
                return self.respond(400, _error(bundle, f"Invalid JSON request: {exc}"))
            return self.respond(200, answer_request(bundle, request, index=index))

    return QueryServer((address, port), Handler)


def serve_queries(directory: Path, host: str = "127.0.0.1", port: int = 8769,
                  *, expect_snapshot: str | None = None) -> None:
    server = create_query_server(directory, host, port, expect_snapshot=expect_snapshot)
    bound_host, bound_port = cast(tuple[str, int], server.server_address)[:2]
    literal = f"[{bound_host}]" if ":" in bound_host else bound_host
    print(f"Local Salesforce query service: http://{literal}:{bound_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def request_query(server_url: str, request: dict, *, expected_snapshot: str | None = None) -> dict:
    """Issue one fast local query without proxies, redirects, or snapshot disk I/O."""
    if server_url.strip() != server_url or any(char.isspace() for char in server_url):
        raise DataError("Query server URL must not contain whitespace.")
    try:
        parsed = urlsplit(server_url)
        if (parsed.scheme != "http" or parsed.username is not None or parsed.password is not None
                or not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or parsed.port is None):
            raise DataError("Use an HTTP loopback server URL with an explicit port and no extra path or credentials.")
        address, _ = _local_address(parsed.hostname)
        port = parsed.port
        if not 1 <= port <= 65535:
            raise DataError("Query client requires a valid local server port.")
    except ValueError as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError(f"Invalid query server URL: {exc}") from exc
    if expected_snapshot is not None and (
            not isinstance(expected_snapshot, str) or not expected_snapshot.isascii()
            or any(char.isspace() for char in expected_snapshot) or not expected_snapshot):
        raise DataError("Expected snapshot must be a nonempty ASCII identifier without whitespace.")
    try:
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise DataError(f"Invalid query request: {exc}") from exc
    if len(payload) > _REQUEST_LIMIT:
        raise DataError("Query request exceeds 1 MiB; no request or response data was truncated.")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if expected_snapshot is not None:
        headers[_EXPECTED_HEADER] = expected_snapshot
    connection = HTTPConnection(address, port, timeout=30)
    try:
        connection.request("POST", "/query", body=payload, headers=headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise DataError("Query service redirects are not followed.")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise DataError("Query service returned a non-JSON response.")
        result = parse_request(response.read().decode("utf-8"))
        if (not isinstance(result, dict) or set(result) != {"id", "exit_code", "result"}
                or type(result["exit_code"]) is not int or not isinstance(result["result"], dict)):
            raise DataError("Query service returned an invalid response envelope.")
        if response.status != 200 and result["exit_code"] == 0:
            raise DataError("Query service returned inconsistent success status.")
        if expected_snapshot is not None and result["exit_code"] == 0:
            if result["result"].get("snapshot_id") != expected_snapshot:
                raise DataError("Query response does not match the expected snapshot.")
        return result
    except (OSError, UnicodeError, ValueError, RecursionError, HTTPException) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError(f"Cannot query the local service: {exc}") from exc
    finally:
        connection.close()
