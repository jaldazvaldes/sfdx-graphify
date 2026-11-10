"""Dependency evidence from decomposed Salesforce CustomField metadata."""
from __future__ import annotations

from .formula_refs import formula_field_context, formula_references
from .relationship_paths import add_navigation_reads, resolve_source_field_path
from .schema import API_NAME, component_id

_ROLLUP_TAGS = frozenset({"summaryForeignKey", "summaryOperation", "summarizedField", "summaryFilterItems"})
FIELD_DEPENDENCY_TAGS = _ROLLUP_TAGS | {"formula", "lookupFilter"}


def _formula_reads(document, node, store, source, owner):
    location = document.location(node)
    if node.children or not node.text:
        store.diagnostic("CUSTOM_FIELD_FORMULA_INVALID", "A nonempty scalar field formula is required.",
                         location, state="unresolved")
        return
    references, issues = formula_references(node.text)
    for issue in issues:
        store.diagnostic("CUSTOM_FIELD_FORMULA_SYNTAX_UNSUPPORTED",
                         "Field formula syntax exceeds lexical reference coverage.", location, issue=issue)
    for number, reference in enumerate(references, 1):
        occurrence = {**location, "locator": f"{node.locator}/reference[{number}]"}
        context = formula_field_context(reference.text, owner, record_basis="custom_field_object")
        if context is None:
            store.diagnostic("CUSTOM_FIELD_FORMULA_GLOBAL_UNSUPPORTED",
                             "This global formula reference has no supported typed field target.",
                             occurrence, reference=reference.text)
            continue
        target_owner, field_path, basis = context
        resolved = resolve_source_field_path(store, target_owner, field_path)
        if resolved is None:
            store.diagnostic("CUSTOM_FIELD_FORMULA_PATH_UNRESOLVED",
                             "The complete field path is not uniquely established.", occurrence,
                             state="unresolved", reference=reference.text, object=target_owner)
            continue
        target = store.field(resolved["owner"], resolved["field"], occurrence)
        if target is None:
            continue
        resolution = {**basis, "reference": reference.text, "relationship_path": field_path,
                      "steps": resolved["steps"], "formula_reference": {
                          "expression_locator": node.locator, "decoded_character_offset": reference.offset,
                          "source_position_basis": "containing_xml_expression_element",
                      }}
        store.relation(source, target, "reads", occurrence, "custom_field.formula_reference",
                       resolution=resolution, qualifiers={"context": "custom_field_formula"})
        add_navigation_reads(store, source, occurrence, resolution, rule="custom_field.formula_navigation",
                             qualifiers={"context": "custom_field_formula_navigation"})


def _single_leaf(nodes):
    return nodes[0] if len(nodes) == 1 and not nodes[0].children and nodes[0].text else None


def _rollup_operand(document, nodes, parent, store, source, detail, basis, *, tag, relation, context):
    node = _single_leaf(nodes)
    if node is not None:
        parts = node.text.split(".")
        if len(parts) == 2 and parts[0].casefold() == detail.casefold():
            parts = parts[1:]
        if len(parts) == 1 and API_NAME.fullmatch(parts[0]):
            location = document.location(node)
            target = store.field(detail, parts[0], location)
            if target is not None:
                store.relation(source, target, relation, location, f"custom_field.{context}",
                               resolution={**basis, "operand": tag, "reference": node.text},
                               qualifiers={"context": context})
            return
    store.diagnostic("CUSTOM_FIELD_ROLLUP_FIELD_UNRESOLVED",
                     "A roll-up operand needs one direct field of the declared detail object.",
                     document.location(node if node is not None else parent), state="unresolved", construct=tag)


def _rollup_reads(document, store, source, owner, lookup_targets):
    root = document.root
    key = _single_leaf(root.all("summaryForeignKey"))
    parts = key.text.split(".") if key is not None else []
    if key is None or len(parts) != 2 or not all(API_NAME.fullmatch(part) for part in parts):
        store.diagnostic("CUSTOM_FIELD_ROLLUP_KEY_UNRESOLVED",
                         "A roll-up requires one qualified detail-object foreign key.",
                         document.location(key if key is not None else root), state="unresolved")
        return
    detail, field = parts
    key_location = document.location(key)
    key_id = component_id("field", f"{detail}.{field}")
    declaration = store.components.get(key_id, {})
    data_type = declaration.get("data_type") or ""
    targets = lookup_targets.get(key_id, set())
    if (data_type.casefold() not in {"", "masterdetail", "lookup"}
            or (targets and targets != {owner.casefold()})):
        store.diagnostic("CUSTOM_FIELD_ROLLUP_KEY_CONFLICT",
                         "The roll-up foreign key conflicts with the local field declaration.",
                         key_location, state="unresolved", reference=key.text)
        return
    target = store.field(detail, field, key_location)
    if target is None:
        return
    operation_node = _single_leaf(root.all("summaryOperation"))
    operation = operation_node.text.casefold() if operation_node is not None else None
    if operation not in {"count", "min", "max", "sum"}:
        store.diagnostic("CUSTOM_FIELD_ROLLUP_OPERATION_UNRESOLVED", "The roll-up operation is not supported.",
                         document.location(operation_node if operation_node is not None else root),
                         state="unresolved")
    basis = {"basis": "explicit_rollup_foreign_key", "foreign_key": key_location,
             "detail_object": detail, "master_object": owner, "operation": operation}
    store.relation(source, target, "reads", key_location, "custom_field.rollup_foreign_key",
                   resolution={**basis, "reference": key.text}, qualifiers={"context": "rollup_foreign_key"})
    summarized = root.all("summarizedField")
    if summarized or operation in {"min", "max", "sum"}:
        _rollup_operand(document, summarized, root, store, source, detail, basis,
                        tag="summarizedField", relation="reads", context="rollup_summarized_field")
    for item in root.all("summaryFilterItems"):
        _rollup_operand(document, item.all("field"), item, store, source, detail, basis,
                        tag="field", relation="filters", context="rollup_filter")
        value_fields = item.all("valueField")
        if value_fields:
            _rollup_operand(document, value_fields, item, store, source, detail, basis,
                            tag="valueField", relation="filters", context="rollup_filter")



def _lookup_filter_operand(owner, targets, text):
    prefix, separator, path = text.partition(".")
    if not separator:
        return None
    if prefix.casefold() in {"$source", owner.casefold()}:
        return owner, path, {"basis": "lookup_filter_source_object", "object": owner}
    if prefix.casefold() in targets:
        return prefix, path, {"basis": "lookup_filter_declared_target", "object": prefix}
    if prefix.casefold() in {"$user", "$profile", "$userrole", "$organization"}:
        return formula_field_context(text, owner, record_basis="lookup_filter_source_object")
    return None


def _lookup_filter_reads(document, store, source, owner, targets, filters):
    root = document.root
    if len(filters) != 1:
        store.diagnostic("LOOKUP_FILTER_AMBIGUOUS", "Expected one lookupFilter definition.",
                         document.location(root), state="ambiguous")
        return
    lookup_filter = filters[0]
    field_types = root.all("type")
    if field_types and (len(field_types) != 1 or field_types[0].children
                        or field_types[0].text.casefold() not in {"lookup", "masterdetail", "hierarchy"}):
        store.diagnostic("LOOKUP_FILTER_FIELD_TYPE_UNSUPPORTED",
                         "A lookup filter needs a compatible relationship field declaration.",
                         document.location(lookup_filter), state="unresolved")
        return
    active = _single_leaf(lookup_filter.all("active"))
    status = {"true": "Active", "false": "Inactive", "1": "Active", "0": "Inactive"}.get(
        active.text if active is not None else "", "Unknown"
    )
    if status == "Unknown":
        store.diagnostic("LOOKUP_FILTER_STATUS_UNRESOLVED", "Expected one boolean lookup filter active flag.",
                         document.location(lookup_filter), state="unresolved")
    qualifiers = {"context": "lookup_filter", "local_status": status}
    for item in lookup_filter.all("filterItems"):
        for tag in ("field", "valueField"):
            matches = item.all(tag)
            if tag == "valueField" and not matches:
                continue
            node = _single_leaf(matches)
            location = document.location(node if node is not None else item)
            context = _lookup_filter_operand(owner, targets, node.text) if node is not None else None
            resolved = resolve_source_field_path(store, context[0], context[1]) if context is not None else None
            if resolved is None or node is None or context is None:
                store.diagnostic("LOOKUP_FILTER_FIELD_UNRESOLVED",
                                 "Expected a literal lookup filter field on a proven source or target.",
                                 location, state="unresolved", construct=tag)
                continue
            target = store.field(resolved["owner"], resolved["field"], location)
            if target is None:
                continue
            resolution = {**context[2], "reference": node.text, "relationship_path": context[1],
                          "steps": resolved["steps"], "operand": tag}
            store.relation(source, target, "filters", location, "custom_field.lookup_filter_operand",
                           resolution=resolution, qualifiers=qualifiers)
            add_navigation_reads(store, source, location, resolution, rule="custom_field.lookup_filter_navigation",
                                 qualifiers={**qualifiers, "context": "lookup_filter_navigation"})


def _picklist_controller(document, store, source, owner):
    root = document.root
    sets = root.all("valueSet")
    controllers = [node for value_set in sets for node in value_set.all("controllingField")]
    if not controllers:
        return
    node = _single_leaf(controllers)
    location = document.location(node if node is not None else root)
    name = node.text if node is not None else ""
    prefix, separator, field = name.partition(".")
    if separator and prefix.casefold() == owner.casefold():
        name = field
    if (node is None or len(sets) != 1 or root.value("type").casefold() not in {"picklist", "multiselectpicklist"}
            or not API_NAME.fullmatch(name)):
        store.diagnostic("PICKLIST_CONTROLLER_UNRESOLVED",
                         "A dependent picklist needs one literal controlling field on the same object.",
                         location, state="unresolved")
        return
    target_id = component_id("field", f"{owner}.{name}")
    declared_type = store.components.get(target_id, {}).get("data_type") or ""
    if target_id == source or (declared_type and declared_type.casefold() not in {"checkbox", "picklist"}):
        store.diagnostic("PICKLIST_CONTROLLER_CONFLICT", "The controlling field conflicts with its local declaration.",
                         location, state="unresolved")
        return
    target = store.field(owner, name, location)
    if target is not None:
        store.relation(source, target, "filters", location, "custom_field.dependent_picklist_controller",
                       resolution={"basis": "explicit_picklist_controller", "object": owner, "reference": node.text},
                       qualifiers={"context": "dependent_picklist"})


def extract_field_dependencies(document, store, source, owner, lookup_targets):
    root = document.root
    formulas = root.all("formula")
    if len(formulas) > 1:
        store.diagnostic("CUSTOM_FIELD_FORMULA_AMBIGUOUS", "Multiple field formulas prevent unique resolution.",
                         document.location(root), state="ambiguous")
    elif formulas:
        _formula_reads(document, formulas[0], store, source, owner)

    if root.value("type").casefold() == "summary":
        _rollup_reads(document, store, source, owner, lookup_targets)
    elif any(child.tag in _ROLLUP_TAGS for child in root.children):
        store.diagnostic("CUSTOM_FIELD_ROLLUP_TYPE_UNRESOLVED", "Roll-up metadata needs a Summary field type.",
                         document.location(root), state="unresolved")

    lookup_filters = root.all("lookupFilter")
    if lookup_filters:
        _lookup_filter_reads(document, store, source, owner, lookup_targets.get(source, set()), lookup_filters)
    _picklist_controller(document, store, source, owner)
