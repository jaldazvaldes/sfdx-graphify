"""Qualified calls from local inheritance chains, using fictional Apex only."""
import hashlib

import pytest

from graphify.salesforce.apex import extract_apex, index_apex
from graphify.salesforce.inventory import index_definitions
from graphify.salesforce.schema import FactStore
from graphify.salesforce.triggers import extract_triggers, index_triggers
from graphify.salesforce.xml_source import parse_xml


def analyze(classes, *, trigger=None, namespace=True):
    store = FactStore()
    if namespace:
        index_definitions([parse_xml(
            "objects/vendor__Settings__c/vendor__Settings__c.object-meta.xml", b"<CustomObject/>")], store)
    source = {path: text.encode() for path, text in classes.items()}
    index = index_apex(source, store)
    extract_apex(index, store)
    if trigger:
        triggers = index_triggers({"triggers/Example.trigger": trigger.encode()}, [], index, store)
        extract_triggers(triggers, index, store)
    return store


def fixtures(*, base="public virtual class Base {}", middle="", member="", parameters="List<Account> rows",
             body="Checks.run(); Database.update(rows, false); vendor.Worker.run();"):
    return {
        "classes/Base.cls": base,
        **({"classes/Middle.cls": middle} if middle else {}),
        "classes/Handler.cls": f"public class Handler extends {'Middle' if middle else 'Base'} "
                               f"{{ {member} public void execute({parameters}) {{ {body} }} }}",
        "classes/Checks.cls": "public class Checks { public static void run() {} }",
    }


def targets(store, source="sf:apexmethod:handler.execute"):
    return {edge["target"] for edge in store.relationships.values()
            if edge["source"] == source and edge["relation"] == "calls"}


def database_writes(store, source="sf:apexmethod:handler.execute"):
    return [edge for edge in store.relationships.values()
            if edge["source"] == source and edge["relation"] == "writes"
            and edge["qualifiers"].get("effect") == "database"]


def test_complete_local_chain_resolves_static_database_and_managed_calls():
    classes = fixtures(middle="public virtual class Middle extends Base {}")
    store = analyze(classes)
    assert targets(store) == {"sf:apexmethod:checks.run", "sf:apexmethod:vendor.worker.run"}
    edge, = database_writes(store)
    assert edge["target"] == "sf:object:account"
    assert edge["qualifiers"]["operation"] == "update"
    for edge in store.relationships.values():
        if edge["source"] != "sf:apexmethod:handler.execute":
            continue
        for eid in edge["evidence_ids"]:
            evidence = store.evidence[eid]
            assert evidence["path"] == "classes/Handler.cls"
            assert evidence["content_hash"] == hashlib.sha256(classes[evidence["path"]].encode()).hexdigest()
    assert store.components["sf:apexmethod:vendor.worker.run"]["definition_status"] == "referenced_only"


def test_trigger_handler_helper_second_hop_and_database_effect_are_connected():
    store = analyze({
        "classes/Base.cls": "public virtual class Base { public void dispatch() { beforeInsert(); } "
                            "protected virtual void beforeInsert() {} }",
        "classes/Handler.cls": "public class Handler extends Base { protected override void beforeInsert() "
                               "{ Checks.normalize(); } }",
        "classes/Checks.cls": "public class Checks { public static void normalize() { Sink.persist(); } }",
        "classes/Sink.cls": "public class Sink extends Base { public static void persist() "
                            "{ Database.update(new Account(Name='Fictional')); vendor.Worker.run(); } }",
    }, trigger="trigger Example on Account(before insert) { Base handler = new Handler(); handler.dispatch(); }")
    assert "sf:apexmethod:handler.beforeinsert" in targets(store, "sf:apextrigger:example")
    assert targets(store, "sf:apexmethod:handler.beforeinsert") == {"sf:apexmethod:checks.normalize"}
    assert targets(store, "sf:apexmethod:checks.normalize") == {"sf:apexmethod:sink.persist"}
    assert targets(store, "sf:apexmethod:sink.persist") == {"sf:apexmethod:vendor.worker.run"}
    assert {edge["target"] for edge in database_writes(store, "sf:apexmethod:sink.persist")} == {
        "sf:object:account", "sf:field:account.name"}


@pytest.mark.parametrize("member", [
    "public Object Checks; public Object Database; public Object vendor;",
    "public Object Checks {get;set;} public Object Database {get;set;} public Object vendor {get;set;}",
    "public class Checks {} public class Database {} public class vendor {}",
])
@pytest.mark.parametrize("level", ["self", "parent", "grandparent"])
def test_members_properties_and_inner_types_anywhere_in_chain_shadow_qualifiers(member, level):
    store = analyze(fixtures(
        member=member if level == "self" else "",
        base=f"public virtual class Base {{ {member if level in {'parent', 'grandparent'} else ''} }}",
        middle="public virtual class Middle extends Base {}" if level == "grandparent" else "",
    ))
    assert not targets(store)
    assert not database_writes(store)


@pytest.mark.parametrize("parameters,body", [
    ("List<Account> rows, Object Checks, Object Database, Object vendor",
     "Checks.run(); Database.update(rows, false); vendor.Worker.run();"),
    ("List<Account> rows", "Object Checks; Object Database; Object vendor; "
     "Checks.run(); Database.update(rows, false); vendor.Worker.run();"),
    ("List<Account> rows", "Object Checks; Object Checks; Object Database; Object Database; Object vendor; Object vendor; "
     "Checks.run(); Database.update(rows, false); vendor.Worker.run();"),
])
def test_local_or_ambiguous_bindings_still_shadow_qualifiers(parameters, body):
    store = analyze(fixtures(parameters=parameters, body=body))
    assert not targets(store)
    assert not database_writes(store)


@pytest.mark.parametrize("scenario", ["missing", "external", "parse_error", "duplicate", "cycle"])
def test_unavailable_or_ambiguous_ancestor_never_proves_a_qualified_receiver(scenario):
    classes = fixtures()
    if scenario == "missing":
        del classes["classes/Base.cls"]
    elif scenario == "external":
        classes["classes/Base.cls"] = "public virtual class Base extends vendor.ExternalBase {}"
    elif scenario == "parse_error":
        classes["classes/Base.cls"] = "public virtual class Base { invalid Apex"
    elif scenario == "duplicate":
        classes["second/Base.cls"] = classes["classes/Base.cls"]
    elif scenario == "cycle":
        classes["classes/Base.cls"] = "public virtual class Base extends Handler {}"
    store = analyze(classes)
    if scenario in {"missing", "external"}:
        assert targets(store) == {"sf:apexmethod:checks.run"}
        edge, = [edge for edge in store.relationships.values() if edge["relation"] == "calls"]
        assert edge["qualifiers"]["resolution_status"] == "candidate"
    else:
        assert not targets(store)
    assert not database_writes(store)
    assert any(d["code"] == "APEX_EXTERNAL_CALL_UNSUPPORTED" for d in store.diagnostics)


def test_namespace_requires_local_corroboration_even_in_complete_hierarchy():
    store = analyze(fixtures(), namespace=False)
    assert targets(store) == {"sf:apexmethod:checks.run"}
    assert len(database_writes(store)) == 1
    assert "sf:apexmethod:vendor.worker.run" not in store.components


@pytest.mark.parametrize("qualifier", ["Database", "vendor"])
def test_local_top_level_type_masks_builtin_or_namespace(qualifier):
    classes = fixtures()
    classes[f"classes/{qualifier}.cls"] = f"public class {qualifier} {{}}"
    store = analyze(classes)
    if qualifier == "Database":
        assert not database_writes(store)
    else:
        assert "sf:apexmethod:vendor.worker.run" not in targets(store)


def test_static_target_overload_ambiguity_is_not_relaxed_by_known_caller_hierarchy():
    classes = fixtures(body="Checks.run(null);")
    classes["classes/Checks.cls"] = (
        "public class Checks { public static void run(Account row) {} public static void run(Contact row) {} }")
    store = analyze(classes)
    assert not targets(store)


def test_target_static_method_can_be_inherited_when_both_chains_are_known():
    classes = fixtures(body="Checks.run();")
    classes["classes/Checks.cls"] = "public class Checks extends CheckBase {}"
    classes["classes/CheckBase.cls"] = "public virtual class CheckBase { public static void run() {} }"
    store = analyze(classes)
    assert targets(store) == {"sf:apexmethod:checkbase.run"}


def test_database_dynamic_query_diagnostics_are_retained_from_a_subclass():
    store = analyze(fixtures(body="Database.query('SELECT Id FROM Account');"))
    assert any(d["code"] == "APEX_DYNAMIC_SOQL_UNSUPPORTED" for d in store.diagnostics)
    assert not targets(store)
    assert not database_writes(store)
