"""Check the published synthetic project's impact paths against their source."""

import hashlib
from pathlib import Path
import shutil

from graphify.salesforce import query, snapshot
from graphify.salesforce.schema import canonical_json, component_id
from graphify.salesforce.service import build_project


EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "salesforce"


def test_example_impact_connects_trigger_and_outbound_message_with_evidence(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "snapshot"
    shutil.copytree(EXAMPLE, source)
    build_project(source, out=output)
    bundle = snapshot.load(output)

    field = component_id("field", "Account.Description")
    normalize = component_id("apexmethod", "SampleAction.normalize")
    entry = component_id("apexmethod", "SampleAction.beforeUpdate")
    optional_handler = component_id("apexmethod", "SampleAccountHandler.beforeUpdate")
    trigger = component_id("apextrigger", "SampleAccountTrigger")
    outbound = component_id("outboundmessage", "Account.SendSampleNotice")
    notify = component_id("flow", "SampleNotify")

    refs = query.refs(bundle, "Account.Description", source=source)
    assert refs["freshness"]["state"] == "current"
    assert {(row["source"], row["relation"]) for row in refs["results"]} == {
        (normalize, "writes"), (outbound, "reads"),
    }

    impact = query.impact(bundle, "Account.Description", depth=3)
    assert not impact["truncated"]
    rows = {row["source"]: row for row in impact["results"]}
    assert len(rows) == len(impact["results"])
    assert {
        source_id: (row["target"], row["relation"], row["depth"])
        for source_id, row in rows.items()
    } == {
        normalize: (field, "writes", 1),
        outbound: (field, "reads", 1),
        entry: (normalize, "calls", 2),
        optional_handler: (normalize, "calls", 2),
        notify: (outbound, "calls", 2),
        trigger: (entry, "calls", 3),
    }

    assert rows[normalize]["qualifiers"]["effect"] == "memory"
    assert rows[outbound]["qualifiers"]["operation"] == "outbound_payload"
    assert rows[trigger]["traversal"]["relationship_ids"] == [
        rows[trigger]["id"], rows[entry]["id"], rows[normalize]["id"],
    ]
    assert rows[notify]["traversal"]["relationship_ids"] == [
        rows[notify]["id"], rows[outbound]["id"],
    ]
    assert rows[optional_handler]["resolution"] == {
        "status": "candidate", "confidence": "AMBIGUOUS", "reason": "external_hierarchy",
        "missing_ancestors": ["SampleExternalBase"],
    }
    assert {
        source_id for source_id, row in rows.items()
        if row["traversal"]["includes_candidate_calls"]
    } == {optional_handler}
    diagnostic, = impact["diagnostic_context"]["diagnostics"]
    assert diagnostic["code"] == "APEX_CALL_CANDIDATE"
    assert diagnostic["candidates"] == [normalize]
    assert diagnostic["location"]["path"] == rows[optional_handler]["evidence"][0]["path"]

    expected_evidence = {
        normalize: ("classes/SampleAction.cls", "record.Description = record.Name;", "apex.direct_field"),
        outbound: ("workflows/Account.workflow-meta.xml", "<fields>Description</fields>", "workflow.outbound_payload_field"),
        entry: ("classes/SampleAction.cls", "normalize(records);", "apex.local_static_call"),
        optional_handler: ("classes/SampleAccountHandler.cls", "SampleAction.normalize(records);", "apex.candidate_static_call"),
        notify: ("flows/SampleNotify.flow-meta.xml", "<actionName>Account.SendSampleNotice</actionName>", "flow.literal_call"),
        trigger: ("triggers/SampleAccountTrigger.trigger", "SampleAction.beforeUpdate(Trigger.new);", "trigger.local_method_call"),
    }
    for source_id, (relative_path, line_text, rule) in expected_evidence.items():
        evidence, = rows[source_id]["evidence"]
        path = f"force-app/main/default/{relative_path}"
        content = (source / path).read_bytes()
        assert evidence["path"] == path
        assert content.decode("utf-8").splitlines()[evidence["line"] - 1].strip() == line_text
        assert evidence["content_hash"] == hashlib.sha256(content).hexdigest()
        assert evidence["rule"] == rule

    assert "https://example.invalid/salesforce/sample" not in canonical_json(bundle)
