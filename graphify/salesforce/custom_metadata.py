"""Extract CustomMetadata record identities, declared values and exact text hints."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from pathlib import PurePosixPath

from .schema import API_NAME, DataError, FactStore, component_id
from .xml_source import XML_SCHEMA_INSTANCE_NS, Document, Node


CUSTOM_METADATA_SUFFIX = ".md-meta.xml"
_XML_SCHEMA_NS = "http://www.w3.org/2001/XMLSchema"
_XSI_TYPE = f"{XML_SCHEMA_INSTANCE_NS}|type"
_XSI_NIL = f"{XML_SCHEMA_INSTANCE_NS}|nil"
_BOOLEANS = {"true": True, "1": True, "false": False, "0": False}
_INTEGER = re.compile(r"[+-]?[0-9]+\Z")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?\Z")
_XML_TYPES = {
    "boolean": "boolean", "int": "number", "double": "number",
    "string": "string", "picklist": "string", "date": "string", "dateTime": "string",
}
_LOCAL_TYPES = {
    "checkbox": "boolean", "number": "number", "percent": "number", "currency": "number",
    **dict.fromkeys(("text", "textarea", "longtextarea", "phone", "url", "email", "picklist",
                     "metadatarelationship", "date", "datetime"), "string"),
}
_MENTION_KINDS = frozenset({"apexclass", "flow", "field", "object"})


def _record_documents(documents, store):
    for document in documents:
        path = PurePosixPath(document.path)
        if not path.name.casefold().endswith(CUSTOM_METADATA_SUFFIX):
            continue
        name = path.name[:-len(CUSTOM_METADATA_SUFFIX)]
        type_name, _, record_name = name.partition(".")
        declared = document.root.all("fullName")
        if (path.parent.name.casefold() != "custommetadata" or document.root.tag != "CustomMetadata"
                or not API_NAME.fullmatch(type_name) or type_name.casefold().endswith("__mdt")
                or not API_NAME.fullmatch(record_name)
                or (declared and (len(declared) != 1 or declared[0].children
                                  or declared[0].text.casefold() != name.casefold()))):
            store.diagnostic(
                "CUSTOM_METADATA_IDENTITY_UNRESOLVED",
                "Expected a matching CustomMetadata root and a Type.Record filename.",
                document.location(document.root), state="unresolved",
            )
            continue
        yield document, name, f"{type_name}__mdt"


def index_custom_metadata(documents: list[Document], store: FactStore) -> None:
    """Index records before resolving references to them."""
    for document, name, _ in _record_documents(documents, store):
        store.ensure_component("custommetadatarecord", name, document.location(document.root))


def _value_fields(document, store):
    fields: dict[str, list[tuple[str, Node]]] = defaultdict(list)
    complete = True
    for row in document.root.all("values"):
        names = row.all("field")
        if (len(names) != 1 or names[0].children or not API_NAME.fullmatch(names[0].text)
                or not names[0].text.casefold().endswith("__c")):
            store.diagnostic(
                "CUSTOM_METADATA_FIELD_UNRESOLVED",
                "Expected one unqualified custom field API name.",
                document.location(names[0] if names else row), state="unresolved",
            )
            complete = False
            continue
        name = names[0].text
        fields[name.casefold()].append((name, row))
    return fields, complete


def _explicit_values(document, store, fields):
    for declarations in fields.values():
        name, row = declarations[0]
        if len(declarations) != 1:
            store.diagnostic(
                "CUSTOM_METADATA_VALUE_DUPLICATE",
                "A record declares more than one value row for the same field.",
                document.location(row), state="ambiguous", field=name,
            )
            continue
        nodes = row.all("value")
        if len(nodes) != 1 or nodes[0].children:
            store.diagnostic(
                "CUSTOM_METADATA_VALUE_INVALID",
                "A custom metadata value row needs one scalar value element.",
                document.location(nodes[0] if nodes else row), state="unresolved", field=name,
            )
            continue
        yield name, nodes[0]


def _scalar(node, local_type):
    attributes = node.attributes or {}
    lexical_text = node.text if node.lexical_text is None else node.lexical_text
    proof = {"basis": "custom_metadata_xml_value"}
    nil = attributes.get(_XSI_NIL)
    if nil is not None:
        nil = nil.strip()
        if nil not in _BOOLEANS:
            raise DataError("Invalid xsi:nil boolean.")
        if _BOOLEANS[nil]:
            if node.text:
                raise DataError("An explicit nil value contains text.")
            return "null", None, {**proof, "source_type": "explicit_xsi_nil"}

    xml_type = attributes.get(_XSI_TYPE)
    local_family = _LOCAL_TYPES.get((local_type or "").casefold())
    if xml_type is not None:
        prefix, separator, local_name = xml_type.strip().partition(":")
        xml_type = local_name if separator else prefix
        value_type = _XML_TYPES.get(xml_type) if prefix else None
        if node.xsi_type_namespace != _XML_SCHEMA_NS or value_type is None:
            raise DataError("Unsupported xsi:type for a custom metadata scalar.")
        if local_family is not None and value_type != local_family:
            raise DataError("The declared xsi:type conflicts with the local field type.")
        proof.update(source_type="explicit_xsi_type", xml_type=xml_type)
    else:
        value_type = local_family or "string"
        proof["source_type"] = "field_definition" if local_family is not None else "untyped_literal"
    if value_type == "string":
        return value_type, lexical_text, proof
    proof["lexical_value"] = lexical_text
    if value_type == "boolean":
        if node.text not in _BOOLEANS:
            raise DataError("Invalid custom metadata boolean.")
        return value_type, _BOOLEANS[node.text], proof
    if xml_type == "int" or (xml_type is None and _INTEGER.fullmatch(node.text)):
        if not _INTEGER.fullmatch(node.text):
            raise DataError("Invalid custom metadata integer.")
        try:
            return value_type, int(node.text), proof
        except ValueError as exc:
            raise DataError("Custom metadata integer exceeds the supported length.") from exc
    if not _NUMBER.fullmatch(node.text):
        raise DataError("Invalid custom metadata number.")
    number = float(node.text)
    if not math.isfinite(number):
        raise DataError("Custom metadata number must be finite.")
    return value_type, number, proof



def _mention_index(store):
    by_name: dict[str, list[dict]] = defaultdict(list)
    for component in store.components.values():
        if component["kind"] in _MENTION_KINDS and component["definition_status"] != "referenced_only":
            by_name[component["api_name"]].append(component)
    return by_name


def _add_mention(store, source, location, candidates):
    if not candidates:
        return
    if len(candidates) != 1 or candidates[0]["definition_status"] != "defined":
        store.diagnostic(
            "CUSTOM_METADATA_MENTION_AMBIGUOUS",
            "The exact text matches more than one local component definition.",
            location, state="ambiguous", candidates=sorted(component["id"] for component in candidates),
        )
        return
    store.relation(
        source, candidates[0]["id"], "mentions", location, "custom_metadata.exact_text_mention",
        resolution={"basis": "exact_text_mention"},
    )


def _declared_component(store, kind, name, location, construct):
    cid = component_id(kind, name)
    component = store.components.get(cid)
    status = component["definition_status"] if component is not None else "referenced_only"
    if status == "ambiguous":
        store.diagnostic(
            f"CUSTOM_METADATA_{construct}_AMBIGUOUS",
            "The declared component matches multiple local definitions.",
            location, state="ambiguous", candidates=[cid],
        )
        return None
    if status == "referenced_only":
        store.diagnostic(
            f"CUSTOM_METADATA_{construct}_UNDEFINED",
            "The declared component has no definition in the local source.",
            location, state="unresolved", target_component=cid,
        )
    return store.resolve_component(kind, name, location)


def extract_custom_metadata(documents: list[Document], store: FactStore) -> None:
    """Emit declared local settings and bounded, inferred text matches."""
    if not documents:
        return
    mentions = _mention_index(store)
    for document, name, owner in _record_documents(documents, store):
        root_location = document.location(document.root)
        source = component_id("custommetadatarecord", name)
        if store.is_ambiguous(source):
            store.diagnostic(
                "CUSTOM_METADATA_RECORD_AMBIGUOUS",
                "Multiple local files define the same custom metadata record.",
                root_location, state="ambiguous", candidates=[source],
            )
            continue
        owner_id = _declared_component(store, "object", owner, root_location, "TYPE")
        if owner_id is None:
            continue
        store.relation(
            source, owner_id, "instance_of", root_location, "custom_metadata.type",
            resolution={"basis": "custom_metadata_record_filename"},
        )
        fields, complete = _value_fields(document, store)
        store.components[source]["custom_metadata_field_inventory"] = {
            "fields": sorted(fields), "complete": complete,
        }
        for field_name, node in _explicit_values(document, store, fields):
            location = document.location(node)
            qualified = f"{owner}.{field_name}"
            target_id = _declared_component(store, "field", qualified, location, "FIELD")
            if target_id is None:
                continue
            try:
                value_type, value, proof = _scalar(node, store.components[target_id].get("data_type"))
            except DataError as exc:
                store.diagnostic("CUSTOM_METADATA_VALUE_INVALID", str(exc), location, state="unresolved")
                continue
            if proof["source_type"] == "untyped_literal":
                store.diagnostic(
                    "CUSTOM_METADATA_VALUE_UNTYPED",
                    "The XML provides no scalar type and the local field type is unavailable or unsupported.",
                    location, state="unresolved",
                )
            store.relation(
                source, target_id, "sets_value", location, "custom_metadata.value", resolution=proof,
                qualifiers={"value_type": value_type, "value": value},
            )
            if isinstance(value, str) and proof["source_type"] != "untyped_literal":
                _add_mention(store, source, location, mentions.get(value))


def index_custom_metadata_defaults(documents: list[Document], store: FactStore) -> None:
    """Keep explicitly declared checkbox defaults and their XML locations."""
    from .inventory import identity

    for document in documents:
        if document.root.tag != "CustomField" or document.root.value("type").casefold() != "checkbox":
            continue
        kind, name = identity(document)
        if not name.split(".", 1)[0].casefold().endswith("__mdt"):
            continue
        component = store.components[component_id(kind, name)]
        defaults = document.root.all("defaultValue")
        if (component["definition_status"] != "defined" or len(defaults) != 1
                or defaults[0].children or defaults[0].text not in _BOOLEANS
                or document.root.all("defaultValueFormula")):
            continue
        component["declared_default"] = {"value_type": "boolean", "value": _BOOLEANS[defaults[0].text],
                                         "location": document.location(defaults[0])}


def field_default_summary(field, relationships, components):
    default = field.get("declared_default")
    if default is None:
        return {}
    owner, name = field["api_name"].casefold().split(".", 1)
    owner_id = component_id("object", owner)
    record_ids = set()
    explicit_ids = set()
    for edge in relationships:
        if edge["relation"] == "instance_of" and edge["target"] == owner_id:
            record_ids.add(edge["source"])
        elif edge["relation"] == "sets_value" and edge["target"] == field["id"]:
            explicit_ids.add(edge["source"])
    omitted = unresolved = 0
    for record_id in record_ids - explicit_ids:
        inventory = components[record_id].get("custom_metadata_field_inventory", {})
        if inventory.get("complete") is True and name not in inventory["fields"]:
            omitted += 1
        else:
            unresolved += 1
    return {"declared_default": default, "omitted_record_count": omitted, "unresolved_record_count": unresolved}

