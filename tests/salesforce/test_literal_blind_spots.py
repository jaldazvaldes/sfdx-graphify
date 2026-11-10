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
