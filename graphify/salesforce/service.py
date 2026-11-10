"""Build a consistent local snapshot without executing analyzed code or agent tools."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .discovery import discover, lwc_bundle, supported_source
from .activity import inactive_components, remove_inactive
from .flow import extract_flow
from .apex import index_apex, extract_apex, resolve_invocable_calls
from .triggers import index_triggers, extract_triggers
from .lwc import extract_lwc
from .workflow import index_workflow, extract_workflow
from .approval import index_approval_processes, extract_approval_processes
from .ui_apex import ui_source, extract_ui_apex
from .validation_rules import extract_validation_rule
from .inventory import extract_inventory, index_definitions, identity
from .schema import (EXTRACTOR_VERSION, SCHEMA_VERSION, TESTED_API_VERSIONS,
                     UPSTREAM_REVISION, DataError, FactStore, component_id, stable_id, validate_bundle)
from .snapshot import default_output, load, publish, validate_output
from .xml_source import parse_xml

LIMITATIONS = [
    "Dependency evidence is incomplete; missing edges do not prove missing dependencies.",
    "Verify consequential conclusions against current source; no deployed org was inspected.",
    "A dependency graph does not describe Flow execution order or approved requirements.",
    "Formula references and declared parent relationship paths have bounded support; formula evaluation, arbitrary aliases, layouts and permissions remain outside coverage.",
    "Apex analysis is bounded and static; dynamic queries, unknown types and runtime data flow remain unresolved.",
    "Candidate static calls through an external caller hierarchy are explicitly marked; verify the missing base before treating dispatch as resolved.",
    "Flow, trigger, validation rule and workflow rule activation metadata describe local XML, not deployed or active org behavior.",
    "Workflow reads cover bounded criterion and field update expressions. Cross-object updates remain diagnostic; expressions, action values and execution order are not evaluated.",
    "Literal metadata mentions are review hints and do not establish dependency edges.",
    "Referenced-only targets have not been validated against a complete Salesforce schema.",
]


def build_project(root, *, out=None, include_untracked=False, include_inactive=False):
    root = Path(root).expanduser().absolute()
    output = validate_output(Path(out) if out is not None else default_output(root), source_root=root)
    scan = discover(root, include_untracked=include_untracked)
    root = scan.root
    if not scan.files:
        # Confirm deletion only against a previous build of this source and scope.
        from .snapshot import source_root
        previous = load(output) if (output / "CURRENT.json").exists() else None
        old_scope = previous["manifest"]["scope"] if previous else {}
        selection_keys = ("package_dirs", "include_untracked", "selection", "input_mode",
                          "excluded_directory_names", "forceignore", "gitignore")
        old_paths = previous["manifest"]["inputs"] if previous else {}
        previous_source_still_exists = any(
            supported_source(path)
            and (root / path).exists() for path in old_paths
        )
        if (not previous or source_root(output) != root
                or any(old_scope.get(key) != scan.scope.get(key) for key in selection_keys)
                or previous_source_still_exists):
            raise DataError("No supported metadata found. Check package directories and --include-untracked.")
    store = FactStore()
    for diagnostic in scan.diagnostics:
        entry = dict(diagnostic)
        entry.pop("id", None)
        store.diagnostic(**entry)
    documents, apex, triggers, trigger_metadata, lightning, workflows = [], {}, {}, [], {}, []
    approvals, ui = [], {}
    for path, data in sorted(scan.files.items()):
        if lwc_bundle(path):
            lightning[path] = data
        elif ui_source(path):
            ui[path] = data
        elif path.casefold().endswith(".approvalprocess-meta.xml"):
            try:
                approval = parse_xml(path, data)
            except ValueError as exc:
                store.diagnostic("APPROVAL_METADATA_UNRESOLVED", str(exc),
                                 {"path": path, "content_hash": scan.inputs[path]}, state="unresolved")
            else:
                approvals.append(approval)
        elif path.endswith(".workflow-meta.xml"):
            workflows.append(parse_xml(path, data))
        elif path.endswith(".cls"):
            apex[path] = data
        elif path.endswith(".trigger"):
            triggers[path] = data
        elif path.endswith(".trigger-meta.xml"):
            trigger_metadata.append(parse_xml(path, data))
        elif path.casefold().endswith(".recordtype-meta.xml"):
            # RecordType XML only corroborates lookup resolution. An invalid
            # optional definition must not hide unrelated dependency evidence.
            try:
                document = parse_xml(path, data)
                identity(document)
            except (ValueError, LookupError) as exc:
                store.diagnostic("RECORD_TYPE_METADATA_UNRESOLVED", str(exc),
                                 {"path": path, "content_hash": scan.inputs[path]}, state="unresolved")
            else:
                documents.append(document)
        else:
            documents.append(parse_xml(path, data))
    index_definitions(documents, store)
    index_workflow(workflows, store)
    index_approval_processes(approvals, store)
    apex_index = index_apex(apex, store)
    trigger_index = index_triggers(triggers, trigger_metadata, apex_index, store)
    excluded = inactive_components(store.components.values(), include_inactive=include_inactive)
    extract_inventory(documents, store)
    extract_workflow(workflows, store, excluded=excluded)
    extract_approval_processes(approvals, workflows, store, excluded=excluded)
    extract_apex(apex_index, store)
    extract_triggers(trigger_index, apex_index, store, excluded=set(excluded))
    extract_lwc(lightning, apex_index, store)
    extract_ui_apex(ui, apex_index, store)
    for document in documents:
        if document.root.tag in {"Flow", "ValidationRule"}:
            if component_id(*identity(document)) in excluded:
                continue
            if document.root.tag == "Flow":
                extract_flow(document, store)
            else:
                extract_validation_rule(document, store)
    resolve_invocable_calls(store, apex_index)
    activity_policy = remove_inactive(store, excluded, include_inactive=include_inactive)
    facts = store.finish()
    diagnostics = sorted(store.diagnostics, key=lambda d: d["id"])
    observed_versions = {d.root.value("apiVersion") for d in [*documents, *trigger_metadata]
                         if d.root.tag in {"Flow", "ApexTrigger"} and d.root.value("apiVersion")}
    project_version = scan.scope.get("source_api_version")
    if project_version:
        observed_versions.add(project_version)
        if project_version not in TESTED_API_VERSIONS:
            store.diagnostic("project_api_version_untested",
                             "Project API version is outside the synthetic tested baseline.",
                             {"path": "sfdx-project.json"}, version=project_version)
            diagnostics = sorted(store.diagnostics, key=lambda d: d["id"])
    versions = sorted(observed_versions)
    coverage = {
        "status": "partial" if diagnostics else "bounded",
        "analyzed_files": len(scan.files) - activity_policy["skipped_definition_files"],
        "activity_policy": activity_policy,
        "component_counts": dict(sorted(Counter(c["kind"] for c in facts["components"]).items())),
        "relationship_counts": dict(sorted(Counter(r["relation"] for r in facts["relationships"]).items())),
        "candidate_call_count": sum(r.get("qualifiers", {}).get("resolution_status") == "candidate"
                                    for r in facts["relationships"]),
        "evidence_count": len(facts["evidence"]),
        "diagnostic_counts": dict(sorted(Counter(d["code"] for d in diagnostics).items())),
        "tested_api_versions": list(TESTED_API_VERSIONS),
        "observed_api_versions": versions,
        "untested_api_versions": [v for v in versions if v not in TESTED_API_VERSIONS],
        "supported_metadata": ["Flow (bounded typed references, decisions and formula dependencies)", "CustomObject (inventory)",
                               "CustomField (inventory, lookup filters, controlling fields, formulas and roll-ups)",
                               "RecordType (local evidence for a SOQL or Flow relationship)",
                               "ValidationRule (local activation, bounded formula references and error display fields)",
                               "ApexTrigger (events, literal handlers and bounded record references)",
                               "ApexClass/ApexMethod (bounded AST, invocable methods and static SOQL/DML)",
                               "LightningComponentBundle (literal field and Apex imports, imperative and wire calls)",
                               "Aura and Visualforce (bounded static Apex actions)",
                               "ApprovalProcess (bounded criteria, displayed fields and field updates)",
                               "Workflow (outbound messages, bounded rule criteria and declared same-object field updates)"],
        "flow_status_counts": dict(sorted(Counter(
            c.get("local_status") or "Unknown" for c in facts["components"] if c["kind"] == "flow"
        ).items())),
        "trigger_status_counts": dict(sorted(Counter(
            c.get("local_status") or "Unknown" for c in facts["components"] if c["kind"] == "apextrigger"
        ).items())),
        "validation_rule_status_counts": dict(sorted(Counter(
            c.get("local_status") or "Unknown" for c in facts["components"] if c["kind"] == "validationrule"
        ).items())),
        "workflow_rule_status_counts": dict(sorted(Counter(
            c.get("local_status") or "Unknown" for c in facts["components"] if c["kind"] == "workflowrule"
        ).items())),
        "test_classification": dict(sorted(Counter(
            f"{c['kind']}:{'test' if c.get('is_test') is True else 'production' if c.get('is_test') is False else 'unknown'}"
            for c in facts["components"] if c["kind"] in {"apexclass", "apexmethod"}
        ).items())),
        "limitations": LIMITATIONS,
    }
    manifest = {
        "schema_version": SCHEMA_VERSION, "extractor_version": EXTRACTOR_VERSION,
        "upstream_revision": UPSTREAM_REVISION, "source_revision": scan.revision,
        "dirty": scan.dirty, "input_mode": "working_tree", "scope": scan.scope,
        "activity_policy": activity_policy,
        "inputs": scan.inputs, "coverage": coverage,
    }
    from .blind_spots import build_literal_index
    manifest["literal_blind_spots"] = build_literal_index(scan, facts["components"])
    bundle = {"manifest": manifest, "facts": facts, "diagnostics": diagnostics}
    manifest["snapshot_id"] = stable_id("snapshot", bundle)
    validate_bundle(bundle)
    publish(bundle, output, root, source_guard=scan.assert_unchanged)
    return bundle
