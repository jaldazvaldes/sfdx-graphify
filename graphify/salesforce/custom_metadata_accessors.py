"""Opt-in field reads through explicitly configured local Apex accessors.

Configuration supplies accessor semantics, never a substitute for source proof.
A call must resolve to one local static declaration and its field argument must
itself be a literal naming an unambiguous local custom metadata field.
"""
from __future__ import annotations

import json
import re

from .apex import ApexIndex, _MethodExtractor, _child, _method_candidates, _modifiers, _significant, _walk
from .schema import API_NAME, DataError, FactStore, component_id


_KEYS = frozenset({"class", "method", "arity", "field_argument", "type"})
_FIELD_LITERAL = re.compile(r"'([A-Za-z_][A-Za-z0-9_]*)'")
_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_RULES = 256


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError("Duplicate custom metadata accessor configuration key.")
        result[key] = value
    return result


def parse_accessor_rules(data: bytes | str | list) -> list[dict]:
    """Validate and canonicalize JSON/list rules for deterministic build scope.

    Arguments are one-based. Class, method and metadata type names follow Apex's
    case-insensitive identity. One class/method/arity can have only one rule.
    """
    if isinstance(data, bytes):
        if len(data) > _MAX_CONFIG_BYTES:
            raise DataError("Custom metadata accessor configuration exceeds 1 MiB.")
        try:
            data = data.decode("utf-8-sig")
        except UnicodeError as exc:
            raise DataError("Custom metadata accessor configuration must be UTF-8.") from exc
    if isinstance(data, str):
        try:
            if len(data.encode("utf-8")) > _MAX_CONFIG_BYTES:
                raise DataError("Custom metadata accessor configuration exceeds 1 MiB.")
            data = json.loads(data, object_pairs_hook=_unique_keys)
        except (UnicodeError, ValueError, RecursionError) as exc:
            if isinstance(exc, DataError):
                raise
            raise DataError("Invalid custom metadata accessor JSON configuration.") from exc
    if not isinstance(data, list) or len(data) > _MAX_RULES:
        raise DataError("Custom metadata accessors must be a JSON list of at most 256 rules.")
    normalized, seen = [], set()
    for item in data:
        if not isinstance(item, dict) or item.keys() != _KEYS:
            raise DataError("Each accessor requires exactly class, method, arity, field_argument and type.")
        for key in ("class", "method", "type"):
            if not isinstance(item[key], str) or not API_NAME.fullmatch(item[key]):
                raise DataError(f"Accessor {key} must be one literal API name.")
        if not item["type"].casefold().endswith("__mdt") or len(item["type"]) <= len("__mdt"):
            raise DataError("Accessor type must name a custom metadata type ending in __mdt.")
        arity, position = item["arity"], item["field_argument"]
        if type(arity) is not int or not 1 <= arity <= 256:
            raise DataError("Accessor arity must be an integer from 1 to 256.")
        if type(position) is not int or not 1 <= position <= arity:
            raise DataError("Accessor field_argument must be a one-based integer within its arity.")
        rule = {"class": item["class"].casefold(), "method": item["method"].casefold(),
                "arity": arity, "field_argument": position, "type": item["type"].casefold()}
        identity = (rule["class"], rule["method"], arity)
        if identity in seen:
            raise DataError("Duplicate class/method/arity custom metadata accessor rule.")
        seen.add(identity)
        normalized.append(rule)
    return sorted(normalized, key=lambda rule: (rule["class"], rule["method"], rule["arity"]))


class _AccessorBindings(_MethodExtractor):
    def diagnostic(self, *args, **kwargs):
        # This pass collects scope/shadow bindings only. General Apex analysis
        # remains responsible for record/loop diagnostics.
        pass


def _unresolved(store, method, node, reason):
    store.diagnostic("APEX_CUSTOM_METADATA_ACCESSOR_UNRESOLVED",
                     "The configured accessor needs a proven local static call and a literal locally defined metadata field.",
                     method.unit.location(node), state="unresolved", reason=reason)


def extract_custom_metadata_accessors(index: ApexIndex, store: FactStore, rules: list[dict]) -> None:
    """Extract configured class-qualified static calls in methods/constructors."""
    normalized = parse_accessor_rules(rules)
    if not normalized:
        return
    by_call = {(rule["class"], rule["method"], rule["arity"]): rule for rule in normalized}
    method_tokens = {rule["method"].encode("ascii") for rule in normalized}
    eligible_paths = set()
    for unit in index.units:
        source = unit.data.lower()
        if any(token in source for token in method_tokens):
            eligible_paths.add(unit.path)
    for collection in (index.methods, index.constructors):
        for class_id, methods in collection.items():
            if store.is_ambiguous(class_id):
                continue
            for method in methods:
                if (method.unit.path not in eligible_paths or method.unit.parse_error
                        or store.is_ambiguous(method.id)):
                    continue
                visitor = _AccessorBindings(method, index, store)
                if visitor.body is None:
                    continue
                body = method.unit.data[visitor.body.start_byte:visitor.body.end_byte].lower()
                if not any(token in body for token in method_tokens):
                    continue
                visitor.index_bindings()
                for node in _walk(visitor.body):
                    if node.type != "method_invocation":
                        continue
                    receiver = node.child_by_field_name("object")
                    if receiver is None or receiver.type != "identifier":
                        continue
                    receiver_name = method.unit.text(receiver)
                    method_name = method.unit.text(node.child_by_field_name("name"))
                    arguments = _significant(node.child_by_field_name("arguments"))
                    rule = by_call.get((receiver_name.casefold(), method_name.casefold(), len(arguments)))
                    if rule is None:
                        continue
                    if (not visitor.unshadowed_qualifier(receiver_name, receiver)
                            or visitor._initializing_binding(receiver_name, receiver)
                            or _child(node, "safe_navigation_operator") is not None):
                        _unresolved(store, method, node, "receiver_not_proven")
                        continue
                    candidates = _method_candidates(index, store, component_id("apexclass", receiver_name),
                                                    method_name, len(arguments), include_ambiguous=True)
                    if (len(candidates) != 1 or not candidates[0].static
                            or store.is_ambiguous(candidates[0].id)
                            or (candidates[0].unit.class_id != class_id
                                and not _modifiers(candidates[0].unit.data, candidates[0].node)[0] & {"public", "global"})):
                        _unresolved(store, method, node, "accessor_declaration_not_proven")
                        continue
                    argument = arguments[rule["field_argument"] - 1]
                    literal = (_FIELD_LITERAL.fullmatch(method.unit.text(argument))
                               if argument.type == "string_literal" else None)
                    if literal is None:
                        _unresolved(store, method, node, "literal_field_required")
                        continue
                    field_id = component_id("field", rule["type"] + "." + literal.group(1))
                    field = store.components.get(field_id)
                    owner = store.components.get(component_id("object", rule["type"]))
                    if (field is None or field["definition_status"] != "defined"
                            or (owner is not None and owner["definition_status"] == "ambiguous")):
                        _unresolved(store, method, node, "local_field_not_proven")
                        continue
                    target = candidates[0]
                    store.relation(method.id, field_id, "reads", method.unit.location(node),
                                   "apex.custom_metadata_accessor",
                                   qualifiers={"context": "custom_metadata_accessor"},
                                   resolution={"basis": "configured_literal_custom_metadata_accessor",
                                               "accessor": dict(rule), "accessor_method": target.id,
                                               "accessor_definition": target.unit.location(target.node),
                                               "field_definition": dict(field["definitions"][0])})
