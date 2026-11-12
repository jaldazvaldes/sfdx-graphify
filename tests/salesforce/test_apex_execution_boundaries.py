"""Synthetic SOQL, inner-class, async and platform-event extraction contracts."""
from tests.salesforce.test_apex_review_fixes import build


def edges_from(store, source, *, rule=None):
    return [
        edge for edge in store.relationships.values()
        if edge["source"] == source
        and (rule is None or any(store.evidence[key]["rule"] == rule for key in edge["evidence_ids"]))
    ]


def test_custom_child_relationship_and_where_joins_use_their_own_object():
    source = """public class Reader {
      public void run(Account bound) {
        List<Account> rows = [
          SELECT Id, (SELECT Result__c FROM Inspections__r WHERE Score__c > 0)
          FROM Account WHERE Id IN (SELECT AccountId FROM Contact WHERE Name = :bound.Name)
          AND Id NOT IN (SELECT AccountId FROM Case WHERE IsClosed = false)
        ];
      }
    }"""
    metadata = [
        ("objects/Inspection__c/fields/Parent__c.field-meta.xml",
         "<CustomField><type>MasterDetail</type><referenceTo>Account</referenceTo>"
         "<relationshipName>Inspections</relationshipName></CustomField>"),
    ]
    store = build([("Reader", source)], metadata=metadata)
    edges = edges_from(store, "sf:apexmethod:reader.run")
    fields = {(edge["target"], edge["relation"], edge["qualifiers"].get("query_context"))
              for edge in edges if edge["target"].startswith("sf:field:")}
    assert fields == {
        ("sf:field:account.id", "reads", None),
        ("sf:field:account.id", "filters", None),
        ("sf:field:account.name", "reads", None),
        ("sf:field:inspection__c.parent__c", "reads", None),
        ("sf:field:inspection__c.result__c", "reads", "child_subquery"),
        ("sf:field:inspection__c.score__c", "filters", "child_subquery"),
        ("sf:field:contact.accountid", "reads", "semi_join"),
        ("sf:field:contact.name", "filters", "semi_join"),
        ("sf:field:case.accountid", "reads", "anti_join"),
        ("sf:field:case.isclosed", "filters", "anti_join"),
    }
    navigation, = edges_from(store, "sf:apexmethod:reader.run", rule="apex.static_soql_child_navigation")
    evidence = store.evidence[navigation["evidence_ids"][0]]
    assert evidence["resolution"]["schema_source"]["path"] == metadata[0][0]
    assert evidence["line"] == 4
    assert not store.diagnostics


def test_ambiguous_child_relationship_and_unavailable_constructor_remain_unresolved():
    code = """public class Reader extends Missing {
      Reader() { super(); }
      void run() { List<Account> rows = [SELECT (SELECT Id FROM Ambiguous__r) FROM Account]; }
    }"""
    metadata = [
        (f"objects/{owner}/fields/Account__c.field-meta.xml",
         "<CustomField><type>Lookup</type><referenceTo>Account</referenceTo>"
         "<relationshipName>Ambiguous</relationshipName></CustomField>")
        for owner in ("Alpha__c", "Beta__c")
    ]
    store = build([("Reader", code)], metadata=metadata)
    assert {(d["code"], d["state"]) for d in store.diagnostics} == {
        ("APEX_SOQL_CHILD_RELATIONSHIP_UNRESOLVED", "ambiguous"),
        ("APEX_CONSTRUCTOR_DELEGATION_UNRESOLVED", "unresolved"),
    }
    assert {e["target"] for e in edges_from(store, "sf:apexmethod:reader.run")} == {"sf:object:account"}


def test_inner_types_own_their_methods_and_resolve_explicit_construction():
    classes = [
        ("Outer", """public class Outer {
          void run() { new Worker().work(); }
          public class Worker {
            public Worker() {}
            public void work() { insert new Account(Name = 'Synthetic'); }
          }
        }"""),
        ("Caller", """public class Caller {
          void run() { new Outer.Worker().work(); Type workerType = Outer.Worker.class; }
        }"""),
    ]
    store = build(classes)
    inner, work = "sf:apexclass:outer.worker", "sf:apexmethod:outer.worker.work"
    assert store.components[inner]["namespace"] is None
    assert store.components[inner]["enclosing_class"] == "sf:apexclass:outer"
    assert store.components[work]["namespace"] is None
    assert store.components[work]["enclosing_class"] == inner
    assert {(edge["target"], edge["relation"]) for edge in edges_from(store, inner)} == {
        (work, "contains"), ("sf:apexmethod:outer.worker.new%28%29", "contains")}
    for source in ("sf:apexmethod:outer.run", "sf:apexmethod:caller.run"):
        assert {(edge["target"], edge["relation"]) for edge in edges_from(store, source)} == {
            (work, "calls"), (inner, "references")}
    assert any(e["target"] == "sf:object:account" for e in edges_from(store, work))
    assert not store.diagnostics


def test_async_dispatch_resolves_contract_entries_including_enqueue_this():
    classes = [
        ("Launcher", """public class Launcher {
          public void run() {
            System.enqueueJob(new QueueWorker());
            System.schedule('test', '0 0 1 * * ?', new Scheduler());
            BatchWorker worker = new BatchWorker();
            Database.executeBatch(worker, 25);
          }
        }"""),
        ("QueueWorker", """public class QueueWorker implements System.Queueable {
          public void execute(System.QueueableContext context) {}
          public void requeue() { System.enqueueJob(this); }
        }"""),
        ("Scheduler", """public class Scheduler implements Schedulable {
          public void execute(System.SchedulableContext context) {}
          public void execute(String other) {}
        }"""),
        ("BatchWorker", """public class BatchWorker implements Database.Batchable<Account> {
          public Database.QueryLocator start(Database.BatchableContext context) {
            return Database.getQueryLocator([SELECT Id FROM Account]);
          }
          public void execute(Database.BatchableContext context, List<Account> records) {}
          public void finish(Database.BatchableContext context) {}
        }"""),
    ]
    store = build(classes)
    dispatches = edges_from(store, "sf:apexmethod:launcher.run", rule="apex.async_dispatch")
    assert {(edge["target"], edge["qualifiers"]["async_api"]) for edge in dispatches} == {
        ("sf:apexmethod:queueworker.execute", "System.enqueueJob"),
        ("sf:apexmethod:scheduler.execute%28system.schedulablecontext%29", "System.schedule"),
        ("sf:apexmethod:batchworker.start", "Database.executeBatch"),
        ("sf:apexmethod:batchworker.execute", "Database.executeBatch"),
        ("sf:apexmethod:batchworker.finish", "Database.executeBatch"),
    }
    requeue, = edges_from(store, "sf:apexmethod:queueworker.requeue", rule="apex.async_dispatch")
    assert requeue["target"] == "sf:apexmethod:queueworker.execute"
    assert not store.diagnostics


def test_async_dispatch_does_not_assume_a_loop_carried_job_keeps_its_initial_type():
    launcher = """public class Launcher {
      public void run() {
        Queueable job = new FirstJob();
        for (Integer i = 0; i < 2; i++) {
          if (i > 0) System.enqueueJob(job);
          job = new SecondJob();
        }
      }
    }"""
    worker = "public class {name} implements Queueable {{ public void execute(QueueableContext context) {{}} }}"
    store = build([("Launcher", launcher)] + [(name, worker.format(name=name)) for name in ("FirstJob", "SecondJob")])
    assert not edges_from(store, "sf:apexmethod:launcher.run", rule="apex.async_dispatch")
    unresolved, = [d for d in store.diagnostics if d["code"] == "APEX_ASYNC_TARGET_UNRESOLVED"]
    assert unresolved["location"]["line"] == 5


def test_trigger_submits_a_batch_and_queueable_and_publishes_an_event():
    trigger = """trigger Sample on Account (after insert) {
      BatchWorker batch = new BatchWorker();
      Database.executeBatch(batch);
      System.enqueueJob(new QueueWorker());
      EventBus.publish(new Notice__e[]{new Notice__e()});
    }"""
    classes = [
        ("BatchWorker", """public class BatchWorker implements Database.Batchable<Account> {
          public Database.QueryLocator start(Database.BatchableContext context) { return null; }
          public void execute(Database.BatchableContext context, List<Account> scope) {}
          public void finish(Database.BatchableContext context) {}
        }"""),
        ("QueueWorker", """public class QueueWorker implements Queueable {
          public void execute(QueueableContext context) {}
        }"""),
    ]
    store = build(classes, trigger=trigger)
    jobs = edges_from(store, "sf:apextrigger:sample", rule="apex.async_dispatch")
    assert {edge["target"] for edge in jobs} == {
        "sf:apexmethod:batchworker.start", "sf:apexmethod:batchworker.execute",
        "sf:apexmethod:batchworker.finish", "sf:apexmethod:queueworker.execute",
    }
    publication, = edges_from(store, "sf:apextrigger:sample", rule="apex.platform_event_publish")
    assert publication["target"] == "sf:object:notice__e"
    assert publication["qualifiers"]["collection"] is True
    assert not any(d["code"].startswith(("APEX_ASYNC_", "APEX_PLATFORM_")) for d in store.diagnostics)


def test_publication_preserves_payload_writes_and_reports_unproven_targets():
    source = """public class Sender {
      void run(List<Notice__e> batch, SObject unknown) {
        EventBus.publish(batch);
        System.EventBus.publish(new Notice__e(Payload__c = 'Synthetic'));
        EventBus.publish(unknown);
      }
    }"""
    store = build([("Sender", source)])
    published = edges_from(store, "sf:apexmethod:sender.run", rule="apex.platform_event_publish")
    assert {(e["target"], e["relation"], e["qualifiers"]["collection"]) for e in published} == {
        ("sf:object:notice__e", "writes", True), ("sf:object:notice__e", "writes", False)}
    payload, = edges_from(store, "sf:apexmethod:sender.run", rule="apex.record_initializer")
    assert payload["target"] == "sf:field:notice__e.payload__c"
    assert payload["qualifiers"]["effect"] == "memory"
    assert {d["code"] for d in store.diagnostics} == {"APEX_PLATFORM_EVENT_PUBLISH_UNRESOLVED"}


def test_publication_treats_an_inline_event_array_as_a_collection():
    source = """public class Sender {
      void run() {
        EventBus.publish(new Notice__e[]{new Notice__e()});
        EventBus.publish(new Notice__e[][]{{new Notice__e()}});
      }
    }"""
    store = build([("Sender", source)])
    edge, = edges_from(store, "sf:apexmethod:sender.run", rule="apex.platform_event_publish")
    assert edge["target"] == "sf:object:notice__e"
    assert edge["qualifiers"]["collection"] is True
    occurrence, = [store.evidence[key] for key in edge["evidence_ids"]]
    assert occurrence["line"] == 3
    unresolved, = store.diagnostics
    assert unresolved["code"] == "APEX_PLATFORM_EVENT_PUBLISH_UNRESOLVED"
    assert unresolved["location"]["line"] == 4


def test_inner_class_sees_the_enclosing_static_member_that_shadows_eventbus():
    source = """public class Sender {
      private static final LocalBus EventBus = new LocalBus();
      private class Worker {
        void run(Notice__e notice) { EventBus.publish(notice); }
      }
      private class LocalBus {
        public void publish(Notice__e notice) {}
      }
    }"""
    store = build([("Sender", source)])
    assert not edges_from(store, "sf:apexmethod:sender.worker.run", rule="apex.platform_event_publish")
    unresolved, = [d for d in store.diagnostics if d["location"]["line"] == 4]
    assert unresolved["code"] == "APEX_EXTERNAL_CALL_UNSUPPORTED"


def test_platform_names_shadowed_by_local_classes_or_unavailable_ancestors():
    sender = """public class Sender {
      public void run() { EventBus.publish(new Notice__e(Payload__c = 'Synthetic')); }
    }"""
    event_bus = "public class EventBus { public static void publish(Notice__e item) {} }"
    store = build([("Sender", sender), ("EventBus", event_bus)])
    local, = edges_from(store, "sf:apexmethod:sender.run", rule="apex.cross_class_static_call")
    assert local["target"] == "sf:apexmethod:eventbus.publish"
    assert not edges_from(store, "sf:apexmethod:sender.run", rule="apex.platform_event_publish")
    assert not store.diagnostics

    store = build([("Sender", sender.replace("class Sender", "class Sender extends Missing"))])
    assert not edges_from(store, "sf:apexmethod:sender.run", rule="apex.platform_event_publish")
    assert {d["code"] for d in store.diagnostics} == {"APEX_PLATFORM_RECEIVER_UNRESOLVED"}
