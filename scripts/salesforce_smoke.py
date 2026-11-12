"""Exercise an installed sfdx-graphify entry point on the fictional project."""

from __future__ import annotations

import argparse
from html import unescape
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import shutil
from socket import create_server
import subprocess
import tempfile
import time


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "salesforce"
SAMPLE_FLOW = "sf:flow:sampleaccount"
SAMPLE_ACTION = "sf:apexmethod:sampleaction.run"
SAMPLE_READ = "sf:field:contact.lastname"
SAMPLE_WRITE = "sf:field:account.description"


def prepare_example(directory: Path) -> tuple[Path, Path]:
    example = directory / "example"
    shutil.copytree(EXAMPLE, example)
    git = ["git", "-C", str(example), "-c", "core.autocrlf=false"]
    subprocess.run([*git, "init", "--quiet", "--template="], check=True, timeout=30)
    subprocess.run([*git, "add", "--force", "--", "."], check=True, timeout=30)
    hooks = directory / "empty-hooks"
    hooks.mkdir()
    subprocess.run([
        *git, "-c", f"core.hooksPath={hooks}", "-c", "commit.gpgSign=false", "-c",
        "user.name=Salesforce wheel smoke", "-c", "user.email=smoke@example.invalid",
        "commit", "--quiet", "-m", "Add synthetic Salesforce fixture",
    ], check=True, timeout=30)
    revision = subprocess.check_output([*git, "rev-parse", "HEAD"], encoding="utf-8", timeout=30).strip()
    flow_path = "force-app/main/default/flows/SampleAccount.flow-meta.xml"
    lines = (example / flow_path).read_text(encoding="utf-8").splitlines()
    citations = [
        {"path": flow_path, "line": next(i for i, line in enumerate(lines, 1) if marker in line), "label": label}
        for marker, label in (("GetContact.LastName", "Read the contact name"),
                              ("<field>Description</field>", "Write the account description"))
    ]
    step = {
        "id": "copy-name", "label": "Copy the contact name", "kind": "action",
        "description": "Copy the contact name to the account description.",
        "components": [SAMPLE_FLOW, SAMPLE_READ, SAMPLE_WRITE], "effects": [], "evidence": citations,
    }
    process = {"id": "sample-account", "title": "Sample account update", "summary": step["description"],
               "steps": [step], "edges": []}
    feature = {"id": "sample", "name": "Sample feature", "processes": [process]}
    country = {"id": "XX", "name": "Sample country", "features": [feature]}
    catalog = directory / "processes.json"
    catalog.write_text(json.dumps({"source_revision": revision, "countries": [country]}), encoding="utf-8")
    return example, catalog


def query(cli: Path, directory: Path, *args: str, env: dict[str, str] | None = None) -> dict:
    result = subprocess.run(
        [str(cli), *args], cwd=directory, env=env, capture_output=True, encoding="utf-8", timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr or result.stdout}")
    return json.loads(result.stdout)


def viewer_port(log_path: Path, process: subprocess.Popen) -> int:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        log = log_path.read_text(encoding="utf-8", errors="replace")
        ready = re.search(r"^Local Salesforce viewer: http://127\.0\.0\.1:(\d+)$", log, re.M)
        if ready:
            return int(ready[1])
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise RuntimeError(f"Viewer did not start:\n{log_path.read_text(encoding='utf-8', errors='replace')}")


def viewer_get(connection: HTTPConnection, path: str) -> bytes:
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read()
    assert response.status == 200, (path, response.status, body[:300])
    return body


def check_process_graph(connection: HTTPConnection, path: str, *, step: bool) -> None:
    page = viewer_get(connection, path).decode("utf-8")
    assert "Process dependencies · Sample account update" in page, path
    if step:
        assert "Step · Copy the contact name · cited source" in page, path
    assets = [
        unescape(url) for url in re.findall(r'(?:href|src)="([^"]+)"', page)
        if url.startswith("/process-graph/standard")
    ]
    assert assets, path
    scripts = [viewer_get(connection, asset).decode("utf-8") for asset in assets]
    graph = next(script for script in scripts if "const RAW_EDGES = " in script)
    edges = json.loads(re.search(r"^const RAW_EDGES = (.*);$", graph, re.M)[1])
    relations = {(edge["from"], edge["to"], detail["relation"])
                 for edge in edges for detail in edge["relationship_details"]}
    assert (SAMPLE_FLOW, SAMPLE_READ, "reads") in relations, path
    assert (SAMPLE_FLOW, SAMPLE_WRITE, "writes") in relations, path
    assert ((SAMPLE_FLOW, SAMPLE_ACTION, "calls") in relations) is (not step), path


def check_viewer(cli: Path, directory: Path, example: Path, snapshot_args: tuple[str, str],
                 snapshot_id: str, catalog: Path) -> None:
    cache_key = "LOCALAPPDATA" if os.name == "nt" else "XDG_CACHE_HOME"
    env = {**os.environ, cache_key: str(directory / "cache")}
    with create_server(("127.0.0.1", 0)) as listener:
        port = str(listener.getsockname()[1])
        registration = query(
            cli, example, "processes", "register", *snapshot_args, "--catalog", str(catalog), "--port", port, env=env,
        )
        assert registration["snapshot_id"] == snapshot_id, registration
    log_path = directory / "viewer.log"
    with log_path.open("w", encoding="utf-8") as log, subprocess.Popen(
        [str(cli), "serve", "--registered-process-maps"], cwd=example, env=env, stdout=log, stderr=subprocess.STDOUT,
    ) as process:
        try:
            connection = HTTPConnection("127.0.0.1", viewer_port(log_path, process), timeout=10)
            try:
                for path, marker in (
                    ("/", b"complete graph"),
                    ("/explorer", b"Salesforce dependency explorer"),
                    ("/api/meta", snapshot_id.encode("ascii")),
                    ("/api/processes", b"Sample account update"),
                ):
                    assert marker in viewer_get(connection, path), path
                route = "/process-graph?country=XX&feature=sample&process=sample-account"
                check_process_graph(connection, route, step=False)
                check_process_graph(connection, route + "&step=copy-name", step=True)
            finally:
                connection.close()
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    # The installed launcher can spawn a viewer process that
                    # inherits the log handle. Stop both before cleaning up.
                    stopped = subprocess.run(
                        ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True, timeout=10,
                    )
                    if stopped.returncode and process.poll() is None:
                        process.kill()
                        raise RuntimeError("Could not stop the Windows viewer process tree")
                else:
                    process.terminate()
                process.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cli", type=Path, help="Path to the installed sfdx-graphify entry point")
    cli = parser.parse_args().cli.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="sfdx-graphify-smoke-") as temporary:
        directory = Path(temporary)
        for platform, config_dir in (("claude", ".claude"), ("codex", ".agents")):
            subprocess.run([str(cli), "install", "--platform", platform, "--project"],
                           cwd=directory, check=True, timeout=30)
            skill = directory / config_dir / "skills" / "sfdx-graphify" / "SKILL.md"
            assert "name: sfdx-graphify" in skill.read_text(encoding="utf-8"), skill
        example, catalog = prepare_example(directory)
        snapshot = str(directory / "snapshot")
        snapshot_args = ("--snapshot", snapshot)
        build = query(cli, directory, "build", str(example), "--include-inactive", "--out", snapshot)
        refs = query(cli, directory, "refs", "Contact.LastName", *snapshot_args)
        assert any(row["source"] == SAMPLE_FLOW for row in refs["results"]), refs
        deps = query(cli, directory, "deps", "SampleAccount", *snapshot_args, "--relation", "calls")
        action = next(row for row in deps["results"] if row["target"] == SAMPLE_ACTION)
        evidence = query(cli, directory, "explain", action["id"], *snapshot_args)
        assert any(row["path"].endswith("/SampleAccount.flow-meta.xml")
                   for row in evidence["results"][0]["evidence"]), evidence

        impact = query(cli, directory, "impact", "Account.Description", *snapshot_args, "--depth", "3")
        assert any(row["source"] == "sf:apexmethod:sampleaccounthandler.beforeupdate"
                   and row["resolution"]["status"] == "candidate"
                   and row["resolution"]["missing_ancestors"] == ["SampleExternalBase"]
                   for row in impact["results"]), impact
        projected = directory / "projection"
        query(cli, directory, "export", "graphify", *snapshot_args, "--out", str(projected))
        assert (projected / "graph.json").is_file()
        check_viewer(cli, directory, example, snapshot_args, build["snapshot_id"], catalog)
    print("Salesforce wheel smoke passed: install, build, refs, deps, explain, candidate impact, export, "
          "viewer and process graphs.")


if __name__ == "__main__":
    main()
