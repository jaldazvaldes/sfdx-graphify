"""Incomplete external ancestry yields explicit candidates, never proven calls."""
import hashlib

import pytest

from graphify.salesforce.apex import extract_apex, index_apex
from graphify.salesforce.schema import FactStore


def analyze(*, base=None, middle=None, member="", parameters="Account row",
            body="Helper.normalize(row.Name);", helper=None, extras=None):
    classes = {
        "classes/Handler.cls": (
            f"public class Handler extends {'Middle' if middle else 'ExternalBase'} {{ {member}\n"
            f"public void execute({parameters}) {{\n{body}\n}} }}"
        ),
        "classes/Helper.cls": helper or "public class Helper { public static void normalize(String value) {} }",
        **(extras or {}),
    }
    if base is not None:
        classes["classes/ExternalBase.cls"] = base
    if middle is not None:
        classes["classes/Middle.cls"] = middle
    source = {path: value.encode() for path, value in classes.items()}
    store = FactStore()
    index = index_apex(source, store)
    extract_apex(index, store)
    return store, source


def calls(store):
    return [edge for edge in store.relationships.values()
            if edge["source"] == "sf:apexmethod:handler.execute" and edge["relation"] == "calls"]


@pytest.mark.parametrize("middle,missing", [
    (None, "ExternalBase"),
    ("public virtual class Middle extends ExternalBase {}", "ExternalBase"),
    ("public virtual class Middle extends package.ExternalBase {}", "package.ExternalBase"),
])
def test_absent_ancestor_candidate_preserves_local_target_argument_read_and_evidence(middle, missing):
    store, source = analyze(middle=middle)
    edge, = calls(store)
    assert edge["target"] == "sf:apexmethod:helper.normalize"
    assert edge["qualifiers"] == {
        "invocation": "static_method", "resolution_status": "candidate",
        "uncertainty": "external_hierarchy", "missing_ancestors": [missing],
    }
    proof, = [store.evidence[eid] for eid in edge["evidence_ids"]]
    assert proof["rule"] == "apex.candidate_static_call"
    assert proof["resolution"]["basis"] == "local_static_target_unverified_hierarchy"
    assert proof["resolution"]["missing_ancestors"] == [missing]
    assert proof["line"] == 3
    assert proof["content_hash"] == hashlib.sha256(source[proof["path"]]).hexdigest()
    assert any(r["target"] == "sf:field:account.name" and r["relation"] == "reads"
               for r in store.relationships.values())
    diagnostic, = [d for d in store.diagnostics if d["code"] == "APEX_CALL_CANDIDATE"]
    assert diagnostic["state"] == "ambiguous"
    assert diagnostic["candidates"] == [edge["target"]]
    assert diagnostic["missing_ancestors"] == [missing]
    assert not any(d["code"] == "APEX_EXTERNAL_CALL_UNSUPPORTED" for d in store.diagnostics)


def test_known_base_keeps_original_proven_call_identity_and_resolution():
    store, _ = analyze(base="public virtual class ExternalBase {}")
    edge, = calls(store)
    assert edge["qualifiers"] == {"invocation": "static_method"}
    proof = store.evidence[edge["evidence_ids"][0]]
    assert proof["rule"] == "apex.cross_class_static_call"
    assert proof["resolution"] == {"basis": "unique_local_static_method"}
    assert not any(d["code"] == "APEX_CALL_CANDIDATE" for d in store.diagnostics)


@pytest.mark.parametrize("member", [
    "public Object Helper;", "public Object Helper { get; set; }", "public class Helper {}",
])
@pytest.mark.parametrize("level", ["self", "parent"])
def test_known_members_properties_or_types_block_candidates(member, level):
    store, _ = analyze(member=member if level == "self" else "",
                       middle=f"public virtual class Middle extends ExternalBase {{ {member} }}"
                       if level == "parent" else None)
    assert not calls(store)


@pytest.mark.parametrize("parameters,body", [
    ("Account row, Object Helper", "Helper.normalize(row.Name);"),
    ("Account row", "Object Helper; Helper.normalize(row.Name);"),
    ("Account row", "Object Helper; Object Helper; Helper.normalize(row.Name);"),
    ("Account row", "for (Account Helper : new List<Account>()) Helper.normalize(row.Name);"),
])
def test_lexical_bindings_including_ambiguous_and_loop_aliases_block_candidates(parameters, body):
    store, _ = analyze(parameters=parameters, body=body)
    assert not calls(store)


@pytest.mark.parametrize("base,extras", [
    ("public virtual class ExternalBase { invalid Apex", None),
    ("public virtual class ExternalBase extends Handler {}", None),
    ("public virtual class ExternalBase {}", {"other/ExternalBase.cls": "public virtual class ExternalBase {}"}),
    ("public interface ExternalBase {}", None),
])
def test_local_unsupported_duplicate_or_cyclic_base_is_not_treated_as_external(base, extras):
    store, _ = analyze(base=base, extras=extras)
    assert not calls(store)
    assert not any(d["code"] == "APEX_CALL_CANDIDATE" for d in store.diagnostics)


@pytest.mark.parametrize("helper", [
    "public class Helper { private static void normalize(String value) {} }",
    "public class Helper { public void normalize(String value) {} }",
    "public class Helper { public static void normalize() {} }",
    "public class Helper { public static void normalize(String value) {} public static void normalize(Account value) {} }",
    "public class Helper { public static void normalize(Account value) {} public static void normalize(Account value) {} public static void normalize(String value) {} }",
    "public class Helper extends UnavailableHelperBase { public static void normalize(String value) {} }",
    "public class Helper { invalid Apex",
])
def test_target_must_be_unique_accessible_static_and_have_complete_hierarchy(helper):
    store, _ = analyze(helper=helper)
    assert not calls(store)


def test_duplicate_target_class_is_not_a_candidate():
    store, _ = analyze(extras={"other/Helper.cls": "public class Helper { public static void normalize(String value) {} }"})
    assert not calls(store)


def test_global_static_target_in_known_target_base_is_supported():
    store, _ = analyze(helper="public class Helper extends LocalHelperBase {}", extras={
        "classes/LocalHelperBase.cls": "global virtual class LocalHelperBase { global static void normalize(String value) {} }",
    })
    edge, = calls(store)
    assert edge["target"] == "sf:apexmethod:localhelperbase.normalize"
    assert edge["qualifiers"]["resolution_status"] == "candidate"


def test_unqualified_super_namespace_and_database_calls_are_not_relaxed():
    store, _ = analyze(body="normalize(row.Name); super.normalize(row.Name); vendor.Helper.normalize(row.Name); Database.update(row);")
    assert not calls(store)
    assert not any(d["code"] == "APEX_CALL_CANDIDATE" for d in store.diagnostics)
    assert not any(r["qualifiers"].get("effect") == "database" for r in store.relationships.values())


def test_repeated_candidate_occurrences_preserve_each_location_and_all_argument_diagnostics():
    store, _ = analyze(body="Helper.normalize(row.Name);\nHelper.normalize(row.Name);\nHelper.normalize(unknown.Name);")
    edge, = calls(store)
    assert len(edge["evidence_ids"]) == 3
    assert {store.evidence[eid]["line"] for eid in edge["evidence_ids"]} == {3, 4, 5}
    assert any(d["code"] == "APEX_SYMBOL_UNRESOLVED" and d["location"]["line"] == 5 for d in store.diagnostics)
    assert len([d for d in store.diagnostics if d["code"] == "APEX_CALL_CANDIDATE"]) == 3
