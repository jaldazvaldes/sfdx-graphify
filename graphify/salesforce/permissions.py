"""Extract explicit access grants from Profile and PermissionSet metadata."""

from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath

from .schema import DataError, FactStore, component_id
from .xml_source import Document, Node


_PERMISSION_DOCUMENTS = {
    ".profile-meta.xml": ("profiles", "Profile", "profile"),
    ".permissionset-meta.xml": ("permissionsets", "PermissionSet", "permissionset"),
}

_PERMISSION_ROWS = {
    "fieldPermissions": ("field", "field", {"readable": "read", "editable": "edit"}),
    "objectPermissions": ("object", "object", {
        "allowRead": "read", "allowCreate": "create", "allowEdit": "edit", "allowDelete": "delete",
        "viewAllRecords": "view_all_records", "modifyAllRecords": "modify_all_records",
        "viewAllFields": "view_all_fields",
    }),
    "classAccesses": ("apexclass", "apexClass", {"enabled": "execute"}),
}
_OPTIONAL_FLAGS = frozenset({"readable", "viewAllFields"})
_BOOLEAN_VALUES = {"true": True, "1": True, "false": False, "0": False}


def _permission_documents(documents, store):
    for document in documents:
        path = PurePosixPath(document.path)
        for suffix, (folder, root_tag, kind) in _PERMISSION_DOCUMENTS.items():
            if not path.name.casefold().endswith(suffix):
                continue
            name = path.name[:-len(suffix)]
            declared = document.root.all("fullName")
            try:
                component_id(kind, name)
            except DataError:
                valid_name = False
            else:
                valid_name = True
            if (path.parent.name.casefold() != folder or document.root.tag != root_tag or not valid_name
                    or (declared and (len(declared) != 1 or declared[0].children
                                      or declared[0].text.casefold() != name.casefold()))):
                store.diagnostic(
                    "PERMISSION_IDENTITY_UNRESOLVED",
                    "Expected a matching permission metadata folder, filename, and XML root.",
                    document.location(document.root), state="unresolved",
                )
            else:
                yield document, kind, name
            break


def index_permissions(documents: list[Document], store: FactStore) -> None:
    """Index permission sources using filenames, which carry the metadata identity."""
    for document, kind, name in _permission_documents(documents, store):
        store.ensure_component(kind, name, document.location(document.root))


def _declared_target(document, row, target_kind, target_tag, store):
    targets = row.all(target_tag)
    if len(targets) == 1 and not targets[0].children:
        target = targets[0].text
        try:
            component_id(target_kind, target)
        except DataError:
            pass
        else:
            return target
    store.diagnostic(
        "PERMISSION_TARGET_UNRESOLVED",
        "A permission declaration needs one literal target API name.",
        document.location(targets[0] if targets else row), state="unresolved", construct=target_tag,
    )
    return None


def _permission_flags(document, row, flags, target_tag, store):
    for node in row.children:
        if node.tag != target_tag and node.tag not in flags:
            store.diagnostic(
                "PERMISSION_DECLARATION_UNSUPPORTED",
                "This permission declaration contains a flag outside extraction coverage.",
                document.location(node), construct=node.tag,
            )
    for tag, operation in flags.items():
        nodes = row.all(tag)
        if not nodes and tag in _OPTIONAL_FLAGS:
            continue
        if len(nodes) != 1 or nodes[0].children or nodes[0].text not in _BOOLEAN_VALUES:
            store.diagnostic(
                "PERMISSION_BOOLEAN_INVALID",
                "A permission flag needs one XML boolean value.",
                document.location(nodes[0] if nodes else row), state="unresolved", construct=tag,
            )
            continue
        yield operation, nodes[0], _BOOLEAN_VALUES[nodes[0].text]


def _extract_permission_rows(document, source, store):
    for row_tag, (target_kind, target_tag, flags) in _PERMISSION_ROWS.items():
        observations: dict[tuple[str, str], list[tuple[str, Node, bool]]] = defaultdict(list)
        for row in document.root.all(row_tag):
            target = _declared_target(document, row, target_kind, target_tag, store)
            if target is None:
                continue
            for operation, node, enabled in _permission_flags(document, row, flags, target_tag, store):
                observations[target.casefold(), operation].append((target, node, enabled))

        for (_, operation), declarations in observations.items():
            positive = [(target, node) for target, node, enabled in declarations if enabled]
            if not positive:
                continue
            target, first_node = positive[0]
            location = document.location(first_node)
            if len(positive) != len(declarations):
                store.diagnostic(
                    "PERMISSION_DECLARATION_CONFLICT",
                    "This permission source declares both true and false for the same target and operation.",
                    location, state="ambiguous", source_component=source,
                    declared_target=target, permission=operation,
                )
                continue
            # Preserve the Task or Event scope of a grant on a shared Activity field.
            target_id = store.resolve_component(target_kind, target, location)
            if target_id is None:
                continue
            for declared_target, node in positive:
                store.relation(
                    source, target_id, "grants", document.location(node), "permission.declaration",
                    resolution={"basis": "explicit_permission_boolean", "declared_target": declared_target},
                    qualifiers={"permission": operation},
                )


def extract_permissions(documents: list[Document], store: FactStore) -> None:
    """Emit explicit true flags from a unique local permission definition."""
    for document, kind, name in _permission_documents(documents, store):
        source = store.resolve_component(kind, name, document.location(document.root))
        if source is not None:
            _extract_permission_rows(document, source, store)
