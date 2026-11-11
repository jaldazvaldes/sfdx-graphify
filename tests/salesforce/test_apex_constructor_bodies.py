"""Synthetic contracts for outer Apex constructor source dependencies."""
from __future__ import annotations

import hashlib

from graphify.salesforce import query
from graphify.salesforce.apex import extract_apex, index_apex, resolve_invocable_calls
from graphify.salesforce.lwc import extract_lwc
from graphify.salesforce.schema import SCHEMA_VERSION, FactStore, component_id, stable_id, validate_bundle
from graphify.salesforce.ui_apex import extract_ui_apex
from tests.salesforce.test_apex import attach_flow
from tests.salesforce.test_apex_review_fixes import build


def test_constructor_bodies_keep_dependencies_and_no_new_to_constructor_call():
    worker = """public class Worker extends Base {
      public Worker() { this(normalize(null)); }
      public Worker(Id key) {
        super(normalize(key));
        if (Settings__c.getInstance(key).Enabled__c) {
          List<Account> records = [SELECT Name FROM Account WHERE Id = :key];
          update records;
          prepare(key);
        }
      }
      public void Worker() {}
      private static Id normalize(Id value) { return value; }
      private void prepare(Id key) {}
    }"""
    store = build([
        ("Base", "public virtual class Base { protected Base(Id key) {} }"),
        ("Worker", worker),
        ("Launcher", "public class Launcher { public void run(Id key) { Worker item = new Worker(key); } }"),
    ], metadata=[("objects/Settings__c/Settings__c.object-meta.xml",
                 "<CustomObject><customSettingsType>Hierarchy</customSettingsType></CustomObject>")])
    facts = store.finish()
    inputs = {item["path"]: item["content_hash"]
              for component in facts["components"] for item in component["definitions"]}
    bundle = {"manifest": {"schema_version": SCHEMA_VERSION, "inputs": inputs},
              "facts": facts, "diagnostics": store.diagnostics}
    bundle["manifest"]["snapshot_id"] = stable_id("snapshot", bundle)
    validate_bundle(bundle)

    constructor_id = component_id("apexmethod", "Worker.new(Id)")
    constructor = query.resolve_component(bundle, constructor_id)
    assert constructor["is_constructor"] and not constructor["invocable"]
    assert constructor["definitions"][0]["content_hash"] == hashlib.sha256(worker.encode()).hexdigest()
    assert query.resolve_component(bundle, "Worker.Worker")["id"] == "sf:apexmethod:worker.worker"
    assert query.resolve_component(bundle, "Worker.new()")["id"] == component_id("apexmethod", "Worker.new()")
    edges = query.deps(bundle, "Worker.new(Id)")["results"]
    by_target = {(edge["target"], edge["relation"]) for edge in edges}
    assert {
        ("sf:field:settings__c.enabled__c", "reads"),
        ("sf:field:account.name", "reads"),
        ("sf:field:account.id", "filters"),
        ("sf:object:account", "writes"),
        ("sf:apexmethod:worker.prepare", "calls"),
        ("sf:apexmethod:worker.normalize", "calls"),
    } <= by_target
    assert all(item["path"] == "classes/Worker.cls" for edge in edges for item in edge["evidence"])
    incoming, = query.refs(bundle, constructor_id)["results"]
    assert incoming["relation"] == "contains" and incoming["evidence"][0]["rule"] == "apex.constructor_owner"
    launch, = query.deps(bundle, "Launcher.run")["results"]
    assert (launch["target"], launch["relation"]) == ("sf:apexclass:worker", "references")
    assert launch["evidence"][0]["rule"] == "apex.constructed_class"
    assert {d["location"]["line"] for d in store.diagnostics if d["code"] == "APEX_CONSTRUCTOR_DELEGATION_UNSUPPORTED"} == {2, 4}


def test_duplicate_and_malformed_constructors_do_not_generate_body_facts():
    duplicate = """public class Duplicate {
      public Duplicate(Id key) { insert new Account(Name='First'); }
      public Duplicate(Id item) { insert new Account(Name='Second'); }
    }"""
    malformed = """public class Malformed {
      public Other() { insert new Account(Name='Different name'); }
      public static Malformed() { insert new Account(Name='Static'); }
    }"""
    collision = """public class Collision {
      public void new() {}
      public Collision() { insert new Account(Name='Collision'); }
    }"""
    store = build([("Duplicate", duplicate), ("Malformed", malformed), ("Collision", collision)])
    constructor_id = component_id("apexmethod", "Duplicate.new(Id)")
    assert store.components[constructor_id]["definition_status"] == "ambiguous"
    assert not any(edge["source"] == constructor_id or edge["target"] == constructor_id
                   for edge in store.relationships.values())
    assert "sf:apexmethod:collision.new" in store.components
    assert component_id("apexmethod", "Collision.new()") not in store.components
    assert "sf:object:account" not in store.components
    assert {item["code"] for item in store.diagnostics} == {
        "APEX_CONSTRUCTOR_DECLARATION_UNSUPPORTED", "APEX_CONSTRUCTOR_AMBIGUOUS", "APEX_CONSTRUCTOR_ID_AMBIGUOUS"}


def test_initializers_and_inner_types_keep_separate_diagnostics():
    source = """public class Worker {
      static { insert new Account(Name='Static initializer'); }
      { insert new Account(Name='Instance initializer'); }
      public class Inner { public Inner() { insert new Account(Name='Inner'); } }
      public Worker() {}
    }"""
    store = build([("Worker", source)])
    assert {item["api_name"] for item in store.components.values() if item.get("is_constructor")} == {"Worker.new()"}
    assert {item["code"] for item in store.diagnostics} == {"APEX_CLASS_INITIALIZER_UNSUPPORTED", "APEX_INNER_TYPE_UNSUPPORTED"}
    assert "sf:object:account" not in store.components


def test_constructor_does_not_enter_method_invocable_or_ui_lookup():
    store = FactStore()
    index = index_apex({"classes/Worker.cls": b"""public class Worker {
      public Worker() {}
      @InvocableMethod public static void run(List<Id> keys) {}
    }"""}, store)
    extract_apex(index, store)
    attach_flow(store, "Worker")
    resolve_invocable_calls(store, index)
    extract_ui_apex({"pages/Fixture.page": b'<apex:page controller="Worker" action="{!new}"/>'}, index, store)
    extract_lwc({"lwc/fixture/fixture.js": b'import init from "@salesforce/apex/Worker.new"; init();'}, index, store)
    calls = {edge["target"] for edge in store.relationships.values() if edge["relation"] == "calls"}
    assert calls == {"sf:apexclass:worker", "sf:apexmethod:worker.run"}
    assert {item["code"] for item in store.diagnostics} == {
        "VISUALFORCE_APEX_TARGET_UNRESOLVED", "LWC_APEX_TARGET_UNRESOLVED"}
