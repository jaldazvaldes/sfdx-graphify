"""Literal NamedCredential references without retaining endpoint values.

Only lexically declared HttpRequest parameters and locals establish receivers.
Class members, expression receivers, dynamic endpoints and incomplete type
hierarchies remain unresolved. A reference is not proof that a request is sent.
"""
from __future__ import annotations

from pathlib import PurePosixPath
import re

from .apex import ApexIndex, _MethodExtractor, _significant, _walk
from .schema import API_NAME, FactStore
from .xml_source import Document


_SUFFIX = ".namedCredential-meta.xml"
_ENDPOINT = re.compile(r"'callout:([A-Za-z_][A-Za-z0-9_]*)(?:/[^'\\\r\n]*)?'")


def index_named_credentials(documents: list[Document], store: FactStore) -> None:
    """Index filename/root identities; never copy credential configuration."""
    for document in documents:
        path = PurePosixPath(document.path)
        if not path.name.casefold().endswith(_SUFFIX.casefold()):
            continue
        name = path.name[:-len(_SUFFIX)]
        declared = document.root.all("fullName")
        if (path.parent.name != "namedCredentials" or document.root.tag != "NamedCredential"
                or not API_NAME.fullmatch(name) or len(declared) > 1
                or (declared and (declared[0].children or declared[0].text.casefold() != name.casefold()))):
            store.diagnostic("NAMED_CREDENTIAL_IDENTITY_UNRESOLVED",
                             "A NamedCredential needs a literal filename and matching metadata root/name.",
                             document.location(document.root), state="unresolved")
            continue
        cid = store.ensure_component("namedcredential", name, document.location(document.root))
        if store.is_ambiguous(cid):
            store.diagnostic("duplicate_definition", "Multiple local definitions name this NamedCredential.",
                             document.location(document.root), state="ambiguous", candidates=[cid])


class _RequestBindings(_MethodExtractor):
    """Reuse Apex's lexical scope/shadow handling with a narrower type proof."""

    def diagnostic(self, *args, **kwargs):
        # This pass only collects bindings. Existing Apex extraction owns the
        # general diagnostics for unsupported loops and record expressions.
        pass

    def record_type(self, node):
        if node is None or node.type not in {"type_identifier", "scoped_type_identifier"}:
            return None, False
        name = re.sub(r"\s+", "", re.sub(r"/\*[\s\S]*?\*/|//[^\r\n]*", "", self.unit.text(node))).casefold()
        if name not in {"httprequest", "system.httprequest"} or self.qualifier_hierarchy is None:
            return None, False
        qualifier = name.split(".")[0]
        if (qualifier in self.index.apex_types
                or any(qualifier in unit.declared_types or qualifier in unit.declared_members
                       for unit in self.qualifier_hierarchy)):
            return None, False
        return "HttpRequest", False


def extract_named_credential_endpoints(index: ApexIndex, store: FactStore) -> None:
    """Add method→credential references for a single literal callout endpoint."""
    # Most units contain no HTTP endpoint calls. Avoid a second lexical-binding
    # pass over their methods; this filter is only an optimization, never proof.
    eligible_paths = {unit.path for unit in index.units if b"setendpoint" in unit.data.lower()}
    for collection in (index.methods, index.constructors):
        for class_id, methods in collection.items():
            if store.is_ambiguous(class_id):
                continue
            for method in methods:
                if (method.unit.path not in eligible_paths or method.unit.parse_error
                        or store.is_ambiguous(method.id)):
                    continue
                visitor = _RequestBindings(method, index, store)
                if visitor.body is None:
                    continue
                if b"setendpoint" not in method.unit.data[visitor.body.start_byte:visitor.body.end_byte].lower():
                    continue
                visitor.index_bindings()
                for node in _walk(visitor.body):
                    if node.type != "method_invocation":
                        continue
                    method_name = node.child_by_field_name("name")
                    if method.unit.text(method_name).casefold() != "setendpoint":
                        continue
                    receiver = node.child_by_field_name("object")
                    arguments = _significant(node.child_by_field_name("arguments"))
                    literal = (method.unit.text(arguments[0]) if len(arguments) == 1
                               and arguments[0].type == "string_literal" else "")
                    match = _ENDPOINT.fullmatch(literal)
                    binding = (visitor.binding(method.unit.text(receiver), receiver, report=False)
                               if receiver is not None and receiver.type == "identifier" else None)
                    proven = (binding is not None and binding.owner == "HttpRequest"
                              and not visitor._initializing_binding(method.unit.text(receiver), receiver))
                    if not match or not proven:
                        if (match is not None or (proven and not literal)):
                            store.diagnostic("APEX_NAMED_CREDENTIAL_UNRESOLVED",
                                             "A literal callout endpoint on an unambiguous HttpRequest local or parameter is required.",
                                             method.unit.location(node), state="unresolved")
                        continue
                    name = match.group(1)
                    location = method.unit.location(node)
                    target = store.resolve_component("namedcredential", name, location)
                    if target is not None:
                        store.relation(method.id, target, "references", location,
                                       "apex.named_credential_endpoint",
                                       resolution={"basis": "literal_endpoint_on_typed_http_request", "name": name},
                                       qualifiers={"context": "named_credential_endpoint"})
