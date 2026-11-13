"""Resolve bounded Apex constants without changing literal write queries."""

from graphify.salesforce import query
from tests.salesforce.test_apex_review_fixes import build
from tests.salesforce.test_apex_write_values import _writes


def test_same_class_string_constant_proves_the_exact_value_and_its_declaration_site():
    source = """public class Writer {
      static final String READY = ('Ready'), ALTERNATE = 'Alternate';
      static void write(Order row, Id orderId) {
        row.Status = READY;
        update new Order(Id = orderId, Status = Writer.ALTERNATE);
        row.Status = 'Other';
        row.Status = 'Ready';
      }
    }"""
    store = build([("Writer", source)])
    bundle = {"manifest": {"snapshot_id": "synthetic"}, "facts": store.finish(), "diagnostics": store.diagnostics}
    result = query.refs(bundle, "Order.Status", value="Ready")
    assert result["value_filter"] == {"literal": "Ready", "occurrence_count": 2, "unknown_occurrence_count": 0}
    hits = {item["line"]: item["resolution"]["assigned_value_location"]
            for edge in result["results"] for item in edge["evidence"]}
    assert {line: location["line"] for line, location in hits.items()} == {4: 2, 7: 7}
    assert hits[4]["locator"].endswith(
        "variable_declarator[@name=ready]/parenthesized_expression/string_literal"
    )
    other = query.refs(bundle, "Order.Status", value="Alternate")
    assert other["value_filter"]["occurrence_count"] == 2
    assert {edge["qualifiers"]["effect"] for edge in other["results"]} == {"memory", "database"}
    assert query.refs(bundle, "Order.Status", value="ready")["results"] == []


def test_unsupported_values_and_lexical_shadowing_preserve_unvalued_writes():
    source = """public class Writer {
      static final String STATE = 'Ready';
      static String MUTABLE = 'Ready';
      static final String LABEL_VALUE = System.Label.Ready;
      static final String EXPRESSION = 'Re' + 'ady';
      static final String DUPLICATE = 'Ready', duplicate = 'Other';
      String instanceState = 'Ready';
      static void write(Order row, String STATE, Account Writer) {
        row.Status = STATE;
        row.Status = Writer.STATE;
        row.Status = MUTABLE;
        row.Status = LABEL_VALUE;
        row.Status = EXPRESSION;
        row.Status = DUPLICATE;
        row.Status = External.STATE;
        row.Status = System.Label.Ready;
      }
      void local(Order row) {
        row.Status = instanceState;
        { String state = 'Shadow'; row.Status = STATE; }
        row.Status = STATE;
        String STATE = (row.Status = STATE);
        row.Status = STATE;
      }
    }"""
    store = build([("Writer", source)])
    writes = {item["line"]: item["resolution"].get("assigned_value")
              for _, item in _writes(store, "sf:field:order.status")}
    assert writes == dict.fromkeys((*range(9, 17), 19, 20, 22, 23)) | {
        21: {"type": "string", "value": "Ready"}
    }
