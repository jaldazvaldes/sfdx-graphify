"""Provenance from an in-memory loop assignment to a record update attempt."""

from copy import deepcopy

import pytest

from graphify.salesforce.schema import DataError, SCHEMA_VERSION, stable_id, validate_bundle
from tests.salesforce.test_flow import analyze, variable


def loop_assignment(loop, body, source, accumulator, update, value):
    return f"""
      <loops><name>{loop}</name><collectionReference>{source}</collectionReference>
        <nextValueConnector><targetReference>{body}</targetReference></nextValueConnector>
        <noMoreValuesConnector><targetReference>{update}</targetReference></noMoreValuesConnector>
      </loops>
      <assignments><name>{body}</name>
        <assignmentItems><assignToReference>{loop}.Status</assignToReference><operator>Assign</operator>
          <value><stringValue>{value}</stringValue></value></assignmentItems>
        <assignmentItems><assignToReference>{accumulator}</assignToReference><operator>Add</operator>
          <value><elementReference>{loop}</elementReference></value></assignmentItems>
        <connector><targetReference>{loop}</targetReference></connector>
      </assignments>"""


FLOW = (variable("OriginalOrders", "Order", True) + variable("ReadyOrders", "Order", True)
        + variable("RejectedOrders", "Order", True) + variable("SpareOrders", "Order", True)
        + loop_assignment("ReadyLoop", "MarkReady", "OriginalOrders", "ReadyOrders", "SaveReady", "Ready")
        + loop_assignment("FaultLoop", "MarkRejected", "ReadyOrders", "RejectedOrders", "SaveRejected", "Rejected")
        + """
      <start><connector><targetReference>ReadyLoop</targetReference></connector></start>
      <recordUpdates><name>SaveReady</name><inputReference>ReadyOrders</inputReference>
        <faultConnector><targetReference>FaultLoop</targetReference></faultConnector>
      </recordUpdates>
      <recordUpdates><name>SaveRejected</name><inputReference>RejectedOrders</inputReference></recordUpdates>""")


def writes_by_value(body):
    facts, _ = analyze(body)
    edges = {edge["id"]: edge for edge in facts["relationships"]}
    return {item["resolution"]["assigned_value"]["value"]: item for item in facts["evidence"]
            if edges[item["relationship_id"]]["target"] == "sf:field:order.status"
            and edges[item["relationship_id"]]["qualifiers"]["effect"] == "memory"}


def test_loop_exit_update_keeps_success_and_fault_assignments_distinct():
    writes = writes_by_value(FLOW)
    assert set(writes) == {"Ready", "Rejected"}
    for value, update, collection in (("Ready", "SaveReady", "ReadyOrders"),
                                      ("Rejected", "SaveRejected", "RejectedOrders")):
        occurrence = writes[value]
        record_update = occurrence["resolution"]["record_update"]
        assert occurrence["rule"] == "flow.assignment_destination"
        assert record_update["element"] == update
        assert record_update["collection"] == collection
        assert record_update["site"]["path"] == occurrence["path"]
        assert record_update["site"]["locator"].endswith(f"recordUpdates[@name={update}]/inputReference")
        assert record_update["site"]["line"] > occurrence["line"]


def test_update_provenance_is_validated_without_changing_write_identity():
    facts, diagnostics = analyze(FLOW)
    evidence = next(row for row in facts["evidence"] if "record_update" in row["resolution"])
    bundle = {"manifest": {"schema_version": SCHEMA_VERSION,
                           "inputs": {evidence["path"]: evidence["content_hash"]}},
              "facts": facts, "diagnostics": diagnostics}

    def refingerprint(value):
        value["manifest"].pop("snapshot_id", None)
        value["manifest"]["snapshot_id"] = stable_id("snapshot", value)

    refingerprint(bundle)
    validate_bundle(bundle)
    invalid = deepcopy(bundle)
    invalid_evidence = next(row for row in invalid["facts"]["evidence"] if row["id"] == evidence["id"])
    invalid_evidence["resolution"]["record_update"]["site"]["content_hash"] = "0" * 64
    refingerprint(invalid)
    with pytest.raises(DataError, match="Provenance content hash differs"):
        validate_bundle(invalid)

    evidence["resolution"].pop("record_update")
    refingerprint(bundle)
    validate_bundle(bundle)


@pytest.mark.parametrize(("old", "new"), [
    ("<inputReference>ReadyOrders</inputReference>", "<inputReference>SpareOrders</inputReference>"),
    ("<targetReference>SaveReady</targetReference>", "<targetReference>SaveRejected</targetReference>"),
    ("<inputReference>ReadyOrders</inputReference>",
     "<inputReference>ReadyOrders</inputReference><inputReference>ReadyOrders</inputReference>"),
    ("<connector><targetReference>ReadyLoop</targetReference></connector>",
     "<connector><targetReference>SaveReady</targetReference></connector>"),
    ("<inputReference>ReadyOrders</inputReference>",
     "<inputReference>ReadyOrders</inputReference><object>Order</object>"),
    ("<stringValue>Ready</stringValue></value></assignmentItems>",
     "<stringValue>Ready</stringValue></value><futureModifier/></assignmentItems>"),
    ("<inputReference>ReadyOrders</inputReference>",
     "<inputReference>ReadyOrders</inputReference><futureInputMode/>"),
    ("<collectionReference>OriginalOrders</collectionReference>",
     "<collectionReference>OriginalOrders</collectionReference><futureIteratorMode/>"),
    ("<targetReference>SaveReady</targetReference></noMoreValuesConnector>",
     "<targetReference>SaveReady</targetReference><futureConnectorMode/></noMoreValuesConnector>"),
])
def test_unproven_update_keeps_only_the_existing_assignment_evidence(old, new):
    changed = FLOW.replace(old, new, 1)
    ready = writes_by_value(changed)["Ready"]
    assert "record_update" not in ready["resolution"]
