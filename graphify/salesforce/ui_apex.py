"""Source-backed dependencies from bounded Salesforce UI bindings."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
from pathlib import PurePosixPath
import re

from .apex import _modifiers
from .lwc import Unit, _FUNCTIONS, _assigned_bindings, _literal, _resolved, _scoped_nodes
from .schema import API_NAME, FactStore, component_id


_MAX_BYTES = 8 * 1024 * 1024
_MAX_NODES = 200_000
_AURA_ROOTS = frozenset({"aura:component", "aura:application"})
_AURA_MARKUP = frozenset({".cmp", ".app"})
_VF_TYPES = {
    (".page", "pages"): ("visualforcepage", "apex:page"),
    (".component", "components"): ("visualforcecomponent", "apex:component"),
}
_VF_ACTION_TAGS = frozenset({
    "apex:page", "apex:commandbutton", "apex:commandlink", "apex:actionfunction",
    "apex:actionsupport", "apex:actionpoller",
})
_VF_ACTION = re.compile(r"\{\s*!\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}")
_VF_REMOTE = re.compile(r"\{\s*!\s*\$RemoteAction\.([A-Za-z_][A-Za-z0-9_.]*)\s*\}")


def _aura_bundle(path: PurePosixPath) -> str | None:
    if len(path.parts) < 3 or path.parts[-3].casefold() != "aura":
        return None
    bundle = path.parent.name
    if not API_NAME.fullmatch(bundle):
        return None
    if path.name.casefold() in {f"{bundle}{suffix}".casefold() for suffix in
                                (".cmp", ".app", "Controller.js", "Helper.js")}:
        return path.parent.as_posix()
    return None


def ui_source(relative: str) -> bool:
    """Select source files with a supported Aura or Visualforce role."""
    path = PurePosixPath(relative)
    vf = (path.suffix.casefold(), path.parent.name.casefold()) in _VF_TYPES
    return bool((vf and API_NAME.fullmatch(path.stem)) or _aura_bundle(path))


@dataclass
class Tag:
    name: str
    attributes: list[tuple[str, str | None]]
    line: int
    column: int

    def values(self, name: str) -> list[str]:
        return [(value or "").strip() for key, value in self.attributes if key == name]


class Markup(HTMLParser):
    """Read literal attributes in Visualforce and Aura markup with unbound prefixes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root: Tag | None = None
        self.actions: list[Tag] = []
        self.scripts: list[tuple[str, int, int]] = []
        self.tag_count = 0
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        self.tag_count += 1
        if self.tag_count > _MAX_NODES:
            raise ValueError("UI markup exceeds the tag limit.")
        line, column = self.getpos()
        element = Tag(tag, attrs, line, column + 1)
        if self.root is None:
            self.root = element
        if tag in _VF_ACTION_TAGS and element.values("action"):
            self.actions.append(element)
        if tag == "script":
            self.in_script = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script and "invokeAction" in data:
            line, column = self.getpos()
            self.scripts.append((data, line, column))


def _location(path, digest, locator="/filename", *, line=None, column=None):
    return {"path": path, "locator": locator, "line": line, "column": column,
            "end_line": None, "end_column": None, "content_hash": digest}


def _markup(path, data, expected_roots, store):
    digest = hashlib.sha256(data).hexdigest()
    fallback = _location(path, digest)
    markup = Markup()
    try:
        if len(data) > _MAX_BYTES:
            raise ValueError("UI markup exceeds the byte limit.")
        markup.feed(data.decode("utf-8-sig"))
        markup.close()
        if markup.root is None or markup.root.name not in expected_roots:
            raise ValueError("The expected Salesforce markup root was not found.")
    except (ValueError, AssertionError) as exc:
        store.diagnostic("UI_MARKUP_UNSUPPORTED", str(exc), fallback, state="unresolved")
        return None, fallback
    root = markup.root
    return markup, _location(path, digest, f"/markup/{root.name}", line=root.line, column=root.column)


def _literal_class(name):
    parts = name.split(".")
    return 1 <= len(parts) <= 2 and all(API_NAME.fullmatch(part) for part in parts)


def _controller_names(markup, root_location, store, *, visualforce=False):
    root = markup.root
    declared = root.values("controller")
    extensions = root.values("extensions") if visualforce else []
    standard = root.values("standardcontroller") if visualforce else []
    names = declared + ([part.strip() for part in extensions[0].split(",")] if extensions else [])
    if (len(declared) > 1 or len(extensions) > 1 or len(standard) > 1 or (declared and standard)
            or not all(_literal_class(name) for name in names)
            or len({name.casefold() for name in names}) != len(names)):
        store.diagnostic("UI_APEX_CONTROLLER_UNRESOLVED", "Expected a literal, unambiguous controller declaration.",
                         root_location, state="unresolved")
        return None
    return names


def _target(name, owners, apex_index, local_classes, store, location, *, channel, annotation=None):
    candidates = []
    ambiguous = False
    incomplete = not owners
    for owner in owners:
        class_id = component_id("apexclass", owner)
        klass = store.components.get(class_id)
        if klass and store.is_ambiguous(class_id):
            ambiguous = True
            continue
        unit = local_classes.get(class_id)
        if unit is None:
            incomplete = True
            continue
        local = [method for method in apex_index.methods.get(class_id, ())
                 if method.name.casefold() == name.casefold() and (annotation or method.parameter_count == 0)]
        if not local and unit.has_superclass:
            incomplete = True
        candidates.extend(local)
    if not ambiguous and not incomplete and len(candidates) == 1:
        method = candidates[0]
        modifiers, annotations = _modifiers(method.unit.data, method.node)
        if (method.id in store.components and not store.is_ambiguous(method.id)
                and not method.is_test and modifiers & {"public", "global"}
                and method.static == (annotation is not None)
                and (annotation is None or annotation in annotations)):
            return method.id
    store.diagnostic(f"{channel}_APEX_TARGET_UNRESOLVED", "A unique visible local Apex method is required for this UI action.",
                     location, state="ambiguous" if ambiguous or len(candidates) > 1 else "unresolved",
                     controllers=owners, method=name, candidates=sorted({item.id for item in candidates}))
    return None


def _javascript(parser, data):
    tree = parser.parse(data)
    root = tree.root_node
    if root.has_error:
        return None
    pending = [(root, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > _MAX_NODES or depth > 128:
            return None
        pending.extend((child, depth + 1) for child in node.named_children)
    return root


def _arguments(node):
    args = node.child_by_field_name("arguments")
    return [child for child in args.named_children if child.type != "comment"] if args is not None else []


def _member(unit, call, name):
    function = call.child_by_field_name("function")
    if function is None or function.type != "member_expression":
        return None
    if unit.text(function.child_by_field_name("property")) != name:
        return None
    return function.child_by_field_name("object")


def _unbound(scope, name):
    while scope is not None:
        if name in scope.bindings:
            return False
        scope = scope.parent
    return True


def _action_parameters(unit, root):
    body = [child for child in root.named_children if child.type != "comment"]
    if len(body) != 1 or body[0].type != "expression_statement":
        return set(), set()
    expression = body[0]
    while expression.type in {"expression_statement", "parenthesized_expression"}:
        children = [child for child in expression.named_children if child.type != "comment"]
        if len(children) != 1:
            return set(), set()
        expression = children[0]
    if expression.type != "object":
        return set(), set()
    parameters = set()
    names = set()
    for entry in expression.named_children:
        if entry.type == "method_definition":
            function = entry
            key = entry.child_by_field_name("name")
        else:
            function = entry.child_by_field_name("value")
            key = entry.child_by_field_name("key")
        if function is None or function.type not in _FUNCTIONS:
            continue
        if key is not None and key.type in {"identifier", "property_identifier", "string"}:
            name = _literal(unit, key) if key.type == "string" else unit.text(key)
            if name:
                names.add(name.casefold())
        params = function.child_by_field_name("parameters")
        first = next((p for p in params.named_children if p.type != "comment"), None) if params is not None else None
        if first is not None and first.type == "identifier":
            parameters.add(first.id)
    return parameters, names


def _enclosing_function(node):
    while node is not None and node.type not in _FUNCTIONS:
        node = node.parent
    return node.id if node is not None else None


def _aura_script(parser, path, data, store):
    digest = hashlib.sha256(data).hexdigest()
    fallback = _location(path, digest)
    if len(data) > _MAX_BYTES:
        store.diagnostic("AURA_APEX_SCRIPT_UNSUPPORTED", "Aura JavaScript exceeds the byte limit.", fallback)
        return
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        store.diagnostic("AURA_APEX_SCRIPT_UNSUPPORTED", "Aura JavaScript must be UTF-8.", fallback)
        return
    root = _javascript(parser, data)
    if root is None:
        store.diagnostic("AURA_APEX_SCRIPT_UNSUPPORTED", "Aura JavaScript exceeds bounded AST coverage.", fallback)
        return
    unit = Unit(path, data, digest, [0] + [i + 1 for i, byte in enumerate(data) if byte == 10])
    parameters, names = _action_parameters(unit, root)
    if not parameters and b"enqueueAction" in data:
        store.diagnostic("AURA_APEX_SCRIPT_UNSUPPORTED", "Expected top-level Aura action functions with a component argument.", fallback)
    return unit, root, parameters, names


def _aura_actions(unit, root, parameters, client_actions, source, owners, controller, apex_index, local_classes, store):
    if not parameters:
        return
    scopes, nodes = _scoped_nodes(unit, root)
    for _, binding in _assigned_bindings(unit, scopes, nodes):
        binding.mutated = True
    calls = [node for node in nodes if node.type == "call_expression"]
    lookups = {}
    for node in calls:
        receiver = _member(unit, node, "get")
        binding = _resolved(unit, scopes, receiver)
        if binding is None or binding.node.id not in parameters or binding.mutated:
            continue
        args = _arguments(node)
        literal = _literal(unit, args[0]) if len(args) == 1 else None
        if literal and literal.startswith("c.") and API_NAME.fullmatch(literal[2:]):
            lookups[node.id] = (node, literal[2:])

    enqueued = set()
    for queue in calls:
        receiver = _member(unit, queue, "enqueueAction")
        if (receiver is None or receiver.type != "identifier" or unit.text(receiver) != "$A"
                or not _unbound(scopes[receiver.id], "$A")):
            continue
        args = _arguments(queue)
        if not args:
            continue
        value = args[0]
        if value.type == "identifier":
            binding = _resolved(unit, scopes, value)
            declaration = binding.node.parent if binding is not None and not binding.mutated else None
            if (declaration is None or declaration.type != "variable_declarator"
                    or declaration.start_byte >= queue.start_byte):
                continue
            value = declaration.child_by_field_name("value")
        if value is None or value.id not in lookups or _enclosing_function(value) != _enclosing_function(queue):
            continue
        enqueued.add(value.id)
        lookup, name = lookups[value.id]
        location = unit.location(lookup)
        if name.casefold() in client_actions:
            store.diagnostic("AURA_APEX_ACTION_AMBIGUOUS", "A client-side action shares the literal Apex action name.",
                             location, state="ambiguous", method=name)
            continue
        target = _target(name, owners, apex_index, local_classes, store, location,
                         channel="AURA", annotation="auraenabled")
        if target:
            store.relation(source, target, "calls", location, "aura.apex_action",
                           {"basis": "literal_enqueued_aura_action", "controller_declaration": controller,
                            "enqueue": unit.location(queue)}, {"context": "aura_apex_action"})
    for lookup_id in lookups.keys() - enqueued:
        lookup, name = lookups[lookup_id]
        store.diagnostic("AURA_APEX_ACTION_USE_UNRESOLVED", "Could not trace this literal action to an enqueue in the same function.",
                         unit.location(lookup), state="unresolved", method=name)


def _vf_remote_actions(parser, script, path, digest, index, source, owners, controller, apex_index, local_classes, store):
    text, line, column = script
    data = text.encode("utf-8")
    fallback = _location(path, digest, f"/markup/script[{index}]", line=line, column=column + 1)
    root = _javascript(parser, data)
    if root is None or any(child.type == "import_statement" for child in root.named_children):
        store.diagnostic("VISUALFORCE_APEX_SCRIPT_UNSUPPORTED", "This remoting script exceeds bounded JavaScript coverage.", fallback)
        return
    unit = Unit(path, data, digest, [0] + [i + 1 for i, byte in enumerate(data) if byte == 10])
    scopes, nodes = _scoped_nodes(unit, root)
    for node in nodes:
        if node.type != "call_expression":
            continue
        receiver = _member(unit, node, "invokeAction")
        if receiver is None or receiver.type != "member_expression":
            continue
        middle = receiver.child_by_field_name("object")
        if (middle is None or middle.type != "member_expression"
                or unit.text(receiver.child_by_field_name("property")) != "Manager"
                or unit.text(middle.child_by_field_name("property")) != "remoting"):
            continue
        base = middle.child_by_field_name("object")
        if (base is None or base.type != "identifier" or unit.text(base) != "Visualforce"
                or not _unbound(scopes[base.id], "Visualforce")):
            continue
        args = _arguments(node)
        literal = _literal(unit, args[0]) if args else None
        match = _VF_REMOTE.fullmatch(literal or "")
        local = unit.location(node)
        location = _location(path, digest, f"/markup/script[{index}]{local['locator']}",
                             line=line + local["line"] - 1,
                             column=local["column"] + (column if local["line"] == 1 else 0))
        owner, _, name = (match[1] if match else literal or "").rpartition(".")
        if (not _literal_class(owner) or not API_NAME.fullmatch(name)
                or owner.casefold() not in {item.casefold() for item in owners}):
            store.diagnostic("VISUALFORCE_APEX_REMOTE_TARGET_UNRESOLVED",
                             "Expected a literal remoting method on a declared controller or extension.",
                             location, state="unresolved")
            continue
        target = _target(name, [owner], apex_index, local_classes, store, location,
                         channel="VISUALFORCE", annotation="remoteaction")
        if target:
            store.relation(source, target, "calls", location, "visualforce.apex_remote_action",
                           {"basis": "literal_visualforce_remote_action", "controller_declaration": controller},
                           {"context": "visualforce_apex_remote_action"})


def extract_ui_apex(sources: dict[str, bytes], apex_index, store: FactStore) -> None:
    """Index selected UI definitions and resolve their explicit local Apex actions."""
    if not sources:
        return
    from tree_sitter_language_pack import get_parser

    parser = get_parser("javascript")
    local_classes = {unit.class_id: unit for unit in apex_index.units}
    aura = defaultdict(dict)
    visualforce = []
    for relative, data in sorted(sources.items()):
        path = PurePosixPath(relative)
        bundle = _aura_bundle(path)
        if bundle is not None:
            aura[bundle][relative] = data
            continue
        entry = _VF_TYPES.get((path.suffix.casefold(), path.parent.name.casefold()))
        if entry is None:
            continue
        kind, root = entry
        markup, definition = _markup(relative, data, {root}, store)
        source = store.ensure_component(kind, path.stem, definition)
        visualforce.append((source, markup, definition))

    bundles = []
    for directory, files in aura.items():
        roots = []
        for path, data in files.items():
            if PurePosixPath(path).suffix.casefold() in _AURA_MARKUP:
                roots.append(_markup(path, data, _AURA_ROOTS, store))
        if not roots:
            path, data = next(iter(files.items()))
            roots.append((None, _location(path, hashlib.sha256(data).hexdigest())))
        for markup, definition in roots:
            source = store.ensure_component("aura", PurePosixPath(directory).name, definition,
                                            source_files=sorted(files))
        markup, definition = roots[0]
        bundles.append((source, markup, definition, files))

    for source, markup, definition, files in bundles:
        if store.is_ambiguous(source):
            store.diagnostic("AURA_BUNDLE_AMBIGUOUS", "Multiple Aura roots share this bundle identity.", definition,
                             state="ambiguous")
            continue
        owners = _controller_names(markup, definition, store) if markup is not None else []
        if owners is not None:
            scripts = []
            client_actions = set()
            for path, data in files.items():
                if PurePosixPath(path).suffix.casefold() == ".js":
                    parsed = _aura_script(parser, path, data, store)
                    if parsed is not None:
                        unit, root, parameters, names = parsed
                        scripts.append((unit, root, parameters))
                        if path.casefold().endswith("controller.js"):
                            client_actions.update(names)
            for unit, root, parameters in scripts:
                _aura_actions(unit, root, parameters, client_actions, source, owners, definition,
                              apex_index, local_classes, store)

    for source, markup, definition in visualforce:
        if store.is_ambiguous(source):
            store.diagnostic("VISUALFORCE_COMPONENT_AMBIGUOUS", "Multiple Visualforce files share this identity.", definition,
                             state="ambiguous")
            continue
        if markup is None:
            continue
        owners = _controller_names(markup, definition, store, visualforce=True)
        if owners is None:
            continue
        for index, element in enumerate(markup.actions if owners else [], 1):
            location = _location(definition["path"], definition["content_hash"],
                                 f"/markup/{element.name}[{index}]/@action", line=element.line, column=element.column)
            values = element.values("action")
            match = _VF_ACTION.fullmatch(values[0]) if len(values) == 1 else None
            if not match:
                store.diagnostic("VISUALFORCE_APEX_ACTION_UNRESOLVED", "Expected a literal Visualforce action method.",
                                 location, state="unresolved")
                continue
            target = _target(match[1], owners, apex_index, local_classes, store, location, channel="VISUALFORCE")
            if target:
                store.relation(source, target, "calls", location, "visualforce.apex_action",
                               {"basis": "literal_visualforce_action", "controller_declaration": definition},
                               {"context": "visualforce_apex_action"})
        for index, script in enumerate(markup.scripts, 1):
            _vf_remote_actions(parser, script, definition["path"], definition["content_hash"], index, source,
                               owners, definition, apex_index, local_classes, store)
