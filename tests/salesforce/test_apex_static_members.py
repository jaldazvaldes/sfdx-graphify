"""Class references from proven static field and property access."""

import hashlib

from tests.salesforce.test_apex_review_fixes import build


def static_occurrences(store):
    return [row for row in store.evidence.values() if row["rule"] == "apex.static_member_reference"]


def test_static_members_reference_the_declaring_class_from_methods_and_triggers():
    holder = """public class FlagHolder {
      public static Integer First = 1, Second = 2;
      public static Account Row;
      private static Integer local;
      public static Integer own() { return FlagHolder.local; }
    }"""
    reader = """public class Reader {
      public static void run() {
        Integer value = FlagHolder.First;
        FlagHolder.Second += 2;
        ++FlagHolder.Second;
        String name = FlagHolder.Row.Name;
      }
    }"""
    trigger = "trigger Sample on Account (before insert) { FlagHolder.Second = 7; }"
    store = build([("FlagHolder", holder), ("Reader", reader)], trigger=trigger)
    rows = static_occurrences(store)
    assert len(rows) == 6
    assert {store.relationships[row["relationship_id"]]["target"] for row in rows} == {"sf:apexclass:flagholder"}
    assert {store.relationships[row["relationship_id"]]["relation"] for row in rows} == {"references"}
    assert {row["source_component"] for row in rows} == {
        "sf:apexmethod:flagholder.own", "sf:apexmethod:reader.run", "sf:apextrigger:sample",
    }
    assert {row["line"] for row in rows if row["source_component"] == "sf:apexmethod:reader.run"} == {3, 4, 5, 6}
    assert {row["resolution"]["member"] for row in rows} == {"First", "Second", "Row", "local"}
    for row in rows:
        site = row["resolution"]["member_declaration"]
        assert site["path"] == "classes/FlagHolder.cls"
        assert site["content_hash"] == hashlib.sha256(holder.encode()).hexdigest()
        assert site["locator"].endswith(f"/variable_declarator[@name={row['resolution']['member'].casefold()}]")


def test_static_members_respect_visibility_and_property_accessors():
    holder = """public class FlagHolder {
      public static Integer ReadOnly { get; private set; }
      public static Integer WriteOnly { set; }
      private static Integer PrivateValue;
      public static final Integer Constant = 10;
      public Integer InstanceValue;
      public static void own() { FlagHolder.ReadOnly = 5; }
    }"""
    reader = """public class Reader {
      public static void run() {
        Integer value = FlagHolder.ReadOnly;
        FlagHolder.ReadOnly = 1;
        FlagHolder.ReadOnly += 2;
        FlagHolder.WriteOnly = 3;
        value = FlagHolder.WriteOnly;
        value = FlagHolder.PrivateValue;
        value = FlagHolder.InstanceValue;
        value = FlagHolder.Constant;
        FlagHolder.Constant = 6;
        value = FlagHolder.Missing;
      }
    }"""
    store = build([("FlagHolder", holder), ("Reader", reader)])
    rows = static_occurrences(store)
    assert {(row["source_component"], row["line"]) for row in rows} == {
        ("sf:apexmethod:flagholder.own", 7),
        ("sf:apexmethod:reader.run", 3), ("sf:apexmethod:reader.run", 6), ("sf:apexmethod:reader.run", 10),
    }
    assert {row["location"]["line"] for row in store.diagnostics
            if row["code"] == "APEX_STATIC_MEMBER_UNRESOLVED"} == {4, 5, 7, 8, 9, 11, 12}


def test_static_member_proof_rejects_shadowing_ambiguity_and_unavailable_classes():
    reader = """public class Reader {
      public class HiddenType {}
      public void local(Account FlagHolder) { String name = FlagHolder.Name; }
      public void initializer() { Object FlagHolder = FlagHolder.First; }
      public void unsupported() {
        Object value = Unparsed.First;
        value = Duplicated.First;
        value = BadMembers.First;
        value = FlagChild.First;
        value = HiddenType.First;
        value = FlagHolder?.First;
      }
    }"""
    members = "public virtual class FlagHolder { public static Integer First; }"
    shadow = "public virtual class Shadow { public Account FlagHolder { get; set; } }"
    store = build([
        ("Reader", reader),
        ("FlagHolder", members),
        ("FlagChild", "public class FlagChild extends FlagHolder {}"),
        ("HiddenType", members.replace("FlagHolder", "HiddenType")),
        ("BadMembers", "public class BadMembers { public static Integer First, first; }"),
        ("Unparsed", "public class Unparsed {"),
        ("one/Duplicated", members.replace("FlagHolder", "Duplicated")),
        ("two/Duplicated", members.replace("FlagHolder", "Duplicated")),
        ("Shadow", shadow),
        ("ChildReader", "public class ChildReader extends Shadow { public Integer run() { return FlagHolder.First; } }"),
        ("ExternalReader", "public class ExternalReader extends MissingBase { public Integer run() { return FlagHolder.First; } }"),
    ])
    assert not static_occurrences(store)
    assert any(edge["source"] == "sf:apexmethod:reader.local" and edge["target"] == "sf:field:account.name"
               and edge["relation"] == "reads" for edge in store.relationships.values())
    assert {(row["location"]["line"], row["state"]) for row in store.diagnostics
            if row["code"] == "APEX_STATIC_MEMBER_UNRESOLVED"} == {
        (6, "unresolved"), (7, "ambiguous"), (8, "ambiguous"), (9, "unresolved"),
    }
