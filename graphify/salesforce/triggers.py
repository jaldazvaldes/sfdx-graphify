"""Bounded trigger entry points from the native Apex AST, with original locations."""
from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass, field
import hashlib
from pathlib import PurePosixPath

from .apex import (ApexIndex, Binding, Method, PARSER_VERSION, Unit, _MethodExtractor,
                   _filename_location, _modifiers, _significant, _text, _walk,
                   _MAX_NODES, _MAX_SOURCE_BYTES, _parse_source, _method_candidates)
from .schema import API_NAME, FactStore, component_id


_EVENTS = frozenset({"before_insert", "before_update", "before_delete", "after_insert",
                     "after_update", "after_delete", "after_undelete"})
_CONTEXT_RECORDS = frozenset({"new", "old", "newmap", "oldmap"})
_FFLIB = "fflib_sobjectdomain"
# Single-Type dispatcher and matching event hooks in the public fflib contract:
# https://github.com/apex-enterprise-patterns/fflib-apex-common/blob/master/sfdx-source/apex-common/main/classes/fflib_SObjectDomain.cls
_FFLIB_EVENT_METHODS = {
    "before_insert": ("beforeinsert", ()),
    "before_update": ("beforeupdate", ("map<id,sobject>",)),
    "before_delete": ("beforedelete", ()),
    "after_insert": ("afterinsert", ()),
    "after_update": ("afterupdate", ("map<id,sobject>",)),
    "after_delete": ("afterdelete", ()),
    "after_undelete": ("afterundelete", ()),
}


def _list_element_type(unit, declared_type):
    if declared_type is None or declared_type.type != "generic_type":
        return None
    parts = _significant(declared_type)
    if len(parts) != 2 or unit.text(parts[0]).casefold() != "list":
        return None
    types = _significant(parts[1])
    if len(types) != 1 or types[0].type != "type_identifier":
        return None
    return unit.text(types[0]).casefold()


def _list_parameter(unit, declaration):
    """Return the name and element type of one literal List<T> parameter."""
    parameters = _significant(declaration.child_by_field_name("parameters"))
    if len(parameters) != 1 or parameters[0].type != "formal_parameter":
        return None
    parameter = parameters[0]
    element_type = _list_element_type(unit, parameter.child_by_field_name("type"))
    if element_type is None:
        return None
    return unit.text(parameter.child_by_field_name("name")).casefold(), element_type


def _fflib_constructor(unit, owner, apex_types):
    """Prove the bounded public nested factory creates this concrete domain."""
    platform_types = {"list", "map", "sobject", "id"}
    if ("abstract" in _modifiers(unit.data, unit.class_node)[0] or apex_types & platform_types
            or unit.declared_types & (platform_types | {_FFLIB, unit.name.casefold()})):
        return None
    owner = owner.casefold()
    record_types = {"sobject"}
    if owner not in apex_types and owner not in unit.declared_types:
        record_types.add(owner)
    members = _significant(unit.class_node.child_by_field_name("body"))
    factories = [node for node in members if node.type == "class_declaration"
                 and unit.text(node.child_by_field_name("name")).casefold() == "constructor"]
    if len(factories) != 1:
        return None
    factory = factories[0]
    flags, _ = _modifiers(unit.data, factory)
    if ("abstract" in flags or not flags & {"public", "global"}
            or factory.child_by_field_name("superclass") is not None):
        return None
    interfaces = _significant(factory.child_by_field_name("interfaces"))
    if len(interfaces) != 1 or not any(
            tuple(unit.text(part).casefold() for part in _significant(interface)) == (_FFLIB, "iconstructable")
            for interface in _significant(interfaces[0]) if interface.type == "scoped_type_identifier"):
        return None
    factory_members = _significant(factory.child_by_field_name("body"))
    factory_constructors = [node for node in factory_members if node.type == "constructor_declaration"]
    if factory_constructors:
        no_argument = [node for node in factory_constructors if not _significant(node.child_by_field_name("parameters"))]
        if len(no_argument) != 1 or not _modifiers(unit.data, no_argument[0])[0] & {"public", "global"}:
            return None
    constructs = [node for node in factory_members if node.type == "method_declaration"
                  and unit.text(node.child_by_field_name("name")).casefold() == "construct"
                  and (parameter := _list_parameter(unit, node)) is not None and parameter[1] == "sobject"]
    if len(constructs) != 1:
        return None
    construct = constructs[0]
    flags, _ = _modifiers(unit.data, construct)
    if ("static" in flags or not flags & {"public", "global"}
            or unit.text(construct.child_by_field_name("type")).casefold() != _FFLIB):
        return None
    statements = _significant(construct.child_by_field_name("body"))
    if len(statements) != 1 or statements[0].type != "return_statement":
        return None
    expressions = _significant(statements[0])
    if len(expressions) != 1 or expressions[0].type != "object_creation_expression":
        return None
    construction = expressions[0]
    arguments = _significant(construction.child_by_field_name("arguments"))
    parameter = _list_parameter(unit, construct)
    if (unit.text(construction.child_by_field_name("type")).casefold() != unit.name.casefold()
            or len(arguments) != 1 or parameter is None):
        return None
    argument = arguments[0]
    if argument.type == "cast_expression":
        cast_type = _list_element_type(unit, argument.child_by_field_name("type"))
        if cast_type not in record_types:
            return None
        argument = argument.child_by_field_name("value")
    if argument is None or argument.type != "identifier" or unit.text(argument).casefold() != parameter[0]:
        return None
    constructors = [node for node in members if node.type == "constructor_declaration"
                    and len(_significant(node.child_by_field_name("parameters"))) == 1]
    if len(constructors) != 1 or (parameter := _list_parameter(unit, constructors[0])) is None:
        return None
    if parameter[1] not in record_types:
        return None
    return construction, constructors[0]


@dataclass
class TriggerIndex:
    units: list[Unit] = field(default_factory=list)


def index_triggers(sources: dict[str, bytes], metadata, apex_index: ApexIndex,
                   store: FactStore) -> TriggerIndex:
    """Inventory source definitions and companion state before activity filtering."""
    index = TriggerIndex()
    companions = {doc.path: doc for doc in metadata}
    if not sources:
        return index
    from tree_sitter_language_pack import get_parser
    parser = get_parser("apex")
    for path, data in sorted(sources.items()):
        filename = PurePosixPath(path).name.removesuffix(".trigger")
        companion = companions.get(path + "-meta.xml")
        status, metadata_location = "Unknown", None
        if companion is not None:
            if companion.root.tag != "ApexTrigger":
                store.diagnostic("TRIGGER_METADATA_INVALID", "Expected an ApexTrigger metadata root.",
                                 companion.location(companion.root), state="unresolved")
            else:
                statuses = companion.root.all("status")
                if len(statuses) > 1:
                    # A repeated singleton is malformed even when values agree.
                    # Unknown keeps the trigger visible under activity filtering.
                    for node in statuses:
                        store.diagnostic("TRIGGER_STATUS_AMBIGUOUS", "Multiple status elements prevent choosing a local trigger state.",
                                         companion.location(node), state="ambiguous")
                    metadata_location = companion.location(companion.root)
                else:
                    status = (statuses[0].text or "Unknown") if statuses else "Unknown"
                    metadata_location = companion.location(statuses[0] if statuses else companion.root)
        attrs = {"local_status": status, "status_location": metadata_location,
                 "is_test": False, "parser": PARSER_VERSION}

        def unavailable(code, message):
            location = _filename_location(path, data)
            store.ensure_component("apextrigger", filename, location, inventory_method="filename", **attrs)
            store.diagnostic(code, message, location)

        if len(data) > _MAX_SOURCE_BYTES:
            unavailable("TRIGGER_SOURCE_LIMIT", "Trigger source exceeds the bounded parser size limit.")
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            unavailable("TRIGGER_ENCODING_UNSUPPORTED", "Trigger source must be valid UTF-8.")
            continue
        tree, normalized = _parse_source(parser, data)
        parse_error = tree.root_node.has_error
        stack, count, exceeded = [(tree.root_node, 0)], 0, False
        while stack:
            node, depth = stack.pop()
            count += 1
            if count > _MAX_NODES or depth > 128:
                exceeded = True
                break
            stack.extend((child, depth + 1) for child in node.named_children)
        if exceeded:
            unavailable("TRIGGER_STRUCTURE_LIMIT", "Trigger source exceeds bounded AST structural limits.")
            continue
        declarations = [node for node in tree.root_node.named_children if node.type == "trigger_declaration"]
        if (len(declarations) != 1
                or _text(data, declarations[0].child_by_field_name("name")).casefold() != filename.casefold()):
            unavailable("TRIGGER_SHAPE_UNSUPPORTED", "Expected one trigger declaration matching its source filename.")
            continue
        declaration = declarations[0]
        header_nodes = [declaration.child_by_field_name("name"), declaration.child_by_field_name("object")]
        header_nodes.extend(node for node in declaration.named_children if node.type == "trigger_event")
        if any(node is None or node.has_error or node.is_missing for node in header_nodes):
            unavailable("TRIGGER_PARSE_ERROR", "The trigger declaration is malformed; no header or body is selected.")
            continue
        name = _text(data, declaration.child_by_field_name("name"))
        owner = _text(data, declaration.child_by_field_name("object"))
        events = sorted({"_".join(_text(data, node).split()).casefold()
                         for node in declaration.named_children if node.type == "trigger_event"})
        if not API_NAME.fullmatch(owner) or not events or any(event not in _EVENTS for event in events):
            unavailable("TRIGGER_HEADER_UNSUPPORTED", "A literal object and recognized trigger events are required.")
            continue
        offsets = [0] + [i + 1 for i, byte in enumerate(data) if byte == 10]
        unit = Unit(path, data, tree, declaration, component_id("apextrigger", name), name,
                    hashlib.sha256(data).hexdigest(), offsets, parse_error=parse_error)
        location = unit.location(declaration)
        cid = store.ensure_component("apextrigger", name, location, inventory_method="apex_ast",
                                     trigger_object=owner, trigger_events=events,
                                     parser_normalized_trigger_context=normalized, **attrs)
        if parse_error:
            store.diagnostic("TRIGGER_PARSE_ERROR", "The pinned grammar could not parse the body; the valid declaration is retained, body dependencies are not extracted.",
                             location)
        definitions = store.components[cid].setdefault("metadata_by_definition", [])
        definitions.append({"location": location, "trigger_object": owner, "trigger_events": events, **attrs})
        apex_index.object_names.add(owner.casefold())
        index.units.append(unit)
    for component in store.components.values():
        if component["kind"] == "apextrigger" and component["definition_status"] == "ambiguous":
            for key in ("local_status", "status_location", "trigger_object", "trigger_events"):
                component[key] = None
            for definition in component["definitions"]:
                store.diagnostic("TRIGGER_AMBIGUOUS", "Multiple source definitions name this trigger; no body is selected.",
                                 definition, state="ambiguous", candidates=[component["id"]])
    return index


@dataclass
class _Constructed:
    binding: Binding
    class_id: str


class _TriggerExtractor(_MethodExtractor):
    _constructor_reference_rule = "trigger.constructed_class"

    def __init__(self, unit, apex_index, store):
        super().__init__(Method(unit, unit.class_node, unit.class_id, unit.name, False, False, False, 0),
                         apex_index, store)
        self.constructed: list[_Constructed] = []
        self.owner = store.components[unit.class_id]["trigger_object"]

    def _local_class(self, name):
        if not API_NAME.fullmatch(name):
            return None
        cid = component_id("apexclass", name)
        if cid not in self.store.components or self.store.is_ambiguous(cid):
            return None
        return cid

    def index_bindings(self):
        if self.body is None:
            return
        for node in _walk(self.body):
            if node.type == "local_variable_declaration":
                scope = node.parent
                while scope is not None and scope.type not in {"block", "for_statement", "enhanced_for_statement"}:
                    scope = scope.parent
                owner, collection = self.record_type(node.child_by_field_name("type"))
                for declaration in node.named_children:
                    if declaration.type != "variable_declarator":
                        continue
                    binding = Binding(self.unit.text(declaration.child_by_field_name("name")), owner, collection,
                                      scope or self.body, declaration, declaration.end_byte)
                    self.bindings.append(binding)
                    value = declaration.child_by_field_name("value")
                    if value is not None and value.type == "object_creation_expression":
                        cid = self._local_class(self.unit.text(value.child_by_field_name("type")))
                        if cid:
                            self.constructed.append(_Constructed(binding, cid))
            elif node.type == "enhanced_for_statement":
                if not self.index_loop_binding(node):
                    self.diagnostic("TRIGGER_LOOP_TYPE_UNRESOLVED", "Loop item type is not a proven single-record object.", node, "unresolved")
            elif node.type == "formal_parameter" and node.parent is not None and node.parent.type == "catch_clause":
                self.bindings.append(Binding(self.unit.text(node.child_by_field_name("name")), None, False,
                                             node.parent, node, node.start_byte))

    def _trigger_context(self, node):
        if node is None or node.type != "field_access":
            return None
        receiver, member = node.child_by_field_name("object"), node.child_by_field_name("field")
        if (receiver is not None and receiver.type == "identifier"
                and self.unit.text(receiver).casefold() == "trigger"
                and not self.has_binding("Trigger", node)):
            return self.unit.text(member).casefold()
        return None

    def _record_expression(self, node):
        if node is None:
            return None
        if node.type == "parenthesized_expression":
            children = _significant(node)
            return self._record_expression(children[0]) if len(children) == 1 else None
        if node.type == "identifier":
            binding = self.binding(self.unit.text(node), node, report=False)
            return binding.owner if binding and not binding.collection else None
        if node.type == "array_access":
            array = node.child_by_field_name("array")
            if self._trigger_context(array) in {"new", "old"}:
                return self.owner
            if array is not None and array.type == "identifier":
                binding = self.binding(self.unit.text(array), node, report=False)
                return binding.owner if binding and binding.collection else None
        if node.type == "method_invocation":
            owner = self._setting_owner(node)
            if owner:
                return owner
            receiver = node.child_by_field_name("object")
            method = self.unit.text(node.child_by_field_name("name")).casefold()
            arguments = _significant(node.child_by_field_name("arguments"))
            if method == "get" and len(arguments) == 1 and self._trigger_context(receiver) in _CONTEXT_RECORDS:
                return self.owner
        return None

    def field_access(self, node, relation="reads", *, operation="field_access"):
        context = self._trigger_context(node)
        if context is not None:
            if context in _CONTEXT_RECORDS:
                self.edge(self.owner, None, relation, node, "trigger.context_records",
                          {"operation": "trigger_context", "context": context, "collection": True,
                           "field_set": "unknown"})
            return  # Other Trigger fields are execution flags, not object fields.
        receiver, member = node.child_by_field_name("object"), node.child_by_field_name("field")
        owner = self._record_expression(receiver)
        if owner and member is not None and API_NAME.fullmatch(self.unit.text(member)):
            self.edge(owner, self.unit.text(member), relation, member, "trigger.typed_field",
                      {"operation": operation, "effect": "memory"},
                      {"basis": "trigger_ast_record_type", "object": owner, "parser": PARSER_VERSION})
            if receiver is not None and receiver.type != "identifier":
                self.visit(receiver)
            return
        super().field_access(node, relation, operation=operation)

    def _constructed_class(self, receiver, node, *, require_immutable=False):
        if receiver is None:
            return None
        if receiver.type == "object_creation_expression":
            return self._local_class(self.unit.text(receiver.child_by_field_name("type")))
        if receiver.type != "identifier":
            return None
        binding = self.binding(self.unit.text(receiver), node, report=False)
        if binding is None:
            return None
        matches = [item for item in self.constructed if item.binding is binding]
        if len(matches) != 1:
            return None
        # Async targets require an unreassigned local, including writes after a
        # submission that could reach it again on another loop iteration.
        for current in _walk(self.body):
            if current.start_byte < binding.available_from:
                continue
            if not require_immutable and current.start_byte >= node.start_byte:
                continue
            if current.type == "assignment_expression":
                left = current.child_by_field_name("left")
                if left is not None and left.type == "identifier" and self.unit.text(left).casefold() == binding.name.casefold():
                    return None
        return matches[0].class_id

    def invocation(self, node):
        receiver = node.child_by_field_name("object")
        name = self.unit.text(node.child_by_field_name("name"))
        arguments = _significant(node.child_by_field_name("arguments"))
        if self._platform_invocation(node, receiver, name, arguments) or self._managed_invocation(node):
            for argument in arguments:
                self.visit(argument)
            return
        if (receiver is not None and receiver.type == "identifier"
                and self.unit.text(receiver).casefold() == "database"):
            super().invocation(node)
            return
        owner = self._setting_owner(node)
        if owner:
            self.edge(owner, None, "reads", node, "trigger.custom_setting_accessor",
                      {"operation": "custom_setting", "field_set": "partial_explicit"},
                      {"basis": "local_custom_setting_metadata", "object": owner})
        elif name.casefold() == "get" and self._trigger_context(receiver) in _CONTEXT_RECORDS and len(arguments) == 1:
            self.visit(receiver)
        elif not self._fflib_dispatch(node, receiver, name, arguments):
            static = bool(receiver is not None and receiver.type == "identifier"
                          and not self.has_binding(self.unit.text(receiver), node))
            cid = self._local_class(self.unit.text(receiver)) if static else self._constructed_class(receiver, node)
            if receiver is not None and receiver.type != "identifier":
                self.visit(receiver)
            candidates = (_method_candidates(self.index, self.store, cid, name, len(arguments), include_ambiguous=True)
                          if cid else [])
            method, overload = self._select_method(candidates, arguments)
            if (method is not None and method.static == static
                    and _modifiers(method.unit.data, method.node)[0] & {"public", "global"}):
                self.store.relation(self.method.id, method.id, "calls", self.unit.location(node),
                                    "trigger.local_method_call", qualifiers={"invocation": "static_method" if static else "constructed_instance_method"},
                                    resolution={"basis": "unique_local_static_method" if static else "explicit_constructor_type",
                                                "class": cid, "parser": PARSER_VERSION, **overload})
                if not static:
                    self._constructed_virtual_dispatch(cid, method, node)
            elif not self._ambiguous_call(node, candidates, code="TRIGGER_CALL_UNRESOLVED"):
                self.diagnostic("TRIGGER_CALL_UNRESOLVED", "Call target requires unavailable, ambiguous or inherited dispatch analysis.",
                                node, "ambiguous" if len(candidates) > 1 else "unresolved")
        # An fflib domain literal also keeps its independent class reference.
        for argument in arguments:
            self.visit(argument)

    def _fflib_dispatch(self, node, receiver, name, arguments):
        # A local framework implementation may have different dispatch semantics.
        # Apply the public contract only when the exact base is external.
        if (receiver is None or receiver.type != "identifier" or self.unit.text(receiver).casefold() != _FFLIB
                or name.casefold() != "triggerhandler" or len(arguments) != 1
                or _FFLIB in self.index.apex_types or self.has_binding(_FFLIB, node)):
            return False
        literal = arguments[0]
        if literal.type != "field_access" or self.unit.text(literal.child_by_field_name("field")).casefold() != "class":
            return False
        domain_token = literal.child_by_field_name("object")
        domain_name = self.unit.text(domain_token)
        if domain_token is None or domain_token.type != "identifier" or self.has_binding(domain_name, literal):
            return False
        domain_id = self._local_class(domain_name)
        domains = [unit for unit in self.index.units if unit.class_id == domain_id]
        if len(domains) != 1:
            return False
        domain = domains[0]
        if (domain.superclass_name or "").casefold() != _FFLIB:
            return False
        location = self.unit.location(node)
        binding = _fflib_constructor(domain, self.owner, self.index.apex_types)
        if binding is None:
            self.store.diagnostic("TRIGGER_FFLIB_FACTORY_UNRESOLVED",
                                  "The fflib domain's nested Constructor could not be bound to a compatible domain constructor.",
                                  location, state="unresolved", source_component=self.method.id,
                                  target_component=domain.class_id)
            return True
        construct_site, constructor = binding
        methods = self.index.methods[domain_id]
        methods_by_name = {}
        for method in methods:
            methods_by_name.setdefault(method.name.casefold(), []).append(method)
        for event in self.store.components[self.method.id]["trigger_events"]:
            suffix, parameter_types = _FFLIB_EVENT_METHODS[event]
            if "handle" + suffix in methods_by_name:
                self.store.diagnostic("TRIGGER_FFLIB_HANDLER_UNSUPPORTED",
                                      "A local fflib handler may bypass the default event hook.",
                                      location, trigger_event=event)
                continue
            hooks = methods_by_name.get("on" + suffix, [])
            if not hooks:
                continue
            matches = [hook for hook in hooks if tuple(item.casefold() for item in hook.parameter_types) == parameter_types]
            if len(matches) != 1 or not self._fflib_hook(matches[0]):
                self.store.diagnostic("TRIGGER_FFLIB_HOOK_UNRESOLVED",
                                      "The local fflib hook name lacks a unique compatible event override.",
                                      location, state="unresolved", trigger_event=event)
                continue
            hook = matches[0]
            self.store.relation(self.method.id, hook.id, "calls", location,
                                "trigger.fflib_event_dispatch",
                                qualifiers={"invocation": "fflib_event_dispatch", "trigger_event": event,
                                            "resolution_status": "candidate", "uncertainty": "fflib_event_dispatch"},
                                resolution={"basis": "fflib_sobjectdomain_type_contract", "class": domain.class_id,
                                            "dispatcher": "fflib_SObjectDomain.triggerHandler(Type)",
                                            "class_literal": self.unit.location(literal),
                                            "construct_site": domain.location(construct_site),
                                            "constructor": domain.location(constructor), "hook": domain.location(hook.node),
                                            "conditional": True, "parser": PARSER_VERSION})
        return True

    def _fflib_hook(self, hook):
        flags, _ = _modifiers(hook.unit.data, hook.node)
        return (not self.store.is_ambiguous(hook.id) and not hook.static and "override" in flags
                and bool(flags & {"public", "global"}) and hook.unit.text(hook.node.child_by_field_name("type")).casefold() == "void"
                and hook.node.child_by_field_name("body") is not None)

    def _constructed_virtual_dispatch(self, concrete_class, entry_method, call_node):
        """Retain overrides actually called by a proven inherited entry body.

        These are conditional static candidates, not an execution trace. Hook
        names do not establish event guards. Every candidate needs an explicit
        invocation in the selected entry body; its runtime branch is unresolved.
        """
        body = entry_method.node.child_by_field_name("body")
        if body is None or entry_method.unit.class_id == concrete_class:
            return
        events = self.store.components[self.method.id]["trigger_events"]
        for invocation in _walk(body):
            if invocation.type != "method_invocation":
                continue
            receiver = entry_method.unit.text(invocation.child_by_field_name("object"))
            name = entry_method.unit.text(invocation.child_by_field_name("name"))
            args = _significant(invocation.child_by_field_name("arguments"))
            if receiver.casefold() not in {"", "this"}:
                continue
            base = _method_candidates(self.index, self.store, entry_method.unit.class_id, name, len(args))
            derived = _method_candidates(self.index, self.store, concrete_class, name, len(args))
            if len(base) != 1 or len(derived) != 1 or base[0].id == derived[0].id:
                continue
            if (base[0].static or derived[0].static
                    or not (_modifiers(base[0].unit.data, base[0].node)[0] & {"virtual", "abstract"})
                    or "override" not in _modifiers(derived[0].unit.data, derived[0].node)[0]):
                continue
            self.store.relation(self.method.id, derived[0].id, "calls", self.unit.location(call_node),
                                "trigger.constructed_virtual_dispatch",
                                qualifiers={"invocation": "constructed_virtual_dispatch", "entry_method": entry_method.id,
                                            "trigger_events": events, "resolution_status": "candidate",
                                            "uncertainty": "conditional_virtual_dispatch"},
                                resolution={"basis": "explicit_constructor_and_local_virtual_call", "class": concrete_class,
                                            "dispatch_site": entry_method.unit.location(invocation),
                                            "hook": derived[0].unit.location(derived[0].node),
                                            "conditional": True, "parser": PARSER_VERSION})


def extract_triggers(index: TriggerIndex, apex_index: ApexIndex, store: FactStore, *, excluded: Set[str] = frozenset()):
    """Connect trigger events and provable handler/data dependencies, never runtime order."""
    for unit in index.units:
        if unit.class_id in excluded or store.is_ambiguous(unit.class_id):
            continue
        item = store.components[unit.class_id]
        owner_node = unit.class_node.child_by_field_name("object")
        location = unit.location(owner_node)
        target = store.resolve_component("object", item["trigger_object"], location)
        if target:
            store.relation(unit.class_id, target, "references", location, "trigger.event_object",
                           qualifiers={"operation": "trigger", "events": item["trigger_events"]},
                           resolution={"basis": "trigger_declaration", "parser": PARSER_VERSION})
        if not unit.parse_error:
            _TriggerExtractor(unit, apex_index, store).run()
