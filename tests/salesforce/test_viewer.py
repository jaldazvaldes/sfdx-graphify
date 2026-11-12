"""Synthetic contracts for bounded local views, comparison, and HTTP isolation."""
from __future__ import annotations

import copy
from html import unescape
import http.client
import json
from pathlib import Path
import re
import socket
from threading import Thread
from urllib.parse import urlencode

import pytest

from graphify.salesforce import viewer
from graphify.salesforce.schema import (
    DataError, EXTRACTOR_VERSION, FactStore, SCHEMA_VERSION, component_id, stable_id, validate_bundle,
)
from graphify.salesforce.snapshot import publish


def location(number=1, path="force-app/main/default/flows/Synthetic.flow-meta.xml"):
    return {"path": path, "line": number, "column": 1, "end_line": number,
            "end_column": 20, "locator": f"/Synthetic/reference[{number}]", "content_hash": "0" * 64}


def seal(store):
    facts = store.finish()
    paths = {item["path"] for item in facts["evidence"]}
    paths.update(entry["path"] for item in facts["components"] for entry in item["definitions"])
    bundle = {"manifest": {"schema_version": SCHEMA_VERSION, "extractor_version": EXTRACTOR_VERSION,
                           "source_revision": "1" * 40, "dirty": False, "input_mode": "working_tree",
                           "inputs": {path: "0" * 64 for path in paths},
                           "scope": {"package_dirs": ["force-app"], "unsupported_paths": ["force-app/layouts/Other.xml"]},
                           "coverage": {"status": "partial", "analyzed_files": len(paths),
                                        "tested_api_versions": ["64.0"], "untested_api_versions": ["63.0"],
                                        "limitations": ["Bounded synthetic fixture."]}},
              "facts": facts, "diagnostics": []}
    bundle["manifest"]["snapshot_id"] = stable_id("snapshot", bundle)
    validate_bundle(bundle)
    return bundle


def reseal(bundle):
    bundle["manifest"].pop("snapshot_id", None)
    bundle["manifest"]["snapshot_id"] = stable_id("snapshot", bundle)
    validate_bundle(bundle)
    return bundle


@pytest.fixture
def bundle():
    store = FactStore()
    account = store.ensure_component("object", "Account", location())
    name = store.ensure_component("field", "Account.Name", location())
    lookup = store.ensure_component("field", "Account.Contact__c", location())
    contact = store.ensure_component("object", "Contact", location())
    active = store.ensure_component("flow", "Update_Account", location(), local_status="Active", api_version="64.0")
    draft = store.ensure_component("flow", "Draft_Account", location(), local_status="Draft")
    caller = store.ensure_component("flow", "Caller", location(), local_status="Active")
    cls = store.ensure_component("apexclass", "SampleAction", location(path="force-app/classes/SampleAction.cls"))
    method = store.ensure_component("apexmethod", "SampleAction.execute", location(path="force-app/classes/SampleAction.cls"))
    for number, (source, target, relation, qualifiers) in enumerate([
        (account, name, "contains", {}), (account, lookup, "contains", {}),
        (lookup, contact, "references", {"context": "lookup_declaration"}),
        (active, name, "reads", {}), (active, name, "filters", {"operation": "start_filter"}),
        (active, name, "writes", {"operation": "update", "effect": "field_value"}),
        (draft, name, "writes", {}), (caller, active, "calls", {}),
        (active, caller, "calls", {}), (active, method, "calls", {"operation": "invocable"}),
        (cls, method, "contains", {}), (method, name, "reads", {"operation": "soql_select"}),
    ], 1):
        store.relation(source, target, relation, location(number), "synthetic." + relation, qualifiers=qualifiers)
    return seal(store)


def cid(kind, name):
    return component_id(kind, name)


def test_impact_is_incoming_cycle_safe_and_keeps_exact_evidence(bundle):
    result = viewer.build_view(bundle, cid("field", "Account.Name"))
    assert {edge["relation"] for edge in result["edges"]} == {"reads", "writes", "filters"}
    assert len(result["nodes"]) == 4
    assert all(edge["evidence"] and edge["evidence"][0]["line"] for edge in result["edges"])
    expanded = viewer.build_view(bundle, cid("field", "Account.Name"), depth=3)
    assert len({edge["id"] for edge in expanded["edges"]}) == len(expanded["edges"])
    assert "contains" not in {edge["relation"] for edge in expanded["edges"]}
    assert cid("apexclass", "SampleAction") not in {node["id"] for node in expanded["nodes"]}
    assert expanded["freshness"] == "unknown"


def test_object_groups_fields_without_changing_canonical_targets(bundle):
    result = viewer.build_view(bundle, cid("object", "Account"))
    assert result["field_targets_grouped"] == 2
    assert result["method_members_grouped"] == 0
    assert all(edge["target"] == cid("object", "Account") for edge in result["edges"])
    assert all(record["target"] == cid("field", "Account.Name")
               for edge in result["edges"] for record in edge["relationships"])
    assert all(edge["relation"] != "contains" for edge in result["edges"])


def test_class_focus_uses_declared_methods_as_presentation_context(bundle):
    result = viewer.build_view(bundle, cid("apexclass", "SampleAction"), mode="automation")
    assert result["method_members_grouped"] == 1
    assert result["field_targets_grouped"] == 0
    assert {edge["relation"] for edge in result["edges"]} == {"reads", "calls"}
    read = next(edge for edge in result["edges"] if edge["relation"] == "reads")
    assert read["source"] == cid("apexclass", "SampleAction")
    assert read["relationships"][0]["source"] == cid("apexmethod", "SampleAction.execute")
    assert read["relationships"][0]["qualifiers"]["operation"] == "soql_select"
    member = result["ownership_context"]["members"][0]
    assert member["component"]["id"] == cid("apexmethod", "SampleAction.execute")
    ownership = viewer.SnapshotView(bundle).evidence(member["relationship_id"])
    assert ownership["relationship"]["relation"] == "contains"
    incoming = viewer.build_view(bundle, cid("apexclass", "SampleAction"))
    assert [edge["relation"] for edge in incoming["edges"]] == ["calls"]


def test_schema_contains_only_fields_and_lookup_declarations(bundle):
    result = viewer.build_view(bundle, cid("object", "Account"), mode="schema", depth=2)
    assert {edge["relation"] for edge in result["edges"]} == {"contains", "references"}
    assert {node["kind"] for node in result["nodes"]} <= {"object", "field"}
    assert cid("object", "Contact") in {node["id"] for node in result["nodes"]}
    assert result["field_targets_grouped"] == 0


def test_validation_rule_automation_keeps_formula_and_display_field_evidence():
    store = FactStore()
    owner = store.ensure_component("object", "Account")
    field = store.ensure_component("field", "Account.Name")
    definition = location(path="force-app/main/default/objects/Account/validationRules/Require_Name.validationRule-meta.xml")
    rule = store.ensure_component("validationrule", "Account.Require_Name", definition, local_status="Active")
    store.relation(owner, rule, "contains", definition, "metadata.validation_rule_owner")
    for relation, context in (("reads", "validation_formula"), ("references", "error_display_field")):
        store.relation(rule, field, relation, definition, "validation." + context,
                       qualifiers={"context": context})
    bundle = seal(store)
    original = copy.deepcopy(bundle)
    result = viewer.build_view(bundle, rule, mode="automation")
    assert {edge["relation"] for edge in result["edges"]} == {"reads", "references", "contains"}
    assert {node["id"] for node in result["nodes"]} == {rule, field, owner}
    assert result["focus"]["local_status"] == "Active"
    assert all(edge["evidence"][0]["path"] == definition["path"] for edge in result["edges"])
    incoming = viewer.build_view(bundle, field, mode="automation")
    assert rule in {node["id"] for node in incoming["nodes"]}
    assert bundle == original


def test_object_reaches_its_global_only_rule_and_rule_reaches_owner_as_context():
    store = FactStore()
    definition = location(path="force-app/objects/Catalog__c/validationRules/Policy.validationRule-meta.xml")
    owner = store.ensure_component("object", "Catalog__c")
    rule = store.ensure_component("validationrule", "Catalog__c.Policy", definition)
    global_field = store.ensure_component("field", "PolicySettings__c.Enabled__c")
    membership = store.relation(owner, rule, "contains", definition, "validation_rule.owner")
    store.relation(rule, global_field, "reads", definition, "validation_rule.formula_reference")
    model = viewer.SnapshotView(seal(store))
    automation = model.view(owner, mode="automation", depth=2)
    assert {node["id"] for node in automation["nodes"]} == {owner, rule, global_field}
    assert {edge["relation"] for edge in automation["edges"]} == {"contains", "reads"}
    assert automation["ownership_context"]["members"][0]["relationship_id"] == membership
    assert model.view(rule, mode="automation")["ownership_context"]["owners"][0]["component"]["id"] == owner
    impact = model.view(owner, mode="impact")
    assert not impact["edges"]  # Membership is navigation, not a claim of causal impact.
    assert impact["ownership_context"]["total"] == 1
    assert impact["ownership_context"]["members"][0]["component"]["id"] == rule


def test_component_diagnostics_are_bounded_and_full_pages_preserve_every_record():
    store = FactStore()
    definition = location(path="force-app/classes/PolicyAction.cls")
    component = store.ensure_component("apexmethod", "PolicyAction.run", definition)
    for number in range(1, 151):
        store.diagnostic("SYNTHETIC_UNRESOLVED", "A synthetic unresolved reference.",
                         location(number, definition["path"]))
    bundle = seal(store)
    bundle["diagnostics"] = store.diagnostics
    reseal(bundle)
    model = viewer.SnapshotView(bundle)
    context = model.view(component)["diagnostic_context"]
    assert context["mode"] == "summary" and context["total"] == 150
    assert context["returned_count"] == 1 and context["omitted_count"] == 149
    first = model.diagnostics_page(component)
    second = model.diagnostics_page(component, offset=first["next_offset"])
    assert first["total"] == 150 and len(first["results"]) == 100
    assert len(second["results"]) == 50 and second["next_offset"] is None
    assert {item["id"] for item in first["results"] + second["results"]} == {
        item["id"] for item in bundle["diagnostics"]}
    with pytest.raises(DataError, match="Invalid diagnostics page"):
        model.diagnostics_page(component, offset=-1)
    with pytest.raises(DataError, match="No matching component"):
        model.diagnostics_page("missing")


def test_validation_rule_status_is_visible_and_ambiguous_definitions_are_not_preferred():
    store = FactStore()
    rule = store.ensure_component("validationrule", "Account.Check_Name", location(), local_status="Inactive")
    bundle = seal(store)
    model = viewer.SnapshotView(bundle)
    assert model.search("Check_Name")["results"][0]["local_status"] == "Inactive"
    before = copy.deepcopy(bundle)
    item = next(item for item in bundle["facts"]["components"] if item["id"] == rule)
    item["local_status"] = "Active"
    result = viewer.SnapshotView(bundle, before).view(rule, mode="automation")
    assert result["focus"]["before_local_status"] == "Inactive"
    assert result["focus"]["local_status"] == "Active"
    item["definition_status"] = "ambiguous"
    item["metadata_by_definition"] = [{"local_status": "Active"}, {"local_status": "Inactive"}]
    result = viewer.SnapshotView(bundle).search("Check_Name")["results"][0]
    assert result["local_status"] == "ambiguous"
    assert len(result["metadata_by_definition"]) == 2


def test_flow_status_filters_count_hidden_facts_without_changing_focus(bundle):
    model = viewer.SnapshotView(bundle)
    result = model.view(cid("field", "Account.Name"), status="Active")
    assert result["filters"] == {"hidden_flow_count": 1, "hidden_relationship_count": 1}
    assert cid("flow", "Draft_Account") not in {node["id"] for node in result["nodes"]}
    flow = next(item for item in bundle["facts"]["components"] if item["id"] == cid("flow", "Draft_Account"))
    flow.pop("local_status")
    assert viewer.SnapshotView(bundle).search("Draft_Account")["results"][0]["local_status"] == "unknown"
    flow["definition_status"] = "ambiguous"
    flow["local_status"] = "Active"
    flow["metadata_by_definition"] = [{"local_status": "Active"}, {"local_status": "Draft"}]
    value = viewer.SnapshotView(bundle).search("Draft_Account")["results"][0]
    assert value["local_status"] == "ambiguous"
    assert len(value["metadata_by_definition"]) == 2


def test_comparison_discloses_extractor_scope_and_source_inputs(bundle):
    before = copy.deepcopy(bundle)
    before["manifest"]["extractor_version"] = "0.1.0"
    before["manifest"]["scope"]["include_untracked"] = False
    for record in before["facts"]["evidence"]:
        record["extractor_version"] = "0.1.0"
    flow = next(item for item in before["facts"]["components"] if item["kind"] == "flow")
    flow.pop("local_status")
    reseal(before)
    model = viewer.SnapshotView(bundle, before)
    assert set(model.changes.values()) == {"unchanged"}
    assert model._node(flow["id"])["change"] == "index_metadata_changed"
    comparison = model.metadata["comparison"]
    assert comparison["extraction_changed"]
    assert comparison["before"]["extractor_version"] == "0.1.0"
    assert comparison["scope_changed_keys"] == ["include_untracked"]
    assert comparison["inputs"] == {"added": 0, "removed": 0, "changed": 0}
    assert "unknown" in model.statuses


@pytest.mark.parametrize("field,value", [("line", 400), ("content_hash", "1" * 64)])
def test_comparison_marks_actual_proof_change(bundle, field, value):
    before = copy.deepcopy(bundle)
    bundle["facts"]["evidence"][0][field] = value
    model = viewer.SnapshotView(bundle, before)
    rid = bundle["facts"]["evidence"][0]["relationship_id"]
    assert model.changes[rid] == "evidence_changed"
    if field == "content_hash":
        component = bundle["facts"]["components"][0]
        component["definitions"][0][field] = value
        assert viewer.SnapshotView(bundle, before)._node(component["id"])["change"] == "source_changed"


def test_comparison_keeps_removed_edges_and_baseline_flow_status(bundle):
    before = copy.deepcopy(bundle)
    active = next(item for item in bundle["facts"]["components"] if item["id"] == cid("flow", "Update_Account"))
    active["local_status"] = "Draft"
    removed = next(edge for edge in bundle["facts"]["relationships"] if edge["relation"] == "filters")
    bundle["facts"]["relationships"].remove(removed)
    bundle["facts"]["evidence"] = [item for item in bundle["facts"]["evidence"] if item["id"] not in removed["evidence_ids"]]
    model = viewer.SnapshotView(bundle, before)
    result = model.view(cid("field", "Account.Name"), status="Active")
    edge = next(edge for edge in result["edges"] if edge["relation"] == "filters")
    assert edge["change"] == "removed"
    assert model.evidence(removed["id"], snapshot="before")["total"] == 1
    with pytest.raises(DataError):
        model.evidence(removed["id"])


def test_node_edge_caps_search_and_evidence_pagination():
    store = FactStore()
    field = store.ensure_component("field", "Account.Name")
    for number in range(130):
        flow = store.ensure_component("flow", f"Synthetic_{number:03}", local_status="Active")
        for relation in ("reads", "filters", "writes"):
            store.relation(flow, field, relation, location(number + 1), "synthetic." + relation)
    flow = cid("flow", "Synthetic_000")
    for number in range(200, 315):
        rid = store.relation(flow, field, "reads", location(number), "synthetic.reads")
    model = viewer.SnapshotView(seal(store))
    result = model.view(field)
    assert len(result["nodes"]) == 100 and len(result["edges"]) == 200
    assert result["truncation"]["nodes_omitted"] == 31
    assert result["truncation"]["edge_groups_omitted"] == 97
    assert result["truncation"]["relationships_omitted"] == 190
    search = model.search("synthetic")
    assert search["total"] == 130 and len(search["results"]) == 25 and search["truncated"]
    first = model.evidence(rid)
    second = model.evidence(rid, offset=first["next_offset"])
    assert first["total"] == 116 and len(first["results"]) == 100
    assert len(second["results"]) == 16 and second["next_offset"] is None


@pytest.mark.parametrize("options", [{"mode": "runtime"}, {"depth": 0}, {"depth": 4}, {"status": "Deployed"}])
def test_invalid_view_options_fail_closed(bundle, options):
    with pytest.raises(DataError):
        viewer.build_view(bundle, cid("object", "Account"), **options)


def test_pure_view_never_opens_socket(bundle, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Pure views must not open sockets")
    monkeypatch.setattr(socket, "socket", forbidden)
    assert viewer.build_view(bundle, cid("object", "Account"))["edges"]


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.invalid", "::"])
def test_server_rejects_non_loopback_before_loading(host, monkeypatch):
    def forbidden(*args):
        raise AssertionError("Invalid server host must fail before snapshot load")
    monkeypatch.setattr(viewer, "load", forbidden)
    with pytest.raises(DataError):
        viewer.create_server(Path("unused"), host=host, port=0)


@pytest.mark.parametrize("with_processes", [False, True])
def test_registered_http_routes_integrity_and_host_isolation(bundle, tmp_path, monkeypatch, capsys, with_processes):
    source = tmp_path / "synthetic-source"
    source.mkdir()
    output = tmp_path / "snapshot"
    publish(bundle, output, source)
    real_load = viewer.load
    calls = []
    def counted_load(path):
        calls.append(path)
        return real_load(path)
    monkeypatch.setattr(viewer, "load", counted_load)
    catalog_path = None
    if with_processes:
        catalog_path = tmp_path / "processes.json"
        evidence = {"path": location()["path"], "line": 4, "label": "Read the field"}
        field = cid("field", "Account.Name")
        step = {
            "id": "inspect", "label": "Inspect", "kind": "start", "description": "Inspect one field.",
            "components": [cid("flow", "Update_Account"), field],
            "effects": [{"kind": "read", "summary": "Read the name.", "component": field, "evidence": [evidence]}],
            "evidence": [evidence],
        }
        process = {"id": "example", "title": "Example", "summary": "One step.", "steps": [step], "edges": []}
        missing = cid("apexclass", "MissingClass")
        missing_step = {**step, "components": [missing], "effects": []}
        incomplete = {**process, "id": "incomplete", "title": "Incomplete", "steps": [missing_step]}
        feature = {"id": "example_feature", "name": "Example feature", "processes": [process, incomplete]}
        catalog = {"source_revision": "1" * 40, "countries": [{"id": "XX", "name": "Example", "features": [feature]}]}
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    server = viewer.create_server(output, port=0, process_maps=catalog_path)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            connection.close()
    try:
        code, headers, body = request("/")
        assert code == 200 and b"graphify" in body
        assert "script-src 'self'" in headers["Content-Security-Policy"]
        assert "script-src 'self';" in headers["Content-Security-Policy"]
        assert headers["Cache-Control"] == "no-store"
        code, headers, body = request("/explorer")
        assert code == 200 and b"Salesforce dependency explorer" in body
        assert "unsafe-inline" not in headers["Content-Security-Policy"]
        assert request("/vis-network.min.js")[0] == 200
        for asset in ("/viewer.css", "/viewer.js"):
            assert request(asset)[0] == 200
        script = request("/viewer.js")[2]
        assert b"innerHTML" not in script and b"https://" not in script
        code, _, body = request("/api/view?" + urlencode({"component": cid("field", "Account.Name")}))
        assert code == 200 and json.loads(body)["edges"]
        code, _, body = request("/api/search?query=Account")
        assert code == 200 and json.loads(body)["total"] > 1
        code, _, body = request("/api/diagnostics?" + urlencode({"component": cid("field", "Account.Name")}))
        assert code == 200 and json.loads(body)["total"] == 0
        assert request("/api/diagnostics?component=missing")[0] == 400
        assert request("/api/diagnostics?component=missing&offset=-1")[0] == 400
        meta = json.loads(request("/api/meta")[2])
        assert meta["coverage"]["unsupported_inventory_files"] == 1
        assert meta["processes_available"] is with_processes
        assert request("/processes")[0] == (200 if with_processes else 404)
        graph_route = "/process-graph?" + urlencode({"country": "XX", "process": "example"})
        assert request(graph_route)[0] == (200 if with_processes else 404)
        code, _, body = request("/api/processes")
        assert code == (200 if with_processes else 404)
        if with_processes:
            from graphify.salesforce import cli, process_registration

            result = json.loads(body)
            assert result["snapshot_id"] == bundle["manifest"]["snapshot_id"]
            complete_map, incomplete_map = result["countries"][0]["features"][0]["processes"]
            assert "review_status" not in complete_map
            assert incomplete_map["review_status"] == "needs_review"
            assert incomplete_map["steps"][0]["missing_components"] == [missing]
            actual = complete_map["steps"][0]
            assert [item["id"] for item in actual["components"]] == step["components"]
            match, = actual["effects"][0]["evidence"][0]["snapshot_matches"]
            assert match["rule"] == "synthetic.reads"
            assert request("/api/evidence?" + urlencode({"relationship": match["relationship"]}))[0] == 200
            assert request("/api/processes", {"Origin": "https://attacker.invalid"})[0] == 403
            assert request("/processes.css")[0] == request("/processes.js")[0] == 200
            code, headers, graph_page = request(graph_route + "&step=inspect")
            assert code == 200
            assert "script-src 'self';" in headers["Content-Security-Policy"]
            graph_assets = [
                unescape(url) for url in re.findall(r'(?:href|src)="([^"]+)"', graph_page.decode())
                if url.startswith("/process-graph/standard")
            ]
            assert graph_assets
            scripts = []
            for asset in graph_assets:
                status, _, content = request(asset)
                assert status == 200
                scripts.append(content.decode())
            graph_data = next(script for script in scripts if "const RAW_NODES = " in script)
            nodes = json.loads(re.search(r"const RAW_NODES = (.*);\n", graph_data)[1])
            assert {node["id"] for node in nodes} == {
                cid("flow", "Update_Account"), field, cid("object", "Account"),
            }
            assert b"/processes#country=XX&amp;process=example&amp;step=inspect" in graph_page
            assert request(graph_route + "&step=missing")[0] == 400
            assert request("/process-graph?country=XX&process=incomplete")[0] == 400
            assert request(graph_route, {"Origin": "https://attacker.invalid"})[0] == 403
            registration = {key: result[key] for key in ("source_revision", "snapshot_id", "catalog_digest")}
            registration.update({"snapshot": str(output), "catalog": str(catalog_path), "port": server.server_port})
            monkeypatch.setattr(process_registration, "load_process_registration", lambda _: registration)
            assert cli.main(["processes", "show", "example", "--step", "inspect"]) == 0
            found = json.loads(capsys.readouterr().out)
            assert found["viewer_url"] == f"http://127.0.0.1:{server.server_port}"
            assert found["process"]["viewer_path"] == "/processes#country=XX&process=example&step=inspect"
            assert found["step"]["effects"] == actual["effects"]
            assert cli.main(["processes", "list", "--component", "Account.Name"]) == 0
            assert [item["id"] for item in json.loads(capsys.readouterr().out)["results"]] == ["example"]
            assert cli.main(["processes", "status"]) == 0
            status = json.loads(capsys.readouterr().out)
            assert status["registered_files"] == "match" and status["snapshot"] == str(output)
            assert cli.main(["refs", "Account.Name", "--registered"]) == 0
            assert json.loads(capsys.readouterr().out)["snapshot_id"] == registration["snapshot_id"]
            assert catalog_path is not None
            with catalog_path.open("ab") as handle:
                handle.write(b"\n")
            assert cli.main(["processes", "list"]) == 1
            assert "register again" in json.loads(capsys.readouterr().out)["error"]
        assert request("/api/meta", {"Host": "attacker.invalid"})[0] == 403
        assert request("/api/meta", {"Origin": "https://attacker.invalid"})[0] == 403
        for path in ("/../SOURCE.json", "/%2e%2e/SOURCE.json", "/facts.json", "/source", "/api/unknown"):
            assert request(path)[0] == 404
        assert request("/api/search?query=a&query=b")[0] == 400
        assert request("/api/view?component=missing")[0] == 400
        assert request("/api/search?query=" + "a" * 161)[0] == 400
        assert len(calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
