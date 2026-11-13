"""Regression coverage for class references and assigned-literal query evidence."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from graphify.salesforce import cli, query, session, snapshot
from graphify.salesforce.schema import FactStore, SCHEMA_VERSION


def make_bundle(store):
    return {"manifest": {"schema_version": SCHEMA_VERSION, "snapshot_id": "snapshot:synthetic"},
            "facts": store.finish(), "diagnostics": []}


def relation(store, source, target, kind, line, *, assigned_value=None, qualifiers=None):
    proof = {"basis": "explicit_syntax"}
    if assigned_value is not None:
        proof["assigned_value"] = assigned_value
    return store.relation(source, target, kind, {
        "path": "classes/Sample.cls", "locator": f"/source[{line}]", "line": line,
        "column": 1, "content_hash": "0" * 64,
    }, "synthetic.reference", resolution=proof, qualifiers=qualifiers)


def test_class_references_include_declared_methods_beside_class_edges():
    store = FactStore()
    controller = store.ensure_component("apexclass", "Controller")
    fetch = store.ensure_component("apexmethod", "Controller.fetch")
    idle = store.ensure_component("apexmethod", "Controller.idle")
    unowned = store.ensure_component("apexmethod", "Controller.unowned")
    consumer = store.ensure_component("lwc", "Consumer")
    tests = store.ensure_component("apexmethod", "ControllerTest.testFetch", is_test=True)
    top = store.ensure_component("flow", "Host")
    relation(store, controller, fetch, "contains", 1)
    relation(store, controller, idle, "contains", 2)
    member = relation(store, consumer, fetch, "calls", 3)
    direct = relation(store, consumer, controller, "references", 4)
    test_use = relation(store, tests, fetch, "calls", 5)
    outer = relation(store, top, consumer, "references", 6)
    relation(store, consumer, unowned, "calls", 7)
    bundle = make_bundle(store)

    result = query.refs(bundle, controller, limit=1)
    assert result["total"] == 3 and result["truncated"]
    assert result["class_references"] == {"method_scope": "directly_owned", "method_count": 2,
                                          "referenced_method_count": 1, "method_relationship_count": 2,
                                          "class_relationship_count": 1}
    assert {row["id"] for row in query.refs(bundle, "Controller")["results"]} == {member, direct, test_use}
    assert query.refs(bundle, "Controller", relation="contains")["results"] == []
    assert [row["id"] for row in query.refs(bundle, controller, relation="calls", exclude_tests=True)["results"]] == [member]
    assert "2 across 1 of 2 directly owned methods" in cli._text(result, brief=True)

    impact = query.impact(bundle, controller, depth=2, exclude_tests=True)
    assert {row["id"]: row["depth"] for row in impact["results"]} == {member: 1, direct: 1, outer: 2}
    witness = next(row for row in impact["results"] if row["id"] == outer)
    assert witness["traversal"]["relationship_ids"] == [outer, member]


def test_impact_distinguishes_candidate_publication_from_candidate_call():
    store = FactStore()
    method = store.ensure_component("apexmethod", "Publisher.send")
    event = store.ensure_component("object", "Notice__e")
    candidate = relation(store, method, event, "publishes", 1, qualifiers={
        "resolution_status": "candidate", "uncertainty": "external_hierarchy",
        "missing_ancestors": ["External"],
    })
    result = query.impact(make_bundle(store), event)
    witness = result["results"][0]["traversal"]
    assert witness["candidate_relationship_ids"] == [candidate]
    assert witness["includes_candidate_relationships"] and not witness["includes_candidate_calls"]
    assert "Witness path includes candidate relationships" in cli._text(result, brief=True)


def test_automation_rows_show_phase_status_event_and_order_in_brief_output():
    store = FactStore()
    flow = store.ensure_component("flow", "Account_Before")
    account = store.ensure_component("object", "Account")
    relation(store, flow, account, "triggered_by", 1, qualifiers={
        "timing": "before_save", "local_status": "Active", "record_trigger_type": "CreateAndUpdate",
        "trigger_order": 25,
    })
    result = query.refs(make_bundle(store), account, relation="triggered_by")
    assert "before-save, Active, CreateAndUpdate, order 25" in cli._text(result, brief=True)


@pytest.fixture
def assignments():
    store = FactStore()
    field = store.ensure_component("field", "Order.Status")
    setter = store.ensure_component("apexmethod", "Controller.save")
    for line, assigned in enumerate([
        {"type": "string", "value": "Ready"}, {"type": "string", "value": "ready"},
        {"type": "boolean", "value": True}, {"type": "number", "value": 7},
        {"type": "null", "value": None}, {"type": "string", "value": ""}, None,
    ], 1):
        relation(store, setter, field, "writes", line, assigned_value=assigned)
    relation(store, setter, field, "reads", 8)
    return make_bundle(store)


@pytest.mark.parametrize(("literal", "line"), [
    ("Ready", 1), ("ready", 2), ("true", 3), ("7", 4), ("null", 5), ("", 6),
])
def test_filter_returns_only_matching_write_evidence(assignments, literal, line):
    original = deepcopy(assignments)
    result = query.refs(assignments, "Order.Status", value=literal)
    write, = result["results"]
    occurrence, = write["evidence"]
    assert occurrence["line"] == line
    assert write["evidence_ids"] == [occurrence["id"]]
    assert result["value_filter"] == {"literal": literal, "occurrence_count": 1, "unknown_occurrence_count": 1}
    assert len(query.explain(assignments, write["id"])["results"][0]["evidence"]) == 7
    assert assignments == original


def test_literal_filter_has_cli_and_session_parity(assignments, monkeypatch, capsys):
    monkeypatch.setattr(snapshot, "load", lambda path: assignments)
    assert cli.main(["refs", "Order.Status", "--snapshot", "sample", "--value", "Ready"]) == 0
    cli_result = json.loads(capsys.readouterr().out)
    request = {"command": "refs", "name": "Order.Status", "value": "Ready"}
    assert session.answer_request(assignments, request)["result"] == cli_result
    command = ["refs", "Order.Status", "--snapshot", "sample", "--value", "Ready"]
    for output_format in ("brief", "evidence", "text"):
        assert cli.main([*command, "--format", output_format]) == 0
        output = capsys.readouterr().out
        assert "1 indexed write occurrence has an unknown assigned value" in output
    assert 'Sample.cls:1 [assigned "Ready"]' in cli._text(cli_result, brief=True)
    assert query.refs(assignments, "Order.Status", value="1")["total"] == 0
    with pytest.raises(query.QueryError, match="requires writes"):
        query.refs(assignments, "Order.Status", value="Ready", relation="reads")
    assert session.answer_request(assignments, {**request, "value": True})["exit_code"] == 2


def test_value_filter_counts_unknown_writes_before_limiting_relationships():
    store = FactStore()
    field = store.ensure_component("field", "Order.Status")
    first = store.ensure_component("apexmethod", "First.save")
    second = store.ensure_component("flow", "Second")
    test = store.ensure_component("apexmethod", "WriterTest.testSave", is_test=True)
    value = {"type": "string", "value": "Ready"}
    relation(store, first, field, "writes", 1, assigned_value=value)
    relation(store, second, field, "writes", 2, assigned_value=value)
    relation(store, second, field, "writes", 3)
    relation(store, test, field, "writes", 4)
    bundle = make_bundle(store)

    result = query.refs(bundle, field, value="Ready", exclude_tests=True, limit=1)
    assert result["truncated"]
    assert [site["line"] for site in result["results"][0]["evidence"]] == [1]
    assert result["value_filter"] == {"literal": "Ready", "occurrence_count": 2, "unknown_occurrence_count": 1}
    all_writes = query.refs(bundle, field, value="missing", limit=1)
    assert all_writes["results"] == []
    assert all_writes["value_filter"]["unknown_occurrence_count"] == 2


def test_value_filter_keeps_rebuild_guidance_for_older_snapshot(assignments):
    assignments["manifest"]["schema_version"] = 12
    for occurrence in assignments["facts"]["evidence"]:
        occurrence["resolution"].pop("assigned_value", None)
    result = query.refs(assignments, "Order.Status", value="Ready")
    assert result["value_filter"] == {"literal": "Ready", "occurrence_count": 0}
    assert "Rebuild to use this filter." in cli._text(result, brief=True)
