"""Source evidence for record properties used as Apex field receivers."""

from graphify.salesforce import query
from graphify.salesforce.schema import component_id
from tests.salesforce.test_apex_review_fixes import build


def test_constructor_writes_through_automatic_record_property_getters():
    source = """public class DraftController {
        public Order draft { get; set; }
        public Order saved { private get; set; }
        public DraftController(ApexPages.StandardController controller) {
            draft = new Order();
            draft.Status = 'Draft';
            this.saved = (Order)controller.getRecord();
            this.saved.Status = 'Pending';
        }
    }"""
    store = build([("DraftController", source)])
    bundle = {"manifest": {"snapshot_id": "synthetic"}, "facts": store.finish(), "diagnostics": store.diagnostics}
    constructor = component_id("apexmethod", "DraftController.new(ApexPages.StandardController)")
    result = query.refs(bundle, "Order.Status", relation="writes")["results"]
    write, = result
    assert write["source"] == constructor
    assert write["qualifiers"] == {"effect": "memory", "operation": "assignment"}
    assert {(item["line"], item["rule"]) for item in write["evidence"]} == {
        (6, "apex.member_record_field"),
        (8, "apex.member_record_field"),
    }
    for item in write["evidence"]:
        assert item["path"] == "classes/DraftController.cls"
        assert item["resolution"]["basis"] == "declared_record_member"
        assert item["resolution"]["declaration"]["path"] == item["path"]


def test_record_properties_respect_getter_shape_and_receiver_scope():
    source = """public class DraftController {
        public Order draft { get; set; }
        public Order computed { get { return draft; } set; }
        public Order writeOnly { set; }
        public void change(Contact draft) {
            draft.Name = 'Contact';
            this.draft.Status = 'Draft';
            computed.Status = 'Unsupported';
            writeOnly.Status = 'Unsupported';
        }
        public static void changeStatic() {
            draft.Status = 'Unsupported';
        }
    }"""
    store = build([("DraftController", source)])
    bundle = {"manifest": {"snapshot_id": "synthetic"}, "facts": store.finish(), "diagnostics": store.diagnostics}
    writes, = query.refs(bundle, "Order.Status", relation="writes")["results"]
    assert [item["line"] for item in writes["evidence"]] == [7]
    contact, = query.refs(bundle, "Contact.Name", relation="writes")["results"]
    assert [item["line"] for item in contact["evidence"]] == [6]
