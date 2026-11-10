"""Literal LWC Apex imports and direct uses from a pinned local JavaScript AST.

Import references and imperative/wire calls are separate facts. These are static
candidates, not proof that a component is deployed or that a call executes.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import hashlib
from urllib.parse import quote

from tree_sitter import Node

from .relationship_paths import resolve_source_field_path
from .schema import API_NAME, FactStore, component_id


PARSER_VERSION = "tree-sitter-language-pack==0.11.0/javascript"
_FUNCTIONS = frozenset({"function_declaration", "function_expression", "generator_function",
                        "generator_function_declaration", "arrow_function", "method_definition"})
_BLOCKS = frozenset({"statement_block", "for_statement", "for_in_statement", "switch_statement", "catch_clause",
                     "class_static_block"})
_SKIP = frozenset({"comment", "string", "regex"})
_MAX_BYTES = 8 * 1024 * 1024
_MAX_NODES = 200_000


@dataclass
class Unit:
    path: str
    data: bytes
    content_hash: str
    offsets: list[int]

    def text(self, node):
        return self.data[node.start_byte:node.end_byte].decode("utf-8") if node is not None else ""

    def location(self, node):
        segments = []
        current = node
        while current is not None:
            key = current.type
            name = self.text(current.child_by_field_name("name"))
            if API_NAME.fullmatch(name):
                key += "[@name=" + quote(name, safe="_") + "]"
            if current.parent is not None:
                siblings = [c for c in current.parent.named_children if c.type == current.type]
                if len(siblings) > 1:
                    key += f"[{next(i for i, c in enumerate(siblings, 1) if c.id == current.id)}]"
            segments.append(key)
            current = current.parent
        row, end_row = node.start_point.row, node.end_point.row
        return {"path": self.path, "locator": "/" + "/".join(reversed(segments)),
                "line": row + 1, "column": len(self.data[self.offsets[row]:node.start_byte].decode("utf-8")) + 1,
                "end_line": end_row + 1,
                "end_column": len(self.data[self.offsets[end_row]:node.end_byte].decode("utf-8")) + 1,
                "content_hash": self.content_hash}


@dataclass
class Binding:
    kind: str
    node: Node
    target: str | None = None
    imported_name: str | None = None
    mutated: bool = False


@dataclass
class Scope:
    parent: Scope | None
    function: bool = False
    bindings: dict[str, list[Binding]] = field(default_factory=lambda: defaultdict(list))

    def resolve(self, name):
        scope = self
        while scope is not None:
            if name in scope.bindings:
                items = scope.bindings[name]
                return items[0] if len(items) == 1 else None
            scope = scope.parent
        return None


def _patterns(node):
    if node is None:
        return []
    if node.type in {"identifier", "shorthand_property_identifier_pattern"}:
        return [node]
    if node.type in {"assignment_pattern", "object_assignment_pattern"}:
        return _patterns(node.child_by_field_name("left"))
    if node.type == "pair_pattern":
        return _patterns(node.child_by_field_name("value"))
    if node.type in {"array_pattern", "object_pattern", "rest_pattern", "formal_parameters"}:
        return [item for child in node.named_children for item in _patterns(child)]
    return []


def _literal(unit, node):
    if node is None or node.type != "string" or any(c.type != "string_fragment" for c in node.named_children):
        return None
    return unit.text(node)[1:-1]


def _target(unit, node, imported_name, apex_index, store):
    parts = imported_name.split(".")
    if len(parts) not in {2, 3} or not all(API_NAME.fullmatch(part) for part in parts):
        store.diagnostic("LWC_APEX_TARGET_UNSUPPORTED", "The Apex import does not name a literal class and method.",
                         unit.location(node), state="unresolved")
        return None
    owner, method_name = ".".join(parts[:-1]), parts[-1]
    class_id = component_id("apexclass", owner)
    local_class = store.components.get(class_id)
    if local_class is not None and local_class.get("definitions"):
        methods = [m for m in apex_index.methods.get(class_id, []) if m.name.casefold() == method_name.casefold()]
        if (len(methods) == 1 and methods[0].static and not store.is_ambiguous(class_id)
                and not store.is_ambiguous(methods[0].id)):
            return methods[0].id
        store.diagnostic("LWC_APEX_TARGET_UNRESOLVED", "A unique local static method is required; imports do not disambiguate overloads.",
                         unit.location(node), state="ambiguous" if len(methods) > 1 else "unresolved",
                         candidates=sorted({m.id for m in methods}), target=imported_name)
        return None
    store.ensure_component("apexclass", owner)
    return store.ensure_component("apexmethod", imported_name, qualified_name=imported_name,
                                  owner_class=class_id, is_test=None)


def _scoped_nodes(unit, root, on_import=None):
    scopes = {}
    nodes = []
    program = Scope(None, function=True)

    def bind(scope, pattern, kind="local", target=None, imported_name=None):
        bindings = [Binding(kind, identifier, target, imported_name) for identifier in _patterns(pattern)]
        for binding in bindings:
            scope.bindings[unit.text(binding.node)].append(binding)
        return bindings

    def visit(node, scope):
        outer = scope
        if node.type in _FUNCTIONS:
            if node.type in {"function_declaration", "generator_function_declaration"}:
                bind(scope, node.child_by_field_name("name"))
            scope = Scope(scope, function=True)
            bind(scope, node.child_by_field_name("parameters"))
            bind(scope, node.child_by_field_name("parameter"))
            if node.type in {"function_expression", "generator_function"}:
                bind(scope, node.child_by_field_name("name"))
        elif node.type in _BLOCKS or node.type in {"class", "class_declaration"}:
            if node.type == "class_declaration":
                bind(scope, node.child_by_field_name("name"))
            scope = Scope(scope, function=node.type == "class_static_block")
            if node.type in {"class", "class_declaration"}:
                bind(scope, node.child_by_field_name("name"))
            elif node.type == "catch_clause":
                bind(scope, node.child_by_field_name("parameter"))
        scopes[node.id] = scope
        nodes.append(node)
        if node.type == "import_statement" and on_import is not None:
            on_import(node, scope, bind)
        elif node.type == "variable_declarator":
            destination = scope
            if node.parent is not None and node.parent.type == "variable_declaration":
                while destination.parent is not None and not destination.function:
                    destination = destination.parent
            bind(destination, node.child_by_field_name("name"))
        elif node.type == "for_in_statement" and node.child_by_field_name("kind") is not None:
            destination = scope
            if unit.text(node.child_by_field_name("kind")) == "var":
                while destination.parent is not None and not destination.function:
                    destination = destination.parent
            bind(destination, node.child_by_field_name("left"))
        if node.type not in _SKIP:
            for child in node.named_children:
                # Method decorators are evaluated outside the method's parameter scope.
                if node.type == "method_definition" and child.type == "decorator":
                    visit(child, outer)
                elif node.type in _FUNCTIONS and child == node.child_by_field_name("body"):
                    # Parameter default initializers cannot see body var/function
                    # bindings. A distinct body environment preserves that rule.
                    visit(child, Scope(scope, function=True))
                else:
                    visit(child, scope)

    visit(root, program)
    return scopes, nodes


def _resolved(unit, scopes, node):
    return (scopes[node.id].resolve(unit.text(node)) if node is not None
            and node.type in {"identifier", "shorthand_property_identifier"} else None)


def _assigned_bindings(unit, scopes, nodes):
    for node in nodes:
        if (node.type in {"assignment_expression", "augmented_assignment_expression", "update_expression"}
                or (node.type == "for_in_statement" and node.child_by_field_name("kind") is None)):
            left = node.child_by_field_name("left") or node.child_by_field_name("argument")
            for identifier in _patterns(left):
                binding = _resolved(unit, scopes, identifier)
                if binding is not None:
                    yield node, binding


def _schema_import(unit, node, module, source, store):
    name = module.removeprefix("@salesforce/schema/")
    parts = name.split(".")
    location = unit.location(node)
    if not 1 <= len(parts) <= 5 or not all(API_NAME.fullmatch(part) for part in parts):
        store.diagnostic("LWC_SCHEMA_TARGET_UNSUPPORTED", "The schema import must name a literal object or field path.",
                         location, state="unresolved", target=name)
        return
    if len(parts) == 1:
        target = store.resolve_component("object", name, location)
        resolved = None
    else:
        resolved = resolve_source_field_path(store, parts[0], ".".join(parts[1:]))
        if resolved is None:
            store.diagnostic("LWC_SCHEMA_TARGET_UNRESOLVED", "The schema import's parent path could not be resolved.",
                             location, state="unresolved", target=name)
            return
        target = store.field(resolved["owner"], resolved["field"], location)
    if target is None:
        return
    resolution = {"basis": "literal_schema_import", "module": module}
    if resolved is not None:
        resolution["path"] = resolved
    store.relation(source, target, "references", location, "lwc.schema_import", resolution,
                   {"context": "lwc_schema_import"})
    for number, hop in enumerate(resolved["steps"] if resolved is not None else [], 1):
        api_name = store.components.get(hop["field"], {}).get("api_name") or hop["field"].split(":", 2)[2]
        owner, field_name = api_name.split(".", 1)
        navigation = {**location, "locator": f"{location['locator']}/navigation[{number}]"}
        lookup = store.field(owner, field_name, navigation)
        if lookup is not None:
            store.relation(source, lookup, "references", navigation, "lwc.schema_import_navigation",
                           {**resolution, "navigation_step": hop}, {"context": "lwc_schema_import"})


def _analyze(unit, root, source, apex_index, store):
    imports = []

    def bind_import(node, scope, bind):
        module = _literal(unit, node.child_by_field_name("source"))
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        has_default = False
        for child in clause.named_children if clause is not None else []:
            if child.type == "identifier":
                has_default = True
                if module and module.startswith("@salesforce/apex/"):
                    name = module.removeprefix("@salesforce/apex/")
                    imports.extend(bind(scope, child, "apex", _target(unit, node, name, apex_index, store), name))
                else:
                    bind(scope, child)
                    if module and module.startswith("@salesforce/schema/"):
                        _schema_import(unit, node, module, source, store)
            elif child.type == "named_imports":
                for spec in child.named_children:
                    name, alias = spec.child_by_field_name("name"), spec.child_by_field_name("alias")
                    local = alias if alias is not None else name
                    if module == "lwc" and unit.text(name) == "wire":
                        imports.extend(bind(scope, local, "wire"))
                    else:
                        bind(scope, local)
            elif child.type == "namespace_import":
                for identifier in child.named_children:
                    bind(scope, identifier)
        if module and not has_default:
            for prefix, diagnostic, message in (
                    ("@salesforce/apex/", "LWC_APEX_IMPORT_SHAPE_UNSUPPORTED",
                     "Only literal default Apex imports are analyzed."),
                    ("@salesforce/schema/", "LWC_SCHEMA_IMPORT_SHAPE_UNSUPPORTED",
                     "Only literal default schema imports are analyzed.")):
                if module.startswith(prefix):
                    store.diagnostic(diagnostic, message, unit.location(node))

    scopes, nodes = _scoped_nodes(unit, root, bind_import)
    for binding in imports:
        if binding.kind == "apex" and binding.target:
            store.relation(source, binding.target, "references", unit.location(binding.node), "lwc.apex_import",
                           {"basis": "literal_module_import", "module": "@salesforce/apex/" + binding.imported_name},
                           {"context": "lwc_apex_import"})

    def resolved(node):
        return _resolved(unit, scopes, node)

    for node, binding in _assigned_bindings(unit, scopes, nodes):
        if binding.kind in {"apex", "wire"}:
            binding.mutated = True
            store.diagnostic("LWC_IMPORT_BINDING_MUTATED", "An imported binding is assigned; calls through it are not resolved.",
                             unit.location(node), state="unresolved")

    recognized = {b.node.id for b in imports}
    for node in nodes:
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        binding = resolved(function)
        if binding is None or binding.mutated:
            continue
        context = "imperative"
        occurrence = function
        if binding.kind == "wire" and node.parent is not None and node.parent.type == "decorator":
            args = node.child_by_field_name("arguments")
            candidates = [c for c in args.named_children if c.type != "comment"] if args is not None else []
            occurrence = candidates[0] if candidates else None
            binding = resolved(occurrence)
            context = "wire"
        if (occurrence is not None and binding is not None and binding.kind == "apex"
                and binding.target and not binding.mutated):
            recognized.add(occurrence.id)
            store.relation(source, binding.target, "calls", unit.location(occurrence), "lwc.apex_" + context,
                           {"basis": "lexically_resolved_import", "module": "@salesforce/apex/" + binding.imported_name},
                           {"context": "lwc_" + context, "dispatch": "static_candidate"})
    for node in nodes:
        if node.type not in {"identifier", "shorthand_property_identifier"} or node.id in recognized:
            continue
        binding = resolved(node)
        if binding is not None and binding.kind == "apex" and binding.target:
            store.diagnostic("LWC_APEX_INDIRECT_USE_UNSUPPORTED", "The Apex import is used outside a direct call or wire adapter; alias/data-flow resolution is deferred.",
                             unit.location(node), state="unresolved", target=binding.target)


def extract_lwc(sources: dict[str, bytes], apex_index, store: FactStore) -> None:
    """Index bundle identity once and extract each direct JavaScript module."""
    if not sources:
        return
    from tree_sitter_language_pack import get_parser
    from .discovery import lwc_bundle

    parser = get_parser("javascript")
    groups = defaultdict(dict)
    for path, data in sorted(sources.items()):
        bundle = lwc_bundle(path)
        if bundle is not None:
            groups[bundle][path] = data
    indexed = []
    for (name, directory), files in sorted(groups.items()):
        main = directory + "/" + name + ".js"
        primary = main if main in files else min(files)
        location = {"path": primary, "locator": "/lwc_bundle", "line": None, "column": None,
                    "end_line": None, "end_column": None, "content_hash": hashlib.sha256(files[primary]).hexdigest()}
        source = store.ensure_component("lwc", name, location, inventory_method="lwc_bundle",
                                        parser=PARSER_VERSION, source_files=sorted(files))
        indexed.append((source, files))
    for source, files in indexed:
        if store.is_ambiguous(source):
            store.diagnostic("LWC_BUNDLE_AMBIGUOUS", "Multiple bundle directories have the same name; their dependencies are not resolved.",
                             store.components[source]["definitions"][0], state="ambiguous", candidates=[source])
            continue
        for path, data in files.items():
            fallback = {"path": path, "locator": "/filename", "line": None, "column": None,
                        "end_line": None, "end_column": None, "content_hash": hashlib.sha256(data).hexdigest()}
            if len(data) > _MAX_BYTES:
                store.diagnostic("LWC_SOURCE_LIMIT", "JavaScript exceeds the bounded parser size limit.", fallback)
                continue
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                store.diagnostic("LWC_ENCODING_UNSUPPORTED", "JavaScript source must be valid UTF-8.", fallback)
                continue
            tree = parser.parse(data)
            if tree.root_node.has_error:
                store.diagnostic("LWC_PARSE_ERROR", "The pinned JavaScript grammar could not parse this module; dependencies were not extracted.", fallback)
                continue
            pending = [(tree.root_node, 0)]
            count = 0
            while pending:
                node, depth = pending.pop()
                count += 1
                if count > _MAX_NODES or depth > 128:
                    break
                pending.extend((child, depth + 1) for child in node.named_children)
            if pending or count > _MAX_NODES or depth > 128:
                store.diagnostic("LWC_STRUCTURE_LIMIT", "JavaScript exceeds bounded AST limits.", fallback)
                continue
            unit = Unit(path, data, fallback["content_hash"], [0] + [i + 1 for i, byte in enumerate(data) if byte == 10])
            _analyze(unit, tree.root_node, source, apex_index, store)
