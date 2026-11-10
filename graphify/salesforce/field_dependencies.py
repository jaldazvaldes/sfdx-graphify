"""Dependency evidence from decomposed CustomField formula and roll-up metadata."""
from __future__ import annotations

from .formula_refs import formula_field_context, formula_references
from .relationship_paths import add_navigation_reads, resolve_source_field_path
from .schema import API_NAME, component_id

_ROLLUP_TAGS = frozenset({"summaryForeignKey", "summaryOperation", "summarizedField", "summaryFilterItems"})
FIELD_DEPENDENCY_TAGS = _ROLLUP_TAGS | {"formula"}


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
