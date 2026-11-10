"""Dependencies declared by Salesforce ApprovalProcess metadata.

See https://developer.salesforce.com/docs/platform/api-metadata/references/meta-types/metadata-approvalprocess.html.
"""
from __future__ import annotations

from pathlib import PurePosixPath

from .schema import API_NAME, component_id
from .workflow import (
    _field_updates,
    _formula_reads,
    _read_reference,
    _single_leaf,
    _workflow_documents,
    extract_field_update,
)

APPROVAL_SUFFIX = ".approvalProcess-meta.xml"
_PROCESS_ACTIONS = ("initialSubmissionActions", "finalApprovalActions", "finalRejectionActions", "recallActions")
_STEP_ACTIONS = ("approvalActions", "rejectionActions")
_REFERENCE_ALIASES = {"owner": "OwnerId", "createdby": "CreatedById", "lastmodifiedby": "LastModifiedById"}


def _approval_documents(documents, store):
    for document in documents:
        path = PurePosixPath(document.path)
        if not path.name.casefold().endswith(APPROVAL_SUFFIX.casefold()):
            continue
        name = path.name[:-len(APPROVAL_SUFFIX)]
        owner, _, process = name.partition(".")
        full_names = document.root.all("fullName")
        declared = _single_leaf(document.root, "fullName")
        if (document.root.tag != "ApprovalProcess" or path.parent.name != "approvalProcesses"
                or not API_NAME.fullmatch(owner) or not API_NAME.fullmatch(process)
                or (full_names and (declared is None or declared.text.casefold() != name.casefold()))):
            store.diagnostic("APPROVAL_PROCESS_IDENTITY_UNRESOLVED",
                             "An approval process needs a literal Object.Process filename and ApprovalProcess root.",
                             document.location(document.root), state="unresolved")
            continue
        yield document, owner, name


def index_approval_processes(documents, store):
    for document, owner, name in _approval_documents(documents, store):
        active = _single_leaf(document.root, "active")
        status = {"true": "Active", "false": "Inactive", "1": "Active", "0": "Inactive"}.get(
            active.text if active is not None else "", "Unknown"
        )
        if status == "Unknown":
            store.diagnostic("APPROVAL_PROCESS_STATUS_UNRESOLVED",
                             "Approval process needs one boolean active element.",
                             document.location(document.root), state="unresolved")
        cid = store.ensure_component("approvalprocess", name, document.location(document.root),
                                     owner=owner, local_status=status)
        if store.is_ambiguous(cid):
            store.components[cid]["local_status"] = None


def _criterion_reads(document, container, store, source, owner, status, phase):
    criteria = container.all("entryCriteria")
    if not criteria:
        return
    if len(criteria) != 1:
        store.diagnostic("APPROVAL_CRITERIA_AMBIGUOUS", "Expected one entryCriteria definition.",
                         document.location(container), state="ambiguous")
        return
    criteria = criteria[0]
    formulas = criteria.all("formula")
    items = criteria.all("criteriaItems")
    qualifiers = {"context": "approval_entry_criteria", "phase": phase, "local_status": status}
    if formulas:
        if len(formulas) != 1 or items:
            store.diagnostic("APPROVAL_CRITERIA_AMBIGUOUS", "Expected one formula or filter items.",
                             document.location(criteria), state="ambiguous")
        else:
            _formula_reads(document, formulas[0], store, source, owner, rule="approval.entry_criteria_formula",
                           qualifiers=qualifiers)
        return

    for item in items:
        field = _single_leaf(item, "field")
        if field is None or not field.text:
            store.diagnostic("APPROVAL_CRITERIA_FIELD_UNRESOLVED", "Expected one literal criterion field.",
                             document.location(item), state="unresolved")
        else:
            _read_reference(store, source, owner, field.text, document.location(field),
                            rule="approval.entry_criteria_field", qualifiers=qualifiers,
                            details={"operand": "field"})
        for unsupported in item.all("valueField"):
            store.diagnostic("APPROVAL_CRITERIA_VALUE_FIELD_UNSUPPORTED",
                             "Approval criteria do not support field-to-field operands.",
                             document.location(unsupported))


def _declared_field_read(document, node, store, source, owner, status, context, reference):
    if node.children or not reference:
        store.diagnostic("APPROVAL_FIELD_UNRESOLVED", "Expected one literal approval field reference.",
                         document.location(node), state="unresolved")
        return
    _read_reference(store, source, owner, reference, document.location(node),
                    rule=f"approval.{context}",
                    qualifiers={"context": context, "local_status": status},
                    details={"declared_reference": node.text})


def _approver_reads(document, store, source, owner, status):
    root = document.root
    for page in root.all("approvalPageFields"):
        for field in page.all("field"):
            ref = _REFERENCE_ALIASES.get(field.text.casefold(), field.text)
            _declared_field_read(document, field, store, source, owner, status, "page_field", ref)

    uses_hierarchy = False
    for step in root.all("approvalStep"):
        for assignment in step.all("assignedApprover"):
            for approver in assignment.all("approver"):
                kind = _single_leaf(approver, "type")
                if kind is None:
                    continue
                if kind.text == "relatedUserField":
                    field = _single_leaf(approver, "name")
                    if field is not None:
                        ref = _REFERENCE_ALIASES.get(field.text.casefold(), field.text)
                        _declared_field_read(document, field, store, source, owner, status, "related_user_field", ref)
                    else:
                        store.diagnostic("APPROVAL_APPROVER_FIELD_UNRESOLVED",
                                         "Related-user approver needs a unique field name.",
                                         document.location(approver), state="unresolved")
                elif kind.text == "userHierarchyField":
                    uses_hierarchy = True
    if uses_hierarchy:
        settings = root.all("nextAutomatedApprover")
        hierarchy = _single_leaf(settings[0], "userHierarchyField") if len(settings) == 1 else None
        if hierarchy is None:
            store.diagnostic("APPROVAL_HIERARCHY_FIELD_UNRESOLVED",
                             "A hierarchy approver needs one configured User hierarchy field.",
                             document.location(root), state="unresolved")
        else:
            ref = "ManagerId" if hierarchy.text.casefold() == "manager" else hierarchy.text
            _declared_field_read(document, hierarchy, store, source, "User", status, "user_hierarchy_field", ref)


def _actions(document, container, tags, store, updates, source, owner, status, phase):
    for tag in tags:
        for group in container.all(tag):
            for action in group.all("action"):
                kind = _single_leaf(action, "type")
                if kind is not None and kind.text == "FieldUpdate":
                    timing = f"{phase}.{tag}"
                    extract_field_update(document, owner, store, updates, source, status, action, timing)
                elif kind is not None and kind.text == "OutboundMessage":
                    name = _single_leaf(action, "name")
                    if name is None or not API_NAME.fullmatch(name.text):
                        store.diagnostic("APPROVAL_OUTBOUND_ACTION_UNRESOLVED",
                                         "Outbound action needs one literal name.",
                                         document.location(action), state="unresolved")
                        continue
                    target = store.resolve_component("outboundmessage", f"{owner}.{name.text}",
                                                     document.location(action))
                    if target:
                        store.relation(source, target, "references", document.location(action),
                                       "approval.outbound_action",
                                       qualifiers={"action_kind": "OutboundMessage",
                                                   "action_timing": f"{phase}.{tag}", "local_status": status})
                else:
                    store.diagnostic("APPROVAL_ACTION_UNSUPPORTED",
                                     "This approval action type is outside dependency coverage.",
                                     document.location(action), construct=kind.text if kind is not None else None)


def extract_approval_processes(documents, workflows, store, *, excluded=()):
    if not documents:
        return
    updates = _field_updates(_workflow_documents(workflows, store))
    for document, owner, name in _approval_documents(documents, store):
        if component_id("approvalprocess", name) in excluded:
            continue
        root = document.root
        location = document.location(root)
        source = store.resolve_component("approvalprocess", name, location)
        if source is None:
            continue
        owner_id = store.resolve_component("object", owner, location)
        if owner_id is None:
            continue
        store.relation(owner_id, source, "contains", location, "approval.process_owner")
        status = store.components[source]["local_status"]
        if status not in {"Active", "Inactive"}:
            continue
        _criterion_reads(document, root, store, source, owner, status, "process")
        _approver_reads(document, store, source, owner, status)
        _actions(document, root, _PROCESS_ACTIONS, store, updates, source, owner, status, "process")
        for step in root.all("approvalStep"):
            _criterion_reads(document, step, store, source, owner, status, "step")
            _actions(document, step, _STEP_ACTIONS, store, updates, source, owner, status, "step")
