"""Index decomposed metadata definitions before resolving any Flow references."""

from __future__ import annotations

from pathlib import PurePosixPath

from .schema import DataError, FactStore, canonical_json
from .xml_source import Document

SUFFIXES = {".flow-meta.xml": ("flow", "Flow"),
            ".object-meta.xml": ("object", "CustomObject"),
            ".field-meta.xml": ("field", "CustomField"),
            ".validationRule-meta.xml": ("validationrule", "ValidationRule")}


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
            if kind in {"field", "validationrule"}:
                directory = "fields" if kind == "field" else "validationRules"
                if path.parent.name != directory or path.parent.parent.parent.name != "objects":
                    raise DataError(f"{root_tag} is not in decomposed Salesforce DX format: {document.path}")
                if kind == "validationrule" and len(document.root.all("fullName")) > 1:
                    raise DataError(f"ValidationRule has multiple fullName elements: {document.path}")
                name = f"{path.parent.parent.name}.{name}"
            return kind, name
    raise DataError(f"Unsupported metadata document: {document.path}")


def index_definitions(documents: list[Document], store: FactStore):
    for document in documents:
        kind, name = identity(document)
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
    for document in documents:
        kind, name = identity(document)
        if kind in {"flow", "validationrule"}:
            continue
        root = document.root
        source = store.resolve_component(kind, name, document.location(root))
        if source is None:
            continue
        if kind == "field":
            owner = name.split(".")[0]
            owner_id = store.resolve_component("object", owner, document.location(root))
            if owner_id:
                store.relation(owner_id, source, "contains", document.location(root), "metadata.field_owner")
            for reference in root.all("referenceTo"):
                target = store.resolve_component("object", reference.text, document.location(reference))
                if target:
                    store.relation(source, target, "references", document.location(reference),
                                   "metadata.lookup_target", qualifiers={"context": "lookup_declaration"})
        for child in root.children:
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
