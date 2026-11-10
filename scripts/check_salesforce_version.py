"""Enforce the Salesforce extractor version contract in GitHub Actions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess


PACKAGE = "graphify/salesforce/"
SCHEMA = PACKAGE + "schema.py"
VERSION = re.compile(r'^EXTRACTOR_VERSION = "(\d+)\.(\d+)\.(\d+)"$', re.MULTILINE)
READ_SIDE_MODULES = frozenset({
    "__init__.py", "__main__.py", "cli.py", "graphify_adapter.py", "grouping.py",
    "listener.py", "orbital_layout.py", "query.py", "query_server.py", "session.py", "skill_install.py", "snapshot.py",
    "standard_graph.py", "viewer.py",
})


def git(*args: str, required: bool = True) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8")
    if required:
        result.check_returncode()
    return result.stdout.strip() if result.returncode == 0 else ""


def version(revision: str) -> tuple[int, ...] | None:
    schema = git("show", f"{revision}:{SCHEMA}", required=False)
    match = VERSION.search(schema)
    if schema and not match:
        raise SystemExit(f"Expected a numeric Salesforce extractor version in {revision}:{SCHEMA}.")
    return tuple(map(int, match.groups())) if match else None


def extraction_changed(base: str) -> bool:
    paths = git("diff", "--name-only", "--no-renames", "-z", base, "HEAD", "--", PACKAGE)
    return any(path.startswith(PACKAGE) and path.endswith(".py")
               and path[len(PACKAGE):] not in READ_SIDE_MODULES for path in paths.split("\0"))


def main() -> None:
    event_name = os.environ["GITHUB_EVENT_NAME"]
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    ref = os.environ["GITHUB_REF"]
    is_tag = ref.startswith("refs/tags/sfdx-v")
    before = event.get("before")
    if is_tag and event_name == "push" and (event.get("forced") or not event.get("created")):
        raise SystemExit("Salesforce release tags must be created once and must never be moved.")
    if event_name == "pull_request":
        base = event["pull_request"]["base"]["sha"]
    elif event_name == "push" and not is_tag and before and before.strip("0"):
        base = before
    else:
        exclude = [f"--exclude={ref.removeprefix('refs/tags/')}"] if is_tag else []
        base = git("describe", "--tags", "--match=sfdx-v[0-9]*", "--abbrev=0",
                   *exclude, "HEAD", required=False)
    current = version("HEAD")
    if current is None:
        raise SystemExit("Salesforce extractor version is missing.")
    if not base:
        return
    git("rev-parse", "--verify", f"{base}^{{commit}}")
    previous = version(base)
    if previous is not None and (current < previous or (current == previous and extraction_changed(base))):
        raise SystemExit(f"Bump EXTRACTOR_VERSION when extraction changes (base {base}).")
    print(f"Salesforce extractor version verified against {base}.")


if __name__ == "__main__":
    main()
