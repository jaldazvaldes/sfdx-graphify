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


def test_typed_wrapper_record_reads_and_writes_keep_the_member_declaration():
    wrapper = """public class Packet {
        public Account record { get; private set; }
        public Account plain;
    }"""
    source = """public class Writer {
        public static void change(List<Packet> items, Packet argument) {
            for (Packet item : items) {
                item.record.Description = item.record.Description + ' revised';
            }
            Packet local = argument;
            local.plain.Description = 'Ready';
        }
    }"""
    store = build([("Packet", wrapper), ("Writer", source)])
    bundle = {"manifest": {"snapshot_id": "synthetic"}, "facts": store.finish(), "diagnostics": store.diagnostics}
    writes, = query.refs(bundle, "Account.Description", relation="writes")["results"]
    read, = query.refs(bundle, "Account.Description", relation="reads")["results"]
    assert {item["line"] for item in writes["evidence"]} == {4, 7}
    assert {item["line"] for item in read["evidence"]} == {4}
    assert writes["qualifiers"] == {"effect": "memory", "operation": "assignment"}
    for item in [*writes["evidence"], *read["evidence"]]:
        assert item["rule"] == "apex.wrapper_record_field"
        proof = item["resolution"]
        assert proof["basis"] == "declared_wrapper_record_member"
        assert proof["declaration"]["path"] == "classes/Writer.cls"
        assert proof["member_declaration"]["path"] == "classes/Packet.cls"
        assert proof["member_declaration"]["line"] == (3 if item["line"] == 7 else 2)
    literal, = query.refs(bundle, "Account.Description", relation="writes", value="Ready")["results"]
    assert [item["line"] for item in literal["evidence"]] == [7]
    assert "APEX_LOOP_ALIAS_UNSUPPORTED" not in {item["code"] for item in store.diagnostics}


def test_wrapper_chain_rejects_unproven_or_inaccessible_members():
    wrapper = """public class Packet {
        public Account readOnly { get; }
        public Account computed { get { return null; } set; }
        public Account inaccessible { private get; set; }
        public static Account staticRecord;
        public SObject dynamicRecord;
        public List<Account> collection;
        public Account duplicate;
        public Account duplicate;
    }"""
    source = """public class Writer {
        public void change(Packet item, Object unknown, Vendor.Packet remote) {
            item.readOnly.Description = 'No';
            item.computed.Description = 'No';
            item.inaccessible.Description = 'No';
            item.staticRecord.Description = 'No';
            item.dynamicRecord.Description = 'No';
            item.collection.Description = 'No';
            item.duplicate.Description = 'No';
            unknown.computed.Description = 'No';
            remote.computed.Description = 'No';
        }
    }"""
    store = build([("Packet", wrapper), ("Writer", source)])
    assert not any(item["rule"] == "apex.wrapper_record_field" for item in store.evidence.values())
