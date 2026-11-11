"""Bounded, location-aware XML parsing with DTD/entity expansion forbidden."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import quote
from xml.parsers import expat

from .schema import DataError

METADATA_NS = "http://soap.sforce.com/2006/04/metadata"
XML_SCHEMA_INSTANCE_NS = "http://www.w3.org/2001/XMLSchema-instance"
MAX_XML_BYTES = 8 * 1024 * 1024


@dataclass
class Node:
    tag: str
    text: str = ""
    children: list[Node] = field(default_factory=list)
    line: int = 1
    column: int = 1
    end_line: int | None = None
    end_column: int | None = None
    locator: str = ""
    lexical_text: str | None = None
    attributes: dict[str, str] | None = None
    xsi_type_namespace: str | None = None

    def child(self, tag):
        return next((child for child in self.children if child.tag == tag), None)

    def all(self, tag):
        return [child for child in self.children if child.tag == tag]

    def value(self, tag, default=""):
        child = self.child(tag)
        return child.text if child is not None else default


@dataclass
class Document:
    root: Node
    path: str
    content_hash: str

    def location(self, node):
        return {"path": self.path, "locator": node.locator, "line": node.line,
                "column": node.column, "end_line": node.end_line, "end_column": node.end_column,
                "content_hash": self.content_hash}


def _locators(node, path):
    node.locator = path
    keys = []
    for child in node.children:
        name = child.value("name") or child.value("fullName")
        keys.append(child.tag + (f"[@name={quote(name, safe='._')}]" if name else ""))
    totals, seen = Counter(keys), Counter()
    for child, key in zip(node.children, keys):
        seen[key] += 1
        suffix = f"[{seen[key]}]" if totals[key] > 1 else ""
        _locators(child, f"{path}/{key}{suffix}")


def parse_xml(path: str, data: bytes) -> Document:
    if len(data) > MAX_XML_BYTES:
        raise DataError(f"XML exceeds the {MAX_XML_BYTES}-byte limit: {path}")
    parser = expat.ParserCreate(namespace_separator="|")
    stack, roots = [], []
    count = 0
    capture_values = path.casefold().endswith(".md-meta.xml")
    namespaces: dict[str | None, list[str | None]] = {}

    def start_namespace(prefix, uri):
        namespaces.setdefault(prefix, []).append(uri)

    def end_namespace(prefix):
        namespaces[prefix].pop()
        if not namespaces[prefix]:
            del namespaces[prefix]

    def start(name, attributes):
        nonlocal count
        count += 1
        if count > 100_000 or len(stack) >= 128:
            raise DataError(f"XML structural limit exceeded: {path}")
        if "|" in name:
            namespace, local = name.rsplit("|", 1)
            name = local if namespace == METADATA_NS else "{" + namespace + "}" + local
        node = Node(name, line=parser.CurrentLineNumber, column=parser.CurrentColumnNumber + 1)
        if capture_values and name == "value":
            node.attributes = attributes or None
            lexical_type = attributes.get(f"{XML_SCHEMA_INSTANCE_NS}|type")
            if lexical_type is not None:
                prefix, separator, _ = lexical_type.strip().partition(":")
                binding = namespaces.get(prefix if separator else None)
                if binding:
                    node.xsi_type_namespace = binding[-1]
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)

    def end(name):
        node = stack.pop()
        if capture_values and node.tag == "value":
            node.lexical_text = node.text
        node.text = node.text.strip()
        # Expat reports the start of the closing tag; keep end precision absent.
        node.end_line = None
        node.end_column = None

    def text(value):
        if stack:
            stack[-1].text += value

    def forbidden(*args):
        raise DataError(f"DTD and entity declarations are forbidden: {path}")

    if capture_values:
        parser.StartNamespaceDeclHandler = start_namespace
        parser.EndNamespaceDeclHandler = end_namespace
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise DataError(f"Malformed XML at {path}:{exc.lineno}:{exc.offset + 1}: {exc}") from exc
    if len(roots) != 1:
        raise DataError(f"Expected one XML root: {path}")
    _locators(roots[0], "/" + roots[0].tag)
    return Document(roots[0], path, hashlib.sha256(data).hexdigest())
