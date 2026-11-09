"""Synthetic LWC imports, lexical binding and provenance regression tests."""

import hashlib
import json

from graphify.salesforce.apex import index_apex
from graphify.salesforce.discovery import discover
from graphify.salesforce.lwc import extract_lwc
from graphify.salesforce.schema import FactStore


PATH = "force-app/main/default/lwc/sample/sample.js"
APEX = b"public class SampleController { @AuraEnabled public static String load(Id recordId) { return null; } }"


def extract(code, *, sources=None, apex=None):
    store = FactStore()
    index = index_apex({"classes/SampleController.cls": APEX} if apex is None else apex, store)
    extract_lwc(sources or {PATH: code.encode()}, index, store)
    return store


def edges(store, verb):
    return [r for r in store.relationships.values() if r["source"] == "sf:lwc:sample" and r["relation"] == verb]


def test_import_imperative_and_wire_are_distinct_with_precise_occurrences():
    code = """import load from '@salesforce/apex/SampleController.load';
import { LightningElement, wire as wired } from 'lwc';
export default class Sample extends LightningElement {
 @wired(load, {recordId: '$recordId'}) result;
 @wired(load) loadResult(load) {}
 async run() { return await load({recordId: this.recordId}); }
 again() { return load({recordId: 'a'}); }
}
"""
    store = extract(code)
    assert len(edges(store, "references")) == 1
    calls = edges(store, "calls")
    assert {r["qualifiers"]["context"] for r in calls} == {"lwc_imperative", "lwc_wire"}
    assert {r["target"] for r in calls} == {"sf:apexmethod:samplecontroller.load"}
    evidence = [store.evidence[e] for r in calls for e in r["evidence_ids"]]
    assert sorted(e["line"] for e in evidence) == [4, 5, 6, 7]
    assert all(e["content_hash"] == hashlib.sha256(code.encode()).hexdigest() for e in evidence)
    assert all(e["resolution"]["basis"] == "lexically_resolved_import" for e in evidence)
    assert not [d for d in store.diagnostics if d["code"].startswith("LWC_")]


def test_unused_import_is_only_a_reference_and_strings_comments_are_not_calls():
    store = extract(r"""import load from '@salesforce/apex/SampleController.load';
// load(); import other from '@salesforce/apex/Other.load';
const text = 'load()'; const re = /load\(\)/;
""")
    assert len(edges(store, "references")) == 1
    assert edges(store, "calls") == []
    assert "sf:apexclass:other" not in store.components


def test_shadowed_parameters_variables_catch_patterns_and_hoisting_are_not_calls():
    store = extract("""import load from '@salesforce/apex/SampleController.load';
function a(load) { load(); }
function b() { load(); var load = () => 1; }
function c({x: load}) { load(); }
function d([load]) { load(); }
function e() { try {} catch(load) { load(); } }
function f() { for (const load of []) load(); }
function g() { { load(); const load = () => 1; } }
function h() { return (load) => load(); }
function i() { class load {} load(); }
function j() { return function load() { load(); }; }
const k = function() { function load() {} load(); };
function yes() { return () => load({recordId: 'x'}); }
""")
    calls = edges(store, "calls")
    assert len(calls) == 1
    assert [store.evidence[e]["line"] for e in calls[0]["evidence_ids"]] == [13]


def test_only_imported_wire_decorator_resolves_the_adapter():
    store = extract("""import load from '@salesforce/apex/SampleController.load';
function wire() {}
class Sample { @wire(load) value; }
""")
    assert not edges(store, "calls")
    assert any(d["code"] == "LWC_APEX_INDIRECT_USE_UNSUPPORTED" for d in store.diagnostics)


def test_default_parameter_calls_are_outside_body_var_and_function_scope():
    store = extract("""import load from '@salesforce/apex/SampleController.load';
function run(value = load({})) { var load = () => 1; load(); }
function second(value = load({})) { function load() {} load(); }
function shadow(load, value = load({})) {}
class Hidden { static { var load = () => 1; load(); } }
load({});
""")
    calls = edges(store, "calls")
    assert len(calls) == 1
    assert sorted(store.evidence[e]["line"] for e in calls[0]["evidence_ids"]) == [2, 3, 6]


def test_for_of_assignment_to_import_is_not_treated_as_a_call():
    store = extract("""import load from '@salesforce/apex/SampleController.load';
for (load of []) {} load({});
""")
    assert not edges(store, "calls")
    assert any(d["code"] == "LWC_IMPORT_BINDING_MUTATED" for d in store.diagnostics)


def test_alias_and_mutated_imports_remain_diagnostic():
    store = extract("""import load from '@salesforce/apex/SampleController.load';
const alias = load; alias();
function run() { load = () => 1; load(); }
""")
    assert edges(store, "references")
    assert not edges(store, "calls")
    codes = {d["code"] for d in store.diagnostics}
    assert {"LWC_IMPORT_BINDING_MUTATED", "LWC_APEX_INDIRECT_USE_UNSUPPORTED"} <= codes


def test_overloads_and_failed_local_classes_are_not_invented_as_external_methods():
    overloaded = b"public class SampleController { public static void load() {} public static void load(Id x) {} }"
    for code in [overloaded, b"public class SampleController { malformed"]:
        store = extract("import load from '@salesforce/apex/SampleController.load'; load();",
                        apex={"classes/SampleController.cls": code})
        assert not edges(store, "calls")
        assert not edges(store, "references")
        assert any(d["code"] == "LWC_APEX_TARGET_UNRESOLVED" for d in store.diagnostics)


def test_external_method_keeps_literal_namespace_and_referenced_only_state():
    store = extract("import load from '@salesforce/apex/vendor.Controller.load'; load();", apex={})
    target = store.components["sf:apexmethod:vendor.controller.load"]
    assert target["definition_status"] == "referenced_only"
    assert len(edges(store, "calls")) == 1


def test_one_bundle_definition_multiple_modules_and_duplicate_bundles():
    code = b"import load from '@salesforce/apex/SampleController.load'; load();"
    helper = PATH.replace("sample.js", "helper.js")
    store = extract("", sources={PATH: code, helper: code})
    bundle = store.components["sf:lwc:sample"]
    assert bundle["definition_status"] == "defined"
    assert bundle["source_files"] == sorted([PATH, helper])
    assert len(edges(store, "calls")[0]["evidence_ids"]) == 2
    duplicate = extract("", sources={PATH: code, PATH.replace("force-app", "second-package"): code})
    assert duplicate.components["sf:lwc:sample"]["definition_status"] == "ambiguous"
    assert not edges(duplicate, "calls")


def test_parse_failure_is_inventoried_without_inventing_calls():
    store = extract("import load from '@salesforce/apex/SampleController.load'; function broken(")
    assert store.components["sf:lwc:sample"]["definition_status"] == "defined"
    assert not edges(store, "references")
    assert any(d["code"] == "LWC_PARSE_ERROR" for d in store.diagnostics)


def test_discovery_keeps_only_direct_lwc_modules_and_workflow_as_supported(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "sfdx-project.json").write_text(json.dumps({"packageDirectories": [{"path": "force-app"}]}))
    paths = [PATH, PATH.replace("sample.js", "helper.js"), PATH.replace("sample.js", "__tests__/sample.test.js"),
             PATH.replace("sample.js", "sample.spec.js"), "force-app/scripts/script.js",
             "force-app/main/default/workflows/Account.workflow-meta.xml"]
    for name in paths:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("<Workflow/>" if name.endswith("xml") else "export const value = 1;", encoding="utf-8")
    scan = discover(root)
    assert set(scan.files) == {paths[0], paths[1], paths[-1]}
    assert set(paths) <= scan.inputs.keys()
    assert len(scan.diagnostics) == 3


def test_end_to_end_impact_includes_lwc_and_hashes_helper_modules(tmp_path):
    from graphify.salesforce import query
    from graphify.salesforce.service import build_project
    root = tmp_path / "source"
    root.mkdir()
    (root / "sfdx-project.json").write_text(json.dumps({"packageDirectories": [{"path": "force-app"}]}))
    for path, data in {PATH: b"import load from '@salesforce/apex/SampleController.load'; export const run = () => load({});",
                       "force-app/main/default/classes/SampleController.cls": APEX}.items():
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
    bundle = build_project(root, out=tmp_path / "out")
    result = query.impact(bundle, "SampleController.load", exclude_tests=True)
    assert any(row["source"] == "sf:lwc:sample" and row["relation"] == "calls" for row in result["results"])
    assert bundle["manifest"]["inputs"][PATH] == hashlib.sha256((root / PATH).read_bytes()).hexdigest()
