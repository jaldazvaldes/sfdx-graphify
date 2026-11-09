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

    def _setting_owner(self, node):
        if node is None or node.type != "method_invocation":
            return None
        receiver = node.child_by_field_name("object")
        if receiver is None or receiver.type != "identifier":
            return None
        name = self.unit.text(receiver)
        if not API_NAME.fullmatch(name) or self.has_binding(name, node):
            return None
        cid = component_id("object", name)
        item = self.store.components.get(cid)
        if (not item or item.get("definition_status") != "defined"
                or item.get("custom_settings_type") not in {"Hierarchy", "List"}):
            return None
        method = self.unit.text(node.child_by_field_name("name")).casefold()
        arguments = _significant(node.child_by_field_name("arguments"))
        if ((method in {"getinstance", "getvalues"} and len(arguments) == 1)
                or (method == "getinstance" and not arguments and item["custom_settings_type"] == "Hierarchy")
                or (method == "getorgdefaults" and not arguments and item["custom_settings_type"] == "Hierarchy")):
            return item["api_name"]
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

    def constructor(self, node, *, database_operation=None):
        name = self.unit.text(node.child_by_field_name("type"))
        cid = self._local_class(name)
        if cid:
            self.store.relation(self.method.id, cid, "references", self.unit.location(node),
                                "trigger.constructed_class", qualifiers={"invocation": "constructor"},
                                resolution={"basis": "literal_local_class", "parser": PARSER_VERSION})
            for argument in _significant(node.child_by_field_name("arguments")):
                self.visit(argument)
            return None
        return super().constructor(node, database_operation=database_operation)

    def _constructed_class(self, receiver, node):
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
        # Reassignment can change runtime type. Conservatively reject any write
        # to this variable name before the call, including nested control paths.
        for current in _walk(self.body):
            if current.start_byte < binding.available_from or current.start_byte >= node.start_byte:
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
        if self._managed_invocation(node):
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
        else:
            static = bool(receiver is not None and receiver.type == "identifier"
                          and not self.has_binding(self.unit.text(receiver), node))
            cid = self._local_class(self.unit.text(receiver)) if static else self._constructed_class(receiver, node)
            if receiver is not None and receiver.type != "identifier":
                self.visit(receiver)
            candidates = _method_candidates(self.index, self.store, cid, name, len(arguments)) if cid else []
            if (len(candidates) == 1 and not self.store.is_ambiguous(candidates[0].id)
                    and candidates[0].static == static and candidates[0].parameter_count == len(arguments)
                    and _modifiers(candidates[0].unit.data, candidates[0].node)[0] & {"public", "global"}):
                method = candidates[0]
                self.store.relation(self.method.id, method.id, "calls", self.unit.location(node),
                                    "trigger.local_method_call", qualifiers={"invocation": "static_method" if static else "constructed_instance_method"},
                                    resolution={"basis": "unique_local_static_method" if static else "explicit_constructor_type",
                                                "class": cid, "parser": PARSER_VERSION})
                if not static:
                    self._constructed_virtual_dispatch(cid, method, node)
            else:
                self.diagnostic("TRIGGER_CALL_UNRESOLVED", "Call target requires unavailable, ambiguous or inherited dispatch analysis.",
                                node, "ambiguous" if len(candidates) > 1 else "unresolved")
        for argument in arguments:
            self.visit(argument)

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
                                            "trigger_events": events},
                                resolution={"basis": "explicit_constructor_and_local_virtual_call", "class": concrete_class,
                                            "dispatch_site": entry_method.unit.location(invocation),
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
