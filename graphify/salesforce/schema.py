"""Canonical facts and occurrence identities. No I/O or provider imports."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from urllib.parse import quote

SCHEMA_VERSION = 5
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4, 5})
EXTRACTOR_VERSION = "0.5.1"
UPSTREAM_REVISION = "6c8002eca4acad58db6160eabcb466e61fc35afb"
TESTED_API_VERSIONS = ("64.0",)
RELATIONS = frozenset({"reads", "writes", "filters", "calls", "references", "contains"})
KINDS = frozenset({"object", "field", "flow", "apexclass", "apexmethod", "apextrigger", "lwc", "outboundmessage", "validationrule"})
API_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class DataError(ValueError):
    """Invalid input or a failed integrity contract; safe to report to the CLI."""


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
    qualified, separator, signature = api_name.partition("(") if kind == "apexmethod" else (api_name, "", "")
    parts = qualified.split(".")
    expected = {2, 3} if kind == "apexmethod" else {1, 2} if kind == "apexclass" else {2} if kind in {"field", "outboundmessage", "validationrule"} else {1}
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


class FactStore:
    """Aggregate semantic relationships while preserving each source occurrence."""

    def __init__(self):
        self.components: dict[str, dict] = {}
        self.relationships: dict[str, dict] = {}
        self.evidence: dict[str, dict] = {}
        self.diagnostics: list[dict] = []
        self._diagnostic_ids: set[str] = set()

    def ensure_component(self, kind, api_name, definition=None, **attrs):
        cid = component_id(kind, api_name)
        qualified_parts = api_name.partition("(")[0].split(".")
        namespace = (qualified_parts[0] if (kind == "apexclass" and len(qualified_parts) == 2)
                     or (kind == "apexmethod" and len(qualified_parts) == 3)
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
        identity_manifest = {key: value for key, value in manifest.items() if key != "snapshot_id"}
        expected_id = stable_id("snapshot", {"manifest": identity_manifest,
                                             "facts": facts, "diagnostics": diagnostics})
        if manifest["snapshot_id"] != expected_id:
            raise DataError("Snapshot identity does not match its contents.")
        canonical_json(bundle)
    except (KeyError, TypeError, AttributeError) as exc:
        raise DataError("Malformed Salesforce snapshot.") from exc
