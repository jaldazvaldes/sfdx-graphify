"""Local Salesforce dependency CLI, intentionally separate from generic Graphify."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .schema import RELATED_EVIDENCE_SITES, RELATIONS, DataError, EXTRACTOR_VERSION, SCHEMA_VERSION
from . import query


GUIDANCE = """Use sfdx-graphify to locate Salesforce dependency evidence.
From the folder containing sfdx-project.json, run sfdx-graphify build . to save a private snapshot for that checkout.
Local queries and the viewer use that snapshot automatically from the same project root. Pass --snapshot for a custom build location.
New builds exclude explicitly inactive local Flows (Obsolete, Draft, InvalidDraft), Apex triggers (Inactive, Deleted), validation and Workflow rules, and approval processes (active=false).
Use build --include-inactive for historical analysis. Unknown/ambiguous status is retained.
Check activity_policy in query results: older snapshots can include every local status.
Inspect coverage and diagnostics before relying on results.
Use --format brief for one source location per relationship and diagnostic totals.
Use --format evidence on refs, deps, and impact for compact source locations grouped by file.
Use --format text for detailed evidence, --full-coverage for the complete coverage object or JSON for structured output.
refs, deps and impact include diagnostic_context: complete counts by code/file and at most 20 focused records by default.
Use --diagnostics full for every diagnostic in consulted files; JSONL/HTTP requests use diagnostics: "full".
omitted_count reports records omitted from the response; no diagnostics are removed from the snapshot.
Zero results or zero local diagnostics do not establish complete coverage of unknown consumers.
Find exact references with: sfdx-graphify refs Account.Name --format brief
Inspect what a Flow uses with: sfdx-graphify deps Sample_Flow --format json
Inspect validation rule inputs with: sfdx-graphify deps sf:validationrule:account.require_phone --format json
Count declared save automation with: sfdx-graphify refs Order --relation triggered_by --limit 20 --format brief
Find an assigned literal with: sfdx-graphify refs Order.Status --value 'Ready' --format brief
Follow a specific relationship with: sfdx-graphify explain <relationship-id>
Check freshness with: sfdx-graphify status --source . --require-current
For repeated queries: sfdx-graphify session --snapshot <directory> [--expect-snapshot <snapshot-id>]
Send one UTF-8 JSON object per line, e.g. {"command":"refs","name":"Account.Name","limit":20}.
Each response wraps the unchanged query result with id and exit_code. Close stdin to stop.
The session verifies and loads once, pins that snapshot, and never silently switches generations.
Open a private local viewer: sfdx-graphify serve
Discover registered process maps with sfdx-graphify processes list.
Use processes show <id-or-title> --format brief for steps, connections, and effects.
Use processes status and processes check to compare authored evidence with the checkout.
Open the registered catalog with sfdx-graphify serve --registered-process-maps.
Inspect a focused graph: sfdx-graphify view Account.Name --mode impact
Use status --source for explicit full verification at the start or when sources change.
Ordinary queries and sessions do not rescan source and report unknown freshness.
Artifact limit: 512 MiB by default; configure SFDX_GRAPHIFY_MAX_ARTIFACT_MIB. No records are dropped.
For independent agent calls, start the local service with sfdx-graphify query-server --port 0.
Use the URL it prints with --server on subsequent queries. Pass --snapshot to load a custom snapshot.
Server queries from a project root check its saved snapshot ID when available.
Pass --expect-snapshot <id> to pin a specific snapshot, including one from another checkout.
Tests remain in canonical facts. --exclude-tests filters explicit AST classifications; unknowns remain included.
Impact results identify candidates for inspection, not execution order or proven runtime effects.
Candidate calls include unverified external hierarchies and conditional trigger dispatch.
They are included in queries and marked AMBIGUOUS in Graphify; inspect the reason and source evidence.
Impact traversal.candidate_relationship_ids marks candidate relationships anywhere in the returned witness path.
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
    session.add_argument("--snapshot", type=Path, help="Defaults to the current project's saved snapshot.")
    session.add_argument("--expect-snapshot", help="Refuse startup unless the loaded snapshot has this exact ID.")
    query_server = commands.add_parser("query-server", help="Keep a verified snapshot in memory for independent localhost queries.")
    query_server.add_argument("--snapshot", type=Path, help="Defaults to the current project's saved snapshot.")
    query_server.add_argument("--port", type=int, default=8769)
    query_server.add_argument("--expect-snapshot")
    build = commands.add_parser("build", help="Build a local canonical snapshot.")
    build.add_argument("project_root", type=Path)
    build.add_argument("--out", type=Path)
    build.add_argument("--include-untracked", action="store_true")
    build.add_argument("--include-inactive", action="store_true",
                       help="Include explicitly inactive local components for historical analysis.")
    build.add_argument("--include-permissions", action="store_true",
                       help="Extract declared grants from local profiles and permission sets.")
    build.add_argument("--custom-metadata-accessors", type=Path,
                       help="Read optional JSON rules for literal Apex custom metadata field accessors.")
    build.add_argument("--timings", action="store_true", help="Report elapsed build phases in seconds on stderr.")
    build.add_argument("--format", choices=("json", "text"), default="json")
    commands.add_parser("guide", help="Print agent usage guidance without writing files.")
    install = commands.add_parser("install", help="Copy the Salesforce skill for an assistant.")
    install.add_argument("--platform", choices=("claude", "codex"), default="claude")
    install.add_argument("--project", action="store_true", help="Install in the current project directory.")
    install.add_argument("--replace", action="store_true", help="Replace a different skill at the selected path.")
    serve = commands.add_parser("serve", help="Serve the offline viewer on localhost only.")
    serve.add_argument("--snapshot", type=Path, help="Defaults to the current project's saved snapshot.")
    serve.add_argument("--before", type=Path, help="Optional earlier local snapshot for comparison.")
    catalogs = serve.add_mutually_exclusive_group()
    catalogs.add_argument("--process-maps", type=Path, help="Optional local JSON catalog of authored process maps.")
    catalogs.add_argument("--registered-process-maps", action="store_true",
                          help="Use this checkout's registered snapshot, catalog and port.")
    serve.add_argument("--port", type=int, help="Local port, default 8767.")

    processes = commands.add_parser("processes", help="Register and query local process maps.")
    process_commands = processes.add_subparsers(dest="process_command", required=True)
    register = process_commands.add_parser("register", help="Save a private process catalog for this project checkout.")
    register.add_argument("--snapshot", required=True, type=Path)
    register.add_argument("--catalog", required=True, type=Path)
    register.add_argument("--port", type=int, default=8767)
    process_commands.add_parser("status", help="Show the registered files and checkout revision.")
    check_maps = process_commands.add_parser("check", help="Compare process references with local source files.")
    check_maps.add_argument("--source", type=Path, help="Compare against this checkout instead of the current one.")
    check_maps.add_argument("--format", choices=("json", "brief"), default="json")
    country_map = process_commands.add_parser("country", help="Evaluate documented country switches for a process.")
    country_map.add_argument("process", help="Process ID.")
    country_map.add_argument("--country", action="append", required=True, help="Country code. Repeat to compare.")
    list_maps = process_commands.add_parser("list", help="List matching process maps.")
    list_maps.add_argument("--match", help="Filter by process title or ID.")
    list_maps.add_argument("--component", help="Find process steps that mention a typed component ID or API name.")
    show_map = process_commands.add_parser("show", help="Show the steps and optionally evidence for one step.")
    show_map.add_argument("process")
    show_map.add_argument("--step", help="Step ID or label.")
    for command in (list_maps, show_map):
        command.add_argument("--country", help="Country ID or name.")
        command.add_argument("--feature", help="Feature ID or name.")
        command.add_argument("--format", choices=("json", "brief"), default="json")

    for name in ("refs", "deps", "impact", "explain", "components", "status"):
        command = commands.add_parser(name)
        if name in ("refs", "deps", "impact", "explain"):
            command.add_argument("name")
        if name == "components":
            command.add_argument("name", nargs="?", default="")
            command.add_argument("--kind")
        location = command.add_mutually_exclusive_group()
        location.add_argument("--snapshot", type=Path, help="Defaults to the current project's saved snapshot.")
        location.add_argument("--server", help="Query the persistent localhost service without loading snapshot files.")
        location.add_argument("--registered", action="store_true", help="Use the snapshot pinned by the process catalog.")
        command.add_argument("--expect-snapshot")
        command.add_argument("--source", type=Path)
        formats = ("json", "text", "brief")
        if name in ("refs", "deps", "impact"):
            formats += ("evidence",)
        command.add_argument("--format", choices=formats, default="json",
                             help="JSON, detailed text, or a compact source view.")
        command.add_argument("--full-coverage", action="store_true",
                             help="Include full snapshot coverage in text output.")
        command.add_argument("--require-current", action="store_true")
        if name in ("refs", "deps", "impact", "components"):
            command.add_argument("--limit", type=int, default=1000)
            command.add_argument("--exclude-tests", action="store_true")
        if name in ("refs", "deps", "impact"):
            command.add_argument("--relation", choices=sorted(RELATIONS))
            command.add_argument("--diagnostics", choices=("summary", "full"), default="summary",
                                 help="Summary counts and up to 20 focused records (default), or every consulted-file diagnostic.")
        if name == "refs":
            command.add_argument("--value", help="Filter writes by an exact assigned literal, such as 'Ready' or true.")
        if name == "impact":
            command.add_argument("--depth", type=int, default=2)

    apex_check = commands.add_parser("check-apex", help="Classify Apex source lines as code, comment, or uncertain.")
    apex_check.add_argument("citations", nargs="+", metavar="PATH:LINE")
    apex_check.add_argument("--source", type=Path, default=Path("."))

    export = commands.add_parser("export")
    export.add_argument("target", choices=("graphify",))
    export.add_argument("--snapshot", required=True, type=Path)
    export.add_argument("--expect-snapshot")
    export.add_argument("--out", required=True, type=Path)
    export.add_argument("--source", type=Path)
    export.add_argument("--format", choices=("json", "text"), default="json")

    view = commands.add_parser("view", help="Return a bounded graph focused on a component.")
    view.add_argument("name")
    view.add_argument("--snapshot", type=Path, help="Defaults to the current project's saved snapshot.")
    view.add_argument("--expect-snapshot")
    view.add_argument("--before", type=Path)
    view.add_argument("--mode", choices=("impact", "automation", "schema", "permissions"), default="impact")
    view.add_argument("--depth", type=int, default=1)
    view.add_argument("--status", default="all", help="Filter by status declared in local Flow XML.")
    view.add_argument("--format", choices=("json", "text"), default="json")
    return result


def _available_project_snapshot(root: Path) -> Path | None:
    from .snapshot import default_output, source_root

    snapshot = default_output(root)
    try:
        (snapshot / "CURRENT.json").lstat()
    except FileNotFoundError:
        return None
    if source_root(snapshot) != root.resolve():
        raise DataError("The saved snapshot does not belong to this checkout. Rebuild it with sfdx-graphify build .")
    return snapshot


def _project_snapshot() -> Path:
    root = Path.cwd()
    if not (root / "sfdx-project.json").is_file():
        raise DataError("Open the project folder containing sfdx-project.json or pass --snapshot.")
    snapshot = _available_project_snapshot(root)
    if snapshot is None:
        raise DataError("No saved snapshot for this project. Run sfdx-graphify build . or pass --snapshot.")
    return snapshot


def _project_snapshot_id() -> str | None:
    from .snapshot import current_snapshot_id

    root = Path.cwd()
    if not (root / "sfdx-project.json").is_file():
        return None
    snapshot = _available_project_snapshot(root)
    return current_snapshot_id(snapshot) if snapshot is not None else None


def _registered_process_maps() -> dict:
    from .process_registration import load_process_registration

    registration = load_process_registration(Path.cwd())
    if registration is None:
        raise DataError("No process catalog is registered for this checkout. "
                        "Run sfdx-graphify processes register --snapshot <path> --catalog <path>.")
    return registration


def _process_checkout(source_revision: str) -> dict:
    from .process_check import project_revision

    revision = project_revision(Path.cwd())
    result: dict = {"checkout_revision": revision}
    if revision is None:
        result["warnings"] = ["The checkout revision is unavailable. Process evidence refers to the registered snapshot."]
    elif revision != source_revision:
        result["warnings"] = [
            f"Checkout {revision[:12]} differs from the process snapshot source {source_revision[:12]}. "
            "Run sfdx-graphify processes check to review source locations."
        ]
    return result


def _process_command(args: argparse.Namespace) -> int:
    code = 0
    try:
        if args.process_command == "register":
            from .process_registration import save_process_registration

            result = save_process_registration(Path.cwd(), args.snapshot, args.catalog, args.port)
        else:
            from .process_workspace import load_registered_catalog, registered_catalog, require_registered_files

            registration = _registered_process_maps()
            if args.process_command == "status":
                result = {**registration, **_process_checkout(registration["source_revision"]), "registered_files": "match"}
                try:
                    require_registered_files(registration)
                except (OSError, ValueError) as exc:
                    result.update({"registered_files": "unverified", "error": str(exc)})
                    code = 1
            elif args.process_command == "country":
                from .process_countries import evaluate_country_process

                bundle, catalog = load_registered_catalog(registration)
                rules = catalog.get("country_rules")
                if rules is None:
                    raise DataError("No country decisions have been documented for this catalog.")
                result = {
                    "source_revision": catalog["source_revision"], "snapshot_id": catalog["snapshot_id"],
                    "process": args.process,
                    "countries": [evaluate_country_process(bundle, catalog, rules, process=args.process, country=country)
                                  for country in dict.fromkeys(args.country)],
                    **_process_checkout(catalog["source_revision"]),
                }
            else:
                catalog, url = registered_catalog(registration)
                if args.process_command == "check":
                    from .process_check import check_processes

                    result = check_processes(catalog, args.source or Path.cwd())
                    summary = result["summary"]
                    unavailable = any(
                        ref["status"] == "unverified" and ref.get("reason") != "no_snippet"
                        for process in result["processes"] for ref in process["references"]
                    )
                    code = int(unavailable or any(summary.get(key) for key in
                                                  ("changed", "ambiguous", "missing", "missing_components", "catalog_errors")))
                else:
                    from .process_queries import list_processes, show_process

                    if args.process_command == "show":
                        result = show_process(catalog, args.process, country=args.country, feature=args.feature, step=args.step)
                        code = int(bool(result["process"].get("catalog_error")))
                    else:
                        result = list_processes(catalog, country=args.country, feature=args.feature, match=args.match,
                                                component=args.component)
                    result.update({"viewer_url": url, **_process_checkout(catalog["source_revision"])})
    except query.QueryError as exc:
        print(json.dumps({"error": str(exc), "candidates": exc.candidates}, ensure_ascii=False))
        print(str(exc), file=sys.stderr)
        return exc.code
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        print(str(exc), file=sys.stderr)
        return 1
    if getattr(args, "format", None) == "brief":
        if args.process_command == "check":
            from .process_check import format_process_check

            print(format_process_check(result))
        else:
            from .process_queries import format_process_brief

            print(format_process_brief(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


def _value_text(value, *, brief: bool) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text[:117] + "…" if brief and len(text) > 120 else text


def _candidate_label(candidate: dict) -> str:
    api_name = candidate.get("api_name") or candidate["id"]
    return api_name if api_name == candidate["id"] else f"{api_name} [{candidate['id']}]"


def _text(result: dict, *, full_coverage: bool = False, brief: bool = False) -> str:
    from .automation import reference_lines

    lines = [f"Snapshot: {result.get('snapshot_id', 'unavailable')}"]
    if "freshness" in result:
        freshness = result["freshness"]
        lines.append(f"Freshness: {freshness.get('state') if isinstance(freshness, dict) else freshness}")
    coverage = result.get("coverage")
    if coverage:
        if full_coverage:
            lines.append("Coverage: " + json.dumps(coverage, ensure_ascii=False, sort_keys=True))
        else:
            lines.append(f"Coverage: {coverage.get('status', 'unknown')}, "
                         f"{coverage.get('analyzed_files', 'unknown')} files analyzed")
    if "output" in result:
        lines.append(f"Output: {result['output']}")
    lines.extend(reference_lines(result))
    if "class_references" in result:
        methods = result["class_references"]
        lines.append(f"Class references: {methods['class_relationship_count']} direct relationships and "
                     f"{methods['method_relationship_count']} across {methods['referenced_method_count']} of "
                     f"{methods['method_count']} directly owned methods.")
    if "value_filter" in result:
        selected = result["value_filter"]
        literal = _value_text(selected["literal"], brief=brief)
        lines.append(f"Assigned literal {literal}: {selected['occurrence_count']} matching occurrences.")
        if unknown := selected.get("unknown_occurrence_count"):
            subject = "1 indexed write occurrence has" if unknown == 1 else f"{unknown} indexed write occurrences have"
            lines.append(f"{subject} an unknown assigned value. Omit --value to inspect all indexed writes.")
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
    values = result.get("custom_metadata_values")
    if values is not None:
        lines.append(f"Custom metadata values: {values['record_count']} explicit records.")
        if "declared_default" in values:
            default = _value_text(values["declared_default"]["value"], brief=brief)
            lines.append(f"{values['omitted_record_count']} records omit the field. Declared default: {default}.")
            if values["unresolved_record_count"]:
                lines.append(f"{values['unresolved_record_count']} records have unresolved field values.")
        groups = values["groups"]
        for group in groups[:8] if brief else groups:
            names = [name.split(".", 1)[1] for name in group["records"][:12]]
            rest = group["record_count"] - len(names)
            examples = ", ".join(names) + (f", +{rest} more" if rest else "")
            lines.append(f"  {_value_text(group['value'], brief=brief)} in {group['record_count']} records ({examples}).")
        if brief and len(groups) > 8:
            lines.append(f"  {len(groups) - 8} more value groups. Use JSON or detailed text for the full summary.")
    for item in result.get("results", []):
        if "relation" in item:
            qualifiers = item.get("qualifiers", {})
            assignment_context = {"memory": " in memory", "database": " in database operation"}.get(
                qualifiers.get("effect"), "")
            permission = qualifiers.get("permission")
            relation = item["relation"] + (f" {permission.replace('_', ' ')}" if permission else "")
            if item["relation"] == "sets_value":
                if brief and values is not None:
                    continue
                relation += " " + _value_text(qualifiers["value"], brief=brief)
            if item["relation"] == "mentions":
                relation += " (inferred)"
            relationship = f"{item['source']} --{relation}--> {item['target']}"
            lines.append(relationship if brief else f"{relationship} [{item['id']}]")
            if item["relation"] in {"triggered_by", "subscribes", "publishes"}:
                order = qualifiers.get("trigger_order")
                details = [qualifiers.get("timing", "").replace("_", "-"), qualifiers.get("local_status"),
                           qualifiers.get("record_trigger_type"),
                           ", ".join(event.replace("_", " ") for event in qualifiers.get("events", ())),
                           f"order {order}" if order is not None else None]
                if any(details):
                    lines.append("  " + ", ".join(part for part in details if part))
            resolution = item.get("resolution", {})
            if resolution.get("status") == "candidate":
                reason = {"external_hierarchy": "external hierarchy not verified",
                          "conditional_virtual_dispatch": "local virtual dispatch is conditional",
                          "fflib_event_dispatch": "fflib event dispatch is conditional"}.get(
                              resolution.get("reason"), "dispatch remains uncertain")
                missing = ", ".join(resolution.get("missing_ancestors", []))
                lines.append(f"  CANDIDATE: {reason}" + (f"; missing ancestors: {missing}" if missing else ""))
            traversal = item.get("traversal", {})
            if traversal.get("candidate_relationship_ids"):
                witness = ("  Witness path includes candidate relationships"
                           if traversal.get("includes_candidate_relationships") else "  Witness path includes candidate calls")
                lines.append(witness + "." if brief else
                             witness + ": " + ", ".join(traversal["candidate_relationship_ids"]))
            evidence = item.get("evidence", [])
            if brief and evidence:
                occurrence = next((row for row in evidence if query.assigned_write_value(row)), evidence[0])
                line = occurrence.get("line")
                location = occurrence["path"] + (f":{line}" if line is not None else "")
                literal = query.assigned_write_value(occurrence)
                if literal is not None:
                    location += f" [assigned{assignment_context} {_value_text(literal['value'], brief=True)}]"
                extra = len(evidence) - 1
                lines.append(f"  {location}" + (f" (+{extra} more occurrences)" if extra else ""))
            for occurrence in (() if brief else evidence):
                location = occurrence.get("line")
                suffix = f":{location}" if location is not None else ""
                lines.append(f"  {occurrence['path']}{suffix} {occurrence['locator']} [{occurrence['id']}]")
                proof = occurrence.get("resolution")
                if not isinstance(proof, dict):
                    continue
                literal = query.assigned_write_value(occurrence)
                if literal is not None:
                    lines.append(f"    Assigned literal{assignment_context}: {_value_text(literal['value'], brief=False)}.")
                for key, label in RELATED_EVIDENCE_SITES:
                    site = proof.get(key)
                    if isinstance(site, dict) and site.get("path"):
                        lines.append(f"    {label}: {site['path']}:{site.get('line', '?')} {site.get('locator', '')}")
        else:
            lines.append(f"{item['api_name']} ({item['kind']})" if brief else
                         f"{item['id']} {item.get('api_name', '')}")
    if result.get("truncated"):
        lines.append("Results truncated by traversal depth or limit.")
    if "truncation" in result and not brief:
        lines.append("Traversal limits: " + json.dumps(result["truncation"], sort_keys=True))
    if "test_filter" in result:
        test_filter = result["test_filter"]
        if not brief:
            lines.append("Test filter: " + json.dumps(test_filter, sort_keys=True))
        elif test_filter["exclude_tests"]:
            lines.append(f"Test components excluded: {test_filter['excluded_components_in_snapshot']}.")
    if "diagnostic_context" in result and brief:
        context = result["diagnostic_context"]
        lines.append(f"Source diagnostics: {context['total']} ({context['scope']}).")
    elif "diagnostic_context" in result:
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
    if "literal_blind_spots" in result:
        mentions = result["literal_blind_spots"]
        if mentions["state"] == "indexed":
            lines.append(f"Literal search in selected metadata: {mentions['scanned_file_count']} files scanned, "
                         f"{mentions['skipped_file_count']} skipped.")
            for key, label in (("qualified", "qualified"), ("field_name", "field name"),
                               ("method_name", "method name")):
                if key not in mentions:
                    continue
                match = mentions[key]
                lines.append(f"  {match['literal']} ({label}): {match['occurrence_count']} matches "
                             f"in {match['file_count']} files.")
                examples = match["example_paths"]
                lines.extend(f"    {path}" for path in (examples[:1] if brief else examples))
            lines.append(mentions["interpretation"])
            if apex := mentions.get("apex_strings"):
                qualified, field_name = apex["qualified"], apex["field_name"]
                lines.append(f"Apex string hints: {qualified['occurrence_count']} qualified, "
                             f"{field_name['occurrence_count']} field-name matches "
                             f"({apex['scanned_file_count']} files scanned, {apex['skipped_file_count']} skipped).")
                examples = sorted(set(qualified["example_paths"] + field_name["example_paths"]))
                lines.extend(f"    {path}" for path in (examples[:1] if brief else examples))
        else:
            lines.append(f"Literal search unavailable: {mentions['reason']}")
    lines.extend(result.get("limitations", []))
    if result.get("error"):
        lines.append(f"Error: {result['error']}")
    for candidate in result.get("candidates", []):
        lines.append(f"Candidate: {_candidate_label(candidate)}")
        for definition in candidate.get("definitions", []):
            path = definition["path"]
            lines.append(path if brief else f"  {path} {definition.get('locator', '')}")
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
    if args.command == "check-apex":
        from .apex_source_check import check_apex_lines

        try:
            rows = check_apex_lines(args.source, args.citations)
        except (DataError, OSError, ValueError, ImportError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        for row in rows:
            reason = f" ({row['reason']})" if row.get("reason") else ""
            print(f"{row['path']}:{row['line']} {row['state']}{reason}")
        return int(any(row["state"] == "unknown" for row in rows))
    if args.command == "processes":
        return _process_command(args)
    if args.command == "install":
        from .skill_install import install_skill
        try:
            target = install_skill(args.platform, project=args.project, replace=args.replace)
        except (DataError, OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Salesforce skill ready at {target}")
        return 0
    if args.command == "query-server":
        from .query_server import serve_queries
        try:
            serve_queries(args.snapshot or _project_snapshot(), port=args.port,
                          expect_snapshot=args.expect_snapshot)
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
            return run_session(args.snapshot or _project_snapshot(), expect_snapshot=args.expect_snapshot)
        except KeyboardInterrupt:
            return 130
        except (DataError, OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.command == "serve":
        try:
            from .viewer import serve

            if args.registered_process_maps:
                if args.snapshot is not None or args.port is not None:
                    raise DataError("--registered-process-maps uses the registered snapshot and port.")
                registration = _registered_process_maps()
                serve(Path(registration["snapshot"]), port=registration["port"], before=args.before,
                      process_maps=Path(registration["catalog"]), expected_process_identity=registration)
            else:
                port = 8767 if args.port is None else args.port
                serve(args.snapshot or _project_snapshot(), port=port, before=args.before, process_maps=args.process_maps)
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
            accessors = None
            if args.custom_metadata_accessors is not None:
                with args.custom_metadata_accessors.open("rb") as handle:
                    accessors = handle.read(1024 * 1024 + 1)
            timings = {} if args.timings else None
            bundle = build_project(args.project_root, out=args.out,
                                   include_untracked=args.include_untracked,
                                   include_inactive=args.include_inactive,
                                   include_permissions=args.include_permissions,
                                   custom_metadata_accessors=accessors, timings=timings)
            result = query.envelope(bundle, [])
            result["output"] = str(args.out or default_output(args.project_root))
            if timings is not None:
                print(json.dumps({"build_seconds": timings}), file=sys.stderr)
        elif getattr(args, "server", None) is not None:
            if args.source is not None or args.require_current:
                raise DataError("Server queries do not scan source. Use status --snapshot <directory> --source <project> for explicit verification.")
            from .query_server import request_query
            request = {"command": args.command}
            for key in ("name", "relation", "limit", "depth", "kind", "exclude_tests", "diagnostics", "value"):
                if hasattr(args, key) and getattr(args, key) is not None:
                    request[key] = getattr(args, key)
            expected = args.expect_snapshot if args.expect_snapshot is not None else _project_snapshot_id()
            response = request_query(args.server, request, expected_snapshot=expected)
            result, code = response["result"], response["exit_code"]
        else:
            from .snapshot import load
            expected = getattr(args, "expect_snapshot", None)
            if getattr(args, "registered", False):
                registration = _registered_process_maps()
                snapshot_dir = Path(registration["snapshot"])
                if expected is not None and expected != registration["snapshot_id"]:
                    raise DataError("--expect-snapshot differs from the snapshot pinned by the process catalog.")
                expected = registration["snapshot_id"]
            else:
                snapshot_dir = args.snapshot or _project_snapshot()
            bundle = load(snapshot_dir, expect_snapshot=expected) if expected is not None else load(snapshot_dir)
            if args.command == "view":
                from .viewer import build_view
                component = query.resolve_component(bundle, args.name)
                before = load(args.before) if args.before else None
                result = build_view(bundle, component["id"], mode=args.mode,
                                    depth=args.depth, status=args.status, before=before)
            elif args.command == "export":
                from .graphify_adapter import export_graphify
                from .snapshot import source_root, validate_output
                original_root = source_root(snapshot_dir)
                if original_root is not None:
                    validate_output(args.out, source_root=original_root)
                if original_root is None and args.source is None:
                    raise DataError("Specify --source when the snapshot has no local source mapping.")
                path = export_graphify(bundle, args.out, source_root=args.source or original_root,
                                       snapshot_dir=snapshot_dir)
                result = query.envelope(bundle, [], source=args.source)
                result["output"] = str(path)
            else:
                options = {key: getattr(args, key) for key in ("relation", "limit", "depth", "kind", "exclude_tests", "diagnostics", "value")
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
    full_coverage = args.command in {"build", "export"} or getattr(args, "full_coverage", False)
    if output_format == "evidence":
        from .evidence_text import evidence_lines

        header = _text({**result, "results": []}, full_coverage=full_coverage, brief=True)
        output = "\n".join([header, *evidence_lines(result)])
    elif output_format == "json":
        output = json.dumps(result, ensure_ascii=False, sort_keys=True)
    else:
        output = _text(result, full_coverage=full_coverage, brief=output_format == "brief")
    print(output)
    if code:
        print(result["error"], file=sys.stderr)
        for candidate in result.get("candidates", []):
            print(f"Candidate: {_candidate_label(candidate)}", file=sys.stderr)
    return code
