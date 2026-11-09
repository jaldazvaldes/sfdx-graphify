"""Unsupported polymorphic formula paths cannot invent separate fields."""
from __future__ import annotations

import pytest

from graphify.salesforce.formula_refs import formula_references
from graphify.salesforce.inventory import index_definitions
from graphify.salesforce.schema import FactStore
from graphify.salesforce.validation_rules import extract_validation_rule
from graphify.salesforce.xml_source import parse_xml


@pytest.mark.parametrize("path", [
    "Owner:User.Email", "Owner : User.Email", "Owner: User.Email",
    "Owner:/* type */User.Email", "Owner/* type */:User.Email",
    "Owner:User.Manager:User.Email", "Owner:User . Email",
    "Owner::User.Email", "Owner:User.Email[0]", "What:Account.Name",
])
def test_polymorphic_path_stays_unsupported_as_a_whole(path):
    expression = path + " <> Email"
    references, issues = formula_references(expression)
    assert [reference.text for reference in references] == ["Email"]
    assert references[0].offset == len(path) + len(" <> ")
    assert issues


def test_polymorphic_text_in_string_or_comment_is_not_a_reference():
    references, issues = formula_references('"Owner:User.Email" /* What:Account.Name */ & Email')
    assert [reference.text for reference in references] == ["Email"]
    assert not issues


def test_validation_rule_reports_typed_owner_path_without_phantom_owner_field():
    document = parse_xml(
        "force-app/main/default/objects/Case/validationRules/OwnerCheck.validationRule-meta.xml",
        b"<ValidationRule><fullName>OwnerCheck</fullName><active>true</active>"
        b"<errorConditionFormula>Owner:User.Email != SuppliedEmail</errorConditionFormula>"
        b"<errorMessage>Owner mismatch</errorMessage></ValidationRule>",
    )
    store = FactStore()
    index_definitions([document], store)
    extract_validation_rule(document, store)
    assert {component["api_name"] for component in store.components.values()
            if component["kind"] == "field"} == {"Case.SuppliedEmail"}
    assert any(diagnostic["code"] == "VALIDATION_RULE_FORMULA_SYNTAX_UNSUPPORTED"
               and diagnostic["issue"] == "polymorphic_reference"
               for diagnostic in store.diagnostics)
