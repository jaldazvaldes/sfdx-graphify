"""Synthetic class references and verified local custom-setting receivers."""
from __future__ import annotations

import hashlib

import pytest

from graphify.salesforce.schema import component_id
from tests.salesforce.test_apex_review_fixes import build


def setting(name, kind, *, package="force-app"):
    body = f"<customSettingsType>{kind}</customSettingsType>" if kind else ""
    return (f"{package}/objects/{name}/{name}.object-meta.xml", f"<CustomObject>{body}</CustomObject>")


def occurrences(store, rule):
    return [item for item in store.evidence.values() if item["rule"] == rule]


def test_custom_setting_calls_read_local_settings_and_keep_accessor_arguments():
    code = """public class Reader {
      public static void run(Contact row) {
        Boolean flag = (Switches__c.getInstance()).Enabled__c;
        flag = Switches__c.getOrgDefaults().Enabled__c;
        flag = Switches__c.getValues(row.OwnerId).Enabled__c;
        flag = Preferences__c.getInstance(row.LastName).Enabled__c;
        flag = Preferences__c.getValues(row.LastName).Enabled__c;
      }
    }"""
    store = build([("Reader", code)], metadata=[setting("Switches__c", "Hierarchy"), setting("Preferences__c", "List")])
    fields = occurrences(store, "apex.custom_setting_field")
    assert {item["line"] for item in fields} == {3, 4, 5, 6, 7}
    assert all(item["content_hash"] == hashlib.sha256(code.encode()).hexdigest() for item in fields)
    assert {store.relationships[item["relationship_id"]]["target"] for item in fields} == {
        "sf:field:switches__c.enabled__c", "sf:field:preferences__c.enabled__c"}
    assert {store.relationships[item["relationship_id"]]["target"]
            for item in occurrences(store, "apex.custom_setting_accessor")} == {
        "sf:object:switches__c", "sf:object:preferences__c"}
    assert {item["target"] for item in store.relationships.values() if item["relation"] == "reads"} >= {
        "sf:field:contact.ownerid", "sf:field:contact.lastname"}
    assert not store.diagnostics


@pytest.mark.parametrize("metadata,access,extension,extra,classes", [
    ([], "getInstance()", "", "", []),
    ([setting("Switches__c", None)], "getInstance()", "", "", []),
    ([setting("Switches__c", "List")], "getInstance()", "", "", []),
    ([setting("Switches__c", "List")], "getOrgDefaults()", "", "", []),
    ([setting("Switches__c", "Hierarchy")], "getInstance(null, null)", "", "", []),
    ([setting("Switches__c", "Hierarchy")], "getInstance()", "", "Object Switches__c;", []),
    ([setting("Switches__c", "Hierarchy")], "getInstance()", "extends Base", "", [
        ("Base", "public virtual class Base { public static Object Switches__c { get; set; } }")]),
    ([setting("Switches__c", "Hierarchy")], "getInstance()", "", "", [("Switches__c", "public class Switches__c {}")]),
    ([setting("Switches__c", "Hierarchy"), setting("Switches__c", "Hierarchy", package="other")],
     "getInstance()", "", "", []),
])
def test_custom_setting_access_requires_unshadowed_receiver_valid_arity_and_unique_metadata(
        metadata, access, extension, extra, classes):
    code = f"public class Reader {extension} {{ {extra} public void run() {{ Boolean flag = Switches__c.{access}.Enabled__c; }} }}"
    store = build([("Reader", code), *classes], metadata=metadata)
    assert not occurrences(store, "apex.custom_setting_field")
    assert not occurrences(store, "apex.custom_setting_accessor")
    assert "sf:field:switches__c.enabled__c" not in store.components


def test_custom_setting_lexical_shadow_does_not_hide_unshadowed_reads_elsewhere():
    code = """public class Reader { public void run() {
      { Object Switches__c = null; Boolean unknown = Switches__c.getInstance().Enabled__c; }
      Boolean known = Switches__c.getInstance().Enabled__c;
    } }"""
    store = build([("Reader", code)], metadata=[setting("Switches__c", "Hierarchy")])
    assert [item["line"] for item in occurrences(store, "apex.custom_setting_field")] == [3]


def test_metadata_proven_local_setting_name_is_resolved_through_one_absent_base():
    code = """public class Reader extends ExternalDomain { public void run(Id userId) {
      Boolean globalFlag = Switches__c.getInstance().Enabled__c;
      Boolean userFlag = Switches__c.getInstance(userId).Enabled__c;
      { Object Switches__c = null; Boolean unknown = Switches__c.getInstance().Enabled__c; }
      Boolean managed = vendor__Switches__c.getInstance().Enabled__c;
    } }"""
    metadata = [setting("Switches__c", "Hierarchy"), setting("vendor__Switches__c", "Hierarchy")]
    store = build([("Reader", code)], metadata=metadata)
    assert sorted(item["line"] for item in occurrences(store, "apex.custom_setting_field")) == [2, 3]


def test_construction_references_include_explicit_receivers_and_async_arguments():
    code = """public class Launcher {
      public void run(Account row) {
        Worker local = new Worker(row.Name);
        new Worker(row.Name).prepare();
        System.enqueueJob(new Worker(row.Name));
        Database.executeBatch(new SampleBatch());
      }
    }"""
    worker = """public class Worker implements Queueable {
      public Worker(String label) {}
      public void prepare() {}
      public void execute(QueueableContext context) {}
    }"""
    batch = """public class SampleBatch implements Database.Batchable<SObject> {
      public Database.QueryLocator start(Database.BatchableContext context) {
        return Database.getQueryLocator([SELECT Id FROM Account]);
      }
      public void execute(Database.BatchableContext context, List<SObject> scope) {}
      public void finish(Database.BatchableContext context) {}
    }"""
    store = build([("Launcher", code), ("Worker", worker), ("SampleBatch", batch)])
    references = occurrences(store, "apex.constructed_class")
    assert {item["line"] for item in references} == {3, 4, 5, 6}
    assert all(item["content_hash"] == hashlib.sha256(code.encode()).hexdigest() for item in references)
    assert {store.relationships[item["relationship_id"]]["target"] for item in references} == {
        "sf:apexclass:worker", "sf:apexclass:samplebatch"}
    assert all(store.relationships[item["relationship_id"]]["relation"] == "references" for item in references)
    outbound = [edge for edge in store.relationships.values() if edge["source"] == "sf:apexmethod:launcher.run"]
    assert {edge["target"] for edge in outbound if edge["relation"] == "calls"} == {
        "sf:apexmethod:worker.prepare", "sf:apexmethod:worker.execute", "sf:apexmethod:samplebatch.start",
        "sf:apexmethod:samplebatch.execute", "sf:apexmethod:samplebatch.finish",
    }
    assert {edge["target"] for edge in outbound if edge["relation"] == "reads"} == {"sf:field:account.name"}


@pytest.mark.parametrize("type_name,classes,metadata,extra", [
    ("Missing", [], [], ""),
    ("Broken", [("Broken", "public class Broken { invalid(")], [], ""),
    ("Worker", [("Worker", "public class Worker {}"), ("other/Worker", "public class Worker {}")], [], ""),
    ("Worker", [("Worker", "public class Worker {}")], [], "public interface Worker {}"),
    ("Record__c", [("Record__c", "public class Record__c {}")], [setting("Record__c", None)], ""),
    ("Account", [("Account", "public class Account {}")], [], ""),
])
def test_construction_does_not_choose_between_ambiguous_or_unverified_types(type_name, classes, metadata, extra):
    code = f"public class Launcher {{ public void run() {{ Object item = new {type_name}(); }} {extra} }}"
    store = build([("Launcher", code), *classes], metadata=metadata)
    assert not occurrences(store, "apex.constructed_class")
    assert any(item["code"] == "APEX_CONSTRUCTOR_TYPE_UNRESOLVED" for item in store.diagnostics)


def test_same_arity_overloads_require_exact_lexical_argument_types_or_casts():
    caller = """public class Caller {
      private void pick(Id key) {}
      private void pick(Set<Id> keys) {}
      private Id dynamicValue() { return null; }
      public void run(Id key, Set<Id> keys, Object unknown) {
        pick(key);
        pick(keys);
        pick((Id) unknown);
        pick(null);
        pick(dynamicValue());
        pick(unknown);
        Selector.pick(key);
        Selector.pick(keys);
        Selector.pick(null);
        new Worker().pick(key);
        new Worker().pick((Set<Id>) unknown);
        new Worker().pick(null);
      }
    }"""
    selector = """public class Selector {
      public static void pick(Id key) {}
      public static void pick(Set<Id> keys) {}
    }"""
    worker = selector.replace("class Selector", "class Worker").replace("static ", "")
    store = build([("Caller", caller), ("Selector", selector), ("Worker", worker)])
    selections = {item["line"]: (store.relationships[item["relationship_id"]]["target"],
                                  item["resolution"]["overload_resolution"]["arguments"][0]["basis"])
                  for item in store.evidence.values() if "overload_resolution" in item["resolution"]}
    expected = {
        6: ("Caller.pick(Id)", "lexical_declaration"),
        7: ("Caller.pick(Set<Id>)", "lexical_declaration"),
        8: ("Caller.pick(Id)", "explicit_cast"),
        12: ("Selector.pick(Id)", "lexical_declaration"),
        13: ("Selector.pick(Set<Id>)", "lexical_declaration"),
        15: ("Worker.pick(Id)", "lexical_declaration"),
        16: ("Worker.pick(Set<Id>)", "explicit_cast"),
    }
    assert selections == {line: (component_id("apexmethod", signature), basis)
                          for line, (signature, basis) in expected.items()}
    unresolved = {item["location"]["line"]: item for item in store.diagnostics
                  if item["code"] == "APEX_CALL_UNRESOLVED"}
    assert set(unresolved) == {9, 10, 11, 14, 17}
    assert unresolved[14]["signatures"] == ["Selector.pick(Id)", "Selector.pick(Set<Id>)"]
    assert unresolved[17]["signatures"] == ["Worker.pick(Id)", "Worker.pick(Set<Id>)"]
    assert all(item["state"] == "ambiguous" for item in unresolved.values())


def test_same_arity_sobject_overloads_resolve_lexical_types_and_explicit_casts():
    caller = """public class Caller {
      void run(Account row, List<Sample__c> customRows, SObject unknown) {
        Selector.pick(row);
        Selector.pick((Contact) unknown);
        Selector.pick(customRows);
        Selector.pick(unknown);
        Selector.pick(null);
      }
    }"""
    selector = """public class Selector {
      public static void pick(Account row) {}
      public static void pick(Contact row) {}
      public static void pick(List<Sample__c> rows) {}
    }"""
    store = build([("Caller", caller), ("Selector", selector)], metadata=[setting("Sample__c", None)])
    selections = {
        item["line"]: store.relationships[item["relationship_id"]]["target"]
        for item in store.evidence.values()
        if item["resolution"].get("overload_resolution", {}).get("basis") == "exact_sobject_argument_types"
    }
    assert selections == {
        3: component_id("apexmethod", "Selector.pick(Account)"),
        4: component_id("apexmethod", "Selector.pick(Contact)"),
        5: component_id("apexmethod", "Selector.pick(List<Sample__c>)"),
    }
    assert {item["location"]["line"] for item in store.diagnostics
            if item["code"] == "APEX_CALL_UNRESOLVED"} == {6, 7}


@pytest.mark.parametrize("type_name,inner_type,metadata", [
    ("Account", "public class Account {}", []),
    ("Sample__c", "", []),
    ("Sample__c", "", [setting("Sample__c", None), setting("Sample__c", None, package="other-app")]),
])
def test_sobject_overloads_require_unshadowed_standard_types_or_unique_object_definitions(
        type_name, inner_type, metadata):
    caller = f"public class Caller {{ void run({type_name} row) {{ Selector.pick(row); }} }}"
    selector = (f"public class Selector {{ {inner_type} public static void pick({type_name} row) {{}} "
                "public static void pick(Contact row) {} }")
    store = build([("Caller", caller), ("Selector", selector)], metadata=metadata)
    assert not any(edge["relation"] == "calls" for edge in store.relationships.values())
    assert any(item["code"] == "APEX_CALL_UNRESOLVED" and item["state"] == "ambiguous"
               for item in store.diagnostics)


@pytest.mark.parametrize("selector,extra", [
    ("private static void pick(Id key) {} public static void pick(Set<Id> keys) {}", []),
    ("public static void pick(Id key) {} public static void pick(Id other) {} public static void pick(Set<Id> keys) {}", []),
    ("public static void pick(Id key) {} public static void pick(Set<Id> keys) {}", [("Id", "public class Id {}")]),
])
def test_overload_proof_does_not_select_inaccessible_duplicate_or_shadowed_type(selector, extra):
    caller = "public class Caller { public void run(Id key) { Selector.pick(key); } }"
    store = build([("Caller", caller), ("Selector", "public class Selector {" + selector + "}"), *extra])
    assert not any(edge["relation"] == "calls" for edge in store.relationships.values())
    assert any(item["code"] == "APEX_CALL_UNRESOLVED" and item["state"] == "ambiguous"
               for item in store.diagnostics)


def test_trigger_uses_exact_argument_types_without_guessing_untyped_overload():
    selector = "public class Selector { public static void pick(Id key) {} public static void pick(Set<Id> keys) {} }"
    trigger = """trigger Sample on Account (after update) {
      Id key = Trigger.new[0].Id;
      Selector.pick(key);
      Selector.pick(null);
    }"""
    store = build([("Selector", selector)], trigger=trigger)
    selected, = occurrences(store, "trigger.local_method_call")
    assert store.relationships[selected["relationship_id"]]["target"] == component_id("apexmethod", "Selector.pick(Id)")
    assert selected["resolution"]["overload_resolution"]["arguments"][0]["location"]["line"] == 2
    diagnostic, = [item for item in store.diagnostics if item["code"] == "TRIGGER_CALL_UNRESOLVED"]
    assert diagnostic["signatures"] == ["Selector.pick(Id)", "Selector.pick(Set<Id>)"]
