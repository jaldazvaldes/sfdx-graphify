"""Exercise an installed sfdx-graphify entry point on the fictional project."""

from __future__ import annotations

import argparse
from http.client import HTTPConnection
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "salesforce"


def query(cli: Path, directory: Path, *args: str) -> dict:
    result = subprocess.run(
        [str(cli), *args], cwd=directory, capture_output=True, encoding="utf-8", timeout=30,
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


def check_viewer(cli: Path, directory: Path, snapshot_args: tuple[str, str], snapshot_id: str) -> None:
    log_path = directory / "viewer.log"
    with log_path.open("w", encoding="utf-8") as log, subprocess.Popen(
        [str(cli), "serve", *snapshot_args, "--port", "0"], cwd=directory,
        stdout=log, stderr=subprocess.STDOUT,
    ) as process:
        try:
            connection = HTTPConnection("127.0.0.1", viewer_port(log_path, process), timeout=10)
            try:
                for path, marker in (
                    ("/", b"complete graph"),
                    ("/explorer", b"Salesforce dependency explorer"),
                    ("/api/meta", snapshot_id.encode("ascii")),
                ):
                    connection.request("GET", path)
                    response = connection.getresponse()
                    assert response.status == 200 and marker in response.read(), path
            finally:
                connection.close()
        finally:
            if process.poll() is None:
                process.terminate()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cli", type=Path, help="Path to the installed sfdx-graphify entry point")
    cli = parser.parse_args().cli.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="sfdx-graphify-smoke-") as temporary:
        directory = Path(temporary)
        example = directory / "example"
        shutil.copytree(EXAMPLE, example)
        snapshot = str(directory / "snapshot")
        snapshot_args = ("--snapshot", snapshot)
        build = query(cli, directory, "build", str(example), "--include-inactive", "--out", snapshot)
        refs = query(cli, directory, "refs", "Contact.LastName", *snapshot_args)
        assert any(row["source"] == "sf:flow:sampleaccount" for row in refs["results"]), refs
        deps = query(cli, directory, "deps", "SampleAccount", *snapshot_args, "--relation", "calls")
        action = next(row for row in deps["results"] if row["target"] == "sf:apexmethod:sampleaction.run")
        evidence = query(cli, directory, "explain", action["id"], *snapshot_args)
        assert any(row["path"].endswith("/SampleAccount.flow-meta.xml")
                   for row in evidence["results"][0]["evidence"]), evidence

        impact = query(cli, directory, "impact", "Account.Description", *snapshot_args, "--depth", "3")
        assert any(row["source"] == "sf:apexmethod:sampleaccounthandler.beforeupdate"
                   and row["resolution"]["status"] == "candidate"
                   and row["resolution"]["missing_ancestors"] == ["SampleExternalBase"]
                   for row in impact["results"]), impact
        check_viewer(cli, directory, snapshot_args, build["snapshot_id"])
    print("Salesforce wheel smoke passed: build, refs, deps, explain, candidate impact and viewer.")


if __name__ == "__main__":
    main()
