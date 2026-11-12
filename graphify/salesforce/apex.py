"""Bounded Apex/SOQL dependencies from a pinned, local Tree-sitter grammar.

Grammar: aheber/tree-sitter-sfapex, distributed by tree-sitter-language-pack
0.11.0. This module never runs Apex, resolves packages remotely, or evaluates
query strings. Syntax evidence does not establish compilation or org behavior.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import math
import re
from pathlib import PurePosixPath
from urllib.parse import quote

from tree_sitter import Node, Tree

from .schema import API_NAME, DataError, FactStore, component_id
from .relationship_paths import add_navigation_reads, resolve_source_field_path


PARSER_VERSION = "tree-sitter-language-pack==0.11.0/apex"
_PRIMITIVES = frozenset("blob boolean date datetime decimal double id integer long object sobject string time void aggregateresult exception".split())
# A bounded standard type vocabulary, not a complete Salesforce schema.
_STANDARD_OBJECTS = frozenset("account contact lead opportunity case task event user group order orderitem asset campaign campaignmember product2 pricebook2 pricebookentry contract quote quotelineitem contentversion contentdocument contentdocumentlink recordtype".split())
_CLASS_LITERAL_STANDARD_OBJECTS = _STANDARD_OBJECTS | {"asyncapexjob"}
_DML = frozenset({"insert", "update", "upsert", "delete", "undelete"})
_DYNAMIC_SOQL = frozenset({"query", "querywithbinds", "countquery", "countquerywithbinds", "getquerylocator", "getquerylocatorwithbinds"})
_COMMENTS = frozenset({"line_comment", "block_comment"})
_IGNORED = _COMMENTS | {"string_literal", "multi_line_string_literal"}
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_NODES = 200_000
_RESERVED_RECEIVERS = frozenset({"trigger", "system", "schema", "database", "label", "page", "apexpages"})
_BUILTIN_COLLECTION_ARITY = {"list": 1, "set": 1, "map": 2}
# https://developer.salesforce.com/docs/platform/apex-dev/references/apex-dev/langcon-apex-primitives.html
_STRING_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", "s": " ",
                   "'": "'", '"': '"', "\\": "\\"}
_MAX_ASSIGNED_STRING_LENGTH = 1024
_NUMERIC_LITERALS = {"int", "decimal_floating_point_literal"}
# https://developer.salesforce.com/docs/platform/salesforce-soql-sosl/guide/sforce-api-calls-soql-relationships-understanding.html
_STANDARD_CHILDREN = {
    ("account", "assets"): ("Asset", "Account"),
    ("account", "cases"): ("Case", "Account"),
    ("account", "contacts"): ("Contact", "Account"),
}
# API name, job argument position, accepted arities, and the job interface.
_ASYNC_APIS = {
    ("system", "enqueuejob"): ("System.enqueueJob", 0, {1, 2}, "Queueable"),
    ("system", "schedule"): ("System.schedule", 2, {3}, "Schedulable"),
    ("database", "executebatch"): ("Database.executeBatch", 0, {1, 2}, "Batchable"),
}
_ASYNC_ENTRIES = {
    "Queueable": ("System", (("execute", 1),)),
    "Schedulable": ("System", (("execute", 1),)),
    "Batchable": ("Database", (("start", 1), ("execute", 2), ("finish", 1))),
}


def _parse_source(parser, data):
    """Work around the grammar treating Trigger as a declaration at statement start.

    Only code tokens followed by a dot are rewritten; comments/string literals
    stay untouched. The equal-length spelling preserves all original offsets.
    Unit.text/location/hash always use the original bytes, never normalized text.
    """
    tree = parser.parse(data)
    if not tree.root_node.has_error:
        return tree, False
    normalized = bytearray(data)
    token = re.compile(rb"//[^\r\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\btrigger\b(?=\s*\.)", re.IGNORECASE)
    changed = False
    for match in token.finditer(data):
        if match.group().lower() == b"trigger":
            normalized[match.start():match.end()] = b"Trigg_r"
            changed = True
    if changed:
        recovered = parser.parse(bytes(normalized))
        if not recovered.root_node.has_error:
            return recovered, True
    return tree, False


def _walk(node):
    """Iterate without recursion, excluding comments and string contents."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if current.type not in _IGNORED:
            stack.extend(reversed(current.named_children))


def _child(node, kind):
    return next((child for child in node.named_children if child.type == kind), None)


def _significant(node):
    """Comments are parser extras, never arguments, types, or expressions."""
    return [child for child in node.named_children if child.type not in _COMMENTS] if node is not None else []


def _without_parentheses(node):
    while node is not None and node.type == "parenthesized_expression":
        children = _significant(node)
        node = children[0] if len(children) == 1 else None
    return node


def _assigned_literal(data, target):
    """Read a scalar only from the explicit right operand of a field write."""
    parent = target.parent
    if parent is not None and parent.type == "field_access" and parent.child_by_field_name("field") == target:
        target, parent = parent, parent.parent
    if (parent is None or parent.type != "assignment_expression" or parent.child_by_field_name("left") != target
            or _text(data, parent.child_by_field_name("operator")) != "="):
        return None
    operand = _without_parentheses(parent.child_by_field_name("right"))
    if operand is None:
        return None
    value = operand
    raw = _text(data, value)
    if value.type == "string_literal":
        if len(raw) > 2 * _MAX_ASSIGNED_STRING_LENGTH + 2:
            return None
        try:
            decoded = re.sub(r"\\(.)", lambda match: _STRING_ESCAPES[match[1]], raw[1:-1])
        except KeyError:
            return None
        return (operand, "string", decoded) if len(decoded) <= _MAX_ASSIGNED_STRING_LENGTH else None
    if value.type == "boolean":
        return operand, "boolean", raw.casefold() == "true"
    if value.type == "null_literal":
        return operand, "null", None
    sign = ""
    if value.type == "unary_expression":
        sign = _text(data, value.child_by_field_name("operator"))
        if sign not in {"-", "+"}:
            return None
        value = _without_parentheses(value.child_by_field_name("operand"))
        raw = _text(data, value)
    if value is None or value.type not in _NUMERIC_LITERALS:
        return None
    spelling = sign + raw.rstrip("lLdDfF")
    if len(spelling) > 128:
        return None
    try:
        if value.type == "int":
            number = int(spelling)
        else:
            number = float(spelling)
            if not math.isfinite(number) or Decimal(str(number)) != Decimal(spelling):
                return None
    except (ValueError, InvalidOperation):
        return None
    return operand, "number", number


def _text(data, node):
    return data[node.start_byte:node.end_byte].decode("utf-8") if node is not None else ""


def _node_key(data, node):
    name = node.child_by_field_name("name")
    suffix = ""
    if name is not None and API_NAME.fullmatch(_text(data, name)):
        suffix = "[@name=" + quote(_text(data, name).casefold(), safe="_") + "]"
    return node.type + suffix


@dataclass
class Unit:
    path: str
    data: bytes
    tree: Tree
    class_node: Node
    class_id: str
    name: str
    content_hash: str
    line_offsets: list[int]
    declared_members: set[str] = field(default_factory=set)
    declared_types: set[str] = field(default_factory=set)
    has_superclass: bool = False
    superclass_name: str | None = None
    is_test: bool = False
    parse_error: bool = False
    member_declarations: dict[str, list[Member]] = field(default_factory=dict)
    enclosing: Unit | None = None

    def text(self, node):
        return _text(self.data, node)

    def location(self, node):
        segments = []
        current = node
        while current is not None:
            key = _node_key(self.data, current)
            if current.parent is not None:
                siblings = [child for child in current.parent.named_children if _node_key(self.data, child) == key]
                if len(siblings) > 1:
                    key += f"[{next(i for i, sibling in enumerate(siblings, 1) if sibling.id == current.id)}]"
            segments.append(key)
            current = current.parent
        row, _ = node.start_point
        end_row, _ = node.end_point
        return {
            "path": self.path, "locator": "/" + "/".join(reversed(segments)),
            "line": row + 1, "column": len(self.data[self.line_offsets[row]:node.start_byte].decode("utf-8")) + 1,
            "end_line": end_row + 1,
            "end_column": len(self.data[self.line_offsets[end_row]:node.end_byte].decode("utf-8")) + 1,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, eq=False, slots=True)
class Member:
    unit: Unit
    node: Node
    type_node: Node
    modifiers: frozenset[str]


@dataclass
class Method:
    unit: Unit
    node: Node
    id: str
    name: str
    static: bool
    invocable: bool
    invocable_valid: bool
    parameter_count: int
    is_test: bool = False
    parameter_types: tuple[str, ...] = ()
    api_name: str = ""

    @property
    def qualified_name(self):
        return self.unit.name + "." + self.name


@dataclass
class ApexIndex:
    units: list[Unit] = field(default_factory=list)
    units_by_class: dict[str, Unit] = field(default_factory=dict)
    methods: dict[str, list[Method]] = field(default_factory=lambda: defaultdict(list))
    constructors: dict[str, list[Method]] = field(default_factory=lambda: defaultdict(list))
    apex_types: set[str] = field(default_factory=set)
    object_names: set[str] = field(default_factory=set)
    namespaces: set[str] = field(default_factory=set)


def _filename_location(path, data):
    return {"path": path, "locator": "/filename", "line": None, "column": None,
            "end_line": None, "end_column": None, "content_hash": hashlib.sha256(data).hexdigest()}


def _modifiers(data, node):
    modifiers = _child(node, "modifiers")
    names, annotations = set(), set()
    if modifiers is not None:
        for item in modifiers.named_children:
            if item.type == "annotation":
                annotations.add(_text(data, item.child_by_field_name("name")).casefold())
            elif item.type == "modifier":
                names.add(_text(data, item).casefold())
    return names, annotations


def _list_type(data, node):
    children = _significant(node)
    return bool(node is not None and node.type == "generic_type" and children
                and _text(data, children[0]).casefold() == "list")


def _query_owner(unit, query):
    body = _child(query, "soql_query_body")
    return _query_root(unit, body)


def _query_root(unit, body):
    from_clause = body.child_by_field_name("from_clause") if body is not None else None
    children = _significant(from_clause)
    if from_clause is None or len(children) != 1:
        return None
    storage = children[0]
    name = unit.text(storage)
    return name if storage.type == "storage_identifier" and API_NAME.fullmatch(name) else None


def _parameter_type(unit, parameter):
    source = unit.text(parameter.child_by_field_name("type"))
    return re.sub(r"\s+", "", re.sub(r"/\*[\s\S]*?\*/|//[^\r\n]*", "", source))


def _constructor_definition(unit, node, store):
    name = unit.text(node.child_by_field_name("name"))
    body = node.child_by_field_name("body")
    flags, _ = _modifiers(unit.data, node)
    parameters = _significant(node.child_by_field_name("parameters"))
    if (name.casefold() != unit.name.rsplit(".", 1)[-1].casefold() or body is None or body.type != "constructor_body"
            or flags & {"static", "abstract", "virtual", "override"}
            or any(item.type != "formal_parameter" for item in parameters)):
        store.diagnostic("APEX_CONSTRUCTOR_DECLARATION_UNSUPPORTED",
                         "Expected a non-static constructor matching its class.",
                         unit.location(node), state="unresolved")
        return None
    types = tuple(_parameter_type(unit, parameter) for parameter in parameters)
    api_name = f"{unit.name}.new({','.join(types)})"
    try:
        if not all(types):
            raise DataError("Constructor parameters require declared types.")
        constructor_id = component_id("apexmethod", api_name)
    except DataError:
        store.diagnostic("APEX_CONSTRUCTOR_SIGNATURE_UNSUPPORTED",
                         "Constructor parameters do not have a supported canonical signature.",
                         unit.location(node), state="unresolved")
        return None
    return Method(unit, node, constructor_id, "new", static=False, invocable=False, invocable_valid=False,
                  parameter_count=len(parameters), is_test=unit.is_test, parameter_types=types, api_name=api_name)


def _index_class_members(unit, index, store):
    cls, data, name = unit.class_node, unit.data, unit.name
    body = cls.child_by_field_name("body")
    if body is None:
        return
    unit.has_superclass = cls.child_by_field_name("superclass") is not None
    if unit.has_superclass:
        superclass = cls.child_by_field_name("superclass")
        candidates = [n for n in _significant(superclass) if n.type in {"type_identifier", "scoped_type_identifier"}]
        parent_name = unit.text(candidates[0]) if len(candidates) == 1 else ""
        if parent_name and all(API_NAME.fullmatch(p) for p in parent_name.split(".")):
            if unit.enclosing is not None and parent_name.casefold() in unit.enclosing.declared_types:
                parent_name = f"{unit.enclosing.name}.{parent_name}"
            unit.superclass_name = parent_name
    for member in body.named_children:
        if member.type == "field_declaration":
            type_node = member.child_by_field_name("type")
            flags = frozenset(_modifiers(data, member)[0])
            for declaration in member.named_children:
                if declaration.type == "variable_declarator":
                    member_name = unit.text(declaration.child_by_field_name("name")).casefold()
                    unit.declared_members.add(member_name)
                    if type_node is not None:
                        unit.member_declarations.setdefault(member_name, []).append(
                            Member(unit, declaration, type_node, flags))
    for node in body.named_children:
        if node.type in _IGNORED:
            continue
        if node.type == "constructor_declaration":
            constructor = _constructor_definition(unit, node, store)
            if constructor is not None:
                index.constructors[unit.class_id].append(constructor)
            continue
        if node.type in {"static_initializer", "block"}:
            store.diagnostic("APEX_CLASS_INITIALIZER_UNSUPPORTED", "Class initializer blocks are outside coverage.",
                             unit.location(node), construct=node.type)
            continue
        if node.type == "class_declaration" and unit.enclosing is None:
            continue  # Indexed separately with its own class and method IDs.
        if node.type in {"class_declaration", "interface_declaration", "enum_declaration"}:
            store.diagnostic("APEX_INNER_TYPE_UNSUPPORTED", "Nested inner classes, interfaces and enums are outside coverage.",
                             unit.location(node), construct=node.type)
            continue
        if node.type != "method_declaration":
            store.diagnostic("APEX_CLASS_MEMBER_UNSUPPORTED", "This class member body is outside dependency coverage.",
                             unit.location(node), construct=node.type)
            continue
        method_name = unit.text(node.child_by_field_name("name"))
        modifiers, annotations = _modifiers(data, node)
        invocable = "invocablemethod" in annotations
        params = node.child_by_field_name("parameters")
        parameters = [p for p in params.named_children if p.type == "formal_parameter"] if params is not None else []
        result_type = node.child_by_field_name("type")
        supported_signature = (len(parameters) <= 1
                               and all(_list_type(data, p.child_by_field_name("type")) for p in parameters)
                               and (unit.text(result_type).casefold() == "void" or _list_type(data, result_type)))
        valid = bool(invocable and unit.enclosing is None and "static" in modifiers and modifiers & {"public", "global"}
                     and supported_signature and node.child_by_field_name("body") is not None
                     and annotations <= {"invocablemethod", "deprecated"})
        # Register methods after every class definition has been indexed.
        # A unique method name does not disambiguate a duplicate owner class.
        method_id = component_id("apexmethod", name + "." + method_name)
        method = Method(unit, node, method_id, method_name, "static" in modifiers, invocable, valid, len(parameters),
                        is_test=unit.is_test or "istest" in annotations or "testmethod" in modifiers,
                        parameter_types=tuple(_parameter_type(unit, p) for p in parameters))
        index.methods[unit.class_id].append(method)
        if invocable and not valid:
            store.diagnostic("APEX_INVOCABLE_SIGNATURE_UNSUPPORTED", "The invocable declaration does not satisfy the supported outer static public/global list signature.", unit.location(node))
    invocables = [m for m in index.methods[unit.class_id] if m.invocable]
    if len(invocables) > 1:
        store.diagnostic("APEX_INVOCABLE_AMBIGUOUS", "More than one local invocable declaration names this class.", unit.location(cls), state="ambiguous")


def _index_inner_classes(outer, index, store):
    body = outer.class_node.child_by_field_name("body")
    for node in _significant(body):
        if node.type != "class_declaration":
            continue
        simple_name = outer.text(node.child_by_field_name("name"))
        name = f"{outer.name}.{simple_name}"
        class_id = component_id("apexclass", name)
        unit = Unit(outer.path, outer.data, outer.tree, node, class_id, name,
                    outer.content_hash, outer.line_offsets, enclosing=outer)
        unit.is_test = outer.is_test or "istest" in _modifiers(unit.data, node)[1]
        unit.declared_types.update(outer.declared_types)
        definition = unit.location(node)
        store.ensure_component("apexclass", name, definition, inventory_method="apex_ast", parser=PARSER_VERSION,
                               is_test=unit.is_test, enclosing_class=outer.class_id)
        index.units.append(unit)
        _index_class_members(unit, index, store)


def index_apex(apex: dict[str, bytes], store: FactStore) -> ApexIndex:
    """Index Apex methods and constructors, including directly enclosed classes."""
    index = ApexIndex(object_names={c["api_name"].casefold() for c in store.components.values() if c["kind"] == "object"})
    index.namespaces.update(str(c["namespace"]).casefold() for c in store.components.values()
                            if c.get("namespace") and c.get("definition_status") == "defined")
    if not apex:
        return index
    from tree_sitter_language_pack import get_parser
    parser = get_parser("apex")
    for path, data in sorted(apex.items()):
        filename = PurePosixPath(path).name.removesuffix(".cls")
        fallback = _filename_location(path, data)

        def unavailable(code, message):
            store.ensure_component("apexclass", filename, fallback, inventory_method="filename", is_test=None)
            store.diagnostic(code, message, fallback)

        if len(data) > _MAX_SOURCE_BYTES:
            unavailable("APEX_SOURCE_LIMIT", "Apex source exceeds the bounded parser size limit.")
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            unavailable("APEX_ENCODING_UNSUPPORTED", "Apex source must be valid UTF-8.")
            continue
        tree, normalized = _parse_source(parser, data)
        root = tree.root_node
        if root.has_error:
            unavailable("APEX_PARSE_ERROR", "The pinned Apex grammar could not parse this file; no method dependencies were extracted.")
            continue
        seen = 0
        stack = [(root, 0)]
        too_large = False
        while stack:
            node, depth = stack.pop()
            seen += 1
            if seen > _MAX_NODES or depth > 128:
                too_large = True
                break
            stack.extend((child, depth + 1) for child in node.named_children)
        if too_large:
            unavailable("APEX_STRUCTURE_LIMIT", "Apex source exceeds the bounded AST structural limits.")
            continue
        classes = [node for node in root.named_children if node.type == "class_declaration"]
        if len(classes) != 1 or _text(data, classes[0].child_by_field_name("name")).casefold() != filename.casefold():
            unavailable("APEX_CLASS_SHAPE_UNSUPPORTED", "Expected one outer class matching the source filename.")
            continue
        cls = classes[0]
        name = _text(data, cls.child_by_field_name("name"))
        offsets = [0] + [i + 1 for i, byte in enumerate(data) if byte == 10]
        unit = Unit(path, data, tree, cls, component_id("apexclass", name), name,
                    hashlib.sha256(data).hexdigest(), offsets)
        unit.is_test = "istest" in _modifiers(data, cls)[1]
        store.ensure_component("apexclass", name, unit.location(cls), inventory_method="apex_ast", parser=PARSER_VERSION,
                               is_test=unit.is_test, parser_normalized_trigger_context=normalized)
        index.units.append(unit)
        for node in _walk(cls):
            if node.type in {"class_declaration", "interface_declaration", "enum_declaration"}:
                if node.id == cls.id:
                    index.apex_types.add(unit.text(node.child_by_field_name("name")).casefold())
                else:
                    unit.declared_types.add(unit.text(node.child_by_field_name("name")).casefold())
            elif node.type == "query_expression":
                owner = _query_owner(unit, node)
                if owner:
                    index.object_names.add(owner.casefold())
        _index_class_members(unit, index, store)
    for unit in list(index.units):
        if not store.is_ambiguous(unit.class_id):
            _index_inner_classes(unit, index, store)
    # Class names from failed/unsupported files also prevent object-type guessing.
    index.apex_types.update(PurePosixPath(path).name.removesuffix(".cls").casefold() for path in apex)
    index.units_by_class = {unit.class_id: unit for unit in index.units if not store.is_ambiguous(unit.class_id)}
    for component in list(store.components.values()):
        if component["kind"] == "apexclass" and component["definition_status"] == "ambiguous":
            component["is_test"] = None
            for definition in component["definitions"]:
                store.diagnostic("APEX_CLASS_AMBIGUOUS", "Multiple source definitions name this Apex class; its methods are not resolution targets.",
                                 definition, state="ambiguous", candidates=[component["id"]])
    for class_id, methods in index.methods.items():
        if store.is_ambiguous(class_id):
            continue
        for method in methods:
            overloads = [m for m in methods if m.name.casefold() == method.name.casefold()]
            method.api_name = method.qualified_name + ("(" + ",".join(method.parameter_types) + ")" if len(overloads) > 1 else "")
            method.id = component_id("apexmethod", method.api_name)
            ownership = {"enclosing_class": class_id} if method.unit.enclosing is not None else {}
            store.ensure_component("apexmethod", method.api_name, method.unit.location(method.node),
                                   invocable=method.invocable, invocable_signature_supported=method.invocable_valid,
                                   static=method.static, parser=PARSER_VERSION, is_test=method.is_test,
                                   qualified_name=method.qualified_name, arity=method.parameter_count,
                                   parameter_types=list(method.parameter_types), **ownership)
        for method in methods:
            if store.is_ambiguous(method.id):
                store.components[method.id]["is_test"] = None
                store.diagnostic("APEX_METHOD_AMBIGUOUS", "Duplicate method signatures share this identity; no target is selected.",
                                 method.unit.location(method.node), state="ambiguous", candidates=[method.id])
    for class_id, constructors in index.constructors.items():
        if store.is_ambiguous(class_id):
            continue
        if any(method.name.casefold() == "new" for method in index.methods.get(class_id, ())):
            for constructor in constructors:
                store.diagnostic("APEX_CONSTRUCTOR_ID_AMBIGUOUS",
                                 "An ordinary method uses the reserved constructor name.",
                                 constructor.unit.location(constructor.node), state="ambiguous")
            constructors.clear()
            continue
        for constructor in constructors:
            ownership = {"enclosing_class": class_id} if constructor.unit.enclosing is not None else {}
            store.ensure_component("apexmethod", constructor.api_name, constructor.unit.location(constructor.node),
                                   is_constructor=True, invocable=False, invocable_signature_supported=False,
                                   static=False, parser=PARSER_VERSION, is_test=constructor.is_test,
                                   qualified_name=constructor.qualified_name, arity=constructor.parameter_count,
                                   parameter_types=list(constructor.parameter_types), **ownership)
        for constructor in constructors:
            if store.is_ambiguous(constructor.id):
                store.components[constructor.id]["is_test"] = None
                store.diagnostic("APEX_CONSTRUCTOR_AMBIGUOUS", "Multiple constructors share this signature.",
                                 constructor.unit.location(constructor.node), state="ambiguous", candidates=[constructor.id])
    return index


@dataclass
class Binding:
    name: str
    owner: str | None
    collection: bool
    scope: Node
    node: Node
    available_from: int


def _class_hierarchy(index, store, class_id):
    """Resolve only a complete, unambiguous local inheritance chain."""
    hierarchy, seen = [], set()
    while class_id:
        if class_id in seen or class_id not in store.components or store.is_ambiguous(class_id):
            return None
        seen.add(class_id)
        unit = index.units_by_class.get(class_id)
        if unit is None:
            return None
        hierarchy.append(unit)
        if not unit.has_superclass:
            return hierarchy
        if not unit.superclass_name:
            return None
        class_id = component_id("apexclass", unit.superclass_name)
    return hierarchy


def _external_hierarchy(index, store, class_id):
    """Return a known prefix only when its sole gap is an absent ancestor.

    A local file with no indexed Unit may have failed parsing or another
    coverage check; it is not an external dependency. Duplicates and cycles
    likewise cannot justify a candidate qualifier.
    """
    hierarchy, seen = [], set()
    while class_id:
        if class_id in seen:
            return None
        seen.add(class_id)
        component = store.components.get(class_id)
        if component and store.is_ambiguous(class_id):
            return None
        unit = index.units_by_class.get(class_id)
        if unit is None:
            if (not hierarchy or (component and component.get("definitions"))
                    or (component and component["api_name"].casefold() in index.apex_types)):
                return None
            return hierarchy, [hierarchy[-1].superclass_name]
        if unit.parse_error:
            return None
        hierarchy.append(unit)
        if not unit.has_superclass or not unit.superclass_name:
            return None
        class_id = component_id("apexclass", unit.superclass_name)
    return None


def _method_candidates(index, store, class_id, name, arity, *, include_ambiguous=False):
    hierarchy = _class_hierarchy(index, store, class_id)
    if hierarchy is None:
        return []
    selected = {}
    for unit in hierarchy:
        for method in index.methods.get(unit.class_id, []):
            if method.name.casefold() != name.casefold() or method.parameter_count != arity:
                continue
            signature = tuple(t.casefold() for t in method.parameter_types)
            selected.setdefault(signature, method)
    return [method for method in selected.values() if include_ambiguous or not store.is_ambiguous(method.id)]


class _MethodExtractor:
    _constructor_reference_rule = "apex.constructed_class"

    def __init__(self, method, index, store):
        self.method, self.index, self.store = method, index, store
        self.unit = method.unit
        self.bindings = []
        self.body = method.node.child_by_field_name("body")
        # Trigger extraction also uses this visitor with a synthetic Unit that
        # has no superclass and is intentionally absent from the Apex index.
        self.qualifier_hierarchy = (_class_hierarchy(index, store, self.unit.class_id)
                                    if self.unit.has_superclass else [self.unit])
        self.external_hierarchy = (_external_hierarchy(index, store, self.unit.class_id)
                                   if self.qualifier_hierarchy is None else None)

    def diagnostic(self, code, message, node, state="unsupported"):
        self.store.diagnostic(code, message, self.unit.location(node), state=state)

    def record_type(self, node):
        if node is None:
            return None, False
        collection = False
        if node.type == "generic_type":
            children = _significant(node)
            if len(children) != 2 or self.unit.text(children[0]).casefold() != "list" or len(_significant(children[1])) != 1:
                return None, True
            node = _significant(children[1])[0]
            collection = True
        elif node.type == "array_type":
            node = node.child_by_field_name("element")
            collection = True
        name = self.unit.text(node)
        if (not API_NAME.fullmatch(name) or name.casefold() in _PRIMITIVES
                or name.casefold() in self.index.apex_types or name.casefold() in self.unit.declared_types):
            return None, collection
        if (name.casefold() in self.index.object_names or name.casefold() in _STANDARD_OBJECTS
                or name.lower().endswith(("__c", "__mdt", "__e", "__x"))):
            return name, collection
        return None, collection

    def index_bindings(self):
        body = self.body
        if body is None:
            return
        parameters = self.method.node.child_by_field_name("parameters")
        if parameters is not None:
            for parameter in parameters.named_children:
                if parameter.type == "formal_parameter":
                    owner, collection = self.record_type(parameter.child_by_field_name("type"))
                    self.bindings.append(Binding(self.unit.text(parameter.child_by_field_name("name")), owner, collection,
                                                 body, parameter, -1))
        for node in _walk(body):
            if node.type == "local_variable_declaration":
                scope = node.parent
                while scope is not None and scope.type not in {"block", "constructor_body", "for_statement", "enhanced_for_statement"}:
                    scope = scope.parent
                owner, collection = self.record_type(node.child_by_field_name("type"))
                for declaration in node.named_children:
                    if declaration.type == "variable_declarator":
                        self.bindings.append(Binding(self.unit.text(declaration.child_by_field_name("name")), owner, collection,
                                                     scope or body, declaration, declaration.end_byte))
            elif node.type == "enhanced_for_statement":
                if not self.index_loop_binding(node):
                    self.diagnostic("APEX_LOOP_ALIAS_UNSUPPORTED", "Loop item declaration does not establish a supported single-record type; collection inference is outside coverage.", node)
            elif node.type == "formal_parameter" and node.parent is not None and node.parent.type == "catch_clause":
                name = node.child_by_field_name("name")
                if name is not None:
                    self.bindings.append(Binding(self.unit.text(name), None, False, node.parent, node, node.start_byte))

    def index_loop_binding(self, node):
        """Use the explicit element type only inside the loop body.

        The iterable is evaluated in the outer scope. Its origin is not inferred;
        unsupported element declarations still shadow any outer record alias.
        """
        name, body = node.child_by_field_name("name"), node.child_by_field_name("body")
        if name is None or body is None:
            return False
        owner, collection = self.record_type(node.child_by_field_name("type"))
        proven_owner = owner if not collection else None
        self.bindings.append(Binding(self.unit.text(name), proven_owner, False, body, node, body.start_byte))
        return proven_owner is not None

    def binding(self, name, node, *, report=True):
        ancestors = []
        parent = node
        while parent is not None:
            ancestors.append(parent.id)
            parent = parent.parent
        candidates = [b for b in self.bindings if b.name.casefold() == name.casefold()
                      and b.scope.id in ancestors and b.available_from <= node.start_byte]
        if not candidates:
            if report:
                self.diagnostic("APEX_SYMBOL_UNRESOLVED", "No supported lexical declaration establishes this record resource.", node, "unresolved")
            return None
        nearest = min(ancestors.index(b.scope.id) for b in candidates)
        matches = [b for b in candidates if ancestors.index(b.scope.id) == nearest]
        if len(matches) != 1:
            if report:
                self.diagnostic("APEX_SYMBOL_AMBIGUOUS", "Multiple declarations in the same scope prevent type resolution.", node, "ambiguous")
            return None
        return matches[0]

    def has_binding(self, name, node):
        """An ambiguous local declaration still shadows a class-like identifier."""
        ancestors = set()
        parent = node
        while parent is not None:
            ancestors.add(parent.id)
            parent = parent.parent
        return any(b.name.casefold() == name.casefold() and b.scope.id in ancestors
                   and b.available_from <= node.start_byte for b in self.bindings)

    def _initializing_binding(self, name, node):
        folded = name.casefold()
        return any(binding.name.casefold() == folded
                   and binding.node.start_byte <= node.start_byte < binding.available_from
                   for binding in self.bindings)

    def unshadowed_qualifier(self, name, node, *, allow_external=False):
        """Prove a class/namespace receiver is not a lexical or inherited member.

        An unavailable ancestor may declare a field, property or inner type with
        this name. A complete local hierarchy lets us check those declarations
        instead of rejecting every qualified call made from a subclass.
        """
        hierarchy = self.qualifier_hierarchy
        if hierarchy is None and allow_external and self.external_hierarchy is not None:
            hierarchy = self.external_hierarchy[0]
        folded = name.casefold()
        enclosing = self.unit.enclosing
        if enclosing is not None and any("static" in member.modifiers
                                         for member in enclosing.member_declarations.get(folded, ())):
            return False
        return (hierarchy is not None and not self.has_binding(name, node)
                and all(folded not in unit.declared_members and folded not in unit.declared_types
                        for unit in hierarchy))

    def _exact_overload_type(self, node):
        if node is None:
            return None
        if node.type == "type_identifier":
            name = self.unit.text(node)
            if name.casefold() in _PRIMITIVES or name.casefold() in _STANDARD_OBJECTS:
                return name
            if API_NAME.fullmatch(name):
                definition = self.store.components.get(component_id("object", name), {})
                if definition.get("definition_status") == "defined":
                    return name
            return None
        if node.type != "generic_type":
            return None
        parts = _significant(node)
        if len(parts) != 2 or parts[1].type != "type_arguments":
            return None
        name = self.unit.text(parts[0])
        arguments = _significant(parts[1])
        if len(arguments) != _BUILTIN_COLLECTION_ARITY.get(name.casefold()):
            return None
        types = []
        for argument in arguments:
            argument_type = self._exact_overload_type(argument)
            if argument_type is None:
                return None
            types.append(argument_type)
        return f"{name}<{','.join(types)}>"

    def _argument_type_proof(self, argument):
        argument = _without_parentheses(argument)
        if argument is None:
            return None
        if argument.type == "cast_expression":
            declaration, basis = argument, "explicit_cast"
        elif argument.type == "identifier":
            binding = self.binding(self.unit.text(argument), argument, report=False)
            if binding is None:
                return None
            declaration = binding.node
            if declaration.type == "variable_declarator":
                declaration = declaration.parent
            basis = "lexical_declaration"
        else:
            return None
        type_node = declaration.child_by_field_name("type") if declaration is not None else None
        name = self._exact_overload_type(type_node)
        if name is None:
            return None
        return {"type": name, "basis": basis, "location": self.unit.location(type_node)}

    def _select_method(self, candidates, arguments):
        """Keep arity-only dispatch, or narrow a proven local overload by exact types."""
        if len(candidates) == 1 and not self.store.is_ambiguous(candidates[0].id):
            return candidates[0], {}
        if (len(candidates) < 2 or self.qualifier_hierarchy is None
                or any(self.store.is_ambiguous(method.id) for method in candidates)):
            return None, {}
        proof = []
        for argument in arguments:
            item = self._argument_type_proof(argument)
            if item is None:
                return None, {}
            proof.append(item)
        signature = tuple(item["type"].casefold() for item in proof)
        matches = [method for method in candidates
                   if tuple(kind.casefold() for kind in method.parameter_types) == signature]
        if len(matches) != 1:
            return None, {}
        target = matches[0]
        types = set(re.findall(r"[a-z_][a-z0-9_]*", ",".join(signature)))
        target_hierarchy = _class_hierarchy(self.index, self.store, target.unit.class_id)
        if (types & self.index.apex_types or target_hierarchy is None
                or any(types & unit.declared_types for unit in self.qualifier_hierarchy + target_hierarchy)):
            return None, {}
        basis = ("exact_sobject_argument_types" if types.difference(_PRIMITIVES, _BUILTIN_COLLECTION_ARITY)
                 else "exact_builtin_argument_types")
        return target, {"overload_resolution": {"basis": basis, "arguments": proof}}

    def _ambiguous_call(self, node, candidates, *, code="APEX_CALL_UNRESOLVED"):
        if len(candidates) < 2 and not any(self.store.is_ambiguous(method.id) for method in candidates):
            return False
        ordered = sorted(candidates, key=lambda method: method.api_name.casefold())
        shown = ordered[:8]
        signatures = [f"{method.qualified_name}({','.join(method.parameter_types)})" for method in shown]
        self.store.diagnostic(code, "No unique supported overload is established. Candidates: " + ", ".join(signatures),
                              self.unit.location(node), state="ambiguous", candidates=[method.id for method in shown],
                              signatures=signatures, candidate_count=len(ordered))
        return True

    def edge(self, owner, name, relation, node, rule, qualifiers, resolution=None):
        location = self.unit.location(node)
        target = self.store.field(owner, name, location) if name else self.store.resolve_component("object", owner, location)
        if target:
            proof = resolution or {"basis": "apex_ast", "parser": PARSER_VERSION, "object": owner}
            if relation == "writes" and name is not None:
                assigned_value = _assigned_literal(self.unit.data, node)
                if assigned_value is not None:
                    operand, kind, value = assigned_value
                    proof = {**proof, "assigned_value": {"type": kind, "value": value},
                             "assigned_value_location": self.unit.location(operand)}
            self.store.relation(self.method.id, target, relation, location, rule,
                                resolution=proof, qualifiers=qualifiers)

    def _setting_owner(self, node):
        node = _without_parentheses(node)
        if node is None or node.type != "method_invocation":
            return None
        method = self.unit.text(node.child_by_field_name("name")).casefold()
        if method not in {"getinstance", "getvalues", "getorgdefaults"}:
            return None
        receiver = node.child_by_field_name("object")
        if receiver is None or receiver.type != "identifier":
            return None
        name = self.unit.text(receiver)
        if not API_NAME.fullmatch(name) or name.casefold() in self.index.apex_types:
            return None
        item = self.store.components.get(component_id("object", name))
        if (not item or item.get("definition_status") != "defined"
                or item.get("custom_settings_type") not in {"Hierarchy", "List"}):
            return None
        # Apex identifiers cannot contain consecutive underscores. For an exact
        # unqualified local setting API name, an absent base cannot declare an
        # Apex member with that name. Still check all visible scopes and types.
        local_setting_name = name.casefold().endswith("__c") and not item.get("namespace")
        if not self.unshadowed_qualifier(name, node, allow_external=local_setting_name):
            return None
        arity = len(_significant(node.child_by_field_name("arguments")))
        if ((method in {"getinstance", "getvalues"} and arity == 1)
                or (method in {"getinstance", "getorgdefaults"} and arity == 0
                    and item["custom_settings_type"] == "Hierarchy")):
            return item["api_name"]
        return None

    def _indexed_record(self, expression):
        """Resolve one element access on a lexically declared List or Map."""
        expression = _without_parentheses(expression)
        if expression is None:
            return None
        if expression.type == "array_access":
            receiver = expression.child_by_field_name("array")
            selector = expression.child_by_field_name("index")
            access = "index"
        elif (expression.type == "method_invocation"
                and self.unit.text(expression.child_by_field_name("name")).casefold() == "get"):
            receiver = expression.child_by_field_name("object")
            arguments = _significant(expression.child_by_field_name("arguments"))
            selector = arguments[0] if len(arguments) == 1 else None
            access = "get"
        else:
            return None
        receiver = _without_parentheses(receiver)
        if receiver is None or receiver.type != "identifier" or selector is None:
            return None
        binding = self.binding(self.unit.text(receiver), receiver, report=False)
        if binding is None:
            return None
        declaration = binding.node
        if declaration.type == "variable_declarator":
            declaration = declaration.parent
        if declaration is None or declaration.type not in {"formal_parameter", "local_variable_declaration"}:
            return None
        declared_type = declaration.child_by_field_name("type")
        if declared_type is None:
            return None
        if declared_type.type == "array_type":
            dimensions = declared_type.child_by_field_name("dimensions")
            if dimensions is None or sum(child.type == "[" for child in dimensions.children) != 1:
                return None
            family = "list"
            element = declared_type.child_by_field_name("element")
        elif declared_type.type == "generic_type":
            parts = _significant(declared_type)
            if len(parts) != 2 or parts[1].type != "type_arguments":
                return None
            family = self.unit.text(parts[0]).casefold()
            arguments = _significant(parts[1])
            if family == "list" and len(arguments) == 1:
                element = arguments[0]
            elif family == "map" and access == "get" and len(arguments) == 2:
                element = arguments[1]
            else:
                return None
            if family in self.index.apex_types or family in self.unit.declared_types:
                return None
        else:
            return None
        owner, collection = self.record_type(element)
        if not owner or collection:
            return None
        return owner, binding, f"{family}_{access}", selector

    def _visible_static_member(self, member, node, relation):
        modifiers = member.modifiers
        if "static" not in modifiers or "protected" in modifiers:
            return False
        same_class = member.unit.class_id == self.unit.class_id
        visible = {"public", "global"}
        if not same_class and (not modifiers & visible
                               or not _modifiers(member.unit.data, member.unit.class_node)[0] & visible):
            return False
        access = {"set" if relation == "writes" else "get"}
        parent = node.parent
        if parent is not None and (parent.type == "update_expression" or (
                parent.type == "assignment_expression" and node == parent.child_by_field_name("left")
                and self.unit.text(parent.child_by_field_name("operator")) != "=")):
            access = {"get", "set"}
        if "set" in access and "final" in modifiers:
            return False
        declaration = member.node.parent
        accessors = _child(declaration, "accessor_list") if declaration is not None else None
        if accessors is None:
            return True
        for accessor in _significant(accessors):
            accessor_modifiers, _ = _modifiers(member.unit.data, accessor)
            if same_class or not accessor_modifiers or accessor_modifiers & visible:
                access.difference_update(child.type for child in accessor.children if child.type in {"get", "set"})
        return not access

    def _static_member_reference(self, node, relation):
        receiver, name_node = node.child_by_field_name("object"), node.child_by_field_name("field")
        if (receiver is None or receiver.type != "identifier" or name_node is None or name_node.type != "identifier"
                or _child(node, "safe_navigation_operator") is not None):
            return False
        owner, name = self.unit.text(receiver), self.unit.text(name_node)
        folded = owner.casefold()
        if (folded not in self.index.apex_types or not API_NAME.fullmatch(owner)
                or not API_NAME.fullmatch(name) or name.casefold() == "class"
                or folded in _RESERVED_RECEIVERS or folded in _PRIMITIVES or folded in _BUILTIN_COLLECTION_ARITY
                or folded in _STANDARD_OBJECTS or folded in self.index.object_names):
            return False
        class_id = component_id("apexclass", owner)
        component = self.store.components.get(class_id)
        if (not component or not component["definitions"] or not self.unshadowed_qualifier(owner, receiver)
                or self._initializing_binding(owner, receiver)):
            return False
        unit = self.index.units_by_class.get(class_id)
        declarations = unit.member_declarations.get(name.casefold(), []) if unit is not None else []
        ambiguous = self.store.is_ambiguous(class_id) or len(declarations) > 1
        if ambiguous or len(declarations) != 1 or not self._visible_static_member(declarations[0], node, relation):
            self.diagnostic("APEX_STATIC_MEMBER_UNRESOLVED",
                            "A unique, visible static member declared by this local class is required.", node,
                            "ambiguous" if ambiguous else "unresolved")
            return True
        member = declarations[0]
        self.store.relation(
            self.method.id, class_id, "references", self.unit.location(node), "apex.static_member_reference",
            resolution={"basis": "unique_local_static_member", "member": member.unit.text(member.node.child_by_field_name("name")),
                        "member_declaration": member.unit.location(member.node), "parser": PARSER_VERSION},
            qualifiers={"context": "static_member_access"},
        )
        return True

    def _class_literal(self, node):
        """Classify a type token without turning an unproven type into Apex.

        Both sObjects and Apex classes use X.class. A qualified class token
        needs a local enclosing class or a corroborated package namespace.
        """
        resource = node.child_by_field_name("object")
        qualifier, name_node = None, resource
        if resource is not None and resource.type == "field_access":
            qualifier = resource.child_by_field_name("object")
            name_node = resource.child_by_field_name("field")
        pieces = [item for item in (qualifier, name_node) if item is not None]
        if (not pieces or any(item.type != "identifier" or not API_NAME.fullmatch(self.unit.text(item)) for item in pieces)
                or _child(node, "safe_navigation_operator") is not None
                or (resource is not None and _child(resource, "safe_navigation_operator") is not None)):
            self.diagnostic("APEX_CLASS_LITERAL_UNRESOLVED", "Expected a supported literal type qualifier.", node, "unresolved")
            return
        root = self.unit.text(pieces[0])
        if not self.unshadowed_qualifier(root, node) or self._initializing_binding(root, node):
            self.diagnostic("APEX_CLASS_LITERAL_UNRESOLVED", "The type qualifier may be a lexical or inherited declaration.",
                            node, "unresolved")
            return
        name = self.unit.text(name_node)
        folded = name.casefold()
        object_proof = ("indexed_object" if folded in self.index.object_names
                        else "standard_object_vocabulary" if folded in _CLASS_LITERAL_STANDARD_OBJECTS else None)
        class_id = component_id("apexclass", name)
        local_class = self.store.components.get(class_id)
        has_class = bool(local_class and local_class["definitions"])
        root_class = self.store.components.get(component_id("apexclass", root))
        root_has_class = bool(root_class and root_class["definitions"])
        kind, basis = None, None
        if qualifier is None:
            if object_proof and has_class:
                self.diagnostic("APEX_CLASS_LITERAL_UNRESOLVED", "This name has both object and Apex class evidence.",
                                node, "ambiguous")
                return
            if object_proof:
                kind, basis = "object", "literal_sobject_type"
            elif has_class:
                kind, basis = "apexclass", "literal_class_token"
        elif root.casefold() == "schema" and not root_has_class:
            if object_proof:
                kind, basis = "object", "literal_sobject_type"
        elif self._visible_inner_class(root, name):
            name = root + "." + name
            kind, basis = "apexclass", "local_inner_class_token"
        elif (root.casefold() in self.index.namespaces and not root_has_class
                and root.casefold() not in _RESERVED_RECEIVERS
                and root.casefold() not in self.index.object_names and root.casefold() not in _CLASS_LITERAL_STANDARD_OBJECTS):
            name = root + "." + name
            kind, basis = "apexclass", "literal_class_token"
        if kind is None:
            self.diagnostic("APEX_CLASS_LITERAL_UNRESOLVED", "No unique supported object or Apex class type is established.",
                            node, "unresolved")
            return
        location = self.unit.location(node)
        target = self.store.resolve_component(kind, name, location)
        if target:
            proof = {"basis": basis, "parser": PARSER_VERSION}
            if kind == "object":
                proof["object_proof"] = object_proof
            self.store.relation(self.method.id, target, "references", location, "apex.class_literal",
                                qualifiers={"context": "class_literal"}, resolution=proof)

    def _record_member(self, receiver):
        """Resolve a record from one field declared in the current Apex class."""
        receiver = _without_parentheses(receiver)
        if receiver is None:
            return None
        explicit = receiver.type == "field_access"
        if explicit:
            qualifier = receiver.child_by_field_name("object")
            if qualifier is None or qualifier.type != "this" or _child(receiver, "safe_navigation_operator") is not None:
                return None
            receiver = receiver.child_by_field_name("field")
        if receiver is None or receiver.type != "identifier":
            return None
        name = self.unit.text(receiver)
        declarations = self.unit.member_declarations.get(name.casefold(), ())
        if len(declarations) != 1:
            return None
        if not explicit and (self.has_binding(name, receiver) or self._initializing_binding(name, receiver)):
            return None
        member = declarations[0]
        static = "static" in member.modifiers
        if (explicit and (static or self.method.static)) or (self.method.static and not static):
            return None
        declaration = member.node.parent
        if declaration is None or _child(declaration, "accessor_list") is not None:
            return None
        owner, collection = self.record_type(member.type_node)
        return (owner, member) if owner and not collection else None

    def field_access(self, node, relation="reads", *, operation="field_access"):
        resource, field_node = node.child_by_field_name("object"), node.child_by_field_name("field")
        if field_node is not None and self.unit.text(field_node).casefold() == "class":
            self._class_literal(node)
            return
        if self._static_member_reference(node, relation):
            return
        if resource is not None and resource.type != "identifier" and field_node is not None:
            setting = self._setting_owner(resource)
            if setting is not None and API_NAME.fullmatch(self.unit.text(field_node)):
                self.edge(setting, self.unit.text(field_node), relation, field_node, "apex.custom_setting_field",
                          {"operation": operation, "effect": "memory"},
                          {"basis": "local_custom_setting_metadata", "object": setting})
                self.visit(resource)
                return
            indexed = self._indexed_record(resource)
            if indexed is not None and field_node.type == "identifier" and self.unit.text(field_node).casefold() != "class":
                owner, binding, access, selector = indexed
                declaration = self.unit.location(binding.node)
                self.edge(owner, self.unit.text(field_node), relation, node, "apex.indexed_field",
                          {"operation": operation, "effect": "memory"},
                          {"basis": "lexical_collection_element_type", "object": owner, "accessor": access,
                           "declaration": {key: declaration[key] for key in ("path", "locator")}})
                self.visit(selector)
                return
        member_receiver = self._record_member(resource)
        if member_receiver is not None and field_node is not None and field_node.type == "identifier":
            owner, member = member_receiver
            declaration = member.unit.location(member.node)
            self.edge(owner, self.unit.text(field_node), relation, node, "apex.member_record_field",
                      {"operation": operation, "effect": "memory"},
                      {"basis": "declared_record_member", "object": owner,
                       "declaration": {key: declaration[key] for key in ("path", "locator")}})
            return
        if resource is None or resource.type != "identifier" or field_node is None:
            self.diagnostic("APEX_FIELD_PATH_UNSUPPORTED", "Only one direct field on a typed local record is supported; paths and indexed collections are not truncated.", node)
            # Preserve calls inside a complex receiver without extracting a
            # shorter, misleading field path from the unsupported chain.
            receiver = resource
            while receiver is not None and receiver.type == "field_access":
                if self._static_member_reference(receiver, "reads"):
                    return
                receiver = receiver.child_by_field_name("object")
            if receiver is not None and receiver.type != "identifier":
                self.visit(receiver)
            return
        binding = self.binding(self.unit.text(resource), node)
        if binding is None:
            return
        if not binding.owner or binding.collection:
            self.diagnostic("APEX_RECORD_TYPE_UNRESOLVED", "The resource is not a proven single record of a supported object type.", node, "unresolved")
            return
        self.edge(binding.owner, self.unit.text(field_node), relation, node, "apex.direct_field", {"operation": operation, "effect": "memory"},
                  {"basis": "lexical_record_type", "object": binding.owner,
                   "declaration": {key: value for key, value in self.unit.location(binding.node).items() if key in {"path", "locator"}}})

    def constructor(self, node, *, database_operation=None):
        type_node = node.child_by_field_name("type")
        owner, collection = self.record_type(type_node)
        args = node.child_by_field_name("arguments")
        assignments = _significant(args)
        if not owner or collection:
            cid = self._literal_local_class(type_node)
            if cid:
                self.store.relation(self.method.id, cid, "references", self.unit.location(node),
                                    self._constructor_reference_rule, qualifiers={"invocation": "constructor"},
                                    resolution={"basis": "literal_local_class", "parser": PARSER_VERSION})
            else:
                self.diagnostic("APEX_CONSTRUCTOR_TYPE_UNRESOLVED",
                                "The constructor does not establish a supported record type or unique local Apex class.",
                                node, "unresolved")
            for arg in assignments:
                self.visit(arg)
            return None
        fields = []
        for arg in assignments:
            left = arg.child_by_field_name("left") if arg.type == "assignment_expression" else None
            if left is None or left.type != "identifier" or self.unit.text(arg.child_by_field_name("operator")) != "=":
                self.diagnostic("APEX_CONSTRUCTOR_ARGUMENT_UNSUPPORTED", "Only literal field initializers are supported for record constructors.", arg)
                self.visit(arg)
                continue
            fields.append((self.unit.text(left), left))
            self.edge(owner, self.unit.text(left), "writes", left, "apex.record_initializer", {"operation": "assignment", "effect": "memory"})
            right = arg.child_by_field_name("right")
            if right is not None:
                self.visit(right)
        if database_operation and database_operation not in {"delete", "undelete"}:
            for name, field_node in fields:
                self.edge(owner, name, "writes", field_node, "apex.dml_initializer", {"operation": database_operation, "effect": "database"})
        return owner

    def dml(self, node, operation, target):
        if operation not in _DML or target is None:
            self.diagnostic("APEX_DML_UNSUPPORTED", "This DML operation or target shape is outside bounded coverage.", node)
            return
        owner, collection = None, False
        direct_fields = False
        if target.type == "identifier":
            binding = self.binding(self.unit.text(target), target)
            if binding:
                owner, collection = binding.owner, binding.collection
        elif target.type == "object_creation_expression":
            owner = self.constructor(target, database_operation=operation)
            direct_fields = operation not in {"delete", "undelete"}
        elif target.type == "query_expression":
            owner = self.query(target)
            collection = True
        else:
            self.diagnostic("APEX_DML_TARGET_UNSUPPORTED", "DML aliases, expressions and collection constructors require unsupported type/data-flow resolution.", target)
            self.visit(target)
            return
        if not owner:
            self.diagnostic("APEX_DML_TYPE_UNRESOLVED", "The DML target's object type is not proven by the supported local syntax.", target, "unresolved")
            return
        qualifiers: dict[str, str | bool] = {
            "operation": operation, "effect": "database", "collection": collection,
            "field_set": "explicit_constructor" if direct_fields else "unknown",
        }
        security = next((child for child in node.children_by_field_name("security_mode") if child.type == "dml_security_mode"), None)
        if security is not None:
            qualifiers["security_mode"] = self.unit.text(security).casefold().removeprefix("as ")
        self.edge(owner, None, "writes", node, "apex.database_operation", qualifiers)

    def constructor_delegation(self, node):
        selector = self.unit.text(node.child_by_field_name("constructor")).casefold()
        args = _significant(node.child_by_field_name("arguments"))
        class_id = self.unit.class_id
        if selector == "super":
            parent_name = self.unit.superclass_name
            class_id = component_id("apexclass", parent_name) if parent_name else None
        candidates = [method for method in self.index.constructors.get(class_id, ())
                      if method.parameter_count == len(args)
                      and (selector == "this" or _modifiers(method.unit.data, method.node)[0] & {"public", "global", "protected"})]
        target, overload = self._select_method(candidates, args)
        if (selector in {"this", "super"} and target is not None and target.id != self.method.id
                and not self.store.is_ambiguous(target.unit.class_id)):
            self.store.relation(self.method.id, target.id, "calls", self.unit.location(node),
                                "apex.constructor_delegation", qualifiers={"invocation": "constructor_delegation", "delegate": selector},
                                resolution={"basis": "unique_local_constructor", "parser": PARSER_VERSION, **overload})
        elif not self._ambiguous_call(node, candidates, code="APEX_CONSTRUCTOR_DELEGATION_UNRESOLVED"):
            self.diagnostic("APEX_CONSTRUCTOR_DELEGATION_UNRESOLVED",
                            "No unique accessible local constructor matches this explicit delegation.", node, "unresolved")
        for arg in args:
            self.visit(arg)

    def query(self, node):
        body = _child(node, "soql_query_body")
        owner = _query_root(self.unit, body)
        if not owner or body is None:
            self.diagnostic("APEX_SOQL_ROOT_UNSUPPORTED", "Static SOQL requires one literal root object without aliases or relationship traversal.", node)
            return None
        self._query_body(node, body, owner)
        return owner

    def _soql_subquery(self, node, outer_owner, clause_name):
        body = _child(node, "soql_query_body")
        root = _query_root(self.unit, body)
        if not root or body is None:
            self.diagnostic("APEX_SOQL_SUBQUERY_UNRESOLVED", "This SOQL subquery requires one literal FROM name.", node)
            return
        parent = node.parent
        operator = _child(parent, "set_comparison_operator") if parent is not None else None
        if clause_name == "where" and operator is not None:
            operator_name = " ".join(self.unit.text(operator).casefold().split())
            if operator_name in {"in", "not in"}:
                self._query_body(node, body, root, context="anti_join" if operator_name == "not in" else "semi_join")
                return
        if clause_name != "select" or parent is None or parent.type != "select_clause":
            self.diagnostic("APEX_SOQL_SUBQUERY_UNRESOLVED", "Expected a child SELECT subquery or a WHERE semi-join.", node)
            return
        key = outer_owner.casefold(), root.casefold()
        local = self.store.child_relationships.get(key, [])
        standard = _STANDARD_CHILDREN.get(key)
        owner, lookup_name, proof = None, None, None
        if len(local) == 1:
            field_id, schema_source = local[0]
            field = self.store.components[field_id]
            owner, lookup_name = field["api_name"].split(".")
            if (not self.store.is_ambiguous(field_id)
                    and (standard is None or standard[0].casefold() == owner.casefold())):
                proof = {"basis": "declared_child_relationship", "schema_source": schema_source}
        elif not local and standard is not None:
            owner, parent_name = standard
            lookup = resolve_source_field_path(self.store, owner, parent_name + ".Id")
            if lookup and lookup["owner"].casefold() == outer_owner.casefold():
                lookup_name = parent_name + "Id"
                proof = {"basis": "standard_child_relationship"}
        if (not owner or not lookup_name or proof is None
                or any(self.store.components.get(component_id("object", name), {}).get("definition_status") == "ambiguous"
                       for name in (outer_owner, owner))):
            self.diagnostic("APEX_SOQL_CHILD_RELATIONSHIP_UNRESOLVED",
                            "No unique declared or supported standard child relationship establishes the SELECT subquery object.", node,
                            "ambiguous" if len(local) > 1 else "unresolved")
            return
        from_clause = body.child_by_field_name("from_clause")
        location_node = _significant(from_clause)[0]
        resolution = {**proof, "relationship_name": root, "parent_object": outer_owner,
                      "lookup_field": component_id("field", owner + "." + lookup_name)}
        self.edge(owner, lookup_name, "reads", location_node, "apex.static_soql_child_navigation",
                  {"operation": "soql", "context": "apex_soql_child_navigation"}, resolution)
        self._query_body(node, body, owner, context="child_subquery", resolution=resolution)

    def _query_body(self, node, body, owner, *, context=None, resolution=None):
        qualifiers = {"operation": "soql"}
        if context:
            qualifiers["query_context"] = context
        self.edge(owner, None, "reads", node, "apex.static_soql_object",
                  {**qualifiers, "effect": "database", "field_set": "partial_explicit"}, resolution)
        aliases = set()
        selected = body.child_by_field_name("select_clause")
        if selected is not None:
            # An aggregate/select alias is not a schema field of the root object.
            pending = list(selected.named_children)
            while pending:
                candidate = pending.pop()
                if candidate.type == "subquery":
                    continue
                if candidate.type == "alias_expression":
                    aliases.update(self.unit.text(child).casefold() for child in candidate.named_children if child.type == "identifier")
                else:
                    pending.extend(candidate.named_children)

        def clause(current, relation, clause_name):
            if current.type in _IGNORED:
                return
            if current.type == "subquery":
                self._soql_subquery(current, owner, clause_name)
                return
            if current.type in {"type_of_clause", "fields_expression", "alias_expression"}:
                self.diagnostic("APEX_SOQL_CONSTRUCT_UNSUPPORTED", "TYPEOF, wildcard field groups and aliases are outside bounded SOQL coverage.", current)
                return
            if current.type == "bound_apex_expression":
                for expression in current.named_children:
                    self.visit(expression)
                return
            if current.type == "field_identifier":
                name = self.unit.text(current)
                if name.casefold() in aliases:
                    self.diagnostic("APEX_SOQL_ALIAS_UNSUPPORTED", "This name matches a SELECT alias; it is not resolved as an object field.", current)
                elif not API_NAME.fullmatch(name):
                    resolved = resolve_source_field_path(self.store, owner, name)
                    if resolved is None:
                        self.diagnostic("APEX_SOQL_FIELD_PATH_UNSUPPORTED", "No unique declared or supported standard parent path establishes the terminal field owner.", current)
                        return
                    location = self.unit.location(current)
                    target = self.store.field(resolved["owner"], resolved["field"], location)
                    if target is None:
                        return
                    path_resolution = {"basis": "declared_parent_path", "root_object": owner,
                                       "steps": resolved["steps"], "parser": PARSER_VERSION}
                    path_qualifiers = {**qualifiers, "clause": clause_name}
                    self.store.relation(self.method.id, target, relation, location, "apex.static_soql_parent_field",
                                        resolution=path_resolution, qualifiers={**path_qualifiers, "field_path": name})
                    add_navigation_reads(self.store, self.method.id, location,
                                         {**path_resolution, "relationship_path": name}, rule="apex.static_soql_navigation",
                                         qualifiers={**path_qualifiers, "context": "apex_soql_navigation"})
                else:
                    self.edge(owner, name, relation, current, "apex.static_soql_field",
                              {**qualifiers, "clause": clause_name})
                return
            for child in current.named_children:
                clause(child, "filters" if child.type == "having_clause" else relation,
                       "having" if child.type == "having_clause" else clause_name)

        for current in body.named_children:
            if current.type == "from_clause":
                continue
            relation = {"select_clause": "reads", "where_clause": "filters", "having_clause": "filters",
                        "group_by_clause": "references", "order_by_clause": "references"}.get(current.type)
            if relation:
                clause(current, relation, current.type.removesuffix("_clause"))
            elif current.type in {"limit_clause", "offset_clause", "all_rows_clause", "for_clause", "with_clause"}:
                # Expose unhandled clauses instead of assuming they cannot carry references.
                self.diagnostic("APEX_SOQL_CLAUSE_UNSUPPORTED", "This SOQL clause is outside dependency extraction coverage.", current)
            else:
                self.diagnostic("APEX_SOQL_CLAUSE_UNSUPPORTED", "The pinned grammar produced an unhandled SOQL clause.", current)

    def invocation(self, node):
        object_node, name_node = node.child_by_field_name("object"), node.child_by_field_name("name")
        object_name, name = self.unit.text(object_node), self.unit.text(name_node)
        args_node = node.child_by_field_name("arguments")
        args = _significant(args_node)
        if self._platform_invocation(node, object_node, name, args):
            for arg in args:
                self.visit(arg)
            return
        # A corroborated namespace.Class receiver is a type qualifier, not a
        # record field access. Resolve it before visiting arbitrary receivers;
        # argument expressions still carry their own dependencies/diagnostics.
        if self._managed_invocation(node):
            for arg in args:
                self.visit(arg)
            return
        if self._member_invocation(node, object_node, name, args):
            for arg in args:
                self.visit(arg)
            return
        if object_node is not None and object_node.type != "identifier":
            self.visit(object_node)
        setting = self._setting_owner(node)
        if setting is not None:
            self.edge(setting, None, "reads", node, "apex.custom_setting_accessor",
                      {"operation": "custom_setting", "field_set": "partial_explicit"},
                      {"basis": "local_custom_setting_metadata", "object": setting})
            for arg in args:
                self.visit(arg)
            return
        is_database = (object_name.casefold() == "database" and object_node is not None
                       and object_node.type == "identifier"
                       and self.unshadowed_qualifier(object_name, object_node)
                       and "database" not in self.index.apex_types)
        if is_database and name.casefold() in _DYNAMIC_SOQL and not (name.casefold() == "getquerylocator" and args and args[0].type == "query_expression"):
            self.diagnostic("APEX_DYNAMIC_SOQL_UNSUPPORTED", "Database query methods are not parsed as literal SOQL; runtime query construction is unresolved.", node, "dynamic")
        elif is_database and name.casefold() == "getquerylocator" and args and args[0].type == "query_expression":
            pass  # The bracketed query is parsed below; no string evaluation occurs.
        elif is_database and name.casefold() in _DML:
            self.dml(node, name.casefold(), args[0] if args else None)
            for arg in args[1:]:
                self.visit(arg)
            return
        elif not object_name or object_name.casefold() == "this" or (object_name.casefold() == self.unit.name.casefold()
                                  and self.binding(object_name, object_node, report=False) is None):
            candidates = _method_candidates(self.index, self.store, self.unit.class_id, name, len(args), include_ambiguous=True)
            target, overload = self._select_method(candidates, args)
            if (target is not None and (target.static or (not self.method.static and object_name.casefold() in {"", "this"}))
                    and (not overload or target.unit.class_id == self.unit.class_id
                         or _modifiers(target.unit.data, target.node)[0] & {"public", "global", "protected"})):
                static = target.static
                self.store.relation(self.method.id, target.id, "calls", self.unit.location(node),
                                    "apex.local_static_call" if static else "apex.local_instance_call",
                                     resolution={"basis": "unique_local_static_method" if static else "local_instance_method_candidate",
                                                 **overload},
                                    qualifiers={"invocation": "local_static_method" if static else "local_instance_method"})
            elif not self._ambiguous_call(node, candidates):
                if self.unit.has_superclass and _class_hierarchy(self.index, self.store, self.unit.class_id) is None:
                    self.diagnostic("APEX_INHERITED_DISPATCH_UNSUPPORTED", "The local inheritance chain is unavailable or ambiguous; no inherited target is selected.", node)
                self.diagnostic("APEX_CALL_UNRESOLVED", "The local call has no unique supported target with matching arity; parameter-type overload resolution remains deferred.", node,
                                "ambiguous" if len(candidates) > 1 else "unresolved")
        else:
            # Literal cross-class static calls can extend a trigger's handler
            # path, but a variable/member or inherited overload must never be
            # mistaken for a class-qualified reference.
            cid = component_id("apexclass", object_name) if API_NAME.fullmatch(object_name) else None
            candidates = (_method_candidates(self.index, self.store, cid, name, len(args), include_ambiguous=True)
                          if cid else [])
            literal_class = (object_node is not None and object_node.type == "identifier"
                             and self.unshadowed_qualifier(object_name, object_node)
                             and cid in self.store.components
                             and not self.store.is_ambiguous(cid))
            target, overload = self._select_method(candidates, args) if literal_class else (None, {})
            if target is not None and target.static and _modifiers(target.unit.data, target.node)[0] & {"public", "global"}:
                self.store.relation(self.method.id, target.id, "calls", self.unit.location(node),
                                     "apex.cross_class_static_call", resolution={"basis": "unique_local_static_method", **overload},
                                    qualifiers={"invocation": "static_method"})
            elif self._candidate_static_invocation(node, object_node, object_name, candidates):
                pass
            elif self._instance_invocation(node):
                pass
            elif not (literal_class and self._ambiguous_call(node, candidates)):
                self.diagnostic("APEX_EXTERNAL_CALL_UNSUPPORTED", "No unique literal local static target is proven; external, instance and inherited dispatch remain unresolved.", node)
        for arg in args:
            self.visit(arg)

    def _platform_invocation(self, node, receiver, name, args):
        if receiver is None:
            return False
        name = name.casefold()
        if receiver.type == "identifier":
            qualifier = self.unit.text(receiver).casefold()
            root = qualifier
        elif (name == "publish" and receiver.type == "field_access"
              and self.unit.text(receiver.child_by_field_name("object")).casefold() == "system"
              and self.unit.text(receiver.child_by_field_name("field")).casefold() == "eventbus"):
            qualifier, root = "eventbus", "system"
        else:
            return False
        key = qualifier, name
        if (key != ("eventbus", "publish") and key not in _ASYNC_APIS
                or root in self.index.apex_types):
            return False
        if not self.unshadowed_qualifier(root, receiver):
            if self.unshadowed_qualifier(root, receiver, allow_external=True):
                self.diagnostic("APEX_PLATFORM_RECEIVER_UNRESOLVED",
                                "An unavailable ancestor may shadow this platform API qualifier.", node, "unresolved")
                if key == ("eventbus", "publish") and self.external_hierarchy is not None:
                    self._event_publication(node, args, missing_ancestors=self.external_hierarchy[1])
                return True
            return False
        if key == ("eventbus", "publish"):
            self._event_publication(node, args)
        elif key in _ASYNC_APIS:
            self._async_invocation(node, args, _ASYNC_APIS[key])
        else:
            return False
        return True

    def _event_publication(self, node, args, *, missing_ancestors=None):
        target = _without_parentheses(args[0]) if len(args) in {1, 2} else None
        owner, collection = None, False
        if target is not None and target.type == "identifier":
            binding = self.binding(self.unit.text(target), target, report=False)
            if binding:
                owner, collection = binding.owner, binding.collection
        elif target is not None and target.type in {"object_creation_expression", "array_creation_expression", "cast_expression"}:
            owner, collection = self.record_type(target.child_by_field_name("type"))
            if target.type == "array_creation_expression":
                dimensions = sum(child.type == "[" for dimension in target.children_by_field_name("dimensions")
                                 for child in dimension.children)
                if dimensions > 1 or (dimensions and collection):
                    owner = None
                elif dimensions == 1:
                    collection = True
        if owner and owner.casefold().endswith("__e"):
            qualifiers = {"operation": "publish", "effect": "event_bus", "collection": collection, "field_set": "unknown"}
            resolution = {"basis": "typed_platform_event", "parser": PARSER_VERSION}
            if missing_ancestors is None:
                self.edge(owner, None, "writes", node, "apex.platform_event_publish", qualifiers, resolution)
            else:
                qualifiers.update(resolution_status="candidate", uncertainty="external_hierarchy",
                                  missing_ancestors=missing_ancestors)
                resolution.update(basis="typed_platform_event_unverified_hierarchy", missing_ancestors=missing_ancestors)
            self.edge(owner, None, "publishes", node, "apex.event_bus_publish", qualifiers, resolution)
        elif missing_ancestors is None:
            self.diagnostic("APEX_PLATFORM_EVENT_PUBLISH_UNRESOLVED",
                            "No concrete platform event type is established for this EventBus.publish argument.", node, "unresolved")

    def _async_invocation(self, node, args, api):
        api_name, argument_index, arities, contract = api
        argument = _without_parentheses(args[argument_index]) if len(args) in arities else None
        receiver_is_this = argument is not None and argument.type == "this" and not self.method.static
        class_id = (self.unit.class_id if receiver_is_this
                    else self._constructed_class(argument, node, require_immutable=True))
        hierarchy = _class_hierarchy(self.index, self.store, class_id) if class_id else None
        namespace, entries = _ASYNC_ENTRIES[contract]
        if not hierarchy or not self._implements_async_contract(hierarchy, namespace, contract):
            self.diagnostic("APEX_ASYNC_TARGET_UNRESOLVED", f"No concrete local {contract} implementation is established for {api_name}.",
                            node, "unresolved")
            return
        context = contract.casefold() + "context"
        for method_name, arity in entries:
            candidates = [method for method in _method_candidates(self.index, self.store, class_id, method_name, arity, include_ambiguous=True)
                          if self._async_type_matches(method.parameter_types[0], namespace, context, hierarchy)]
            target = candidates[0] if len(candidates) == 1 else None
            flags = _modifiers(target.unit.data, target.node)[0] if target is not None else set()
            if (target is None or self.store.is_ambiguous(target.id) or target.static
                    or not flags & {"public", "global"} or (receiver_is_this and flags & {"virtual", "override", "abstract"})
                    or target.node.child_by_field_name("body") is None):
                self.diagnostic("APEX_ASYNC_ENTRY_UNRESOLVED", f"No unique local {contract}.{method_name} implementation is established.",
                                node, "ambiguous" if len(candidates) > 1 else "unresolved")
                continue
            self.store.relation(self.method.id, target.id, "calls", self.unit.location(node), "apex.async_dispatch",
                                qualifiers={"invocation": "async_dispatch", "async_api": api_name, "entry": method_name},
                                resolution={"basis": "local_async_contract", "contract": contract,
                                            "job_class": class_id, "parser": PARSER_VERSION})

    def _async_type_matches(self, type_name, namespace, name, hierarchy):
        for spelling, base in ((name, name), (namespace.casefold() + "." + name, namespace.casefold())):
            if (type_name.casefold() == spelling and base not in self.index.apex_types
                    and all(base not in unit.declared_types for unit in hierarchy)):
                return True
        return False

    def _implements_async_contract(self, hierarchy, namespace, contract):
        for unit in hierarchy:
            interfaces = unit.class_node.child_by_field_name("interfaces")
            if interfaces is None:
                continue
            for node in _significant(_child(interfaces, "type_list")):
                head = _significant(node)[0] if node.type == "generic_type" else node
                name = re.sub(r"\s+", "", unit.text(head))
                if self._async_type_matches(name, namespace, contract.casefold(), hierarchy):
                    return True
        return False

    def _candidate_static_invocation(self, node, receiver, name, candidates):
        """Expose a local static target without claiming an unknown base is safe."""
        if (self.external_hierarchy is None or receiver is None or receiver.type != "identifier"
                or not API_NAME.fullmatch(name) or name.casefold() in _RESERVED_RECEIVERS
                or self.has_binding(name, receiver) or len(candidates) != 1):
            return False
        hierarchy, missing = self.external_hierarchy
        if any(name.casefold() in unit.declared_members or name.casefold() in unit.declared_types
               for unit in hierarchy):
            return False
        target = candidates[0]
        if (not target.static or self.store.is_ambiguous(target.id)
                or not _modifiers(target.unit.data, target.node)[0] & {"public", "global"}):
            return False
        target_hierarchy = _class_hierarchy(self.index, self.store, component_id("apexclass", name))
        if target_hierarchy is None or any(
            self.store.is_ambiguous(method.id)
            for unit in target_hierarchy for method in self.index.methods.get(unit.class_id, [])
            if method.name.casefold() == target.name.casefold() and method.parameter_count == target.parameter_count
        ):
            return False
        location = self.unit.location(node)
        self.store.relation(
            self.method.id, target.id, "calls", location, "apex.candidate_static_call",
            qualifiers={"invocation": "static_method", "resolution_status": "candidate",
                        "uncertainty": "external_hierarchy", "missing_ancestors": missing},
            resolution={"basis": "local_static_target_unverified_hierarchy",
                        "missing_ancestors": missing, "parser": PARSER_VERSION},
        )
        self.store.diagnostic(
            "APEX_CALL_CANDIDATE",
            "A unique local static target is a candidate: the absent ancestor may shadow its class qualifier.",
            location, state="ambiguous", candidates=[target.id], missing_ancestors=missing,
        )
        return True

    def _managed_invocation(self, node):
        """Retain a literal namespace.Class.method target without claiming org existence."""
        receiver = node.child_by_field_name("object")
        if receiver is None or receiver.type != "field_access":
            return False
        namespace = receiver.child_by_field_name("object")
        cls = receiver.child_by_field_name("field")
        if namespace is None or namespace.type != "identifier" or cls is None:
            return False
        names = [self.unit.text(namespace), self.unit.text(cls), self.unit.text(node.child_by_field_name("name"))]
        if (not all(API_NAME.fullmatch(name) for name in names)
                or names[0].casefold() not in self.index.namespaces or names[0].casefold() in _RESERVED_RECEIVERS
                or not self.unshadowed_qualifier(names[0], node)
                or names[0].casefold() in self.index.apex_types):
            return False
        target = self.store.resolve_component("apexmethod", ".".join(names), self.unit.location(node))
        if target:
            self.store.ensure_component("apexclass", ".".join(names[:2]))
            self.store.relation(self.method.id, target, "calls", self.unit.location(node), "apex.namespace_static_call",
                                qualifiers={"invocation": "namespace_static_method"},
                                resolution={"basis": "literal_namespace_class_method", "target_validation": "referenced_only",
                                            "parser": PARSER_VERSION})
        return True

    def _visible_inner_class(self, outer_name, inner_name):
        class_id = component_id("apexclass", f"{outer_name}.{inner_name}")
        unit = self.index.units_by_class.get(class_id)
        if unit is None or unit.enclosing is None:
            return None
        caller = self.unit.enclosing or self.unit
        flags, _ = _modifiers(unit.data, unit.class_node)
        if caller.class_id == unit.enclosing.class_id or flags & {"public", "global"}:
            return class_id
        return None

    def _literal_local_class(self, type_node):
        hierarchy = self.qualifier_hierarchy
        if type_node is None or hierarchy is None:
            return None
        if type_node.type == "scoped_type_identifier":
            names = [self.unit.text(part) for part in _significant(type_node)]
            if (len(names) == 2 and all(API_NAME.fullmatch(name) for name in names)
                    and all(names[0].casefold() not in unit.declared_types for unit in hierarchy)):
                return self._visible_inner_class(*names)
            return None
        name = self.unit.text(type_node)
        if type_node.type != "type_identifier" or not API_NAME.fullmatch(name):
            return None
        outer = self.unit.enclosing or self.unit
        if name.casefold() in outer.declared_types:
            return self._visible_inner_class(outer.name, name)
        if (name.casefold() in self.index.object_names or name.casefold() in _STANDARD_OBJECTS
                or any(name.casefold() in unit.declared_types for unit in hierarchy)):
            return None
        class_id = component_id("apexclass", name)
        return class_id if class_id in self.index.units_by_class else None

    def _constructed_class(self, receiver, node, *, require_immutable=False):
        if receiver is None:
            return None
        value = receiver
        if receiver.type == "identifier":
            binding = self.binding(self.unit.text(receiver), node, report=False)
            if binding is None:
                return None
            value = binding.node.child_by_field_name("value")
            for current in _walk(self.body):
                if current.start_byte < binding.available_from:
                    continue
                # A later assignment can reach a submission on a subsequent
                # iteration. Async targets require an unreassigned local.
                if not require_immutable and current.start_byte >= node.start_byte:
                    continue
                left = current.child_by_field_name("left") if current.type == "assignment_expression" else None
                if left is not None and left.type == "identifier" and self.unit.text(left).casefold() == binding.name.casefold():
                    return None
        if value is None or value.type != "object_creation_expression":
            return None
        type_node = value.child_by_field_name("type")
        if self.qualifier_hierarchy is None:
            # Keep direct construction evidence for ordinary class names when
            # the caller's superclass is unavailable.
            name = self.unit.text(type_node)
            if API_NAME.fullmatch(name) and name.casefold() not in self.unit.declared_types:
                class_id = component_id("apexclass", name)
                return class_id if class_id in self.index.units_by_class else None
        return self._literal_local_class(type_node)

    def _member_invocation(self, node, receiver, method_name, arguments):
        hierarchy = self.qualifier_hierarchy
        receiver = _without_parentheses(receiver)
        if receiver is None or hierarchy is None:
            return False
        explicit = receiver.type == "field_access"
        if explicit:
            if self.unit.text(receiver.child_by_field_name("object")).casefold() != "this":
                return False
            receiver = receiver.child_by_field_name("field")
        if receiver is None or receiver.type != "identifier":
            return False
        member_name = self.unit.text(receiver)
        key = member_name.casefold()
        if not explicit and key in _RESERVED_RECEIVERS:
            return False
        for depth, unit in enumerate(hierarchy):
            if key not in unit.declared_members:
                continue
            if not explicit and (self.has_binding(member_name, receiver) or self._initializing_binding(member_name, receiver)):
                return False
            declarations = unit.member_declarations.get(key, ())
            if len(declarations) != 1:
                self.diagnostic("APEX_MEMBER_CALL_UNRESOLVED", "Expected one receiver member declaration.",
                                receiver, "ambiguous" if declarations else "unresolved")
            else:
                self._call_declared_member(node, receiver, declarations[0], hierarchy[depth:],
                                           explicit, method_name, arguments)
            return True
        return False

    def _call_declared_member(self, node, receiver, member, declaring_hierarchy, explicit, method_name, arguments):
        static_member = "static" in member.modifiers
        if ((explicit and (self.method.static or static_member))
                or (self.method.static and not static_member)
                or (member.unit.class_id != self.unit.class_id
                    and not member.modifiers & {"public", "global", "protected"})):
            self.diagnostic("APEX_MEMBER_CALL_UNRESOLVED", "The receiver member is unavailable in this context.",
                            receiver, "unresolved")
            return
        declaration = member.node.parent
        if declaration is not None and _child(declaration, "accessor_list") is not None:
            self.diagnostic("APEX_MEMBER_CALL_UNRESOLVED", "The receiver is a property with unverified getter access.",
                            receiver, "unresolved")
            return
        declared_type = member.unit.text(member.type_node)
        folded = declared_type.casefold()
        class_id = (component_id("apexclass", declared_type)
                    if member.type_node.type == "type_identifier" and API_NAME.fullmatch(declared_type) else None)
        if (class_id is None or class_id not in self.index.units_by_class or folded in _STANDARD_OBJECTS
                or folded in self.index.object_names or folded in _PRIMITIVES or folded in _BUILTIN_COLLECTION_ARITY
                or any(folded in unit.declared_types for unit in declaring_hierarchy)):
            self.diagnostic("APEX_MEMBER_CALL_UNRESOLVED", "The receiver member type is not a unique local Apex class.",
                            receiver, "unresolved")
            return
        candidates = _method_candidates(self.index, self.store, class_id, method_name, len(arguments), include_ambiguous=True)
        target, overload = self._select_method(candidates, arguments)
        if target is None:
            if not self._ambiguous_call(node, candidates, code="APEX_MEMBER_CALL_UNRESOLVED"):
                self.diagnostic("APEX_MEMBER_CALL_UNRESOLVED", "The declared receiver type has no unique supported local method.",
                                node, "unresolved")
            return
        flags, _ = _modifiers(target.unit.data, target.node)
        if target.static or not flags & {"public", "global"}:
            self.diagnostic("APEX_MEMBER_CALL_UNRESOLVED", "The declared receiver type has no accessible instance method.",
                            node, "unresolved")
            return
        if flags & {"virtual", "abstract", "override"} or target.node.child_by_field_name("body") is None:
            self.diagnostic("APEX_MEMBER_CALL_VIRTUAL_UNRESOLVED",
                            "The declared member type does not establish which virtual method implementation is called.",
                            node, "unresolved")
            return
        self.store.relation(self.method.id, target.id, "calls", self.unit.location(node),
                            "apex.declared_member_call", qualifiers={"invocation": "declared_member_method"},
                            resolution={"basis": "unique_local_declared_member_method", "receiver_type": class_id,
                                        "member_declaration": member.unit.location(member.node),
                                        "parser": PARSER_VERSION, **overload})

    def _instance_invocation(self, node):
        cid = self._constructed_class(node.child_by_field_name("object"), node)
        if not cid:
            return False
        args = _significant(node.child_by_field_name("arguments"))
        candidates = _method_candidates(self.index, self.store, cid, self.unit.text(node.child_by_field_name("name")),
                                        len(args), include_ambiguous=True)
        target, overload = self._select_method(candidates, args)
        if target is None or target.static or not (_modifiers(target.unit.data, target.node)[0] & {"public", "global"}):
            return self._ambiguous_call(node, candidates)
        self.store.relation(self.method.id, target.id, "calls", self.unit.location(node), "apex.constructed_instance_call",
                            qualifiers={"invocation": "constructed_instance_method"},
                             resolution={"basis": "explicit_constructor_type", "class": cid, "parser": PARSER_VERSION, **overload})
        return True

    def visit(self, node):
        if node.type in _IGNORED:
            return
        if node.type == "query_expression":
            self.query(node)
        elif node.type == "dml_expression":
            operation = _child(node, "dml_type")
            self.dml(node, self.unit.text(operation).casefold(), node.child_by_field_name("target"))
            extra = node.child_by_field_name("upsert_key")
            if extra is not None:
                self.diagnostic("APEX_DML_KEY_UNSUPPORTED", "Upsert external-key semantics are outside bounded DML coverage.", extra)
        elif node.type == "method_invocation":
            self.invocation(node)
        elif node.type == "explicit_constructor_invocation":
            self.constructor_delegation(node)
        elif node.type == "assignment_expression":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and left.type == "field_access":
                self.field_access(left, "writes", operation="assignment")
                if self.unit.text(node.child_by_field_name("operator")) != "=":
                    self.field_access(left, "reads", operation="assignment")
            elif left is not None and left.type not in {"identifier"}:
                self.diagnostic("APEX_ASSIGNMENT_TARGET_UNSUPPORTED", "This assignment target requires unsupported data-flow resolution.", left)
            if right is not None:
                self.visit(right)
        elif node.type == "field_access":
            self.field_access(node)
        elif node.type == "object_creation_expression":
            self.constructor(node)
        elif node.type in {"update_expression"}:
            target = next(iter(node.named_children), None)
            if target is not None and target.type == "field_access":
                self.field_access(target, "reads", operation="assignment")
                self.field_access(target, "writes", operation="assignment")
            else:
                # The prefix form has the operator before the operand in this
                # grammar. Keep the existing record-field traversal bounded.
                operand = node.child_by_field_name("operand")
                if (operand is None or operand.type != "field_access"
                        or not self._static_member_reference(operand, "writes")):
                    self.diagnostic("APEX_ASSIGNMENT_TARGET_UNSUPPORTED", "Increment/decrement target is outside direct-field coverage.", node)
        else:
            for child in node.named_children:
                self.visit(child)

    def run(self):
        if self.body is None:
            self.diagnostic("APEX_METHOD_BODY_UNAVAILABLE", "No local method body is available for dependency extraction.", self.method.node)
            return
        self.index_bindings()
        self.visit(self.body)


def extract_apex(index: ApexIndex, store: FactStore) -> None:
    """Extract source dependencies of indexed Apex methods and constructors."""
    for unit in index.units:
        if unit.enclosing is not None and not store.is_ambiguous(unit.class_id):
            store.relation(unit.enclosing.class_id, unit.class_id, "contains", unit.location(unit.class_node),
                           "apex.inner_class_owner")
        if unit.superclass_name and not store.is_ambiguous(unit.class_id):
            node = unit.class_node.child_by_field_name("superclass")
            target = store.resolve_component("apexclass", unit.superclass_name, unit.location(node))
            if target:
                store.relation(unit.class_id, target, "references", unit.location(node), "apex.declared_superclass",
                               qualifiers={"context": "inheritance"}, resolution={"basis": "declared_superclass"})
    for class_id, methods in index.methods.items():
        if store.is_ambiguous(class_id):
            continue
        for method in methods:
            if store.is_ambiguous(method.id):
                continue
            store.relation(class_id, method.id, "contains", method.unit.location(method.node), "apex.method_owner")
            _MethodExtractor(method, index, store).run()
    for class_id, constructors in index.constructors.items():
        if store.is_ambiguous(class_id):
            continue
        for constructor in constructors:
            if store.is_ambiguous(constructor.id):
                continue
            store.relation(class_id, constructor.id, "contains", constructor.unit.location(constructor.node),
                           "apex.constructor_owner")
            _MethodExtractor(constructor, index, store).run()


def resolve_invocable_calls(store: FactStore, index: ApexIndex) -> None:
    """Add Flow-to-method evidence without replacing original class-level calls."""
    for relationship in list(store.relationships.values()):
        if relationship["relation"] != "calls" or relationship["qualifiers"].get("action_kind") != "apex":
            continue
        if store.components[relationship["source"]]["kind"] != "flow":
            continue
        class_id = relationship["target"]
        if store.components[class_id]["kind"] != "apexclass":
            continue
        methods = [m for m in index.methods.get(class_id, []) if m.invocable]
        unique = (len(methods) == 1 and methods[0].invocable_valid
                  and not store.is_ambiguous(class_id) and not store.is_ambiguous(methods[0].id))
        for evidence_id in relationship["evidence_ids"]:
            occurrence = store.evidence[evidence_id]
            location = {key: occurrence[key] for key in ("path", "locator", "line", "column", "end_line", "end_column", "content_hash")}
            if not unique:
                store.diagnostic("APEX_INVOCABLE_TARGET_UNRESOLVED", "The Apex action does not have exactly one supported local invocable declaration.",
                                 location, state="ambiguous" if len(methods) > 1 else "unresolved", candidates=sorted({m.id for m in methods}))
                continue
            method = methods[0]
            definition = method.unit.location(method.node)
            store.relation(relationship["source"], method.id, "calls", location, "apex.unique_invocable_call",
                           qualifiers={**relationship["qualifiers"], "invocation": "invocable_method"},
                           resolution={"basis": "unique_local_invocable", "class": class_id,
                                       "declaration": {key: definition[key] for key in ("path", "locator")}})
