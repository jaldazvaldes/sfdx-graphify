"""Index decomposed metadata definitions before resolving any Flow references."""

from __future__ import annotations

from pathlib import PurePosixPath

from .field_dependencies import FIELD_DEPENDENCY_TAGS, extract_field_dependencies
from .schema import API_NAME, DataError, FactStore, canonical_json
from .xml_source import Document

SUFFIXES = {".flow-meta.xml": ("flow", "Flow"),
            ".object-meta.xml": ("object", "CustomObject"),
            ".field-meta.xml": ("field", "CustomField"),
            ".validationRule-meta.xml": ("validationrule", "ValidationRule"),
            ".recordType-meta.xml": ("recordtype", "RecordType")}
_CHILD_DIRECTORIES = {"field": "fields", "validationrule": "validationRules", "recordtype": "recordTypes"}


def identity(document):
    path = PurePosixPath(document.path)
    for suffix, (kind, root_tag) in SUFFIXES.items():
        if path.name.casefold().endswith(suffix.casefold()):
            if document.root.tag != root_tag:
                raise DataError(f"Expected {root_tag} root: {document.path}")
            name = path.name[:-len(suffix)]
            declared = document.root.value("fullName")
            if declared and declared != name:
                raise DataError(f"Metadata fullName and filename differ: {document.path}")
            if kind in _CHILD_DIRECTORIES:
                if path.parent.name != _CHILD_DIRECTORIES[kind] or path.parent.parent.parent.name != "objects":
                    raise DataError(f"{root_tag} is not in decomposed Salesforce DX format: {document.path}")
                if kind in {"validationrule", "recordtype"} and len(document.root.all("fullName")) > 1:
                    raise DataError(f"{root_tag} has multiple fullName elements: {document.path}")
                if kind == "recordtype" and not all(API_NAME.fullmatch(part) for part in (path.parent.parent.name, name)):
                    raise DataError(f"Invalid RecordType identity: {document.path}")
                name = f"{path.parent.parent.name}.{name}"
            return kind, name
    raise DataError(f"Unsupported metadata document: {document.path}")


def _record_type_owners(definitions, store):
    """Keep one unambiguous definition per owner as schema evidence."""
    owners = {}
    for name, documents in sorted(definitions.items()):
        if len(documents) != 1:
            for document in documents:
                store.diagnostic("RECORD_TYPE_METADATA_AMBIGUOUS", "Multiple local definitions have this RecordType name.",
                                 document.location(document.root), state="ambiguous")
            continue
        document = documents[0]
        statuses = document.root.all("active")
        if len(statuses) != 1 or statuses[0].children or statuses[0].text not in {"true", "false", "1", "0"}:
            store.diagnostic("RECORD_TYPE_METADATA_UNRESOLVED", "A unique local active boolean is required in RecordType metadata.",
                             document.location(document.root), state="unresolved")
            continue
        owners.setdefault(name.split(".", 1)[0], document.location(document.root))
    return owners


def index_definitions(documents: list[Document], store: FactStore):
    record_types = {}
    for document in documents:
        kind, name = identity(document)
        if kind == "recordtype":
            record_types.setdefault(name.casefold(), []).append(document)
            continue
        attrs = {}
        if kind == "field":
            attrs["data_type"] = document.root.value("type") or None
        if kind == "object":
            attrs["custom_settings_type"] = document.root.value("customSettingsType") or None
        if kind == "validationrule":
            from .validation_rules import definition_metadata
            attrs.update(definition_metadata(document, store))
            attrs["owner"] = name.split(".", 1)[0]
        if kind == "flow":
            start = document.root.child("start")
            statuses = document.root.all("status")
            local_status = statuses[0].text.strip() or "Unknown" if len(statuses) == 1 else "Unknown"
            if len(statuses) > 1:
                for status in statuses:
                    store.diagnostic("FLOW_STATUS_AMBIGUOUS", "Multiple local status elements prevent activation classification.",
                                     document.location(status), state="ambiguous",
                                     candidates=[item.text.strip() for item in statuses])
            attrs.update({
                "local_status": local_status,
                "process_type": document.root.value("processType") or None,
                "api_version": document.root.value("apiVersion") or None,
                "trigger_type": start.value("triggerType") if start is not None else None,
                "record_trigger_type": start.value("recordTriggerType") if start is not None else None,
                "trigger_object": start.value("object") if start is not None else None,
            })
        location = document.location(document.root)
        cid = store.ensure_component(kind, name, location, **attrs)
        if kind in {"flow", "validationrule"}:
            metadata = {"location": location, **attrs}
            component = store.components[cid]
            definitions = component.setdefault("metadata_by_definition", [])
            if metadata not in definitions:
                definitions.append(metadata)
                definitions.sort(key=canonical_json)
    store.record_type_definitions = _record_type_owners(record_types, store)
    for component in list(store.components.values()):
        if component["definition_status"] == "ambiguous":
            if component["kind"] == "object":
                component["custom_settings_type"] = None
            if component["kind"] == "validationrule":
                component["local_status"] = None
            if component["kind"] == "flow":
                # Metadata belongs to a definition; never prefer one conflicting package.
                for key in ("local_status", "process_type", "api_version", "trigger_type",
                            "record_trigger_type", "trigger_object"):
                    component[key] = None
            for definition in component["definitions"]:
                store.diagnostic("duplicate_definition", "Multiple local definitions have this API name.",
                                 definition, state="ambiguous", candidates=[component["id"]])


_FIELD_DESCRIPTIVE = {
    "fullName", "label", "description", "inlineHelpText", "type", "length", "precision", "scale",
    "required", "unique", "externalId", "caseSensitive", "trackHistory", "trackTrending",
    "trackFeedHistory", "defaultValue", "defaultValueFormula", "formula", "formulaTreatBlanksAs",
    "referenceTo", "relationshipName", "relationshipLabel", "deleteConstraint", "reparentableMasterDetail",
    "relationshipOrder", "writeRequiresMasterRead", "valueSet", "visibleLines", "displayFormat",
    "startingNumber", "maskChar", "maskType", "securityClassification", "businessOwnerGroup",
    "businessOwnerUser", "businessStatus", "complianceGroup", "deprecated", "isAIPredictionField",
}
_OBJECT_DESCRIPTIVE = {
    "fullName", "label", "pluralLabel", "description", "deploymentStatus", "sharingModel",
    "externalSharingModel", "enableActivities", "enableBulkApi", "enableFeeds", "enableHistory",
    "enableReports", "enableSearch", "enableSharing", "enableStreamingApi", "enableLicensing",
    "nameField", "visibility", "customSettingsType", "gender", "startsWith", "compactLayoutAssignment",
    "searchLayouts", "actionOverrides", "enableEnhancedLookup", "allowInChatterGroups",
}


def extract_inventory(documents: list[Document], store: FactStore):
    field_expressions = []
    lookup_targets: dict[str, set[str]] = {}
    for document in documents:
        kind, name = identity(document)
        if kind in {"flow", "validationrule", "recordtype"}:
            continue
        root = document.root
        source = store.resolve_component(kind, name, document.location(root))
        if source is None:
            continue
        if kind == "field":
            owner = name.split(".")[0]
            if (root.value("type").casefold() == "summary"
                    or any(child.tag in FIELD_DEPENDENCY_TAGS for child in root.children)):
                field_expressions.append((document, source, owner))
            owner_id = store.resolve_component("object", owner, document.location(root))
            if owner_id:
                store.relation(owner_id, source, "contains", document.location(root), "metadata.field_owner")
            for reference in root.all("referenceTo"):
                lookup_targets.setdefault(source, set()).add(reference.text.casefold())
                target = store.resolve_component("object", reference.text, document.location(reference))
                if target:
                    store.relation(source, target, "references", document.location(reference),
                                   "metadata.lookup_target", qualifiers={"context": "lookup_declaration"})
        for child in root.children:
            if kind == "field" and child.tag in FIELD_DEPENDENCY_TAGS:
                continue
            if child.tag in {"formula", "defaultValue", "defaultValueFormula"}:
                # Salesforce defaultValue may be a formula; never scan it as dotted text.
                store.diagnostic("unsupported_field_expression", "Field expressions are not analyzed.",
                                 document.location(child))
            elif child.tag not in (_FIELD_DESCRIPTIVE if kind == "field" else _OBJECT_DESCRIPTIVE):
                store.diagnostic("unsupported_metadata_construct", "Metadata construct is outside v0.1 coverage.",
                                 document.location(child), construct=child.tag)
            elif kind == "object" and child.tag in {"searchLayouts", "actionOverrides", "compactLayoutAssignment"}:
                store.diagnostic("unsupported_object_reference", "Object UI references are not analyzed.",
                                 document.location(child), construct=child.tag)
            elif kind == "field" and child.tag == "valueSet":
                store.diagnostic("unsupported_value_set", "Value set dependencies are not analyzed.",
                                 document.location(child), construct=child.tag)
    # Cross-object formulas need all lookup declarations, including later packages.
    for document, source, owner in field_expressions:
        extract_field_dependencies(document, store, source, owner, lookup_targets)
