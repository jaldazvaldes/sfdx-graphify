"""Synthetic Apex dispatch through declared fields."""
from __future__ import annotations

import hashlib

import pytest

from graphify.salesforce.schema import component_id
from tests.salesforce.test_apex_review_fixes import build


def member_calls(store):
    return [item for item in store.evidence.values() if item["rule"] == "apex.declared_member_call"]


def test_proven_instance_static_and_inherited_fields_keep_call_and_declaration_evidence():
    base = """public virtual class HolderBase {
      protected Worker worker;
      public static Worker shared;
    }"""
    holder = """public class Holder extends HolderBase {
      public void run(Id key, Account row) {
        worker.prepare(key);
        this.worker.prepare(key);
        worker.save(row.Name);
        { Worker worker = new Worker(); worker.save(row.Name); this.worker.prepare(key); }
      }
      public static void runStatic(Id key) { shared.prepare(key); }
    }"""
    worker_base = """public virtual class WorkerBase {
      public void prepare(Id key) {}
      public void prepare(Set<Id> keys) {}
      public void save(String name) {}
    }"""
    store = build([("HolderBase", base), ("Holder", holder), ("WorkerBase", worker_base),
                   ("Worker", "public class Worker extends WorkerBase {}")])
    evidence = {item["line"]: item for item in member_calls(store)}
    assert set(evidence) == {3, 4, 5, 6, 8}
    for line, item in evidence.items():
        assert item["content_hash"] == hashlib.sha256(holder.encode()).hexdigest()
        proof = item["resolution"]
        assert proof["receiver_type"] == "sf:apexclass:worker"
        declaration = proof["member_declaration"]
        assert declaration["path"] == "classes/HolderBase.cls"
        assert declaration["content_hash"] == hashlib.sha256(base.encode()).hexdigest()
        assert declaration["line"] == (3 if line == 8 else 2)
        edge = store.relationships[item["relationship_id"]]
        assert edge["qualifiers"] == {"invocation": "declared_member_method"}
        if line == 5:
            assert edge["target"] == "sf:apexmethod:workerbase.save"
        else:
            assert edge["target"] == component_id("apexmethod", "WorkerBase.prepare(Id)")
            assert proof["overload_resolution"]["basis"] == "exact_builtin_argument_types"
    assert any(item["rule"] == "apex.constructed_instance_call" and item["line"] == 6
               for item in store.evidence.values())
    assert any(edge["target"] == "sf:field:account.name" and edge["relation"] == "reads"
               for edge in store.relationships.values())
    assert not any(item["code"] == "APEX_FIELD_PATH_UNSUPPORTED" for item in store.diagnostics)


@pytest.mark.parametrize("caller,base,state", [
    ("public class Holder { Worker worker; public static void run() { worker.run(); } }", None, "unresolved"),
    ("public class Holder { static Worker worker; public void run() { this.worker.run(); } }", None, "unresolved"),
    ("public class Holder { Worker worker; Worker worker; public void run() { worker.run(); } }", None, "ambiguous"),
    ("public class Holder { Worker worker { get; set; } public void run() { worker.run(); } }", None, "unresolved"),
    ("public class Holder extends HolderBase { public void run() { worker.run(); } }",
     "public virtual class HolderBase { private Worker worker; }", "unresolved"),
    ("public class Holder { Worker worker; public class Worker {} public void run() { worker.run(); } }",
     None, "unresolved"),
])
def test_member_dispatch_rejects_inaccessible_ambiguous_and_shadowed_declarations(caller, base, state):
    classes = [("Holder", caller), ("Worker", "public class Worker { public void run() {} }")]
    if base is not None:
        classes.append(("HolderBase", base))
    store = build(classes)
    assert not member_calls(store)
    assert any(item["code"] == "APEX_MEMBER_CALL_UNRESOLVED" and item["state"] == state
               for item in store.diagnostics)


@pytest.mark.parametrize("worker,diagnostic", [
    ("public virtual class Worker { public virtual void run() {} }", "APEX_MEMBER_CALL_VIRTUAL_UNRESOLVED"),
    ("public abstract class Worker { public abstract void run(); }", "APEX_MEMBER_CALL_VIRTUAL_UNRESOLVED"),
    ("public interface Worker { void run(); }", "APEX_MEMBER_CALL_UNRESOLVED"),
    ("public class Worker { public static void run() {} }", "APEX_MEMBER_CALL_UNRESOLVED"),
    ("public class Worker { private void run() {} }", "APEX_MEMBER_CALL_UNRESOLVED"),
])
def test_member_dispatch_requires_a_visible_nonvirtual_local_instance_method(worker, diagnostic):
    caller = "public class Holder { Worker worker; public void run() { worker.run(); } }"
    store = build([("Holder", caller), ("Worker", worker)])
    assert not member_calls(store)
    assert any(item["code"] == diagnostic for item in store.diagnostics)


def test_nearest_ordinary_field_shadows_an_inherited_member_with_the_same_name():
    parent = "public virtual class HolderBase { protected Worker inherited, local; }"
    caller = """public class Holder extends HolderBase {
      Worker local;
      public void run() { local.run(); inherited.run(); }
    }"""
    worker = "public class Worker { public void run() {} }"
    store = build([("HolderBase", parent), ("Holder", caller), ("Worker", worker)])
    assert {item["resolution"]["member_declaration"]["path"] for item in member_calls(store)} == {
        "classes/HolderBase.cls", "classes/Holder.cls"}


def test_same_named_local_initializer_requires_explicit_this_to_bind_to_member():
    caller = """public class Holder {
      Worker worker;
      public void bare() { Worker worker = worker.copy(); }
      public void explicit() { Worker worker = this.worker.copy(); }
    }"""
    worker = "public class Worker { public Worker copy() { return null; } }"
    store = build([("Holder", caller), ("Worker", worker)])
    evidence, = member_calls(store)
    assert evidence["line"] == 4
