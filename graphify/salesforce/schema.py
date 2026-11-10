"""Canonical facts and occurrence identities. No I/O or provider imports."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from urllib.parse import quote

SCHEMA_VERSION = 9
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9})
EXTRACTOR_VERSION = "0.5.9"
UPSTREAM_REVISION = "5eedffa87476a063a05bed96f17275ed63f05c1a"
TESTED_API_VERSIONS = ("64.0",)
RELATIONS = frozenset({"reads", "writes", "filters", "calls", "references", "contains"})
_SCHEMA_9_KINDS = frozenset({"approvalprocess", "aura", "visualforcepage", "visualforcecomponent"})
KINDS = frozenset({"object", "field", "flow", "apexclass", "apexmethod", "apextrigger", "lwc",
                   "outboundmessage", "validationrule", "workflowrule"}) | _SCHEMA_9_KINDS
API_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_DISPATCH_CANDIDATE_INVOCATIONS = {"conditional_virtual_dispatch": "constructed_virtual_dispatch",
                                   "fflib_event_dispatch": "fflib_event_dispatch"}
DISPATCH_EVIDENCE_SITES = (("dispatch_site", "Dispatch"), ("class_literal", "Domain literal"),
                           ("construct_site", "Factory construction"), ("constructor", "Domain constructor"),
                           ("hook", "Hook"))


class DataError(ValueError):
    """Invalid input or a failed integrity contract; safe to report to the CLI."""


def relationship_resolution(relationship: dict) -> dict:
    """Describe extraction certainty, never runtime execution or org validity."""
    qualifiers = relationship.get("qualifiers", {})
    reason = qualifiers.get("uncertainty") if qualifiers.get("resolution_status") == "candidate" else None
    # Earlier snapshots preserve this conditional dispatch in evidence, but
    # predate explicit candidate qualifiers for it.
    if (reason is None and relationship.get("relation") == "calls"
            and qualifiers.get("invocation") == "constructed_virtual_dispatch"):
        reason = "conditional_virtual_dispatch"
    return {
        "status": "candidate" if reason else "extracted",
        "confidence": "AMBIGUOUS" if reason else "EXTRACTED",
        "reason": reason,
        "missing_ancestors": list(qualifiers.get("missing_ancestors", [])) if reason == "external_hierarchy" else [],
    }


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def stable_id(prefix: str, value) -> str:
    return prefix + ":" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _method_types_valid(text: str) -> bool:
    """Validate the bounded canonical type-list spelling used in overload IDs."""
    if not text:
        return True
    if len(text) > 8192:
        return False
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[.<>,\[\]]", text)
    if "".join(tokens) != text:
        return False

    def parse_type(pos, depth=0):
        if depth > 32 or pos >= len(tokens) or not API_NAME.fullmatch(tokens[pos]):
            raise ValueError
        pos += 1
        while pos < len(tokens) and tokens[pos] == ".":
            if pos + 1 >= len(tokens) or not API_NAME.fullmatch(tokens[pos + 1]):
                raise ValueError
            pos += 2
        if pos < len(tokens) and tokens[pos] == "<":
            pos = parse_type(pos + 1, depth + 1)
            while pos < len(tokens) and tokens[pos] == ",":
                pos = parse_type(pos + 1, depth + 1)
            if pos >= len(tokens) or tokens[pos] != ">":
                raise ValueError
            pos += 1
        while pos < len(tokens) and tokens[pos] == "[":
            if pos + 1 >= len(tokens) or tokens[pos + 1] != "]":
                raise ValueError
            pos += 2
        return pos

    try:
        pos = parse_type(0)
        while pos < len(tokens) and tokens[pos] == ",":
            pos = parse_type(pos + 1)
        return pos == len(tokens)
    except ValueError:
        return False


def component_id(kind: str, api_name: str) -> str:
    if kind not in KINDS:
        raise DataError(f"Unsupported component kind: {kind}")
    if kind == "workflowrule":
        # Legacy rule fullName values can contain spaces and punctuation.
        owner, _, name = api_name.partition(".")
        if not API_NAME.fullmatch(owner) or not name.isprintable() or name != name.strip():
            raise DataError(f"Invalid {kind} API name: {api_name}")
        return f"sf:{kind}:" + quote(api_name.casefold(), safe="._")
    qualified, separator, signature = api_name.partition("(") if kind == "apexmethod" else (api_name, "", "")
    parts = qualified.split(".")
    expected = {2, 3} if kind == "apexmethod" else {1, 2} if kind == "apexclass" else {2} if kind in {"field", "outboundmessage", "validationrule", "approvalprocess"} else {1}
    signature_valid = not separator or (signature.endswith(")") and _method_types_valid(signature[:-1]))
    if len(parts) not in expected or not all(API_NAME.fullmatch(p) for p in parts) or not signature_valid:
        raise DataError(f"Invalid {kind} API name: {api_name}")
    return f"sf:{kind}:" + quote(api_name.casefold(), safe="._")


def _namespace(api_name: str) -> str | None:
    name = api_name.rsplit(".", 1)[-1]
    parts = name.split("__")
    if len(parts) >= 3:
        return parts[0]
    return None


def shared_activity_field(components: dict[str, dict], owner: str, name: str) -> dict | None:
    """Resolve a typed Task/Event field to its authoritative Activity definition."""
    if owner.casefold() not in {"task", "event"} or not API_NAME.fullmatch(name) or not name.casefold().endswith("__c"):
        return None
    if any(components.get(component_id("object", object_name), {}).get("definition_status") == "ambiguous"
           for object_name in (owner, "Activity")):
        return None
    child = components.get(component_id("field", f"{owner}.{name}"))
    if child and child["definition_status"] != "referenced_only":
        return None
    activity = components.get(component_id("field", f"Activity.{name}"))
    if activity and activity["definition_status"] == "defined" and activity.get("data_type"):
        return activity
    return None


class FactStore:
    """Aggregate semantic relationships while preserving each source occurrence."""

    def __init__(self):
        self.components: dict[str, dict] = {}
        self.relationships: dict[str, dict] = {}
        self.evidence: dict[str, dict] = {}
        self.diagnostics: list[dict] = []
        self.record_type_definitions: dict[str, dict] = {}
        self._diagnostic_ids: set[str] = set()

    def ensure_component(self, kind, api_name, definition=None, **attrs):
        cid = component_id(kind, api_name)
        qualified_parts = api_name.partition("(")[0].split(".")
        namespace = (qualified_parts[0] if (kind == "apexclass" and len(qualified_parts) == 2)
                     or (kind == "apexmethod" and len(qualified_parts) == 3)
                     else _namespace(qualified_parts[0]) if kind == "workflowrule"
                     else _namespace(api_name.partition("(")[0]))
        current = self.components.setdefault(cid, {
            "id": cid, "kind": kind, "api_name": api_name,
            "namespace": namespace, "definition_status": "referenced_only",
            "definitions": [],
        })
        if kind == "field":
            current["owner_namespace"] = _namespace(api_name.split(".", 1)[0])
        # Definitions choose spelling; mere references cannot rewrite a definition.
        if definition is not None:
            if definition not in current["definitions"]:
                current["definitions"].append(dict(definition))
            current["definitions"].sort(key=canonical_json)
            current["api_name"] = min(current["api_name"], api_name)
            current["definition_status"] = (
                "defined" if len(current["definitions"]) == 1 else "ambiguous"
            )
        elif not current["definitions"]:
            current["api_name"] = min(current["api_name"], api_name)
        # Metadata is descriptive, never used to resolve conflicting definitions.
        for key, value in attrs.items():
            if key not in {"id", "kind", "api_name", "namespace", "definitions", "definition_status"}:
                current.setdefault(key, value)
        return cid

    def is_ambiguous(self, cid):
        return self.components[cid]["definition_status"] == "ambiguous"

    def resolve_component(self, kind, api_name, location):
        try:
            cid = self.ensure_component(kind, api_name)
        except DataError:
            self.diagnostic("invalid_reference", "Reference is not a literal API name.",
                            location, state="dynamic")
            return None
        if self.is_ambiguous(cid):
            self.diagnostic("ambiguous_component", "Multiple definitions have this API name.",
                            location, state="ambiguous", candidates=[cid])
            return None
        return cid

    def field(self, owner, name, location):
        if not owner or not API_NAME.fullmatch(owner) or not API_NAME.fullmatch(name):
            self.diagnostic("unsupported_field_path", "A direct, typed field is required.",
                            location, state="unresolved")
            return None
        if self.resolve_component("object", owner, location) is None:
            return None
        activity = shared_activity_field(self.components, owner, name)
        if activity is not None:
            return activity["id"]
        return self.resolve_component("field", f"{owner}.{name}", location)

    def relation(self, source, target, relation, location, rule, resolution=None, qualifiers=None):
        if source not in self.components or target not in self.components or relation not in RELATIONS:
            raise DataError("Relationship has an invalid endpoint or relation.")
        qualifiers = qualifiers or {}
        rid = stable_id("rel", [source, target, relation, qualifiers])
        edge = self.relationships.setdefault(rid, {
            "id": rid, "source": source, "target": target, "relation": relation,
            "qualifiers": qualifiers, "evidence_ids": [],
        })
        resolution = resolution or {"basis": "explicit_syntax"}
        eid = stable_id("ev", [rid, location["path"], location["locator"], rule, resolution])
        occurrence = {
            **location, "id": eid, "relationship_id": rid, "source_component": source,
            "rule": rule, "extractor_version": EXTRACTOR_VERSION, "resolution": resolution,
        }
        previous = self.evidence.get(eid)
        if previous is not None and previous != occurrence:
            raise DataError("Conflicting evidence for a stable occurrence identity.")
        self.evidence[eid] = occurrence
        if eid not in edge["evidence_ids"]:
            edge["evidence_ids"].append(eid)
        return rid

    def diagnostic(self, code, message, location=None, state="unsupported", severity="warning", **extra):
        diagnostic = {"code": code, "message": message, "state": state,
                      "severity": severity, "location": location, **extra}
        diagnostic["id"] = stable_id("diag", diagnostic)
        # Stable content identities avoid quadratic list comparisons on large inventories.
        if diagnostic["id"] not in self._diagnostic_ids:
            self._diagnostic_ids.add(diagnostic["id"])
            self.diagnostics.append(diagnostic)

    def finish(self):
        for relationship in self.relationships.values():
            relationship["evidence_ids"].sort()
        return {"components": sorted(self.components.values(), key=lambda x: x["id"]),
                "relationships": sorted(self.relationships.values(), key=lambda x: x["id"]),
                "evidence": sorted(self.evidence.values(), key=lambda x: x["id"])}


def _relative_path(path):
    if not isinstance(path, str) or not path or "\\" in path or ":" in path:
        raise DataError("Invalid portable source path.")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise DataError("Source path escapes the snapshot root.")


def validate_bundle(bundle):
    """Validate loaded data as well as generated records; never trust a pointer alone."""
    try:
        manifest, facts, diagnostics = (bundle[k] for k in ("manifest", "facts", "diagnostics"))
        if manifest["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
            raise DataError("Unsupported Salesforce snapshot schema version.")
        if not isinstance(manifest["snapshot_id"], str):
            raise DataError("Invalid snapshot identity.")
        if not isinstance(diagnostics, list):
            raise DataError("Invalid snapshot diagnostics.")
        def validate_location(location, *, require_hash=True):
            _relative_path(location["path"])
            if location["path"] not in manifest["inputs"]:
                raise DataError("Provenance path is absent from analyzed inputs.")
            if require_hash or "content_hash" in location:
                if location.get("content_hash") != manifest["inputs"][location["path"]]:
                    raise DataError("Provenance content hash differs from analyzed inputs.")
            for key in ("line", "column", "end_line", "end_column"):
                number = location.get(key)
                if number is not None and (not isinstance(number, int) or isinstance(number, bool) or number < 1):
                    raise DataError("Invalid provenance source position.")
            if require_hash and not isinstance(location.get("locator"), str):
                raise DataError("Missing structural source locator.")
        indexes = {}
        for kind in ("components", "relationships", "evidence"):
            records = facts[kind]
            if not isinstance(records, list):
                raise DataError(f"Invalid {kind} collection.")
            index = {x["id"]: x for x in records}
            if len(index) != len(records):
                raise DataError(f"Duplicate {kind} identities.")
            indexes[kind] = index
        components = indexes["components"]
        for cid, component in components.items():
            if manifest["schema_version"] == 1 and component["kind"] == "apexmethod":
                raise DataError("Apex methods require snapshot schema version 2.")
            if manifest["schema_version"] < 3 and component["kind"] == "apextrigger":
                raise DataError("Apex triggers require snapshot schema version 3.")
            if manifest["schema_version"] < 4 and component["kind"] in {"lwc", "outboundmessage"}:
                raise DataError("Lightning components and outbound messages require snapshot schema version 4.")
            if manifest["schema_version"] < 5 and component["kind"] == "validationrule":
                raise DataError("Validation rules require snapshot schema version 5.")
            if manifest["schema_version"] < 9 and component["kind"] in _SCHEMA_9_KINDS:
                raise DataError("Approval processes, Aura and Visualforce require snapshot schema version 9.")
            if manifest["schema_version"] < 8 and component["kind"] == "workflowrule":
                raise DataError("Workflow rules require snapshot schema version 8.")
            if manifest["schema_version"] < 4 and component["kind"] == "apexmethod" and "(" in component["api_name"]:
                raise DataError("Apex method signatures require snapshot schema version 4.")
            if "is_test" in component and component["is_test"] is not None and type(component["is_test"]) is not bool:
                raise DataError("Invalid test classification.")
            if cid != component_id(component["kind"], component["api_name"]):
                raise DataError("Component identity does not match its API name.")
            definitions = component["definitions"]
            expected = "ambiguous" if len(definitions) > 1 else "defined" if definitions else "referenced_only"
            if component["definition_status"] != expected:
                raise DataError("Invalid component definition state.")
            for definition in definitions:
                validate_location(definition)
            if "source_files" in component:
                if not isinstance(component["source_files"], list):
                    raise DataError("Invalid component source files.")
                for path in component["source_files"]:
                    _relative_path(path)
                    if path not in manifest["inputs"]:
                        raise DataError("Component source file is absent from analyzed inputs.")
        used = set()
        for rid, relationship in indexes["relationships"].items():
            source, target, relation = (relationship[k] for k in ("source", "target", "relation"))
            if source not in components or target not in components or relation not in RELATIONS:
                raise DataError("Dangling or invalid relationship.")
            qualifiers = relationship["qualifiers"]
            if not isinstance(qualifiers, dict):
                raise DataError("Invalid relationship qualifiers.")
            dispatch_reason = None
            if "resolution_status" in qualifiers:
                if manifest["schema_version"] < 6:
                    raise DataError("Candidate relationships require snapshot schema version 6.")
                if qualifiers["resolution_status"] != "candidate" or relation != "calls":
                    raise DataError("Invalid candidate call resolution.")
                reason = qualifiers.get("uncertainty")
                if reason == "external_hierarchy":
                    missing = qualifiers.get("missing_ancestors")
                    if (not isinstance(missing, list) or not missing
                            or any(not isinstance(name, str) or not name for name in missing)
                            or len(set(missing)) != len(missing)):
                        raise DataError("Invalid candidate call resolution.")
                elif isinstance(reason, str) and reason in _DISPATCH_CANDIDATE_INVOCATIONS:
                    if manifest["schema_version"] < 7:
                        raise DataError("Dispatch candidates require snapshot schema version 7.")
                    if (qualifiers.get("invocation") != _DISPATCH_CANDIDATE_INVOCATIONS[reason]
                            or "missing_ancestors" in qualifiers or components[source]["kind"] != "apextrigger"
                            or components[target]["kind"] != "apexmethod"):
                        raise DataError("Invalid candidate call resolution.")
                    if (reason == "fflib_event_dispatch"
                            and qualifiers.get("trigger_event") not in (components[source].get("trigger_events") or [])):
                        raise DataError("Invalid fflib candidate trigger event.")
                    dispatch_reason = reason
                else:
                    raise DataError("Invalid candidate call resolution.")
            if rid != stable_id("rel", [source, target, relation, relationship["qualifiers"]]):
                raise DataError("Relationship identity is inconsistent.")
            evidence_ids = relationship["evidence_ids"]
            if not evidence_ids or len(set(evidence_ids)) != len(evidence_ids):
                raise DataError("Relationship must have unique evidence occurrences.")
            for eid in evidence_ids:
                occurrence = indexes["evidence"][eid]
                if occurrence["relationship_id"] != rid or occurrence["source_component"] != source:
                    raise DataError("Evidence points to the wrong relationship.")
                validate_location(occurrence)
                if occurrence["rule"] == "workflow.rule_field_update":
                    field_definition = occurrence["resolution"]["field_definition"]
                    validate_location(field_definition)
                    if (relation != "writes" or components[source]["kind"] != "workflowrule"
                            or components[target]["kind"] != "field"
                            or field_definition["path"] != occurrence["path"]):
                        raise DataError("Invalid Workflow field update evidence.")
                if occurrence["rule"] == "approval.field_update":
                    field_definition = occurrence["resolution"]["field_definition"]
                    validate_location(field_definition)
                    if (relation != "writes" or components[source]["kind"] != "approvalprocess"
                            or components[target]["kind"] != "field"
                            or not occurrence["path"].casefold().endswith(".approvalprocess-meta.xml")
                            or not field_definition["path"].casefold().endswith(".workflow-meta.xml")):
                        raise DataError("Invalid approval process field update evidence.")
                if dispatch_reason:
                    resolution = occurrence["resolution"]
                    if resolution.get("conditional") is not True or resolution.get("hook") not in components[target]["definitions"]:
                        raise DataError("Invalid dispatch candidate evidence.")
                    locations = (("dispatch_site",) if dispatch_reason == "conditional_virtual_dispatch"
                                 else ("class_literal", "construct_site", "constructor"))
                    for key in locations:
                        validate_location(resolution[key])
                if eid != stable_id("ev", [rid, occurrence["path"], occurrence["locator"],
                                           occurrence["rule"], occurrence["resolution"]]):
                    raise DataError("Evidence identity is inconsistent.")
                used.add(eid)
        if used != set(indexes["evidence"]):
            raise DataError("Orphaned evidence.")
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, dict) or not isinstance(diagnostic.get("code"), str):
                raise DataError("Invalid diagnostic record.")
            if diagnostic.get("location") is not None:
                validate_location(diagnostic["location"], require_hash=False)
        for path, digest in manifest["inputs"].items():
            _relative_path(path)
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise DataError("Invalid input digest.")
        if "literal_blind_spots" in manifest:
            from .blind_spots import validate_literal_index
            validate_literal_index(manifest["literal_blind_spots"], manifest["inputs"])
        identity_manifest = {key: value for key, value in manifest.items() if key != "snapshot_id"}
        expected_id = stable_id("snapshot", {"manifest": identity_manifest,
                                             "facts": facts, "diagnostics": diagnostics})
        if manifest["snapshot_id"] != expected_id:
            raise DataError("Snapshot identity does not match its contents.")
        canonical_json(bundle)
    except (KeyError, TypeError, AttributeError) as exc:
        raise DataError("Malformed Salesforce snapshot.") from exc
