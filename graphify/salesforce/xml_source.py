"""Bounded, location-aware XML parsing with DTD/entity expansion forbidden."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import quote
from xml.parsers import expat

from .schema import DataError

METADATA_NS = "http://soap.sforce.com/2006/04/metadata"
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

    def start(name, attributes):
        nonlocal count
        count += 1
        if count > 100_000 or len(stack) >= 128:
            raise DataError(f"XML structural limit exceeded: {path}")
        if "|" in name:
            namespace, local = name.rsplit("|", 1)
            name = local if namespace == METADATA_NS else "{" + namespace + "}" + local
        node = Node(name, line=parser.CurrentLineNumber, column=parser.CurrentColumnNumber + 1)
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)

    def end(name):
        node = stack.pop()
        node.text = node.text.strip()
        # Expat reports the start of the closing tag; keep end precision absent.
        node.end_line = None
        node.end_column = None

    def text(value):
        if stack:
            stack[-1].text += value

    def forbidden(*args):
        raise DataError(f"DTD and entity declarations are forbidden: {path}")

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
