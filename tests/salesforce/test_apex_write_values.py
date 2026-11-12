"""Apex record write typing and literal evidence."""

from tests.salesforce.test_apex_review_fixes import build


def _writes(store, target):
    return [
        (edge, store.evidence[key])
        for edge in store.relationships.values()
        if edge["target"] == target and edge["relation"] == "writes"
        for key in edge["evidence_ids"]
    ]


def test_order_field_writes_cover_static_members_loops_and_locals():
    source = """public class Service {
      global static Order order;
      private Order instanceOrder;
      static void copy(List<Order> rows, String state) {
        order.Status = state;
        for (Order order : rows) { order.Status = 'Cancelled'; }
        Order copy = new Order();
        copy.Status = 'Ready';
        { Account order = new Account(); order.Status__c = 'Account only'; }
        // order.Status = 'Comment';
        /* order.Status = 'Block comment'; */
      }
      void copyInstance() { this.instanceOrder.Status = 'Draft'; }
    }"""
    store = build([("Service", source)])
    writes = {item["line"]: (edge, item) for edge, item in _writes(store, "sf:field:order.status")}
    assert set(writes) == {5, 6, 8, 13}
    assert all(edge["qualifiers"]["effect"] == "memory" for edge, _ in writes.values())
    assert writes[5][1]["resolution"]["basis"] == "declared_record_member"
    assert "assigned_value" not in writes[5][1]["resolution"]
    for line, value in ((6, "Cancelled"), (8, "Ready"), (13, "Draft")):
        assert writes[line][1]["resolution"]["assigned_value"] == {"type": "string", "value": value}
        assert writes[line][1]["resolution"]["assigned_value_location"]["line"] == line


def test_literals_are_per_write_site_and_preserve_the_effect():
    source = r"""public class Service {
      void save(Order row, String state) {
        row.Status = 'Ready';
        row.Status = ('Owner\'s choice');
        row.Status += 'Not the final value';
        row.Status = state;
        row.Status = 'Not' + 'a literal';
        row.Approved__c = TRUE;
        row.TotalAmount = -3.5;
        row.Count__c = 12L;
        row.Status = null;
        update new Order(Status = 'Saved');
      }
    }"""
    store = build([("Service", source)])
    fields = ("status", "approved__c", "totalamount", "count__c")
    occurrences = sorted(
        (item["line"], edge["qualifiers"]["effect"], item["resolution"].get("assigned_value"))
        for field in fields for edge, item in _writes(store, f"sf:field:order.{field}")
    )
    assert occurrences == [
        (3, "memory", {"type": "string", "value": "Ready"}),
        (4, "memory", {"type": "string", "value": "Owner's choice"}),
        (5, "memory", None),
        (6, "memory", None),
        (7, "memory", None),
        (8, "memory", {"type": "boolean", "value": True}),
        (9, "memory", {"type": "number", "value": -3.5}),
        (10, "memory", {"type": "number", "value": 12}),
        (11, "memory", {"type": "null", "value": None}),
        (12, "database", {"type": "string", "value": "Saved"}),
        (12, "memory", {"type": "string", "value": "Saved"}),
    ]


def test_commented_out_methods_do_not_add_record_writes():
    source = """public class Service {
      static void run(List<Order> rows) {
        for (Order row : rows) { row.Status = 'Ready'; }
      }
      /*
      static void legacy(List<Order> rows) {
        for (Order row : rows) { row.Status = 'Old'; }
        Order copy = new Order();
        copy.Status = System.Label.LegacyStatus;
      }
      */
    }"""
    store = build([("Service", source)])
    (_, write), = _writes(store, "sf:field:order.status")
    assert write["line"] == 3


def test_trigger_write_retains_a_literal_from_the_original_assignment():
    store = build(trigger="trigger Sample on Order (before insert) { Trigger.new[0].Status = 'Draft'; }")
    (edge, item), = _writes(store, "sf:field:order.status")
    assert edge["qualifiers"]["effect"] == "memory"
    assert item["resolution"]["assigned_value"] == {"type": "string", "value": "Draft"}
