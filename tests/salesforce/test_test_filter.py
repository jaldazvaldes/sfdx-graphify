"""Production ordering and reversible test filters on full canonical evidence."""
from __future__ import annotations

import copy

import pytest

from graphify.salesforce import query
from graphify.salesforce.schema import FactStore


def sample():
    store = FactStore()
    field = store.ensure_component("field", "Account.Name")
    methods = {}
    for name, flag in (("AlphaTest.run", True), ("ZuluService.run", False), ("UnknownCode.run", None)):
        methods[name] = store.ensure_component("apexmethod", name, is_test=flag)
        for index in range(2):
            store.relation(methods[name], field, "writes", {
                "path": "classes/Sample.cls", "locator": f"/{name}/{index}",
                "line": index + 1, "content_hash": "0" * 64,
            }, "synthetic.field_write")
    return {"manifest": {"snapshot_id": "snapshot:filter", "schema_version": 3},
            "facts": store.finish(), "diagnostics": []}, methods


def test_production_and_unknown_precede_tests_before_limit_without_dropping_evidence():
    bundle, _ = sample()
    original = copy.deepcopy(bundle)
    found = query.refs(bundle, "Account.Name")
    assert found["total"] == 3
    assert found["results"][-1]["source"] == "sf:apexmethod:alphatest.run"
    assert all(len(row["evidence"]) == 2 for row in found["results"])
    limited = query.refs(bundle, "Account.Name", limit=2)
    assert limited["truncated"] and limited["total"] == 3
    assert all(row["source"] != "sf:apexmethod:alphatest.run" for row in limited["results"])
    assert bundle == original


def test_exclusion_is_explicit_reversible_and_keeps_unknown_classification():
    bundle, _ = sample()
    filtered = query.refs(bundle, "Account.Name", exclude_tests=True)
    assert filtered["total"] == 2
    assert {row["source"] for row in filtered["results"]} == {
        "sf:apexmethod:unknowncode.run", "sf:apexmethod:zuluservice.run"}
    assert filtered["test_filter"]["excluded_relationships_in_snapshot"] == 1
    assert filtered["test_filter"]["unknown_apex_components_retained"] == 1
    assert query.refs(bundle, "Account.Name")["total"] == 3
    assert query.components(bundle, kind="apexmethod", exclude_tests=True)["total"] == 2
    with pytest.raises(query.QueryError, match="excluded by the test filter"):
        query.deps(bundle, "AlphaTest.run", exclude_tests=True)


def test_impact_filters_before_traversal_and_reports_returned_count():
    bundle, _ = sample()
    result = query.impact(bundle, "Account.Name", exclude_tests=True, limit=2)
    assert result["returned_count"] == 2 and not result["truncated"]
    assert all(row["source"] != "sf:apexmethod:alphatest.run" for row in result["results"])
    limited = query.impact(bundle, "Account.Name", limit=2)
    assert limited["limit_limited"]
    assert all(row["source"] != "sf:apexmethod:alphatest.run" for row in limited["results"])


def test_non_boolean_filter_is_rejected():
    bundle, _ = sample()
    with pytest.raises(query.QueryError, match="boolean"):
        query.refs(bundle, "Account.Name", exclude_tests="true")  # type: ignore[arg-type]
