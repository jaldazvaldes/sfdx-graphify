"""Process searches and deep links over a synthetic authored catalog."""

from urllib.parse import parse_qs, urlsplit

import pytest

from graphify.salesforce.process_queries import format_process_brief, list_processes, show_process
from graphify.salesforce.query import QueryError


@pytest.fixture
def catalog():
    field = {"id": "sf:field:sample.status", "api_name": "Sample.Status", "kind": "field", "definitions": []}
    evidence = {"path": "force-app/flows/Example.flow-meta.xml", "line": 23, "label": "Save status",
                "snapshot_matches": [{"relationship": "rel:sample", "rule": "flow.update",
                                      "relation": "writes", "resolution": "extracted"}]}
    steps = [
        {"id": "assign", "label": "Set status", "kind": "action", "description": "Assign a value.",
         "components": [field], "effects": [], "evidence": []},
        {"id": "persist + confirm", "label": "Save status", "kind": "outcome", "description": "Attempt to save.",
         "components": [field], "effects": [{"kind": "update", "summary": "Save the record.",
                                             "component": field["id"], "evidence": [evidence]}],
         "evidence": [evidence]},
    ]
    process = {"id": "update-sample", "title": "Update sample records", "summary": "Build a payload first.",
               "steps": steps, "edges": [{"from": "assign", "to": "persist + confirm", "label": "When eligible"}]}
    create = {**process, "id": "create-sample", "title": "Create sample records", "summary": "Create a record."}
    prepare = {"id": "prepare", "name": "Prepare", "processes": [{**process, "entry_step": "assign"}]}
    verify = {"id": "route & check", "name": "Route and check",
              "processes": [{**process, "entry_step": "persist + confirm"}, create]}
    return {"snapshot_id": "snapshot:sample", "source_revision": "a" * 40,
            "countries": [{"id": "AA", "name": "First country", "features": [prepare, verify]},
                          {"id": "BB", "name": "Second country", "features": [prepare]}]}


def _route(viewer_path):
    path = urlsplit(viewer_path)
    assert path.path == "/processes"
    return {key: values[0] for key, values in parse_qs(path.fragment).items()}


def test_process_list_deduplicates_per_country_and_filters_by_feature_and_text(catalog):
    results = list_processes(catalog)["results"]
    assert {(result["country"]["id"], result["id"]) for result in results} == {
        ("AA", "create-sample"), ("AA", "update-sample"), ("BB", "update-sample"),
    }
    first, = list_processes(catalog, country="first COUNTRY", feature="ROUTE AND CHECK", match="build payl")["results"]
    assert first["id"] == "update-sample"
    assert [feature["id"] for feature in first["features"]] == ["prepare", "route & check"]
    assert _route(first["viewer_path"]) == {
        "country": "AA", "process": "update-sample", "feature": "route & check", "step": "persist + confirm",
    }
    assert list_processes(catalog, country="BB", feature="route & check")["results"] == []


def test_process_show_exposes_outline_or_evidence_and_deep_links(catalog):
    outline = show_process(catalog, "update sample", country="aa", feature="Route and check")
    assert outline["snapshot_id"] == catalog["snapshot_id"]
    assert outline["source_revision"] == catalog["source_revision"]
    assert "step" not in outline
    assert outline["process"]["steps"][0] == {
        "id": "assign", "label": "Set status", "kind": "action", "description": "Assign a value.", "effects": [],
    }
    assert outline["process"]["edges"] == [{"from": "assign", "to": "persist + confirm", "label": "When eligible"}]
    compact, = outline["process"]["steps"][1]["effects"]
    assert compact["component_name"] == "Sample.Status"
    reference, = compact["evidence"]
    assert reference == {"path": "force-app/flows/Example.flow-meta.xml", "line": 23, "resolution": "extracted"}
    assert _route(outline["process"]["viewer_path"])["step"] == "persist + confirm"
    detail = show_process(catalog, "update-sample", country="AA", feature="prepare", step="save stat")
    assert _route(detail["process"]["viewer_path"])["step"] == "persist + confirm"
    reference, = detail["step"]["effects"][0]["evidence"]
    assert reference["line"] == 23
    assert reference["snapshot_matches"][0]["relationship"] == "rel:sample"


def test_reverse_lookup_returns_only_authored_steps_and_typed_candidates(catalog):
    reverse = list_processes(catalog, country="AA", match="update sample", component="SAMPLE.STATUS")
    assert reverse["component"]["id"] == "sf:field:sample.status"
    process, = reverse["results"]
    assert [step["id"] for step in process["matching_steps"]] == ["assign", "persist + confirm"]
    assert _route(process["matching_steps"][1]["viewer_path"])["step"] == "persist + confirm"
    assert list_processes(catalog, component="Other.Status")["results"] == []

    first = catalog["countries"][0]["features"][0]["processes"][0]
    first["steps"][0]["components"].append({"id": "sf:apexmethod:sample.status", "api_name": "Sample.Status",
                                            "kind": "apexmethod", "definitions": []})
    with pytest.raises(QueryError, match="Ambiguous component:") as error:
        list_processes(catalog, component="Sample.Status")
    assert {item["id"] for item in error.value.candidates} == {"sf:field:sample.status", "sf:apexmethod:sample.status"}
    typed = list_processes(catalog, component="sf:apexmethod:sample.status")
    assert all([step["id"] for step in process["matching_steps"]] == ["assign"] for process in typed["results"])


def test_brief_includes_connected_steps_effect_and_single_citation(catalog):
    process = catalog["countries"][0]["features"][0]["processes"][0]
    process["review_status"] = "needs_review"
    process["steps"][0]["missing_components"] = ["sf:field:sample.missing"]
    detail = show_process(catalog, "update-sample", country="AA", feature="prepare")
    brief = format_process_brief(detail | {"warnings": ["Different project revision."]})
    assert "Different project revision." in brief
    assert "Review needed." in brief
    assert "Missing components sf:field:sample.missing" in brief
    assert "Assign a value." in brief
    assert "→ persist + confirm · When eligible" in brief
    assert "update · Sample.Status · Save the record. [force-app/flows/Example.flow-meta.xml:23]" in brief
    assert "rel:sample" not in brief


def test_process_show_requires_an_unambiguous_country_and_step(catalog):
    with pytest.raises(QueryError, match="Ambiguous process:") as error:
        show_process(catalog, "update-sample")
    assert error.value.code == 3
    assert {candidate["country"]["id"] for candidate in error.value.candidates} == {"AA", "BB"}
    with pytest.raises(QueryError, match="Ambiguous process step:") as error:
        show_process(catalog, "update-sample", country="AA", step="status")
    assert {candidate["id"] for candidate in error.value.candidates} == {"assign", "persist + confirm"}
    with pytest.raises(QueryError, match="No matching process:") as error:
        show_process(catalog, "unavailable", country="AA")
    assert error.value.code == 4
