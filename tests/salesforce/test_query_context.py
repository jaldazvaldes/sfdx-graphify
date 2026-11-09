"""Agent queries expose extraction gaps without treating zero hits as completeness."""
import copy
import json

import pytest

from graphify.salesforce import cli, query
from graphify.salesforce.activity import inactive_components, remove_inactive
from graphify.salesforce.schema import FactStore


def location(path, line=1):
    return {"path": path, "line": line, "locator": f"/item[{line}]", "content_hash": "a" * 64}


def sample():
    store = FactStore()
    field = store.ensure_component("field", "Account.Code__c", location("objects/Code.xml"))
    flow = store.ensure_component("flow", "Route", location("flows/Route.xml"))
    store.relation(flow, field, "reads", location("flows/Route.xml", 4), "synthetic.read")
    store.diagnostic("FLOW_FORMULA_UNRESOLVED", "A formula is unresolved.", location("flows/Route.xml", 7))
    store.diagnostic("OTHER_GAP", "Unrelated source.", location("flows/Other.xml"))
    return {"manifest": {"snapshot_id": "snapshot:context"}, "facts": store.finish(),
            "diagnostics": store.diagnostics}


def test_zero_impact_summary_keeps_counts_and_discloses_omitted_file_diagnostics():
    bundle = sample()
    before = copy.deepcopy(bundle)
    result = query.impact(bundle, "Route", limit=1)
    assert result["returned_count"] == 0 and not result["truncated"]
    context = result["diagnostic_context"]
    assert context["diagnostics"] == []
    assert context["total"] == 1 and context["omitted_count"] == 1
    assert context["file_counts"] == {"flows/Route.xml": {"total": 1, "code_counts": {"FLOW_FORMULA_UNRESOLVED": 1}}}
    assert context["other_snapshot_diagnostic_count"] == 1
    assert context["truncated"] and context["mode"] == "summary"
    assert context["returned_count"] == 0 and context["omitted_outside_focus_count"] == 1
    assert "unknown consumers" in context["interpretation"]
    assert "FLOW_FORMULA_UNRESOLVED" in cli._text(result)
    assert "--diagnostics full" in cli._text(result)
    full = query.impact(bundle, "Route", limit=1, diagnostics="full")["diagnostic_context"]
    assert full["diagnostics"] == [bundle["diagnostics"][0]]
    assert full["returned_count"] == 1 and full["omitted_count"] == 0
    assert not full["truncated"] and full["record_limit"] is None
    assert bundle == before


def test_refs_include_consumer_file_gaps_independently_of_result_limit():
    bundle = sample()
    for index in range(30):
        bundle["diagnostics"].append({"code": "EXTRA", "message": str(index),
                                      "location": location("flows/Route.xml", index + 20)})
    result = query.refs(bundle, "Account.Code__c", limit=1)
    context = result["diagnostic_context"]
    assert context["total"] == 31 and not context["diagnostics"]
    assert context["omitted_count"] == 31
    assert context["code_counts"] == {"EXTRA": 30, "FLOW_FORMULA_UNRESOLVED": 1}
    assert context["paths"] == ["flows/Route.xml", "objects/Code.xml"]
    full = query.refs(bundle, "Account.Code__c", limit=1, diagnostics="full")["diagnostic_context"]
    assert len(full["diagnostics"]) == 31 and full["omitted_count"] == 0
    assert full["code_counts"] == context["code_counts"] and full["file_counts"] == context["file_counts"]


def test_summary_bounds_focused_records_and_counts_sibling_methods_separately():
    bundle = sample()
    method = {"id": "sf:apexmethod:worker.run", "kind": "apexmethod", "api_name": "Worker.run",
              "definition_status": "defined", "definitions": [{**location("classes/Worker.cls", 10), "end_line": 39}]}
    bundle["facts"]["components"].append(method)
    bundle["diagnostics"] = [
        {"code": "BODY_GAP", "message": f"body-{line}", "location": location("classes/Worker.cls", line)}
        for line in range(10, 40)
    ] + [{"code": "SIBLING_GAP", "message": f"sibling-{line}", "location": location("classes/Worker.cls", line)}
         for line in range(50, 100)]
    before = copy.deepcopy(bundle)
    result = query.deps(bundle, "Worker.run")
    context = result["diagnostic_context"]
    assert result["results"] == [] and not result["truncated"]
    assert context["total"] == 80 and context["relevant_count"] == 30
    assert context["returned_count"] == 20 and context["omitted_count"] == 60
    assert context["omitted_outside_focus_count"] == 50 and context["omitted_by_limit_count"] == 10
    assert context["record_limit"] == 20 and context["truncated"]
    assert [d["location"]["line"] for d in context["diagnostics"]] == list(range(10, 30))
    assert context["file_counts"]["classes/Worker.cls"] == {
        "total": 80, "code_counts": {"BODY_GAP": 30, "SIBLING_GAP": 50}}
    full = query.deps(bundle, "Worker.run", diagnostics="full")["diagnostic_context"]
    assert full["diagnostics"] == bundle["diagnostics"] and not full["truncated"]
    assert bundle == before


def test_summary_focus_uses_evidence_spans_and_explicit_association_without_guessing_line_ranges():
    bundle = sample()
    bundle["facts"]["evidence"][0]["end_line"] = 6
    bundle["diagnostics"].extend([
        {"code": "OVERLAP", "message": "spans evidence", "location": {**location("flows/Route.xml", 3), "end_line": 4}},
        {"code": "LAST_LINE", "message": "last evidence line", "location": location("flows/Route.xml", 6)},
        {"code": "NO_LINE", "message": "no guessed span", "location": {"path": "flows/Route.xml"}},
        {"code": "ASSOCIATED", "message": "explicit target", "location": None, "target_component": "sf:field:account.code__c"},
    ])
    context = query.refs(bundle, "Account.Code__c")["diagnostic_context"]
    assert {d["code"] for d in context["diagnostics"]} == {"OVERLAP", "LAST_LINE", "ASSOCIATED"}
    assert context["total"] == 5 and context["unlocated_count"] == 1
    assert context["omitted_outside_focus_count"] == 2
    assert context["code_counts"]["FLOW_FORMULA_UNRESOLVED"] == 1


def test_summary_size_does_not_scale_with_unrelated_diagnostic_message_bodies():
    bundle = sample()
    bundle["diagnostics"].extend({"code": "GAP", "message": "unrelated details " * 100,
                                  "location": location("flows/Route.xml", line)} for line in range(50, 1050))
    summary = query.refs(bundle, "Account.Code__c")
    full = query.refs(bundle, "Account.Code__c", diagnostics="full")
    assert summary["diagnostic_context"]["total"] == full["diagnostic_context"]["total"] == 1001
    assert len(json.dumps(full)) > 100 * len(json.dumps(summary))
    assert summary["results"] == full["results"]


@pytest.mark.parametrize("command", [query.refs, query.deps, query.impact])
@pytest.mark.parametrize("mode", [None, True, [], "all", "FULL"])
def test_invalid_diagnostics_mode_is_rejected(command, mode):
    with pytest.raises(query.QueryError, match="diagnostics must be summary or full"):
        command(sample(), "Route", diagnostics=mode)


def test_unqualified_fields_resolve_exact_leaf_or_report_ambiguity():
    bundle = sample()
    assert query.refs(bundle, "Code__c")["component"]["api_name"] == "Account.Code__c"
    other = copy.deepcopy(bundle["facts"]["components"][0])
    other.update(id="sf:field:contact.code__c", api_name="Contact.Code__c", kind="field")
    bundle["facts"]["components"].append(other)
    with pytest.raises(query.QueryError) as error:
        query.refs(bundle, "Code__c")
    assert error.value.code == 3
    assert {item["api_name"] for item in error.value.candidates} == {"Account.Code__c", "Contact.Code__c"}


def test_overload_family_requires_exact_signature_when_ambiguous():
    bundle = sample()
    for signature in ("String", "String,Integer"):
        bundle["facts"]["components"].append({
            "id": f"sf:apexmethod:worker.run({signature.lower()})", "kind": "apexmethod",
            "api_name": f"Worker.run({signature})", "qualified_name": "Worker.run",
            "definitions": [], "definition_status": "defined"})
    with pytest.raises(query.QueryError) as error:
        query.deps(bundle, "Worker.run")
    assert error.value.code == 3 and len(error.value.candidates) == 2
    assert query.deps(bundle, "Worker.run(String)")["component"]["api_name"] == "Worker.run(String)"


def test_excluded_component_explains_status_without_restoring_graph_node():
    store = FactStore()
    cid = store.ensure_component("flow", "Old_Route", location("flows/Old.xml"), local_status="Obsolete")
    policy = remove_inactive(store, inactive_components(store.components.values()))
    bundle = {"manifest": {"snapshot_id": "snapshot:inactive", "activity_policy": policy},
              "facts": store.finish(), "diagnostics": store.diagnostics}
    assert not bundle["facts"]["components"]
    with pytest.raises(query.QueryError) as error:
        query.deps(bundle, cid)
    assert error.value.code == 4 and "Obsolete" in str(error.value)
    assert error.value.candidates[0]["api_name"] == "Old_Route"
    bundle["manifest"]["coverage"] = {"activity_policy": policy}
    response = query.status(bundle)
    assert response["activity_policy"]["excluded_component_count"] == 1
    assert "excluded_components" not in response["activity_policy"]
    assert "excluded_components" not in response["coverage"]["activity_policy"]
    assert bundle["manifest"]["activity_policy"]["excluded_components"][0]["id"] == cid


def test_legacy_snapshot_without_exclusion_inventory_stays_readable():
    bundle = sample()
    bundle["manifest"]["activity_policy"] = {"mode": "exclude_explicitly_inactive"}
    with pytest.raises(query.QueryError, match="No matching component"):
        query.refs(bundle, "missing")
