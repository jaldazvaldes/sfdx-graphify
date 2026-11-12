"""Literal metadata hints survive a snapshot round trip without changing graph evidence."""

from copy import deepcopy

import pytest

from graphify.salesforce import cli, query
from graphify.salesforce.schema import DataError, validate_bundle
from graphify.salesforce.service import build_project
from graphify.salesforce.snapshot import load


def test_literal_mentions_are_scoped_and_distinguished_from_dependencies(tmp_path, monkeypatch):
    from graphify.salesforce import blind_spots

    source = tmp_path / "source"
    source.mkdir()
    (source / "sfdx-project.json").write_text(
        '{"packageDirectories":[{"path":"force-app","default":true}]}', encoding="utf-8")

    def put(relative, text):
        path = source / "force-app" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    for owner in ("Account", "Contact"):
        put(f"objects/{owner}/fields/Code__c.field-meta.xml",
            "<CustomField><fullName>Code__c</fullName><type>Text</type></CustomField>")
    put("layouts/Example.layout-meta.xml",
        "<Layout><field>Account.Code__c</field><field>Code__c</field>"
        "<field>Contact.Code__c</field><field>Code__c_Extra</field></Layout>")
    put("pages/Example.page", "<apex:page>{!current.Code__c} {!ACCOUNT.CODE__C}</apex:page>")
    put("reports/Oversized.report-meta.xml", "Account.Code__c " * 50)
    put("notes.txt", "Account.Code__c")
    monkeypatch.setattr(blind_spots, "_MAX_FILE_BYTES", 256)

    output = tmp_path / "snapshot"
    build_project(source, out=output)
    source.rename(tmp_path / "moved-source")
    bundle = load(output)
    result = query.refs(bundle, "Account.Code__c", relation="reads")
    assert result["results"] == []
    hints = result["literal_blind_spots"]
    assert hints["scanned_file_count"] == 2
    assert hints["skipped_file_count"] == 1
    assert hints["qualified"] == {
        "literal": "Account.Code__c", "occurrence_count": 2, "file_count": 2,
        "example_paths": ["force-app/layouts/Example.layout-meta.xml", "force-app/pages/Example.page"],
    }
    assert hints["field_name"] == {
        "literal": "Code__c", "occurrence_count": 2, "file_count": 2,
        "example_paths": hints["qualified"]["example_paths"],
    }
    other = query.refs(bundle, "Contact.Code__c")["literal_blind_spots"]
    assert other["qualified"]["occurrence_count"] == 1
    assert "review hints" in cli._text(result)
    assert "force-app/pages/Example.page" in cli._text(result)

    legacy = {**bundle, "manifest": {key: value for key, value in bundle["manifest"].items()
                                    if key != "literal_blind_spots"}}
    assert query.refs(legacy, "Account.Code__c")["literal_blind_spots"]["state"] == "unavailable"

    invalid = deepcopy(bundle)
    sample = invalid["manifest"]["literal_blind_spots"]["qualified"]["account.code__c"]["example_paths"]
    sample[0] = "force-app/layouts/Absent.layout-meta.xml"
    with pytest.raises(DataError, match="Invalid literal metadata match summary"):
        validate_bundle(invalid)


def test_literal_mentions_cover_additional_metadata_and_methods(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sfdx-project.json").write_text(
        '{"packageDirectories":[{"path":"force-app","default":true}]}', encoding="utf-8")

    def put(relative, text):
        path = source / "force-app" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    put("objects/Account/fields/Code__c.field-meta.xml",
        "<CustomField><fullName>Code__c</fullName><type>Text</type></CustomField>")
    put("classes/DemoController.cls", "public class DemoController { public static void save() {} }")
    for name in ("objects/Account/listViews/Sample.listView-meta.xml",
                 "customMetadata/Setting.Example.md-meta.xml", "objects/Account/webLinks/Help.webLink-meta.xml",
                 "sharingRules/Account.sharingRules-meta.xml", "objects/Account/fieldSets/Sample.fieldSet-meta.xml"):
        put(name, "<Metadata>Account.Code__c DemoController.save</Metadata>")
    put("lwc/card/card.html", '<template data-field="Account.Code__c" data-action="DemoController.save">{save}</template>')
    put("lwc/card/card.js", "import CODE from '@salesforce/schema/Account.Code__c';")

    bundle = build_project(source, out=tmp_path / "snapshot")
    field = query.refs(bundle, "Account.Code__c")
    assert field["literal_blind_spots"]["qualified"]["file_count"] == 7
    assert field["literal_blind_spots"]["scanned_file_count"] == 7

    method = query.refs(bundle, "DemoController.save")
    hints = method["literal_blind_spots"]
    assert hints["scanned_file_count"] == 2
    assert hints["qualified"]["occurrence_count"] == 1
    assert hints["method_name"]["example_paths"] == ["force-app/lwc/card/card.html"]
    assert {row["relation"] for row in method["results"]} == {"contains"}
    assert "method name" in cli._text(method)

    legacy = deepcopy(bundle)
    del legacy["manifest"]["literal_blind_spots"]["methods"]
    assert query.refs(legacy, "DemoController.save")["literal_blind_spots"]["state"] == "unavailable"


def test_apex_string_hints_exclude_comments_and_remain_separate_from_graph_facts(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sfdx-project.json").write_text(
        '{"packageDirectories":[{"path":"force-app"}]}', encoding="utf-8")

    def put(relative, text):
        path = source / "force-app" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    put("objects/Account/fields/Code__c.field-meta.xml",
        "<CustomField><fullName>Code__c</fullName><type>Text</type></CustomField>")
    put("classes/DynamicFields.cls", """public class DynamicFields {
        public static void lookup() {
            // Account.Code__c Code__c
            String qualified = 'Account.Code__c';
            String unrelated = 'Code__c_Extra';
        }
        private class Nested {
            public void inspect() { String qualified = 'Account.Code__c'; }
        }
    }""")
    put("triggers/Example.trigger",
        "trigger Example on Account (before insert) { String fieldName = 'Code__c'; }")
    put("classes/Unparsed.cls", "public class Unparsed { String x = 'Account.Code__c';")

    output = tmp_path / "snapshot"
    build_project(source, out=output)
    bundle = load(output)
    result = query.refs(bundle, "Account.Code__c", relation="reads")
    assert result["results"] == []
    hints = result["literal_blind_spots"]
    assert hints["qualified"]["occurrence_count"] == 0
    assert hints["apex_strings"] == {
        "scanned_file_count": 2,
        "skipped_file_count": 1,
        "qualified": {"literal": "Account.Code__c", "occurrence_count": 2, "file_count": 1,
                      "example_paths": ["force-app/classes/DynamicFields.cls"]},
        "field_name": {"literal": "Code__c", "occurrence_count": 1, "file_count": 1,
                       "example_paths": ["force-app/triggers/Example.trigger"]},
    }
    assert "Apex string hints: 2 qualified, 1 field-name matches" in cli._text(result, brief=True)
    del bundle["manifest"]["literal_blind_spots"]["apex_strings"]
    assert "apex_strings" not in query.refs(bundle, "Account.Code__c")["literal_blind_spots"]
