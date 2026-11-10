"""Measure the local Salesforce graph on the public example at several sizes."""

from __future__ import annotations

import argparse
from http.client import HTTPConnection
import json
from pathlib import Path
import platform
import re
import shutil
from statistics import median
from tempfile import TemporaryDirectory
from threading import Thread
from time import perf_counter

from graphify.salesforce.query import impact
from graphify.salesforce.schema import EXTRACTOR_VERSION
from graphify.salesforce.service import build_project
from graphify.salesforce.viewer import create_server


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "salesforce"
FLOWS = Path("force-app/main/default/flows")


def get_asset(connection: HTTPConnection, route: str) -> bytes:
    connection.request("GET", route)
    response = connection.getresponse()
    body = response.read()
    if response.status != 200 or not body:
        raise RuntimeError(f"The viewer did not serve {route} (HTTP {response.status}).")
    return body


def first_view(snapshot: Path) -> float:
    start = perf_counter()
    with create_server(snapshot, port=0) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=30)
        thread.start()
        try:
            page = get_asset(connection, "/")
            assets = re.findall(rb'(?:src|href)="(/(?!/)[^"?#]+\.(?:js|css))"', page)
            if not assets:
                raise RuntimeError("No local viewer assets were linked from the main page.")
            for asset in assets:
                get_asset(connection, asset.decode("ascii"))
            return perf_counter() - start
        finally:
            connection.close()
            server.shutdown()
            thread.join()


def benchmark(extra_flows: int, repeats: int) -> dict:
    build_times, impact_times, viewer_times = [], [], []
    with TemporaryDirectory(prefix="sfdx-benchmark-") as temporary:
        scratch = Path(temporary)
        project = scratch / "project"
        shutil.copytree(EXAMPLE, project)
        template = (project / FLOWS / "SampleNotify.flow-meta.xml").read_bytes()
        for number in range(extra_flows):
            (project / FLOWS / f"BenchmarkNotify{number:05d}.flow-meta.xml").write_bytes(template)

        for run in range(repeats):
            snapshot = scratch / f"snapshot-{run}"
            start = perf_counter()
            bundle = build_project(project, out=snapshot)
            build_times.append(perf_counter() - start)

            start = perf_counter()
            result = impact(bundle, "Account.Description", depth=3,
                            limit=max(1, len(bundle["facts"]["relationships"])))
            impact_times.append(perf_counter() - start)

            viewer_times.append(first_view(snapshot))

    return {
        "extra_flows": extra_flows,
        "metadata_files": bundle["manifest"]["scope"]["supported_file_count"],
        "components": len(bundle["facts"]["components"]),
        "relationships": len(bundle["facts"]["relationships"]),
        "impact_relationships": result["returned_count"],
        "build_median_ms": round(median(build_times) * 1000, 2),
        "impact_median_ms": round(median(impact_times) * 1000, 2),
        "viewer_start_and_fetch_median_ms": round(median(viewer_times) * 1000, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extra-flows", nargs="+", type=int, default=[0, 100, 1000])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1 or any(size < 0 for size in args.extra_flows):
        parser.error("Use at least one repeat and non-negative Flow counts.")
    print(json.dumps({
        "extractor": EXTRACTOR_VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "repeats": args.repeats,
        "results": [benchmark(size, args.repeats) for size in args.extra_flows],
    }, indent=2))


if __name__ == "__main__":
    main()
