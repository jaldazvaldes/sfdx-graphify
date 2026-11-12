"""Local Flow resource typing; no type is inferred from a resource's name."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
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
        self.loop_scopes: dict[str, set[str]] = {}
        self.supported_loops: set[str] = set()

    def add(self, name, object_type, collection, basis, node, *, output=False):
        if not name:
            self.store.diagnostic(
                "FLOW_RESOURCE_NAME_MISSING", "A Flow resource has no name.",
                self.document.location(node), state="unresolved",
            )
            return
        parts = name.split(".") if output else [name]
        if not all(RESOURCE_NAME.fullmatch(part) for part in parts):
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
        if symbol.basis == "loop_current_item" and _top_level_locator(node) not in self.loop_scopes.get(name.casefold(), ()):
            self.store.diagnostic(
                "FLOW_LOOP_ITEM_SCOPE_UNRESOLVED", "The reference is outside the loop's provable iteration path.",
                self.document.location(node), state="unresolved", symbol=name,
            )
            return None
        if require_record and not symbol.object_type:
            self.store.diagnostic(
                "FLOW_RECORD_TYPE_UNRESOLVED", "This resource does not establish a record object type.",
                self.document.location(node), state="unresolved", symbol=name,
            )
            return None
        return symbol

    def unique(self, name):
        entries = self.symbols.get(name.casefold(), [])
        if "." in name and self.unique(name.split(".", 1)[0]) is None:
            return None
        return entries[0] if len(entries) == 1 else None

    def type_resource(self, name, owner):
        symbol = self.unique(name)
        if symbol is not None:
            self.symbols[name.casefold()] = [replace(symbol, object_type=owner)]


def _top_level_locator(node):
    return "/".join(node.locator.split("/", 3)[:3])


def literal(node, tag):
    entries = node.all(tag)
    return entries[0].text if len(entries) == 1 and not entries[0].children else ""


def screen_fields(node):
    for field in node.all("fields"):
        yield field
        yield from screen_fields(field)


def _screen_symbols(root, table):
    for screen in root.all("screens"):
        for field in screen_fields(screen):
            name = literal(field, "name")
            if not name:
                continue
            field_type = literal(field, "fieldType")
            if field_type in {"InputField", "LargeTextArea", "PasswordField", "RadioButtons", "DropdownBox",
                              "MultiSelectCheckboxes", "MultiSelectPicklist"}:
                table.add(name, None, False, "screen_scalar_field", field)
            elif field_type == "ComponentInstance" and literal(field, "storeOutputAutomatically") == "true":
                table.add(name, None, False, "unsupported_screen_output", field)
                if literal(field, "extensionName") != "flowruntime:datatable":
                    continue
                owners = [literal(mapping, "typeValue") for mapping in field.all("dataTypeMappings")
                          if literal(mapping, "typeName") == "T"]
                if len(owners) == 1 and API_NAME.fullmatch(owners[0]):
                    for output, collection in (("editedRows", True), ("selectedRows", True), ("firstSelectedRow", False)):
                        table.add(f"{name}.{output}", owners[0], collection, "screen_data_table_output", field, output=True)


def _collection_types(root, table):
    dependents = {}
    for processor in root.all("collectionProcessors"):
        name = literal(processor, "name")
        if literal(processor, "collectionProcessorType") != "FilterCollectionProcessor":
            continue
        subtype = processor.all("elementSubtype")
        if subtype and literal(processor, "elementSubtype") != "FilterCollectionProcessor":
            continue
        table.add(name, None, True, "collection_filter_output", processor)
        source = literal(processor, "collectionReference").casefold()
        dependents.setdefault(source, []).append(name)
    pending = deque(source for source in dependents if _record_collection(table, source))
    while pending:
        source = pending.popleft()
        collection = _record_collection(table, source)
        if collection is None:
            continue
        for name in dependents.pop(source, ()):
            table.type_resource(name, collection.object_type)
            if name.casefold() in dependents:
                pending.append(name.casefold())


def _record_collection(table, name):
    symbol = table.unique(name)
    return symbol if symbol and symbol.collection and symbol.object_type else None


def _connector_target(node, tag):
    connectors = node.all(tag)
    return literal(connectors[0], "targetReference").casefold() if len(connectors) == 1 else ""


def _connector_targets(node):
    """Retain possible entrances even in unmodeled or repeated connector forms."""
    pending = list(node.children)
    while pending:
        child = pending.pop()
        if child.tag == "targetReference" and not child.children and child.text:
            yield child.text.casefold()
        else:
            pending.extend(child.children)


def _loop_paths(root):
    """Use the explicit iteration connector and exclude paths also entered from outside."""
    loops = root.all("loops")
    if not loops:
        return {}
    nodes = {}
    duplicates = set()
    entry = {literal(root, "startElementReference").casefold()}
    for node in root.children:
        name = literal(node, "name").casefold()
        if not name:
            entry.update(_connector_targets(node))
            continue
        if name in nodes:
            duplicates.add(name)
            entry.update(_connector_targets(nodes[name]))
            entry.update(_connector_targets(node))
        nodes[name] = node
    for name in duplicates:
        nodes.pop(name)

    edges, definite, incoming = {}, {}, {}
    for name, node in nodes.items():
        edges[name] = set(_connector_targets(node)) & nodes.keys()
        known = {_connector_target(node, tag) for tag in
                 ("connector", "defaultConnector", "faultConnector", "nextValueConnector", "noMoreValuesConnector")}
        known.update(_connector_target(rule, "connector") for rule in node.all("rules"))
        definite[name] = known & nodes.keys()
        for target in edges[name]:
            incoming.setdefault(target, set()).add(name)
    # Entry connectors on a named start node still bypass loop iteration.
    start = root.child("start")
    if start is not None:
        entry.update(_connector_targets(start))

    def reachable(graph, pending, blocked):
        found = set()
        stack = list(pending)
        while stack:
            name = stack.pop()
            if name not in graph or name in blocked or name in found:
                continue
            found.add(name)
            stack.extend(graph[name])
        return found

    scopes = {}
    for loop in loops:
        name = literal(loop, "name").casefold()
        begin = _connector_target(loop, "nextValueConnector")
        end = _connector_target(loop, "noMoreValuesConnector")
        if name not in nodes or not begin or (loop.all("noMoreValuesConnector") and not end):
            continue
        body = reachable(definite, [begin], {name, end})
        alternate_entries = {target for target in body if incoming.get(target, set()) - body - {name}}
        outside = reachable(edges, alternate_entries | entry | {end}, {name})
        scopes[loop.locator] = {nodes[target].locator for target in body - outside}
    return scopes


def _loop_symbols(root, table):
    scopes = _loop_paths(root)
    loops = root.all("loops")
    for loop in loops:
        name = literal(loop, "name")
        collection = _record_collection(table, literal(loop, "collectionReference"))
        explicit = loop.all("assignNextValueToReference")
        if explicit:
            item = table.unique(literal(loop, "assignNextValueToReference"))
            if (collection is not None and collection.object_type and item is not None and item.object_type
                    and item.basis == "declared_variable" and not item.collection
                    and item.object_type.casefold() == collection.object_type.casefold()):
                table.supported_loops.add(loop.locator)
        elif table.unique(name) is not None and scopes.get(loop.locator):
            table.loop_scopes[name.casefold()] = scopes[loop.locator]
            if collection is not None:
                table.type_resource(name, collection.object_type)
                table.supported_loops.add(loop.locator)


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
    for transform in root.all("transforms"):
        owner = literal(transform, "objectType") if literal(transform, "dataType") == "SObject" else ""
        table.add(literal(transform, "name"), owner, literal(transform, "isCollection") == "true",
                  "transform_output", transform)
    for loop in root.all("loops"):
        if not loop.all("assignNextValueToReference"):
            table.add(literal(loop, "name"), None, False, "loop_current_item", loop)
    _screen_symbols(root, table)
    _collection_types(root, table)
    _loop_symbols(root, table)
    return table
