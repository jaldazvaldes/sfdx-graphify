"""Candidate calls remain explicit across every consumer-facing query surface."""
import copy

import pytest

from graphify.salesforce import cli, query
from graphify.salesforce.schema import (
    DataError, FactStore, SCHEMA_VERSION, stable_id, validate_bundle,
)
from graphify.salesforce.session import answer_request


@pytest.fixture
def candidate_bundle():
    store = FactStore()
    ids = [store.ensure_component(kind, name) for kind, name in [
        ("field", "Account.Name"), ("apexmethod", "Helper.normalize"),
        ("apexmethod", "Handler.beforeInsert"), ("apextrigger", "Example"),
    ]]
    for i, (source, target, relation) in enumerate([
        (ids[1], ids[0], "writes"), (ids[2], ids[1], "calls"), (ids[3], ids[2], "calls"),
    ]):
        candidate = i == 1
        qualifiers = ({"resolution_status": "candidate", "uncertainty": "external_hierarchy",
                       "missing_ancestors": ["VendorBase"], "invocation": "static_method"}
                      if candidate else {})
        store.relation(source, target, relation,
                       {"path": "classes/Example.cls", "locator": f"node:{i}",
                        "line": i + 1, "content_hash": "a" * 64},
                       "apex.candidate_static_call" if candidate else "synthetic",
                       resolution={"basis": "local_static_target_unverified_hierarchy",
                                   "missing_ancestors": ["VendorBase"]} if candidate else None,
                       qualifiers=qualifiers)
    bundle = {"manifest": {"schema_version": SCHEMA_VERSION,
                           "inputs": {"classes/Example.cls": "a" * 64}},
              "facts": store.finish(), "diagnostics": []}
    _stamp(bundle)
    return bundle


def _stamp(bundle):
    bundle["manifest"].pop("snapshot_id", None)
    bundle["manifest"]["snapshot_id"] = stable_id("snapshot", bundle)


def test_refs_deps_explain_preserve_candidate_and_evidence(candidate_bundle):
    before = copy.deepcopy(candidate_bundle)
    outgoing = query.deps(candidate_bundle, "Handler.beforeInsert")
    incoming = query.refs(candidate_bundle, "Helper.normalize")
    row, = outgoing["results"]
    assert row == incoming["results"][0]
    assert row == query.explain(candidate_bundle, row["id"])["results"][0]
    assert row["resolution"] == {
        "status": "candidate", "confidence": "AMBIGUOUS", "reason": "external_hierarchy",
        "missing_ancestors": ["VendorBase"],
    }
    assert row["evidence"][0]["line"] == 2
    assert outgoing["resolution_policy"]["candidate_calls_included"]
    assert candidate_bundle == before


def test_impact_marks_candidate_in_later_extracted_hop(candidate_bundle):
    result = query.impact(candidate_bundle, "Account.Name", depth=3)
    assert not result["truncated"]
    by_depth = {row["depth"]: row for row in result["results"]}
    first, second, third = [by_depth[i] for i in (1, 2, 3)]
    assert first["resolution"]["status"] == third["resolution"]["status"] == "extracted"
    assert not first["traversal"]["includes_candidate_calls"]
    assert second["resolution"]["status"] == "candidate"
    assert third["traversal"] == {
        "relationship_ids": [third["id"], second["id"], first["id"]],
        "candidate_relationship_ids": [second["id"]], "includes_candidate_calls": True,
    }
    assert "not all possible paths" in result["traversal_policy"]
    limited = query.impact(candidate_bundle, "Account.Name", depth=2)
    assert limited["depth_limited"]
    assert len(limited["results"]) == 2


def test_text_and_jsonl_expose_candidate_resolution(candidate_bundle):
    response = answer_request(candidate_bundle, {
        "id": "request", "command": "impact", "name": "Account.Name", "depth": 3,
    })
    assert response["exit_code"] == 0
    text = cli._text(response["result"])
    assert "CANDIDATE: external hierarchy not verified; missing ancestors: VendorBase" in text
    assert "Witness path includes candidate calls:" in text
    assert any(row["traversal"]["includes_candidate_calls"]
               for row in response["result"]["results"])


def test_candidate_snapshot_requires_schema_6(candidate_bundle):
    validate_bundle(candidate_bundle)
    candidate_bundle["manifest"]["schema_version"] = 6
    _stamp(candidate_bundle)
    validate_bundle(candidate_bundle)
    candidate_bundle["manifest"]["schema_version"] = 5
    _stamp(candidate_bundle)
    with pytest.raises(DataError, match="require snapshot schema version 6"):
        validate_bundle(candidate_bundle)


@pytest.mark.parametrize("key,value", [
    ("resolution_status", "confirmed"), ("missing_ancestors", []),
    ("missing_ancestors", "VendorBase"), ("uncertainty", "unknown"),
])
def test_invalid_candidate_metadata_is_rejected(candidate_bundle, key, value):
    row = next(item for item in candidate_bundle["facts"]["relationships"]
               if item["qualifiers"].get("resolution_status") == "candidate")
    row["qualifiers"][key] = value
    _stamp(candidate_bundle)
    with pytest.raises(DataError, match="Invalid candidate call resolution"):
        validate_bundle(candidate_bundle)
