"""Literal values at direct XML write sites."""

from __future__ import annotations

import math
import re


_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?\Z")
_INTEGER = re.compile(r"[+-]?[0-9]+\Z")
_BOOLEANS = {"true": True, "1": True, "false": False, "0": False}


def _literal(document, node, kind):
    if node is None or node.children:
        return {}
    text = node.lexical_text if node.lexical_text is not None else node.text
    if len(text) > 1024:
        return {}
    if kind == "boolean" and node.text in _BOOLEANS:
        value = _BOOLEANS[node.text]
    elif kind == "number" and _NUMBER.fullmatch(node.text):
        value = int(node.text) if _INTEGER.fullmatch(node.text) else float(node.text)
        if isinstance(value, float) and not math.isfinite(value):
            return {}
    elif kind == "string" and "{!" not in text:
        value = text
    elif kind == "null":
        value = None
    else:
        return {}
    return {"assigned_value": {"type": kind, "value": value},
            "assigned_value_location": document.location(node)}


def flow_write_value(document, container):
    if container is None or len(container.children) != 1:
        return {}
    node = container.children[0]
    kinds = {"stringValue": "string", "booleanValue": "boolean", "numberValue": "number",
             "dateValue": "string", "dateTimeValue": "string"}
    return _literal(document, node, kinds.get(node.tag))


def workflow_write_value(document, definition, operation, field_type):
    nodes = definition.all("literalValue")
    if operation.text == "Null" and not nodes:
        return _literal(document, operation, "null")
    if operation.text != "Literal" or len(nodes) != 1:
        return {}
    data_type = (field_type or "").casefold()
    kind = "boolean" if data_type == "checkbox" else (
        "number" if data_type in {"number", "currency", "percent"} else "string")
    return _literal(document, nodes[0], kind)
