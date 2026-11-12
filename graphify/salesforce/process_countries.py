"""Evaluate authored country settings for individual process decisions."""
from __future__ import annotations

import re

from .custom_metadata import CUSTOM_METADATA_SUFFIX
from .query import QueryError
from .schema import API_NAME, DataError, component_id

_VALUE_DIAGNOSTICS = {
    "CUSTOM_METADATA_FIELD_AMBIGUOUS",
    "CUSTOM_METADATA_FIELD_UNDEFINED",
    "CUSTOM_METADATA_FIELD_UNRESOLVED",
    "CUSTOM_METADATA_VALUE_DUPLICATE",
    "CUSTOM_METADATA_VALUE_INVALID",
    "CUSTOM_METADATA_VALUE_UNTYPED",
}


def _records(value, description: str) -> list[dict]:
    if not isinstance(value, list) or not value or any(not isinstance(item, dict) for item in value):
        raise DataError(f"Country rules require a non-empty list of {description}.")
    return value


def _references(value, inputs: dict) -> list[dict]:
    result = []
    for reference in _records(value, "evidence references"):
        path, line, label = (reference.get(key) for key in ("path", "line", "label"))
        if (not isinstance(path, str) or path not in inputs or type(line) is not int or line < 1
                or not isinstance(label, str) or not label.strip()):
            raise DataError("Country rule evidence must name a snapshot input, line and label.")
        result.append({"path": path, "line": line, "label": label})
    return result


def _decisions(overlay: dict, catalog: dict, process: str, inputs: dict, components: dict) -> tuple[str, list[dict]]:
    rules = _records(overlay.get("processes"), "processes")
    identifiers = [rule.get("id") for rule in rules]
    folded = {item.casefold() for item in identifiers if isinstance(item, str) and item.strip()}
    if len(folded) != len(identifiers):
        raise DataError("Country rules require unique process IDs.")
    rule = next((entry for entry in rules if entry["id"].casefold() == process.casefold()), None)
    if rule is None:
        raise QueryError(f"No country rules for process: {process}", 4,
                         [{"id": item} for item in identifiers])
    maps = [item for country in catalog["countries"] for feature in country["features"]
            for item in feature["processes"] if item["id"] == rule["id"]]
    if not maps:
        raise DataError(f"Country rules name a process absent from the catalog: {process}.")
    if error := next((item["catalog_error"] for item in maps if "catalog_error" in item), None):
        raise DataError(f"Cannot evaluate the unavailable process {process}: {error}")
    metadata_type = ""
    decisions = []
    seen: set[str] = set()
    for decision in _records(rule.get("decisions"), "decisions"):
        step, field, branches, conditions = (decision.get(key) for key in
                                             ("step", "field", "branches", "remaining_conditions"))
        if not isinstance(step, str) or not step or step in seen:
            raise DataError("Country rules require unique decision step IDs within a process.")
        seen.add(step)
        field_name = field.removeprefix("sf:field:") if isinstance(field, str) and field.startswith("sf:field:") else ""
        owner, _, name = field_name.partition(".")
        if not owner.endswith("__mdt") or not API_NAME.fullmatch(owner) or not API_NAME.fullmatch(name):
            raise DataError(f"Country decision {step} must reference a custom metadata field ID.")
        if metadata_type and metadata_type != owner:
            raise DataError(f"Country decisions for {process} must use one custom metadata type.")
        metadata_type = owner
        definition = components.get(field)
        if (definition is None or definition["kind"] != "field"
                or definition.get("definition_status") != "defined"
                or (definition.get("data_type") or "").casefold() != "checkbox"):
            raise DataError(f"Country decision {step} requires a uniquely defined checkbox: {field}.")
        if (not isinstance(branches, dict) or set(branches) != {"true", "false"}
                or any(not isinstance(target, str) for target in branches.values())
                or branches["true"] == branches["false"]):
            raise DataError(f"Country decision {step} requires distinct true and false targets.")
        for item in maps:
            decision_step = next((entry for entry in item["steps"] if entry["id"] == step), None)
            targets = {edge["to"] for edge in item["edges"] if edge["from"] == step}
            if (decision_step is None or decision_step["kind"] != "decision"
                    or field not in {entry["id"] for entry in decision_step["components"]}
                    or not set(branches.values()) <= targets):
                raise DataError(f"Country decision {step} and both branches must exist in process {process}.")
        if not isinstance(conditions, list) or any(not isinstance(c, str) or not c.strip() for c in conditions):
            raise DataError(f"Country decision {step} needs a list of remaining conditions.")
        result = {"step": step, "field": field, "branches": branches, "remaining_conditions": conditions}
        if "evidence" in decision:
            result["decision_evidence"] = _references(decision["evidence"], inputs)
        if "default" in decision:
            default = decision["default"]
            if (not isinstance(default, dict) or type(default.get("value")) is not bool
                    or type(default.get("applies_to_null", False)) is not bool):
                raise DataError(f"Country decision {step} needs a Boolean default and null policy.")
            references = _references(default.get("evidence"), inputs)
            field_paths = {entry["path"] for entry in definition["definitions"]}
            reference_paths = {entry["path"] for entry in references}
            if not reference_paths & field_paths or not reference_paths - field_paths:
                raise DataError(f"Country decision {step} needs evidence of the field default and where it is applied.")
            result["default"] = {"value": default["value"], "applies_to_null": default.get("applies_to_null", False),
                                 "evidence": references}
        decisions.append(result)
    return metadata_type, decisions


def _location(item: dict) -> dict:
    return {key: item[key] for key in ("path", "line", "locator", "rule") if key in item}


def _setting(decision: dict, declarations: list[dict], diagnostics: list[dict],
             evidence: dict, record_evidence: list[dict]) -> dict:
    setting: dict = {"state": "missing", "reason": "field_omitted", "value": None, "evidence": record_evidence}
    default = decision.get("default")
    if len(declarations) > 1:
        return setting | {"state": "unknown", "reason": "conflicting_values"}
    if declarations:
        declaration = declarations[0]
        qualifiers = declaration["qualifiers"]
        references = [_location(evidence[item]) | {"relationship": declaration["id"]}
                      for item in declaration["evidence_ids"]]
        if qualifiers.get("value_type") == "boolean" and type(qualifiers.get("value")) is bool:
            return {"state": "explicit", "reason": "declared_boolean", "value": qualifiers["value"],
                    "evidence": references}
        if qualifiers.get("value_type") != "null":
            return setting | {"state": "unknown", "reason": "non_boolean_value", "evidence": references}
        setting |= {"state": "unknown", "reason": "explicit_null", "evidence": references}
        if default is None or not default["applies_to_null"]:
            return setting
    else:
        api_name = decision["field"].rsplit(".", 1)[-1]
        names = [item.get("field") for item in diagnostics]
        if any(not isinstance(name, str) or name.casefold() == api_name for name in names):
            return setting | {"state": "unknown", "reason": "unresolved_value_declaration"}
    if default is not None:
        return setting | {"state": "default", "value": default["value"],
                          "evidence": [*setting["evidence"], *default["evidence"]]}
    return setting


def evaluate_country_process(bundle: dict, catalog: dict, overlay: dict, *, process: str, country: str) -> dict:
    """Resolve declared branch settings. Entry and business conditions remain untested."""
    manifest = bundle["manifest"]
    revision = manifest.get("source_revision")
    if (not isinstance(overlay, dict) or not revision or overlay.get("source_revision") != revision
            or catalog.get("source_revision") != revision or catalog.get("snapshot_id") != manifest["snapshot_id"]
            or manifest.get("dirty") is not False):
        raise DataError("Country rules require a clean snapshot matching the catalog and rule revision.")
    if not isinstance(country, str) or not re.fullmatch(r"[A-Za-z]{2}", country.strip()):
        raise DataError("Country evaluation requires a two-letter country code.")
    country = country.strip().upper()
    process = process.strip()
    facts = bundle["facts"]
    components = {item["id"]: item for item in facts["components"]}
    metadata_type, decisions = _decisions(overlay, catalog, process, manifest["inputs"], components)
    record_name = f"{metadata_type.removesuffix('__mdt')}.{country.casefold()}"
    record_id = component_id("custommetadatarecord", record_name)
    record = components.get(record_id)
    related = [item for item in facts["relationships"] if item["source"] == record_id]
    record_evidence = [_location(item) for item in record["definitions"]] if record else []
    evidence_paths = {item["path"] for item in record_evidence}
    diagnostics = [item for item in bundle.get("diagnostics", [])
                   if item["code"] in _VALUE_DIAGNOSTICS and (item.get("location") or {}).get("path") in evidence_paths]
    evidence = {item["id"]: item for item in facts["evidence"]}
    raw_record_name = record_name + CUSTOM_METADATA_SUFFIX
    record_missing = record is None and not any(
        path.rsplit("/", 1)[-1].casefold() == raw_record_name for path in manifest["inputs"])
    record_types = {item["target"] for item in related if item["relation"] == "instance_of"}
    record_verified = (record is not None and record.get("definition_status") == "defined"
                       and record_types == {component_id("object", metadata_type)})
    results = []
    for decision in decisions:
        declarations = [item for item in related if item["relation"] == "sets_value"
                        and item["target"] == decision["field"]]
        if record_verified:
            setting = _setting(decision, declarations, diagnostics, evidence, record_evidence)
        else:
            setting = {"state": "missing" if record_missing else "unknown",
                       "reason": "country_record_missing" if record_missing else "country_record_unresolved",
                       "value": None, "evidence": record_evidence}
        value = setting["value"]
        target = None if value is None else decision["branches"]["true" if value else "false"]
        result = {key: value for key, value in decision.items() if key != "default"}
        results.append(result | setting | {"configured_target": target})
    return {"source_revision": revision, "snapshot_id": manifest["snapshot_id"],
            "process": process, "country": country, "record": record_id if record else None,
            "decisions": results}
