"""Synthetic regressions for parent fields, trigger grammar and handler dispatch."""
import hashlib

import pytest

from graphify.salesforce import query
from graphify.salesforce.apex import extract_apex, index_apex
from graphify.salesforce.inventory import extract_inventory, index_definitions
from graphify.salesforce.schema import DataError, FactStore, component_id
from graphify.salesforce.triggers import extract_triggers, index_triggers
from graphify.salesforce.xml_source import parse_xml


def build(classes=(), *, trigger=None, metadata=()):
    store = FactStore()
    docs = [parse_xml(path, text.encode()) for path, text in metadata]
    index_definitions(docs, store)
    extract_inventory(docs, store)
    index = index_apex({f"classes/{name}.cls": text.encode() for name, text in classes}, store)
    extract_apex(index, store)
    if trigger:
        triggers = index_triggers({"triggers/Sample.trigger": trigger.encode()}, [], index, store)
        extract_triggers(triggers, index, store)
    return store


def calls(store, source=None):
    return [edge for edge in store.relationships.values() if edge["relation"] == "calls"
            and (source is None or edge["source"] == source)]


def lookup(owner="Inspection__c", name="Device__c", targets=("Asset",)):
    return (f"objects/{owner}/fields/{name}.field-meta.xml",
            "<CustomField><type>Lookup</type>" + "".join(f"<referenceTo>{t}</referenceTo>" for t in targets) + "</CustomField>")


def test_parent_soql_path_resolves_terminal_owner_and_retains_chain_evidence():
    code = ("public class Reader { public void read() { List<Inspection__c> rows = "
            "[SELECT Device__r.Product2.Serial__c FROM Inspection__c WHERE Device__r.Product2.Name != null]; } }")
    store = build([("Reader", code)], metadata=[lookup()])
    fields = [edge for edge in store.relationships.values() if edge["source"] == "sf:apexmethod:reader.read"
              and store.components[edge["target"]]["kind"] == "field"
              and edge["qualifiers"].get("context") != "apex_soql_navigation"]
    assert {(store.components[e["target"]]["api_name"], e["relation"]) for e in fields} == {
        ("Product2.Serial__c", "reads"), ("Product2.Name", "filters")}
    evidence = store.evidence[fields[0]["evidence_ids"][0]]
    assert evidence["resolution"]["steps"] == [
        {"field": "sf:field:inspection__c.device__c", "target": "sf:object:asset", "basis": "declared_lookup"},
        {"field": "sf:field:asset.product2id", "target": "sf:object:product2", "basis": "standard_schema"}]
    assert "navigation_step" not in evidence["resolution"]
    assert "sf:field:inspection__c.serial__c" not in store.components
    bundle = {"manifest": {"snapshot_id": "synthetic"}, "facts": store.finish(), "diagnostics": store.diagnostics}
    for step, field_name in enumerate(("Inspection__c.Device__c", "Asset.Product2Id"), 1):
        results = query.refs(bundle, field_name, relation="reads")["results"]
        assert {edge["qualifiers"]["clause"] for edge in results} == {"select", "where"}
        assert len(results) == 2
        for edge in results:
            assert edge["source"] == "sf:apexmethod:reader.read"
            assert edge["qualifiers"]["context"] == "apex_soql_navigation"
            occurrence, = edge["evidence"]
            assert occurrence["content_hash"] == hashlib.sha256(code.encode()).hexdigest()
            assert occurrence["locator"].endswith(f"/navigation[{step}]")
            assert occurrence["resolution"]["navigation_step"] == {"index": step, **evidence["resolution"]["steps"][step - 1]}
        assert {edge["id"] for edge in query.impact(bundle, field_name, depth=1)["results"]} == {
            edge["id"] for edge in results}


def test_trigger_soql_distinguishes_direct_lookup_read_from_repeated_navigation():
    code = ("trigger Sample on Account (after update) {\n"
            "  List<Account> rows = [SELECT ParentId, Parent.Parent.Name FROM Account];\n}")
    store = build(trigger=code)
    bundle = {"manifest": {"snapshot_id": "synthetic"}, "facts": store.finish(), "diagnostics": store.diagnostics}
    results = query.refs(bundle, "Account.ParentId", relation="reads")["results"]
    assert len(results) == 2 and {edge["source"] for edge in results} == {"sf:apextrigger:sample"}
    assert {edge["qualifiers"].get("context") for edge in results} == {None, "apex_soql_navigation"}
    navigation, = [edge for edge in results if edge["qualifiers"].get("context") == "apex_soql_navigation"]
    assert {e["resolution"]["navigation_step"]["index"] for e in navigation["evidence"]} == {1, 2}
    assert len({e["locator"] for e in navigation["evidence"]}) == 2
    assert all(e["line"] == 2 and e["content_hash"] == hashlib.sha256(code.encode()).hexdigest()
               for e in navigation["evidence"])


@pytest.mark.parametrize("data_type,expected", [("Lookup", True), ("Text", False), ("MasterDetail", False)])
def test_standard_parent_partial_metadata_uses_only_a_compatible_verified_lookup(data_type, expected):
    standard = ("objects/Asset/fields/Product2Id.field-meta.xml",
                f"<CustomField><fullName>Product2Id</fullName><type>{data_type}</type>"
                "<lookupFilter><active>true</active></lookupFilter></CustomField>")
    store = build([("Reader", "public class Reader { public void read() { List<Inspection__c> rows = "
                   "[SELECT Device__r.Product2.Serial__c FROM Inspection__c]; } }")], metadata=[lookup(), standard])
    edges = [edge for edge in store.relationships.values() if edge["source"] == "sf:apexmethod:reader.read"
             and edge["target"] == "sf:field:product2.serial__c"]
    assert bool(edges) is expected
    if expected:
        occurrence = store.evidence[edges[0]["evidence_ids"][0]]
        assert occurrence["resolution"]["steps"][1]["basis"] == "standard_schema"


@pytest.mark.parametrize("metadata,path", [([], "Device__r.Name"), ([lookup(targets=("Asset", "Account"))], "Device__r.Name"),
                                            ([], "Owner.Name"), ([lookup()], "Device__r.Unproven__r.Name")])
def test_unresolved_or_polymorphic_path_never_becomes_a_root_field(metadata, path):
    store = build([("Reader", f"public class Reader {{ public void read() {{ List<Inspection__c> rows = [SELECT {path} FROM Inspection__c]; }} }}")],
                  metadata=metadata)
    assert not any(e["source"] == "sf:apexmethod:reader.read" and store.components[e["target"]]["kind"] == "field"
                   for e in store.relationships.values())
    assert any(d["code"] == "APEX_SOQL_FIELD_PATH_UNSUPPORTED" for d in store.diagnostics)


def test_trigger_keyword_statement_normalization_retains_original_hash_and_coordinates():
    source = "trigger Sample on Account (before insert) {\n  // Trigger.new[0].Secret__c = true;\n  Trigger.new[0].Name = 'Trigger.new[0].Fake__c';\n}"
    store = build(trigger=source)
    writes = [e for e in store.relationships.values() if e["relation"] == "writes"]
    assert {e["target"] for e in writes} == {"sf:field:account.name"}
    evidence = store.evidence[writes[0]["evidence_ids"][0]]
    assert evidence["line"] == 3
    assert evidence["column"] == 18
    assert evidence["content_hash"] == hashlib.sha256(source.encode()).hexdigest()
    assert store.components["sf:apextrigger:sample"]["parser_normalized_trigger_context"] is True
    assert not any("fake" in cid or "secret" in cid for cid in store.components)
    assert not any(d["code"] == "TRIGGER_PARSE_ERROR" for d in store.diagnostics)


def test_trigger_keyword_statement_in_apex_class_no_longer_discards_other_dependencies():
    source = "public class Guard { public void check(Id key) { Trigger.newMap.get(key).addError('invalid'); List<Contact> rows = [SELECT LastName FROM Contact]; } }"
    store = build([("Guard", source)])
    assert not any(d["code"] == "APEX_PARSE_ERROR" for d in store.diagnostics)
    assert any(e["target"] == "sf:field:contact.lastname" for e in store.relationships.values())
    assert store.components["sf:apexclass:guard"]["parser_normalized_trigger_context"] is True


def test_header_survives_broken_body_but_no_body_fact_is_recovered():
    store = build(trigger="trigger Sample on Account (before insert, after update) { Unknown.call(; }")
    edges = list(store.relationships.values())
    assert len(edges) == 1
    assert edges[0]["target"] == "sf:object:account"
    assert edges[0]["qualifiers"]["events"] == ["after_update", "before_insert"]
    assert any(d["code"] == "TRIGGER_PARSE_ERROR" for d in store.diagnostics)


def test_overloads_have_signatures_and_trigger_arity_selects_only_one():
    store = build([("Sharing", "public class Sharing { public static void run(List<Account> rows) {} "
                   "public static void run(List<Account> rows, Map<Id,Account> beforeRows) {} }")],
                  trigger="trigger Sample on Account(before update) { Sharing.run(Trigger.new,Trigger.oldMap); }")
    edge, = calls(store, "sf:apextrigger:sample")
    target = store.components[edge["target"]]
    assert target["api_name"] == "Sharing.run(List<Account>,Map<Id,Account>)"
    assert target["arity"] == 2
    assert target["qualified_name"] == "Sharing.run"
    assert target["definition_status"] == "defined"


def test_same_arity_overloads_are_distinct_but_not_resolved_by_argument_guess():
    store = build([("Overloads", "public class Overloads { public static void run(String a) {} public static void run(Integer a) {} }")],
                  trigger="trigger Sample on Account(before update) { Overloads.run(null); }")
    assert not calls(store)
    methods = [c for c in store.components.values() if c["kind"] == "apexmethod"]
    assert {c["api_name"] for c in methods} == {"Overloads.run(String)", "Overloads.run(Integer)"}
    assert all(c["definition_status"] == "defined" for c in methods)


@pytest.mark.parametrize("name", ["Type.run(,)", "Type.run(List<>)", "Type.run(Map<Id,>)", "Type.run(Account[)",
                                    "Type.run(../Secret)", "Type.run(1)", "Type.run(List<Account)"])
def test_malformed_overload_identity_is_rejected(name):
    with pytest.raises(DataError):
        component_id("apexmethod", name)


def test_duplicate_signature_remains_ambiguous_even_with_matching_arity():
    store = build([("Dup", "public class Dup { public static void run(String a) {} public static void run(String b) {} }")],
                  trigger="trigger Sample on Account(before update) { Dup.run(null); }")
    assert not calls(store)
    assert store.components[component_id("apexmethod", "Dup.run(String)")]["definition_status"] == "ambiguous"


def test_method_signature_ignores_comments_inside_generic_parameter_types():
    store = build([("Generic", "public class Generic { public static void run(List</*info*/Account> rows) {} public static void run() {} }")],
                  trigger="trigger Sample on Account(before insert) { Generic.run(Trigger.new); }")
    edge, = calls(store)
    assert store.components[edge["target"]]["api_name"] == "Generic.run(List<Account>)"


def test_class_literal_reaches_handler_without_inventing_framework_execution():
    store = build([("Domain", "public class Domain { public void process() {} }")],
                  trigger="trigger Sample on Account(before insert) { Framework.dispatch(Domain.class); }")
    assert any(e["target"] == "sf:apexclass:domain" and e["qualifiers"].get("context") == "class_literal"
               for e in store.relationships.values())
    assert not calls(store)


def test_namespace_call_is_referenced_only_and_shadowed_receiver_is_not_namespace():
    store = build(trigger="trigger Sample on Account(before insert) { vendor.Auditor.audit(); String hidden='x'; hidden.Auditor.audit(); }",
                  metadata=[("objects/vendor__Setting__c/vendor__Setting__c.object-meta.xml", "<CustomObject/>")])
    edge, = calls(store)
    assert edge["target"] == "sf:apexmethod:vendor.auditor.audit"
    assert store.components[edge["target"]]["definition_status"] == "referenced_only"
    assert store.components[edge["target"]]["namespace"] == "vendor"
    assert not any("hidden.auditor" in cid for cid in store.components)


def test_dotted_field_receivers_are_not_mislabeled_as_managed_namespace_calls():
    store = build(trigger="trigger Sample on Account(before insert) { Integer n=Trigger.new.size(); "
                  "Boolean flag=Label.FeatureFlag.equalsIgnoreCase('true'); Unknown.values.get('x'); }")
    assert not calls(store)
    assert not any(c["api_name"] in {"Trigger.new.size", "Label.FeatureFlag.equalsIgnoreCase", "Unknown.values.get"}
                   for c in store.components.values())


def test_unrelated_inner_type_does_not_shadow_standard_loop_object():
    store = build([("Wrapper", "public class Wrapper { public class Opportunity {} }")],
                  trigger="trigger Sample on Opportunity(before insert) { for(Opportunity row:Trigger.new) { row.Name='A'; } }")
    assert any(e["target"] == "sf:field:opportunity.name" and e["relation"] == "writes" for e in store.relationships.values())
    assert not any(d["code"] == "TRIGGER_LOOP_TYPE_UNRESOLVED" for d in store.diagnostics)


def test_local_instance_calls_and_explicit_constructed_receiver_reach_methods():
    store = build([("Worker", "public class Worker { public void run() { this.work(); } public void work() {} }"),
                  ("Entry", "public class Entry { public void execute() { Worker job = new Worker(); job.run(); } }")])
    pairs = {(e["source"], e["target"]) for e in calls(store)}
    assert ("sf:apexmethod:worker.run", "sf:apexmethod:worker.work") in pairs
    assert ("sf:apexmethod:entry.execute", "sf:apexmethod:worker.run") in pairs


def test_constructed_hierarchy_retains_both_called_hooks_regardless_of_names():
    store = build([("Base", "public virtual class Base { public void dispatch() { beforeInsert(); afterUpdate(); } "
                   "protected virtual void beforeInsert() {} protected virtual void afterUpdate() {} }"),
                  ("Concrete", "public class Concrete extends Base { protected override void beforeInsert() {} "
                   "protected override void afterUpdate() {} }")],
                  trigger="trigger Sample on Account(before insert) { Base runner=new Concrete(); runner.dispatch(); }")
    pairs = {e["target"] for e in calls(store, "sf:apextrigger:sample")}
    assert pairs == {"sf:apexmethod:base.dispatch", "sf:apexmethod:concrete.beforeinsert", "sf:apexmethod:concrete.afterupdate"}
    edge = next(e for e in calls(store, "sf:apextrigger:sample") if e["target"].endswith("beforeinsert"))
    occurrence = store.evidence[edge["evidence_ids"][0]]
    assert occurrence["resolution"]["conditional"] is True
    assert occurrence["resolution"]["dispatch_site"]["path"] == "classes/Base.cls"
    assert edge["qualifiers"]["trigger_events"] == ["before_insert"]
    assert "events" not in edge["qualifiers"]


def test_guarded_virtual_hook_is_a_candidate_without_claiming_branch_execution():
    store = build([("Base", "public virtual class Base { public void dispatch() { if(Trigger.isUpdate) { afterUpdate(); } } "
                   "protected virtual void afterUpdate() {} }"),
                  ("Concrete", "public class Concrete extends Base { protected override void afterUpdate() {} }")],
                  trigger="trigger Sample on Account(before insert) { Base runner=new Concrete(); runner.dispatch(); }")
    edge = next(e for e in calls(store, "sf:apextrigger:sample") if e["target"] == "sf:apexmethod:concrete.afterupdate")
    assert edge["qualifiers"]["trigger_events"] == ["before_insert"]
    assert store.evidence[edge["evidence_ids"][0]]["resolution"]["conditional"] is True


def test_matching_event_method_name_without_base_virtual_invocation_is_not_dispatch():
    store = build([("Base", "public virtual class Base { public void dispatch() {} protected virtual void beforeInsert() {} }"),
                  ("Concrete", "public class Concrete extends Base { protected override void beforeInsert() {} }")],
                  trigger="trigger Sample on Account(before insert) { Base runner=new Concrete(); runner.dispatch(); }")
    assert {e["target"] for e in calls(store, "sf:apextrigger:sample")} == {"sf:apexmethod:base.dispatch"}
