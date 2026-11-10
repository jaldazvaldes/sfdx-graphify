"""Lexical Salesforce formula references, without evaluation or type guessing.

Only identifier paths outside strings/comments are returned. Function names,
the NOT keyword and boolean/null literals are ignored. Consumers resolve identifiers from
their own typed context; this scanner never assigns an object from a name.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .schema import API_NAME

_NAME = r"\$?[A-Za-z_][A-Za-z0-9_]*"
_IDENTIFIER = re.compile(_NAME)
_PATH = re.compile(_NAME + r"(?:\." + _NAME + r")*")
_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_NON_REFERENCES = frozenset({"true", "false", "null", "not"})
_GLOBAL_OBJECTS = {"$user": "User", "$profile": "Profile", "$organization": "Organization", "$userrole": "UserRole"}
# Computed context properties are not SObject fields despite their global prefix.
_GLOBAL_NON_FIELDS = {("$user", "uitheme"), ("$user", "uithemedisplayed"), ("$profile", "licensetype")}


@dataclass(frozen=True)
class FormulaReference:
    text: str
    offset: int


def formula_field_context(reference, owner, *, record_basis):
    """Bind a literal field path to its typed record or supported formula global."""
    parts = reference.split(".")
    if not reference.startswith("$"):
        return owner, reference, {"basis": record_basis, "object": owner}
    global_name = parts[0].casefold()
    if not all(API_NAME.fullmatch(part) for part in parts[1:]):
        return None
    if global_name in _GLOBAL_OBJECTS and len(parts) == 2:
        if (global_name, parts[1].casefold()) in _GLOBAL_NON_FIELDS:
            return None
        target = _GLOBAL_OBJECTS[global_name]
        return target, parts[1], {"basis": "explicit_global_object", "symbol": parts[0], "object": target}
    if global_name == "$setup" and len(parts) == 3:
        return parts[1], parts[2], {"basis": "explicit_setup_reference", "object": parts[1]}
    if global_name == "$custommetadata" and len(parts) == 4:
        return parts[1], parts[3], {"basis": "explicit_custom_metadata_reference", "object": parts[1], "record": parts[2]}
    if global_name == "$objecttype" and len(parts) == 4 and parts[2].casefold() == "fields":
        return parts[1], parts[3], {"basis": "explicit_object_type_field", "object": parts[1]}
    return None


def _quoted_end(expression, start):
    quote = expression[start]
    index = start + 1
    while index < len(expression):
        if expression[index] == "\\":
            index += 2
        elif expression[index] == quote:
            if index + 1 < len(expression) and expression[index + 1] == quote:
                index += 2
            else:
                return index + 1
        else:
            index += 1
    return len(expression)


def _chain_end(expression, start, end):
    """Consume a whole malformed/indexed path so its suffix cannot become a resource."""
    issue = None
    if start and (expression[start - 1].isalnum() or expression[start - 1] in "_.$]:"):
        issue = "invalid_token_boundary"
    while end < len(expression):
        cursor = end
        while cursor < len(expression) and expression[cursor].isspace():
            cursor += 1
        # Comments between path segments still belong to the same unsupported path.
        while expression.startswith("/*", cursor):
            close = expression.find("*/", cursor + 2)
            if close < 0:
                return len(expression), "unterminated_comment"
            cursor = close + 2
            while cursor < len(expression) and expression[cursor].isspace():
                cursor += 1
        if cursor < len(expression) and expression[cursor] == "[":
            issue = "indexed_reference"
            depth = 1
            cursor += 1
            while cursor < len(expression) and depth:
                if expression[cursor] in "\"'":
                    cursor = _quoted_end(expression, cursor)
                    continue
                if expression[cursor] == "[":
                    depth += 1
                elif expression[cursor] == "]":
                    depth -= 1
                cursor += 1
            end = cursor
        elif cursor < len(expression) and expression[cursor] == ":":
            # Salesforce's Owner:User.Name is one typed polymorphic path.
            # Its prefix and suffix are not independent field references.
            issue = issue or "polymorphic_reference"
            cursor += 1
            while cursor < len(expression) and expression[cursor].isspace():
                cursor += 1
            while expression.startswith("/*", cursor):
                close = expression.find("*/", cursor + 2)
                if close < 0:
                    return len(expression), "unterminated_comment"
                cursor = close + 2
                while cursor < len(expression) and expression[cursor].isspace():
                    cursor += 1
            segment = _PATH.match(expression, cursor)
            end = segment.end() if segment else cursor
        elif cursor < len(expression) and expression[cursor] == ".":
            issue = issue or "nonliteral_path"
            cursor += 1
            while cursor < len(expression) and (expression[cursor].isspace() or expression[cursor] == "."):
                cursor += 1
            segment = _IDENTIFIER.match(expression, cursor)
            end = segment.end() if segment else cursor
        elif cursor == end and cursor < len(expression) and expression[cursor] == "$":
            issue = "invalid_token_boundary"
            segment = _PATH.match(expression, cursor)
            end = segment.end() if segment else cursor + 1
        else:
            break
    return end, issue


def _next_code(expression, start):
    """Skip trivia before deciding whether an identifier names a function."""
    cursor = start
    while cursor < len(expression):
        if expression[cursor].isspace():
            cursor += 1
        elif expression.startswith("/*", cursor):
            end = expression.find("*/", cursor + 2)
            cursor = len(expression) if end < 0 else end + 2
        elif expression.startswith("//", cursor):
            end = expression.find("\n", cursor + 2)
            cursor = len(expression) if end < 0 else end + 1
        else:
            break
    return cursor


def formula_references(expression: str) -> tuple[list[FormulaReference], list[str]]:
    """Return literal candidate references and syntax limitations, in order."""
    result, issues = [], []
    i = 0
    while i < len(expression):
        char = expression[i]
        if char in "\"'":
            quote = char
            i += 1
            while i < len(expression):
                if expression[i] == "\\":
                    i += 2
                elif expression[i] == quote:
                    if i + 1 < len(expression) and expression[i + 1] == quote:
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            else:
                issues.append("unterminated_string")
            continue
        if expression.startswith("/*", i):
            end = expression.find("*/", i + 2)
            if end < 0:
                issues.append("unterminated_comment")
                break
            i = end + 2
            continue
        if expression.startswith("//", i):
            end = expression.find("\n", i + 2)
            i = len(expression) if end < 0 else end + 1
            continue
        if expression.startswith("{!", i):
            end = expression.find("}", i + 2)
            if end < 0:
                issues.append("unterminated_merge_reference")
                break
            text = expression[i + 2:end].strip()
            if _PATH.fullmatch(text):
                result.append(FormulaReference(text, i))
            else:
                issues.append("nonliteral_merge_reference")
            i = end + 1
            continue
        number = _NUMBER.match(expression, i)
        if number:
            i = number.end()
            continue
        token = _PATH.match(expression, i)
        if token:
            text = token.group()
            end, issue = _chain_end(expression, i, token.end())
            following = _next_code(expression, end)
            if issue:
                issues.append(issue)
            elif (expression[following:following + 1] != "("
                  and text.casefold() not in _NON_REFERENCES):
                result.append(FormulaReference(text, i))
            i = end
            continue
        i += 1
    return result, sorted(set(issues))
