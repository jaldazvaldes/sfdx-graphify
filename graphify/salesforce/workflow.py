"""Workflow outbound payloads, rule criteria and explicitly declared field updates.

Definitions alone do not establish that an action runs, or is deployed. No
endpoint URL, integration user or action values are copied into the snapshot.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath

from .formula_refs import formula_field_context, formula_references
from .relationship_paths import add_navigation_reads, declared_lookup_targets, resolve_source_field_path
from .schema import API_NAME, DataError, component_id
from .write_values import workflow_write_value

_DESCRIPTIVE = {"fullName", "apiVersion", "description", "endpointUrl", "fields", "includeSessionId",
                "integrationUser", "name", "protected", "useDeadLetterQueue"}
_FIELD_UPDATE_OPERATIONS = {"Formula", "Literal", "LookupValue", "NextValue", "Null", "PreviousValue"}
# The Metadata API uses the child's foreign key in targetObject. Salesforce
# Help lists these standard-to-standard cross-object Workflow field updates.
# https://help.salesforce.com/s/articleView?id=workflow_cross_object_field_updates.htm&type=5
_STANDARD_UPDATE_PARENTS = {
    ("casecomment", "parentid"): "Case",
    ("emailmessage", "parentid"): "Case",
    ("opportunitylineitem", "opportunityid"): "Opportunity",
    ("opportunity", "accountid"): "Account",
}


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
            if name is not None and name.text and name.text.isprintable():
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
    feature = "approval" if store.components[source]["kind"] == "approvalprocess" else "workflow"
    basis_name = "approval_process_object" if feature == "approval" else "workflow_rule_object"
    context = formula_field_context(_relative_field(owner, reference), owner, record_basis=basis_name)
    if context is None:
        store.diagnostic(f"{feature.upper()}_GLOBAL_UNSUPPORTED", "This global reference has no supported typed field target.",
                         occurrence)
        return
    target_owner, field_path, basis = context
    resolved = resolve_source_field_path(store, target_owner, field_path)
    if resolved is None:
        store.diagnostic(f"{feature.upper()}_READ_FIELD_PATH_UNRESOLVED", "The complete field path is not uniquely established.",
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
    feature = "APPROVAL" if store.components[source]["kind"] == "approvalprocess" else "WORKFLOW"
    if node.children or not node.text:
        store.diagnostic(f"{feature}_FORMULA_INVALID", "A nonempty scalar formula expression is required.",
                         location, state="unresolved")
        return
    references, issues = formula_references(node.text)
    for issue in issues:
        store.diagnostic(f"{feature}_FORMULA_SYNTAX_UNSUPPORTED", "Formula syntax exceeds lexical reference coverage.",
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
            extract_field_update(document, owner, store, definitions, source, local_status, action, timing)


def _field_update_parent(store, owner, foreign_key):
    if not API_NAME.fullmatch(foreign_key):
        return None
    child = component_id("object", owner)
    lookup = component_id("field", f"{owner}.{foreign_key}")
    if child in store.components and store.is_ambiguous(child):
        return None
    declaration = store.components.get(lookup)
    if declaration is not None and declaration["definition_status"] != "referenced_only":
        if store.is_ambiguous(lookup) or declaration.get("data_type") not in {"Lookup", "MasterDetail"}:
            return None

    standard = _STANDARD_UPDATE_PARENTS.get((owner.casefold(), foreign_key.casefold()))
    targets = declared_lookup_targets(store).get(lookup, ())
    raw_targets = store.raw_lookup_targets.get(lookup, ())
    if len(targets) > 1 or (raw_targets and (len(raw_targets) != 1 or not targets)):
        return None
    if targets:
        parent = next(iter(targets))
        if store.is_ambiguous(parent) or (standard and parent != component_id("object", standard)):
            return None
        step = {"field": lookup, "target": parent, "basis": "declared_lookup"}
        if declaration and declaration["definitions"]:
            step["schema_source"] = declaration["definitions"][0]
        return store.components[parent]["api_name"], step

    if standard is None:
        return None
    parent = component_id("object", standard)
    if parent in store.components and store.is_ambiguous(parent):
        return None
    return standard, {"field": lookup, "target": parent, "basis": "standard_schema"}


def extract_field_update(document, owner, store, definitions, source, local_status, action, timing):
    """Resolve a Workflow action to a direct field on the record or its parent."""
    location = document.location(action)
    approval = store.components[source]["kind"] == "approvalprocess"
    feature = "APPROVAL" if approval else "WORKFLOW"
    name = _single_leaf(action, "name")
    if _single_leaf(action, "type") is None or name is None or not name.text or not name.text.isprintable():
        store.diagnostic(f"{feature}_FIELD_UPDATE_ACTION_UNRESOLVED", "Field update action needs one literal name and type.",
                         location, state="unresolved")
        return
    candidates = definitions.get((owner.casefold(), name.text.casefold()), [])
    if len(candidates) != 1 or (not approval and candidates[0][0].path != document.path):
        message = ("Approval field update needs one matching Workflow definition on the same object."
                   if approval else "Field update action needs one matching definition in its Workflow file.")
        store.diagnostic(f"{feature}_FIELD_UPDATE_DEFINITION_UNRESOLVED", message,
                         location, state="ambiguous" if len(candidates) > 1 else "unresolved")
        return
    definition_document, definition = candidates[0]
    target_owner = owner
    parent_step = None
    target_object = _single_leaf(definition, "targetObject")
    target_objects = definition.all("targetObject")
    if target_objects:
        # Approval field updates retain their existing same-object policy.
        parent = _field_update_parent(store, owner, target_object.text) if target_object and not approval else None
        if parent is None:
            for node in target_objects:
                store.diagnostic(f"{feature}_FIELD_UPDATE_TARGET_OBJECT_UNRESOLVED",
                                 "Field update targetObject needs one supported, unambiguous parent foreign key.",
                                 definition_document.location(node),
                                 state="ambiguous" if len(target_objects) > 1 else "unresolved")
            return
        target_owner, parent_step = parent
    field = _single_leaf(definition, "field")
    operation = _single_leaf(definition, "operation")
    if operation is None or operation.text not in _FIELD_UPDATE_OPERATIONS:
        store.diagnostic(f"{feature}_FIELD_UPDATE_OPERATION_UNSUPPORTED",
                         "Field update definition needs one recognized operation.",
                         definition_document.location(definition))
        return
    target_name = _relative_field(target_owner, field.text) if field is not None else ""
    if (not API_NAME.fullmatch(target_name) or definition.all("sourceObject") or definition.all("sourceField")):
        owner_description = ("the resolved parent object" if parent_step else
                             "the approval process's object" if approval else "the rule's object")
        store.diagnostic(f"{feature}_FIELD_UPDATE_TARGET_UNSUPPORTED",
                         f"Field update must declare one direct field on {owner_description}.",
                         definition_document.location(field if field is not None else definition))
        return
    field_location = definition_document.location(field)
    target = store.field(target_owner, target_name, field_location)
    qualifiers = {"action_kind": "FieldUpdate", "action_timing": timing,
                  "operation": operation.text, "local_status": local_status}
    if target:
        resolution = {"basis": "workflow_field_update_definition", "action": name.text,
                      "field_definition": field_location}
        if parent_step and target_object is not None:
            target_location = definition_document.location(target_object)
            resolution.update(target_object=target_location, steps=[parent_step])
            add_navigation_reads(store, source, target_location, resolution,
                                 rule="workflow.field_update_parent_navigation",
                                 qualifiers={**qualifiers, "context": "workflow_field_update_parent_navigation"})
        store.relation(
            source, target, "writes", location, "approval.field_update" if approval else "workflow.rule_field_update",
            resolution={**resolution, **workflow_write_value(definition_document, definition, operation,
                                                             store.components[target].get("data_type"))},
            qualifiers=qualifiers,
        )
    if operation.text == "Formula":
        formulas = definition.all("formula")
        if len(formulas) != 1:
            store.diagnostic(f"{feature}_FIELD_UPDATE_FORMULA_UNRESOLVED", "A formula field update needs one formula.",
                             definition_document.location(definition), state="ambiguous" if formulas else "unresolved")
        else:
            _formula_reads(definition_document, formulas[0], store, source, owner,
                           rule=f"{feature.lower()}.field_update_formula",
                           qualifiers={**qualifiers, "context": f"{feature.lower()}_field_update_formula"},
                           action_site=location)


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
