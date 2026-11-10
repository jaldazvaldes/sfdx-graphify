"""Bounded Apex/SOQL dependencies from a pinned, local Tree-sitter grammar.

Grammar: aheber/tree-sitter-sfapex, distributed by tree-sitter-language-pack
0.11.0. This module never runs Apex, resolves packages remotely, or evaluates
query strings. Syntax evidence does not establish compilation or org behavior.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import hashlib
import re
from pathlib import PurePosixPath
from urllib.parse import quote

from tree_sitter import Node, Tree

from .schema import API_NAME, FactStore, component_id
from .relationship_paths import add_navigation_reads, resolve_source_field_path


PARSER_VERSION = "tree-sitter-language-pack==0.11.0/apex"
_PRIMITIVES = frozenset("blob boolean date datetime decimal double id integer long object sobject string time void aggregateresult exception".split())
# A bounded standard type vocabulary, not a complete Salesforce schema.
_STANDARD_OBJECTS = frozenset("account contact lead opportunity case task event user group order orderitem asset campaign campaignmember product2 pricebook2 pricebookentry contract quote quotelineitem contentversion contentdocument contentdocumentlink recordtype".split())
_DML = frozenset({"insert", "update", "upsert", "delete", "undelete"})
_DYNAMIC_SOQL = frozenset({"query", "querywithbinds", "countquery", "countquerywithbinds", "getquerylocator", "getquerylocatorwithbinds"})
_COMMENTS = frozenset({"line_comment", "block_comment"})
_IGNORED = _COMMENTS | {"string_literal", "multi_line_string_literal"}
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_NODES = 200_000
_RESERVED_RECEIVERS = frozenset({"trigger", "system", "schema", "database", "label", "page", "apexpages"})


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
    methods: dict[str, list[Method]] = field(default_factory=lambda: defaultdict(list))
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
    from_clause = body.child_by_field_name("from_clause") if body is not None else None
    children = _significant(from_clause)
    if from_clause is None or len(children) != 1:
        return None
    storage = children[0]
    name = unit.text(storage)
    return name if storage.type == "storage_identifier" and API_NAME.fullmatch(name) else None


def index_apex(apex: dict[str, bytes], store: FactStore) -> ApexIndex:
    """Index outer methods; parser errors never produce recovered dependency facts."""
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
        body = cls.child_by_field_name("body")
        if body is None:
            continue
        unit.has_superclass = cls.child_by_field_name("superclass") is not None
        if unit.has_superclass:
            superclass = cls.child_by_field_name("superclass")
            candidates = [n for n in _significant(superclass) if n.type in {"type_identifier", "scoped_type_identifier"}]
            parent_name = unit.text(candidates[0]) if len(candidates) == 1 else ""
            if parent_name and all(API_NAME.fullmatch(p) for p in parent_name.split(".")):
                unit.superclass_name = parent_name
        for member in body.named_children:
            if member.type == "field_declaration":
                for declaration in member.named_children:
                    if declaration.type == "variable_declarator":
                        unit.declared_members.add(unit.text(declaration.child_by_field_name("name")).casefold())
        for node in body.named_children:
            if node.type in _IGNORED:
                continue
            if node.type != "method_declaration":
                store.diagnostic("APEX_CLASS_MEMBER_UNSUPPORTED", "Only outer method bodies are analyzed; this class member is outside coverage.",
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
            valid = bool(invocable and "static" in modifiers and modifiers & {"public", "global"}
                         and supported_signature and node.child_by_field_name("body") is not None
                         and annotations <= {"invocablemethod", "deprecated"})
            # Register methods after every class definition has been indexed.
            # A unique method name does not disambiguate a duplicate owner class.
            method_id = component_id("apexmethod", name + "." + method_name)
            method = Method(unit, node, method_id, method_name, "static" in modifiers, invocable, valid, len(parameters),
                            is_test=unit.is_test or "istest" in annotations or "testmethod" in modifiers,
                            parameter_types=tuple(re.sub(r"\s+", "", re.sub(r"/\*[\s\S]*?\*/|//[^\r\n]*", "",
                                                  unit.text(p.child_by_field_name("type")))) for p in parameters))
            index.methods[unit.class_id].append(method)
            if invocable and not valid:
                store.diagnostic("APEX_INVOCABLE_SIGNATURE_UNSUPPORTED", "The invocable declaration does not satisfy the supported outer static public/global list signature.", unit.location(node))
        invocables = [m for m in index.methods[unit.class_id] if m.invocable]
        if len(invocables) > 1:
            store.diagnostic("APEX_INVOCABLE_AMBIGUOUS", "More than one local invocable declaration names this class.", unit.location(cls), state="ambiguous")
    # Class names from failed/unsupported files also prevent object-type guessing.
    index.apex_types.update(PurePosixPath(path).name.removesuffix(".cls").casefold() for path in apex)
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
            store.ensure_component("apexmethod", method.api_name, method.unit.location(method.node),
                                   invocable=method.invocable, invocable_signature_supported=method.invocable_valid,
                                   static=method.static, parser=PARSER_VERSION, is_test=method.is_test,
                                   qualified_name=method.qualified_name, arity=method.parameter_count,
                                   parameter_types=list(method.parameter_types))
        for method in methods:
            if store.is_ambiguous(method.id):
                store.components[method.id]["is_test"] = None
                store.diagnostic("APEX_METHOD_AMBIGUOUS", "Duplicate method signatures share this identity; no target is selected.",
                                 method.unit.location(method.node), state="ambiguous", candidates=[method.id])
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
        units = [unit for unit in index.units if unit.class_id == class_id]
        if len(units) != 1:
            return None
        unit = units[0]
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
        units = [unit for unit in index.units if unit.class_id == class_id]
        if not units:
            if (not hierarchy or (component and component.get("definitions"))
                    or (component and component["api_name"].casefold() in index.apex_types)):
                return None
            return hierarchy, [hierarchy[-1].superclass_name]
        if len(units) != 1 or units[0].parse_error:
            return None
        unit = units[0]
        hierarchy.append(unit)
        if not unit.has_superclass or not unit.superclass_name:
            return None
        class_id = component_id("apexclass", unit.superclass_name)
    return None


def _method_candidates(index, store, class_id, name, arity):
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
    return [method for method in selected.values() if not store.is_ambiguous(method.id)]


class _MethodExtractor:
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
                while scope is not None and scope.type not in {"block", "for_statement", "enhanced_for_statement"}:
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

    def unshadowed_qualifier(self, name, node):
        """Prove a class/namespace receiver is not a lexical or inherited member.

        An unavailable ancestor may declare a field, property or inner type with
        this name. A complete local hierarchy lets us check those declarations
        instead of rejecting every qualified call made from a subclass.
        """
        return (self.qualifier_hierarchy is not None and not self.has_binding(name, node)
                and all(name.casefold() not in unit.declared_members
                        and name.casefold() not in unit.declared_types
                        for unit in self.qualifier_hierarchy))

    def edge(self, owner, name, relation, node, rule, qualifiers, resolution=None):
        location = self.unit.location(node)
        target = self.store.field(owner, name, location) if name else self.store.resolve_component("object", owner, location)
        if target:
            self.store.relation(self.method.id, target, relation, location, rule,
                                resolution=resolution or {"basis": "apex_ast", "parser": PARSER_VERSION, "object": owner},
                                qualifiers=qualifiers)

    def field_access(self, node, relation="reads", *, operation="field_access"):
        resource, field_node = node.child_by_field_name("object"), node.child_by_field_name("field")
        if field_node is not None and self.unit.text(field_node).casefold() == "class":
            name = self.unit.text(resource)
            pieces = name.split(".")
            if (len(pieces) in {1, 2} and all(API_NAME.fullmatch(p) for p in pieces)
                    and not self.has_binding(pieces[0], node) and pieces[0].casefold() not in self.unit.declared_members):
                target = self.store.resolve_component("apexclass", name, self.unit.location(node))
                if target:
                    self.store.relation(self.method.id, target, "references", self.unit.location(node),
                                        "apex.class_literal", qualifiers={"context": "class_literal"},
                                        resolution={"basis": "literal_class_token", "parser": PARSER_VERSION})
                return
        if resource is None or resource.type != "identifier" or field_node is None:
            self.diagnostic("APEX_FIELD_PATH_UNSUPPORTED", "Only one direct field on a typed local record is supported; paths and indexed collections are not truncated.", node)
            # Preserve calls inside a complex receiver without extracting a
            # shorter, misleading field path from the unsupported chain.
            receiver = resource
            while receiver is not None and receiver.type == "field_access":
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
        owner, collection = self.record_type(node.child_by_field_name("type"))
        args = node.child_by_field_name("arguments")
        assignments = _significant(args)
        if not owner or collection:
            self.diagnostic("APEX_CONSTRUCTOR_TYPE_UNRESOLVED", "The constructor does not establish a supported single-record object type.", node, "unresolved")
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

    def query(self, node):
        owner = _query_owner(self.unit, node)
        body = _child(node, "soql_query_body")
        if not owner or body is None:
            self.diagnostic("APEX_SOQL_ROOT_UNSUPPORTED", "Static SOQL requires one literal root object without aliases or relationship traversal.", node)
            return None
        self.edge(owner, None, "reads", node, "apex.static_soql_object", {"operation": "soql", "effect": "database", "field_set": "partial_explicit"})
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
            if current.type in {"subquery", "type_of_clause", "fields_expression", "alias_expression"}:
                self.diagnostic("APEX_SOQL_CONSTRUCT_UNSUPPORTED", "Subqueries, TYPEOF, wildcard field groups and aliases are outside bounded SOQL coverage.", current)
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
                    resolution = {"basis": "declared_parent_path", "root_object": owner,
                                  "steps": resolved["steps"], "parser": PARSER_VERSION}
                    qualifiers = {"operation": "soql", "clause": clause_name}
                    self.store.relation(self.method.id, target, relation, location, "apex.static_soql_parent_field",
                                        resolution=resolution, qualifiers={**qualifiers, "field_path": name})
                    add_navigation_reads(self.store, self.method.id, location,
                                         {**resolution, "relationship_path": name}, rule="apex.static_soql_navigation",
                                         qualifiers={**qualifiers, "context": "apex_soql_navigation"})
                else:
                    self.edge(owner, name, relation, current, "apex.static_soql_field", {"operation": "soql", "clause": clause_name})
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
        return owner

    def invocation(self, node):
        object_node, name_node = node.child_by_field_name("object"), node.child_by_field_name("name")
        object_name, name = self.unit.text(object_node), self.unit.text(name_node)
        args_node = node.child_by_field_name("arguments")
        args = _significant(args_node)
        # A corroborated namespace.Class receiver is a type qualifier, not a
        # record field access. Resolve it before visiting arbitrary receivers;
        # argument expressions still carry their own dependencies/diagnostics.
        if self._managed_invocation(node):
            for arg in args:
                self.visit(arg)
            return
        if object_node is not None and object_node.type != "identifier":
            self.visit(object_node)
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
            candidates = _method_candidates(self.index, self.store, self.unit.class_id, name, len(args))
            if (len(candidates) == 1 and (candidates[0].static or (not self.method.static
                    and object_name.casefold() in {"", "this"}))):
                static = candidates[0].static
                self.store.relation(self.method.id, candidates[0].id, "calls", self.unit.location(node),
                                    "apex.local_static_call" if static else "apex.local_instance_call",
                                    resolution={"basis": "unique_local_static_method" if static else "local_instance_method_candidate"},
                                    qualifiers={"invocation": "local_static_method" if static else "local_instance_method"})
            else:
                if self.unit.has_superclass and _class_hierarchy(self.index, self.store, self.unit.class_id) is None:
                    self.diagnostic("APEX_INHERITED_DISPATCH_UNSUPPORTED", "The local inheritance chain is unavailable or ambiguous; no inherited target is selected.", node)
                self.diagnostic("APEX_CALL_UNRESOLVED", "The local call has no unique supported target with matching arity; parameter-type overload resolution remains deferred.", node,
                                "ambiguous" if len(candidates) > 1 else "unresolved")
        else:
            # Literal cross-class static calls can extend a trigger's handler
            # path, but a variable/member or inherited overload must never be
            # mistaken for a class-qualified reference.
            cid = component_id("apexclass", object_name) if API_NAME.fullmatch(object_name) else None
            candidates = _method_candidates(self.index, self.store, cid, name, len(args)) if cid else []
            literal_class = (object_node is not None and object_node.type == "identifier"
                             and self.unshadowed_qualifier(object_name, object_node)
                             and cid in self.store.components
                             and not self.store.is_ambiguous(cid))
            if (literal_class and len(candidates) == 1 and candidates[0].static
                    and candidates[0].parameter_count == len(args)
                    and not self.store.is_ambiguous(candidates[0].id)
                    and _modifiers(candidates[0].unit.data, candidates[0].node)[0] & {"public", "global"}):
                self.store.relation(self.method.id, candidates[0].id, "calls", self.unit.location(node),
                                    "apex.cross_class_static_call", resolution={"basis": "unique_local_static_method"},
                                    qualifiers={"invocation": "static_method"})
            elif self._candidate_static_invocation(node, object_node, object_name, candidates):
                pass
            elif self._instance_invocation(node):
                pass
            else:
                self.diagnostic("APEX_EXTERNAL_CALL_UNSUPPORTED", "No unique literal local static target is proven; external, instance and inherited dispatch remain unresolved.", node)
        for arg in args:
            self.visit(arg)

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

    def _constructed_class(self, receiver, node):
        if receiver is None:
            return None
        value = receiver
        if receiver.type == "identifier":
            binding = self.binding(self.unit.text(receiver), node, report=False)
            if binding is None:
                return None
            value = binding.node.child_by_field_name("value")
            for current in _walk(self.body):
                if current.start_byte < binding.available_from or current.start_byte >= node.start_byte:
                    continue
                left = current.child_by_field_name("left") if current.type == "assignment_expression" else None
                if left is not None and left.type == "identifier" and self.unit.text(left).casefold() == binding.name.casefold():
                    return None
        if value is None or value.type != "object_creation_expression":
            return None
        name = self.unit.text(value.child_by_field_name("type"))
        if not API_NAME.fullmatch(name):
            return None
        cid = component_id("apexclass", name)
        return cid if cid in self.store.components and not self.store.is_ambiguous(cid) else None

    def _instance_invocation(self, node):
        cid = self._constructed_class(node.child_by_field_name("object"), node)
        if not cid:
            return False
        methods = _method_candidates(self.index, self.store, cid, self.unit.text(node.child_by_field_name("name")),
                                     len(_significant(node.child_by_field_name("arguments"))))
        if len(methods) != 1 or methods[0].static or not (_modifiers(methods[0].unit.data, methods[0].node)[0] & {"public", "global"}):
            return False
        self.store.relation(self.method.id, methods[0].id, "calls", self.unit.location(node), "apex.constructed_instance_call",
                            qualifiers={"invocation": "constructed_instance_method"},
                            resolution={"basis": "explicit_constructor_type", "class": cid, "parser": PARSER_VERSION})
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
    """Extract supported static method effects; duplicate definitions stay unresolved."""
    for unit in index.units:
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
