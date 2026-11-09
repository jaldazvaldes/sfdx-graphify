"""Local Flow resource typing; no type is inferred from a resource's name."""

from __future__ import annotations

from dataclasses import dataclass
import re


API_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
RESOURCE_NAME = re.compile(r"\$?[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True)
class Symbol:
    name: str
    object_type: str | None
    collection: bool
    basis: str
    location: dict


class SymbolTable:
    """Retain duplicate declarations so resolution cannot choose a first match."""

    def __init__(self, document, store):
        self.document = document
        self.store = store
        self.symbols: dict[str, list[Symbol]] = {}

    def add(self, name, object_type, collection, basis, node):
        if not name:
            self.store.diagnostic(
                "FLOW_RESOURCE_NAME_MISSING", "A Flow resource has no name.",
                self.document.location(node), state="unresolved",
            )
            return
        if not RESOURCE_NAME.fullmatch(name):
            self.store.diagnostic(
                "FLOW_RESOURCE_NAME_INVALID", "The resource name is not a literal API name.",
                self.document.location(node), state="dynamic",
            )
            return
        if object_type and not API_NAME.fullmatch(object_type):
            self.store.diagnostic(
                "FLOW_OBJECT_TYPE_DYNAMIC", "The record resource has a nonliteral object type.",
                self.document.location(node), state="dynamic",
            )
            object_type = None
        symbol = Symbol(name, object_type or None, collection, basis, self.document.location(node))
        entries = self.symbols.setdefault(name.casefold(), [])
        entries.append(symbol)
        if len(entries) > 1:
            self.store.diagnostic(
                "FLOW_SYMBOL_AMBIGUOUS", "Multiple declarations have the same Flow resource name.",
                self.document.location(node), state="ambiguous", symbol=name,
                candidates=sorted({item.object_type for item in entries if item.object_type}),
            )

    def resolve(self, name, node, *, require_record=False):
        entries = self.symbols.get(name.casefold(), [])
        if len(entries) > 1:
            self.store.diagnostic(
                "FLOW_SYMBOL_AMBIGUOUS", "The reference has multiple resource declarations.",
                self.document.location(node), state="ambiguous", symbol=name,
                candidates=sorted({item.object_type for item in entries if item.object_type}),
            )
            return None
        if not entries:
            self.store.diagnostic(
                "FLOW_SYMBOL_UNRESOLVED", "The reference has no supported resource declaration.",
                self.document.location(node), state="unresolved", symbol=name,
            )
            return None
        symbol = entries[0]
        if require_record and not symbol.object_type:
            self.store.diagnostic(
                "FLOW_RECORD_TYPE_UNRESOLVED", "This resource does not establish a record object type.",
                self.document.location(node), state="unresolved", symbol=name,
            )
            return None
        return symbol


def index_symbols(document, store):
    """Index declarations and explicitly automatic lookup outputs in two passes."""
    root = document.root
    table = SymbolTable(document, store)
    start = root.child("start")
    if start is not None and start.value("object"):
        for name in ("$Record", "$Record__Prior"):
            table.add(name, start.value("object"), False, "record_trigger_context", start)

    for variable in root.all("variables"):
        declared_type = variable.value("dataType")
        owner = variable.value("objectType")
        if declared_type == "SObject" and not owner:
            store.diagnostic(
                "FLOW_RECORD_TYPE_UNRESOLVED", "The record variable does not declare an owning object type.",
                document.location(variable), state="unresolved",
            )
        if declared_type and declared_type not in {"SObject", "String", "Boolean", "Number", "Currency", "Date", "DateTime", "Picklist", "Multipicklist"}:
            store.diagnostic(
                "FLOW_VARIABLE_TYPE_UNSUPPORTED", "This resource data type is outside v0.1 typing coverage.",
                document.location(variable),
            )
        if owner and declared_type not in ("SObject", ""):
            store.diagnostic(
                "FLOW_VARIABLE_TYPE_CONFLICT", "Object type conflicts with the declared resource data type.",
                document.location(variable), state="ambiguous",
            )
            owner = None
        table.add(variable.value("name"), owner, variable.value("isCollection") == "true",
                  "declared_variable", variable)

    for tag in ("constants", "formulas", "textTemplates", "choices", "dynamicChoiceSets"):
        for resource in root.all(tag):
            table.add(resource.value("name"), None, False, tag, resource)

    for lookup in root.all("recordLookups"):
        if lookup.value("storeOutputAutomatically") == "true":
            table.add(lookup.value("name"), lookup.value("object"),
                      lookup.value("getFirstRecordOnly") != "true",
                      "automatic_get_records_output", lookup)

    for tag in ("actionCalls", "subflows"):
        for call in root.all(tag):
            if call.value("storeOutputAutomatically") == "true":
                table.add(call.value("name"), None, False, "unsupported_call_output", call)
                store.diagnostic(
                    "FLOW_CALL_OUTPUT_TYPING_UNSUPPORTED", "Automatic call output typing is outside v0.1 coverage.",
                    document.location(call),
                )
    for create in root.all("recordCreates"):
        if create.value("storeOutputAutomatically") == "true":
            table.add(create.value("name"), None, False, "unsupported_create_output", create)
            store.diagnostic(
                "FLOW_CREATE_OUTPUT_TYPING_UNSUPPORTED", "Automatic Create Records output typing is outside v0.1 coverage.",
                document.location(create),
            )
    return table
