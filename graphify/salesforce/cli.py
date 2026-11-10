"""Local Salesforce dependency CLI, intentionally separate from generic Graphify."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .schema import DISPATCH_EVIDENCE_SITES, DataError, EXTRACTOR_VERSION, SCHEMA_VERSION
from . import query


GUIDANCE = """Use sfdx-graphify to locate Salesforce dependency evidence.
Build with: sfdx-graphify build <project-root>
New builds exclude explicitly inactive local Flows (Obsolete, Draft, InvalidDraft), Apex triggers (Inactive, Deleted), and validation and Workflow rules (active=false).
Use build --include-inactive for historical analysis. Unknown/ambiguous status is retained.
Check activity_policy in query results: older snapshots can include every local status.
Inspect coverage and diagnostics before relying on results.
refs, deps and impact include diagnostic_context: complete counts by code/file and at most 20 focused records by default.
Use --diagnostics full for every diagnostic in consulted files; JSONL/HTTP requests use diagnostics: "full".
omitted_count reports records omitted from the response; no diagnostics are removed from the snapshot.
Zero results or zero local diagnostics do not establish complete coverage of unknown consumers.
Find exact references with: sfdx-graphify refs Account.Name --snapshot <directory> --format json
Inspect what a Flow uses with: sfdx-graphify deps Sample_Flow --snapshot <directory> --format json
Inspect validation rule inputs with: sfdx-graphify deps sf:validationrule:account.require_phone --snapshot <directory> --format json
Follow a specific relationship with: sfdx-graphify explain <relationship-id> --snapshot <directory>
Check freshness with: sfdx-graphify status --snapshot <directory> --source <project-root>
For repeated queries: sfdx-graphify session --snapshot <directory> [--expect-snapshot <snapshot-id>]
Send one UTF-8 JSON object per line, e.g. {"command":"refs","name":"Account.Name","limit":20}.
Each response wraps the unchanged query result with id and exit_code. Close stdin to stop.
The session verifies and loads once, pins that snapshot, and never silently switches generations.
Open a private local viewer: sfdx-graphify serve --snapshot <directory>
Inspect a focused graph: sfdx-graphify view Account.Name --snapshot <directory> --mode impact
Use status --source for explicit full verification at the start or when sources change.
Ordinary queries and sessions do not rescan source and report unknown freshness.
Artifact limit: 512 MiB by default; configure SFDX_GRAPHIFY_MAX_ARTIFACT_MIB. No records are dropped.
For independent agent calls, keep a local service running: sfdx-graphify query-server --snapshot <directory> --port 8769
Then: sfdx-graphify refs Account.Name --server http://127.0.0.1:8769 --exclude-tests
Use --expect-snapshot <id> to require the same snapshot on either local or server queries.
Tests remain in canonical facts. --exclude-tests filters explicit AST classifications; unknowns remain included.
Impact results identify candidates for inspection, not execution order or proven runtime effects.
Candidate calls include unverified external hierarchies and conditional trigger dispatch.
They are included in queries and marked AMBIGUOUS in Graphify; inspect the reason and source evidence.
Impact traversal.candidate_relationship_ids marks candidate calls anywhere in the returned witness path.
Open the cited relative source files and locators before making consequential changes.
An absent edge is never proof that no dependency exists. Unknown coverage remains unknown.
Keep private snapshots outside repositories. Never upload source, snapshots, or logs automatically.
The Salesforce command uses local extraction; generic Graphify commands have a separate contract.
"""


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="sfdx-graphify", description=__doc__)
    result.add_argument("--version", action="version", version=f"%(prog)s {EXTRACTOR_VERSION}")
    commands = result.add_subparsers(dest="command", required=True)
    session = commands.add_parser("session", help="Load a snapshot once and answer UTF-8 JSONL queries until EOF.")
    session.add_argument("--snapshot", required=True, type=Path)
    session.add_argument("--expect-snapshot", help="Refuse startup unless the loaded snapshot has this exact ID.")
    query_server = commands.add_parser("query-server", help="Keep a verified snapshot in memory for independent localhost queries.")
    query_server.add_argument("--snapshot", required=True, type=Path)
    query_server.add_argument("--port", type=int, default=8769)
    query_server.add_argument("--expect-snapshot")
    build = commands.add_parser("build", help="Build a local canonical snapshot.")
    build.add_argument("project_root", type=Path)
    build.add_argument("--out", type=Path)
    build.add_argument("--include-untracked", action="store_true")
    build.add_argument("--include-inactive", action="store_true",
                       help="Include explicitly inactive local components for historical analysis.")
    build.add_argument("--format", choices=("json", "text"), default="json")
    commands.add_parser("guide", help="Print agent usage guidance without writing files.")
    install = commands.add_parser("install", help="Copy the Salesforce skill for an assistant.")
    install.add_argument("--platform", choices=("claude", "codex"), default="claude")
    install.add_argument("--project", action="store_true", help="Install in the current project directory.")
    serve = commands.add_parser("serve", help="Serve the offline viewer on localhost only.")
    serve.add_argument("--snapshot", required=True, type=Path)
    serve.add_argument("--before", type=Path, help="Optional earlier local snapshot for comparison.")
    serve.add_argument("--port", type=int, default=8767)

    for name in ("refs", "deps", "impact", "explain", "components", "status"):
        command = commands.add_parser(name)
        if name in ("refs", "deps", "impact", "explain"):
            command.add_argument("name")
        if name == "components":
            command.add_argument("name", nargs="?", default="")
            command.add_argument("--kind")
        location = command.add_mutually_exclusive_group(required=True)
        location.add_argument("--snapshot", type=Path)
        location.add_argument("--server", help="Query the persistent localhost service without loading snapshot files.")
        command.add_argument("--expect-snapshot")
        command.add_argument("--source", type=Path)
        command.add_argument("--format", choices=("json", "text"), default="json")
        command.add_argument("--require-current", action="store_true")
        if name in ("refs", "deps", "impact", "components"):
            command.add_argument("--limit", type=int, default=1000)
            command.add_argument("--exclude-tests", action="store_true")
        if name in ("refs", "deps", "impact"):
            command.add_argument("--relation", choices=("reads", "writes", "filters", "calls",
                                                         "references", "contains"))
            command.add_argument("--diagnostics", choices=("summary", "full"), default="summary",
                                 help="Summary counts and up to 20 focused records (default), or every consulted-file diagnostic.")
        if name == "impact":
            command.add_argument("--depth", type=int, default=2)

    export = commands.add_parser("export")
    export.add_argument("target", choices=("graphify",))
    export.add_argument("--snapshot", required=True, type=Path)
    export.add_argument("--expect-snapshot")
    export.add_argument("--out", required=True, type=Path)
    export.add_argument("--source", type=Path)
    export.add_argument("--format", choices=("json", "text"), default="json")

    view = commands.add_parser("view", help="Return a bounded graph focused on a component.")
    view.add_argument("name")
    view.add_argument("--snapshot", required=True, type=Path)
    view.add_argument("--expect-snapshot")
    view.add_argument("--before", type=Path)
    view.add_argument("--mode", choices=("impact", "automation", "schema"), default="impact")
    view.add_argument("--depth", type=int, default=1)
    view.add_argument("--status", default="all", help="Filter by status declared in local Flow XML.")
    view.add_argument("--format", choices=("json", "text"), default="json")
    return result


def _text(result: dict) -> str:
    lines = [f"Snapshot: {result.get('snapshot_id', 'unavailable')}"]
    if "freshness" in result:
        freshness = result["freshness"]
        lines.append(f"Freshness: {freshness.get('state') if isinstance(freshness, dict) else freshness}")
    if "coverage" in result:
        lines.append("Coverage: " + json.dumps(result["coverage"], ensure_ascii=False, sort_keys=True))
    if "output" in result:
        lines.append(f"Output: {result['output']}")
    if "focus" in result:
        lines.append(f"Focus: {result['focus']['id']} ({result['mode']})")
        lines.append(result.get("interpretation", ""))
        lines.append(f"Visible: {len(result['nodes'])} components, {len(result['edges'])} grouped edges")
        for edge in result["edges"]:
            lines.append(f"{edge['source']} --{edge['relation']}--> {edge['target']}")
            candidate_count = edge.get("resolution_counts", {}).get("candidate", 0)
            if candidate_count:
                lines.append(f"  CANDIDATE: {candidate_count} call(s); inspect their resolution and evidence.")
            for occurrence in edge.get("evidence", []):
                lines.append(f"  {occurrence['path']}:{occurrence.get('line', '?')} "
                             f"{occurrence.get('locator', '')} [{occurrence['id']}]")
    for item in result.get("results", []):
        if "relation" in item:
            lines.append(f"{item['source']} --{item['relation']}--> {item['target']} [{item['id']}]")
            resolution = item.get("resolution", {})
            if resolution.get("status") == "candidate":
                reason = {"external_hierarchy": "external hierarchy not verified",
                          "conditional_virtual_dispatch": "local virtual dispatch is conditional",
                          "fflib_event_dispatch": "fflib event dispatch is conditional"}.get(
                              resolution.get("reason"), "dispatch remains uncertain")
                missing = ", ".join(resolution.get("missing_ancestors", []))
                lines.append(f"  CANDIDATE: {reason}" + (f"; missing ancestors: {missing}" if missing else ""))
            traversal = item.get("traversal", {})
            if traversal.get("includes_candidate_calls"):
                lines.append("  Witness path includes candidate calls: "
                             + ", ".join(traversal["candidate_relationship_ids"]))
            for occurrence in item.get("evidence", []):
                location = occurrence.get("line")
                suffix = f":{location}" if location is not None else ""
                lines.append(f"  {occurrence['path']}{suffix} {occurrence['locator']} [{occurrence['id']}]")
                proof = occurrence.get("resolution")
                if not isinstance(proof, dict):
                    continue
                for key, label in DISPATCH_EVIDENCE_SITES:
                    site = proof.get(key)
                    if isinstance(site, dict) and site.get("path"):
                        lines.append(f"    {label}: {site['path']}:{site.get('line', '?')} {site.get('locator', '')}")
        else:
            lines.append(f"{item['id']} {item.get('api_name', '')}")
    if result.get("truncated"):
        lines.append("Results truncated by traversal depth or limit.")
    if "truncation" in result:
        lines.append("Traversal limits: " + json.dumps(result["truncation"], sort_keys=True))
    if "test_filter" in result:
        lines.append("Test filter: " + json.dumps(result["test_filter"], sort_keys=True))
    if "diagnostic_context" in result:
        context = result["diagnostic_context"]
        lines.append(f"Source diagnostics: {context['total']} ({context['scope']}); "
                     f"{context['returned_count']} shown, {context['omitted_count']} omitted [{context['mode']}]")
        lines.append("Diagnostic counts: " + json.dumps(context["code_counts"], sort_keys=True))
        for path, counts in context["file_counts"].items():
            lines.append(f"  {path}: {counts['total']} " + json.dumps(counts["code_counts"], sort_keys=True))
        for item in context["diagnostics"]:
            location = item.get("location") or {}
            lines.append(f"  {item['code']} {location.get('path', '?')}:{location.get('line', '?')} {item['message']}")
        lines.append(context["interpretation"])
        if context["omitted_count"]:
            lines.append(context["full_details"])
    lines.extend(result.get("limitations", []))
    if result.get("error"):
        lines.append(f"Error: {result['error']}")
    for candidate in result.get("candidates", []):
        lines.append(f"Candidate: {candidate['id']}")
        for definition in candidate.get("definitions", []):
            lines.append(f"  {definition['path']} {definition.get('locator', '')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    # Agent callers consume UTF-8 JSON even when Windows pipes inherit a legacy
    # encoding. Apply the same output contract to local, HTTP and JSONL queries.
    for stream in (sys.stdout, sys.stderr):
        configure = getattr(stream, "reconfigure", None)
        if callable(configure):
            configure(encoding="utf-8")
    args = parser().parse_args(argv)
    if args.command == "guide":
        print(GUIDANCE, end="")
        return 0
    if args.command == "install":
        from .skill_install import install_skill
        try:
            target = install_skill(args.platform, project=args.project)
        except (DataError, OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Salesforce skill ready at {target}")
        return 0
    if args.command == "query-server":
        from .query_server import serve_queries
        try:
            serve_queries(args.snapshot, port=args.port, expect_snapshot=args.expect_snapshot)
            return 0
        except KeyboardInterrupt:
            return 0
        except (DataError, OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.command == "session":
        from .session import run_session
        try:
            for stream in (sys.stdin,):
                configure = getattr(stream, "reconfigure", None)
                if callable(configure):
                    configure(encoding="utf-8")
            return run_session(args.snapshot, expect_snapshot=args.expect_snapshot)
        except KeyboardInterrupt:
            return 130
        except (DataError, OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.command == "serve":
        try:
            from .viewer import serve
            serve(args.snapshot, port=args.port, before=args.before)
            return 0
        except KeyboardInterrupt:
            return 0
        except (DataError, OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    output_format = args.format
    bundle = None
    try:
        code = 0
        if args.command == "build":
            from .service import build_project
            from .snapshot import default_output
            bundle = build_project(args.project_root, out=args.out,
                                   include_untracked=args.include_untracked,
                                   include_inactive=args.include_inactive)
            result = query.envelope(bundle, [])
            result["output"] = str(args.out or default_output(args.project_root))
        elif getattr(args, "server", None) is not None:
            if args.source is not None or args.require_current:
                raise DataError("Server queries do not scan source. Use status --snapshot <directory> --source <project> for explicit verification.")
            from .query_server import request_query
            request = {"command": args.command}
            for key in ("name", "relation", "limit", "depth", "kind", "exclude_tests", "diagnostics"):
                if hasattr(args, key) and getattr(args, key) is not None:
                    request[key] = getattr(args, key)
            response = request_query(args.server, request, expected_snapshot=args.expect_snapshot)
            result, code = response["result"], response["exit_code"]
        else:
            from .snapshot import load
            expected = getattr(args, "expect_snapshot", None)
            bundle = load(args.snapshot, expect_snapshot=expected) if expected is not None else load(args.snapshot)
            if args.command == "view":
                from .viewer import build_view
                component = query.resolve_component(bundle, args.name)
                before = load(args.before) if args.before else None
                result = build_view(bundle, component["id"], mode=args.mode,
                                    depth=args.depth, status=args.status, before=before)
            elif args.command == "export":
                from .graphify_adapter import export_graphify
                from .snapshot import source_root, validate_output
                original_root = source_root(args.snapshot)
                if original_root is not None:
                    validate_output(args.out, source_root=original_root)
                if original_root is None and args.source is None:
                    raise DataError("Specify --source when the snapshot has no local source mapping.")
                path = export_graphify(bundle, args.out, source_root=args.source or original_root,
                                       snapshot_dir=args.snapshot)
                result = query.envelope(bundle, [], source=args.source)
                result["output"] = str(path)
            else:
                options = {key: getattr(args, key) for key in ("relation", "limit", "depth", "kind", "exclude_tests", "diagnostics")
                           if hasattr(args, key)}
                result = query.dispatch(bundle, args.command, getattr(args, "name", ""),
                                        source=args.source, **options)
        if getattr(args, "require_current", False) and result["freshness"]["state"] != "current":
            result["error"] = "The snapshot could not be verified as current."
            code = 5
    except query.QueryError as exc:
        result = (query.envelope(bundle, [], source=getattr(args, "source", None))
                  if bundle is not None else
                  {"schema_version": SCHEMA_VERSION, "snapshot_id": None,
                   "freshness": {"state": "unknown"}, "coverage": {}, "results": [],
                   "truncated": False})
        result.update({"error": str(exc), "candidates": exc.candidates})
        code = exc.code
    except (DataError, OSError, ValueError) as exc:
        result = {"schema_version": SCHEMA_VERSION, "snapshot_id": None,
                  "freshness": {"state": "unknown"}, "coverage": {}, "results": [],
                  "truncated": False, "error": str(exc)}
        code = 1
    except ImportError as exc:
        result = {"schema_version": SCHEMA_VERSION, "snapshot_id": None,
                  "freshness": {"state": "unknown"}, "coverage": {}, "results": [],
                  "truncated": False,
                  "error": f"Required local dependency is unavailable: {exc}"}
        code = 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if output_format == "json"
          else _text(result))
    if code:
        print(result["error"], file=sys.stderr)
    return code
