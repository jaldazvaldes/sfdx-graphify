"""Workflow outbound payloads, rule criteria and explicitly declared field updates.

Definitions alone do not establish that an action runs, or is deployed. No
endpoint URL, integration user or action values are copied into the snapshot.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath

from .formula_refs import formula_field_context, formula_references
from .relationship_paths import add_navigation_reads, resolve_source_field_path
from .schema import API_NAME, DataError, component_id

_DESCRIPTIVE = {"fullName", "apiVersion", "description", "endpointUrl", "fields", "includeSessionId",
                "integrationUser", "name", "protected", "useDeadLetterQueue"}
_FIELD_UPDATE_OPERATIONS = {"Formula", "Literal", "LookupValue", "NextValue", "Null", "PreviousValue"}


def _workflow_documents(documents, store):
    for document in documents:
        if not document.path.endswith(".workflow-meta.xml"):
            continue
        owner = PurePosixPath(document.path).name.removesuffix(".workflow-meta.xml")
        if document.root.tag != "Workflow" or not API_NAME.fullmatch(owner):
            store.diagnostic("WORKFLOW_ROOT_INVALID", "Expected a Workflow root and literal object filename.",
                             document.location(document.root), state="unresolved")
            continue
        yield document, owner


def _outbound_definitions(document, owner, store):
    for node in document.root.all("outboundMessages"):
        names = node.all("fullName")
        name = names[0].text if len(names) == 1 else ""
        if not API_NAME.fullmatch(name):
            store.diagnostic("WORKFLOW_OUTBOUND_NAME_UNRESOLVED", "Outbound message needs a unique literal fullName.",
                             document.location(node), state="ambiguous" if len(names) > 1 else "unresolved")
            continue
        yield node, f"{owner}.{name}"


def _single_leaf(node, tag):
    matches = node.all(tag)
    return matches[0] if len(matches) == 1 and not matches[0].children else None


def _rule_names(document, owner, store):
    for node in document.root.all("rules"):
        name = _single_leaf(node, "fullName")
        qualified = f"{owner}.{name.text if name else ''}"
        try:
            component_id("workflowrule", qualified)
        except DataError:
            store.diagnostic("WORKFLOW_RULE_NAME_UNRESOLVED", "Workflow rule needs a unique literal fullName.",
                             document.location(node), state="unresolved")
        else:
            yield node, qualified


def index_workflow(documents, store):
    for document, owner in _workflow_documents(documents, store):
        for node, name in _outbound_definitions(document, owner, store):
            store.ensure_component("outboundmessage", name, document.location(node),
                                   owner=owner, action_kind="outboundMessage")
        for node, name in _rule_names(document, owner, store):
            active = _single_leaf(node, "active")
            status = {"true": "Active", "false": "Inactive"}.get(active.text if active else "", "Unknown")
            if status == "Unknown":
                store.diagnostic("WORKFLOW_RULE_STATUS_UNRESOLVED", "Workflow rule needs a unique boolean active element.",
                                 document.location(node), state="unresolved")
            cid = store.ensure_component("workflowrule", name, document.location(node), owner=owner, local_status=status)
            if store.is_ambiguous(cid):
                store.components[cid]["local_status"] = None


def _extract_outbound(document, owner, store):
    for node, name in _outbound_definitions(document, owner, store):
        source = store.resolve_component("outboundmessage", name, document.location(node))
        if source is None:
            continue
        owner_id = store.resolve_component("object", owner, document.location(node))
        if owner_id:
            store.relation(owner_id, source, "contains", document.location(node), "workflow.outbound_owner")
        for field in node.all("fields"):
            name = field.text
            if name.startswith(owner + "."):
                name = name[len(owner) + 1:]
            if not API_NAME.fullmatch(name):
                store.diagnostic("WORKFLOW_OUTBOUND_FIELD_UNSUPPORTED", "Payload field must be a literal direct field.",
                                 document.location(field))
                continue
            target = store.field(owner, name, document.location(field))
            if target:
                store.relation(source, target, "reads", document.location(field), "workflow.outbound_payload_field",
                               qualifiers={"operation": "outbound_payload", "action_kind": "outboundMessage"})
        for child in node.children:
            if child.tag not in _DESCRIPTIVE:
                store.diagnostic("WORKFLOW_OUTBOUND_CONSTRUCT_UNSUPPORTED", "Outbound message element is outside coverage.",
                                 document.location(child), construct=child.tag)


def _field_updates(workflows):
    definitions = defaultdict(list)
    for document, owner in workflows:
        for node in document.root.all("fieldUpdates"):
            name = _single_leaf(node, "fullName")
            if name is not None and name.text.isprintable():
                definitions[owner.casefold(), name.text.casefold()].append((document, node))
    return definitions


def _rule_actions(rule):
    for action in rule.all("actions"):
        yield action, "immediate"
    for trigger in rule.all("workflowTimeTriggers"):
        for action in trigger.all("actions"):
            yield action, "time_dependent"


def _relative_field(owner, path):
    prefix, separator, field = path.partition(".")
    if separator and prefix.casefold() == owner.casefold() and not field.startswith("$"):
        return field
    return path


def _read_reference(store, source, owner, reference, occurrence, *, rule, qualifiers, details):
    context = formula_field_context(_relative_field(owner, reference), owner, record_basis="workflow_rule_object")
    if context is None:
        store.diagnostic("WORKFLOW_GLOBAL_UNSUPPORTED", "This global reference has no supported typed field target.",
                         occurrence)
        return
    target_owner, field_path, basis = context
    resolved = resolve_source_field_path(store, target_owner, field_path)
    if resolved is None:
        store.diagnostic("WORKFLOW_READ_FIELD_PATH_UNRESOLVED", "The complete field path is not uniquely established.",
                         occurrence, state="unresolved")
        return
    target = store.field(resolved["owner"], resolved["field"], occurrence)
    if target is None:
        return
    resolution = {**basis, **details, "reference": reference, "relationship_path": field_path, "steps": resolved["steps"]}
    store.relation(source, target, "reads", occurrence, rule, resolution=resolution, qualifiers=qualifiers)
    add_navigation_reads(store, source, occurrence, resolution, rule=f"{rule}_navigation",
                         qualifiers={**qualifiers, "context": f"{qualifiers['context']}_navigation"})


def _formula_reads(document, node, store, source, owner, *, rule, qualifiers, action_site=None):
    location = document.location(node)
    if node.children or not node.text:
        store.diagnostic("WORKFLOW_FORMULA_INVALID", "A nonempty scalar formula expression is required.",
                         location, state="unresolved")
        return
    references, issues = formula_references(node.text)
    for issue in issues:
        store.diagnostic("WORKFLOW_FORMULA_SYNTAX_UNSUPPORTED", "Formula syntax exceeds lexical reference coverage.",
                         location, issue=issue)
    for number, reference in enumerate(references, 1):
        occurrence = {**location, "locator": f"{node.locator}/reference[{number}]"}
        details = {"formula_reference": {
            "expression_locator": node.locator, "decoded_character_offset": reference.offset,
            "source_position_basis": "containing_xml_expression_element",
        }}
        if action_site is not None:
            details["action_site"] = action_site
        _read_reference(store, source, owner, reference.text, occurrence, rule=rule,
                        qualifiers=qualifiers, details=details)


def _criteria_reads(document, rule, store, source, owner, local_status):
    formulas = rule.all("formula")
    criteria = rule.all("criteriaItems")
    if formulas:
        if len(formulas) > 1 or criteria:
            store.diagnostic("WORKFLOW_CRITERIA_AMBIGUOUS", "Expected one formula or a list of criteria items.",
                             document.location(rule), state="ambiguous")
        else:
            _formula_reads(document, formulas[0], store, source, owner, rule="workflow.rule_criteria_formula",
                           qualifiers={"context": "workflow_criteria_formula", "local_status": local_status})
        return
    for item in criteria:
        for tag in ("field", "valueField"):
            if tag == "valueField" and not item.all(tag):
                continue
            operand = _single_leaf(item, tag)
            if operand is None or not operand.text:
                store.diagnostic("WORKFLOW_CRITERIA_FIELD_UNRESOLVED", "A criteria field needs one literal field path.",
                                 document.location(item), state="unresolved", construct=tag)
                continue
            _read_reference(store, source, owner, operand.text, document.location(operand),
                            rule="workflow.rule_criteria_field", details={"operand": tag},
                            qualifiers={"context": "workflow_criteria_item", "local_status": local_status})


def _extract_rule_updates(document, owner, store, definitions, excluded):
    for rule, name in _rule_names(document, owner, store):
        if component_id("workflowrule", name) in excluded:
            continue
        source = store.resolve_component("workflowrule", name, document.location(rule))
        if source is None:
            continue
        local_status = store.components[source].get("local_status")
        owner_id = store.resolve_component("object", owner, document.location(rule))
        if owner_id is None:
            continue
        store.relation(owner_id, source, "contains", document.location(rule), "workflow.rule_owner")
        if local_status not in {"Active", "Inactive"}:
            continue
        _criteria_reads(document, rule, store, source, owner, local_status)
        for action, timing in _rule_actions(rule):
            types = action.all("type")
            if not any(value.text == "FieldUpdate" for value in types):
                store.diagnostic("WORKFLOW_RULE_ACTION_UNSUPPORTED",
                                 "Workflow rule actions other than field updates are outside coverage.",
                                 document.location(action), construct=action.tag)
                continue
            _extract_field_update(document, owner, store, definitions, source, local_status, action, timing)


def _extract_field_update(document, owner, store, definitions, source, local_status, action, timing):
    location = document.location(action)
    name = _single_leaf(action, "name")
    if _single_leaf(action, "type") is None or name is None or not name.text.isprintable():
        store.diagnostic("WORKFLOW_FIELD_UPDATE_ACTION_UNRESOLVED", "Field update action needs one literal name and type.",
                         location, state="unresolved")
        return
    candidates = definitions.get((owner.casefold(), name.text.casefold()), [])
    if len(candidates) != 1 or candidates[0][0].path != document.path:
        store.diagnostic("WORKFLOW_FIELD_UPDATE_DEFINITION_UNRESOLVED",
                         "Field update action needs one matching definition in its Workflow file.",
                         location, state="ambiguous" if len(candidates) > 1 else "unresolved")
        return
    _, definition = candidates[0]
    target_objects = definition.all("targetObject")
    if target_objects:
        for node in target_objects:
            store.diagnostic("WORKFLOW_FIELD_UPDATE_TARGET_OBJECT_UNRESOLVED",
                             "Field update targetObject requires an independently resolved parent target.",
                             document.location(node), state="unresolved")
        return
    field = _single_leaf(definition, "field")
    operation = _single_leaf(definition, "operation")
    if operation is None or operation.text not in _FIELD_UPDATE_OPERATIONS:
        store.diagnostic("WORKFLOW_FIELD_UPDATE_OPERATION_UNSUPPORTED",
                         "Field update definition needs one recognized operation.",
                         document.location(definition))
        return
    target_name = _relative_field(owner, field.text) if field is not None else ""
    if (not API_NAME.fullmatch(target_name) or definition.all("sourceObject") or definition.all("sourceField")):
        store.diagnostic("WORKFLOW_FIELD_UPDATE_TARGET_UNSUPPORTED",
                         "Field update must declare one direct field on the rule's object.",
                         document.location(field if field is not None else definition))
        return
    field_location = document.location(field)
    target = store.field(owner, target_name, field_location)
    qualifiers = {"action_kind": "FieldUpdate", "action_timing": timing,
                  "operation": operation.text, "local_status": local_status}
    if target:
        store.relation(
            source, target, "writes", location, "workflow.rule_field_update",
            resolution={"basis": "workflow_field_update_definition", "action": name.text,
                        "field_definition": field_location},
            qualifiers=qualifiers,
        )
    if operation.text == "Formula":
        formulas = definition.all("formula")
        if len(formulas) != 1:
            store.diagnostic("WORKFLOW_FIELD_UPDATE_FORMULA_UNRESOLVED", "A formula field update needs one formula.",
                             document.location(definition), state="ambiguous" if formulas else "unresolved")
        else:
            _formula_reads(document, formulas[0], store, source, owner, rule="workflow.field_update_formula",
                           qualifiers={**qualifiers, "context": "workflow_field_update_formula"}, action_site=location)


def extract_workflow(documents, store, *, excluded=()):
    workflows = list(_workflow_documents(documents, store))
    definitions = _field_updates(workflows)
    for document, owner in workflows:
        _extract_outbound(document, owner, store)
        _extract_rule_updates(document, owner, store, definitions, excluded)
        for child in document.root.children:
            if child.tag not in {"outboundMessages", "rules", "fieldUpdates"}:
                store.diagnostic("WORKFLOW_CONSTRUCT_UNSUPPORTED", "Workflow element is outside coverage.",
                                 document.location(child), construct=child.tag)
