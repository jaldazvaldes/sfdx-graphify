"""Build a consistent local snapshot without executing analyzed code or agent tools."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .discovery import discover, lwc_bundle, supported_source
from .activity import inactive_components, remove_inactive
from .automation import extract_entry_points
from .flow import extract_flow
from .apex import index_apex, extract_apex, resolve_invocable_calls
from .triggers import index_triggers, extract_triggers
from .lwc import extract_lwc
from .workflow import index_workflow, extract_workflow
from .approval import index_approval_processes, extract_approval_processes
from .custom_metadata import extract_custom_metadata, index_custom_metadata, index_custom_metadata_defaults
from .custom_metadata_accessors import extract_custom_metadata_accessors, parse_accessor_rules
from .permissions import index_permissions, extract_permissions
from .named_credentials import index_named_credentials, extract_named_credential_endpoints
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
    "Formula references and declared parent relationship paths have bounded support; formula evaluation, arbitrary aliases and layouts remain outside coverage.",
    "Apex analysis is bounded and static; dynamic queries, unknown types and runtime data flow remain unresolved.",
    "Candidate static calls through an external caller hierarchy are explicitly marked; verify the missing base before treating dispatch as resolved.",
    "Flow, trigger, validation rule and workflow rule activation metadata describe local XML, not deployed or active org behavior.",
    "Workflow covers bounded criterion reads, resolved parent updates and declared literal values. Execution order and formula results require runtime evidence.",
    "Literal metadata mentions are review hints and do not establish dependency edges.",
    "When included, permission edges record explicit grants in local profiles and permission sets. Assignments, groups, muting, sharing and effective access are outside coverage.",
    "Named Credential references cover proven HttpRequest receivers with a literal callout endpoint; dynamic destinations and integration execution are outside coverage.",
    "Custom metadata record values and supported defaults come from local declarations. Apex accessor reads require a literal field argument and source evidence or an explicit rule.",
    "Referenced-only targets have not been validated against a complete Salesforce schema.",
]


def build_project(root, *, out=None, include_untracked=False, include_inactive=False,
                  include_permissions=False, custom_metadata_accessors=None):
    accessor_rules = parse_accessor_rules(custom_metadata_accessors) if custom_metadata_accessors is not None else []
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
    permissions, named_credentials, custom_metadata = [], [], []
    permission_file_count = 0
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
        elif path.casefold().endswith(".md-meta.xml"):
            try:
                document = parse_xml(path, data)
            except DataError as exc:
                store.diagnostic("CUSTOM_METADATA_XML_UNRESOLVED", str(exc),
                                 {"path": path, "content_hash": scan.inputs[path]}, state="unresolved")
            else:
                custom_metadata.append(document)
        elif path.casefold().endswith((".profile-meta.xml", ".permissionset-meta.xml", ".namedcredential-meta.xml")):
            is_credential = path.casefold().endswith(".namedcredential-meta.xml")
            if not is_credential:
                permission_file_count += 1
                if not include_permissions:
                    continue
            try:
                document = parse_xml(path, data)
            except DataError as exc:
                code = "NAMED_CREDENTIAL_METADATA_UNRESOLVED" if is_credential else "PERMISSION_METADATA_UNRESOLVED"
                store.diagnostic(code, str(exc), {"path": path, "content_hash": scan.inputs[path]}, state="unresolved")
            else:
                (named_credentials if is_credential else permissions).append(document)
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
    index_custom_metadata(custom_metadata, store)
    if include_permissions:
        index_permissions(permissions, store)
    index_named_credentials(named_credentials, store)
    apex_index = index_apex(apex, store)
    trigger_index = index_triggers(triggers, trigger_metadata, apex_index, store)
    excluded = inactive_components(store.components.values(), include_inactive=include_inactive)
    extract_inventory(documents, store)
    extract_workflow(workflows, store, excluded=excluded)
    extract_approval_processes(approvals, workflows, store, excluded=excluded)
    extract_apex(apex_index, store)
    extract_custom_metadata_accessors(apex_index, store, accessor_rules)
    extract_named_credential_endpoints(apex_index, store)
    if include_permissions:
        extract_permissions(permissions, store)
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
    extract_entry_points(documents, store, excluded=excluded)
    activity_policy = remove_inactive(store, excluded, include_inactive=include_inactive)
    index_custom_metadata_defaults(documents, store)
    extract_custom_metadata(custom_metadata, store)
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
    permission_selection = {
        "included": bool(include_permissions),
        "selected_file_count": permission_file_count,
        "analyzed_file_count": len(permissions),
        "skipped_file_count": permission_file_count if not include_permissions else 0,
        "failed_file_count": permission_file_count - len(permissions) if include_permissions else 0,
    }
    coverage = {
        "status": "partial" if diagnostics else "bounded",
        "analyzed_files": len(scan.files) - activity_policy["skipped_definition_files"]
                          - (permission_file_count - len(permissions)),
        "activity_policy": activity_policy,
        "permission_selection": permission_selection,
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
                               *(["Profile and PermissionSet (explicit field, object and Apex class grants)"]
                                 if include_permissions else []),
                               "NamedCredential (local definitions and literal Apex HttpRequest endpoint references)",
                               "CustomMetadata (local records, explicit values and exact inferred text mentions)",
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
    scope = dict(scan.scope)
    if accessor_rules:
        scope["custom_metadata_accessors"] = accessor_rules
    if include_permissions:
        scope["include_permissions"] = True
    manifest = {
        "schema_version": SCHEMA_VERSION, "extractor_version": EXTRACTOR_VERSION,
        "upstream_revision": UPSTREAM_REVISION, "source_revision": scan.revision,
        "dirty": scan.dirty, "input_mode": "working_tree",
        "scope": scope,
        "activity_policy": activity_policy,
        "inputs": scan.inputs, "coverage": coverage,
    }
    from .blind_spots import build_literal_index
    manifest["literal_blind_spots"] = build_literal_index(
        scan, facts["components"], apex_units=[*apex_index.units, *trigger_index.units],
    )
    bundle = {"manifest": manifest, "facts": facts, "diagnostics": diagnostics}
    manifest["snapshot_id"] = stable_id("snapshot", bundle)
    validate_bundle(bundle)
    publish(bundle, output, root, source_guard=scan.assert_unchanged)
    return bundle
