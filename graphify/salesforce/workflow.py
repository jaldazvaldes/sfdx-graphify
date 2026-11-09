"""Workflow outbound message definitions and declared payload dependencies.

Definitions alone do not establish that an action runs, or is deployed. No
endpoint URL, integration user or payload values are copied into the snapshot.
"""
from __future__ import annotations

from pathlib import PurePosixPath

from .schema import API_NAME

_DESCRIPTIVE = {"fullName", "apiVersion", "description", "endpointUrl", "fields", "includeSessionId",
                "integrationUser", "name", "protected", "useDeadLetterQueue"}


def _definitions(documents, store):
    for document in documents:
        if not document.path.endswith(".workflow-meta.xml"):
            continue
        owner = PurePosixPath(document.path).name.removesuffix(".workflow-meta.xml")
        if document.root.tag != "Workflow" or not API_NAME.fullmatch(owner):
            store.diagnostic("WORKFLOW_ROOT_INVALID", "Expected a Workflow root and literal object filename.",
                             document.location(document.root), state="unresolved")
            continue
        for node in document.root.all("outboundMessages"):
            names = node.all("fullName")
            name = names[0].text if len(names) == 1 else ""
            if not API_NAME.fullmatch(name):
                store.diagnostic("WORKFLOW_OUTBOUND_NAME_UNRESOLVED", "Outbound message needs a unique literal fullName.",
                                 document.location(node), state="ambiguous" if len(names) > 1 else "unresolved")
                continue
            yield document, node, owner, f"{owner}.{name}"


def index_workflow(documents, store):
    for document, node, owner, name in _definitions(documents, store):
        store.ensure_component("outboundmessage", name, document.location(node),
                               owner=owner, action_kind="outboundMessage")


def extract_workflow(documents, store):
    for document, node, owner, name in _definitions(documents, store):
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
    for document in documents:
        if document.path.endswith(".workflow-meta.xml") and document.root.tag == "Workflow":
            for child in document.root.children:
                if child.tag != "outboundMessages":
                    store.diagnostic("WORKFLOW_CONSTRUCT_UNSUPPORTED", "Only outbound message definitions are analyzed in Workflow metadata.",
                                     document.location(child), construct=child.tag)
