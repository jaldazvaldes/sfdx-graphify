"""Source-only Flow structure and CLI pagination."""
import json

from graphify.salesforce import cli


def test_flow_outline_cli_scopes_names_and_pages(tmp_path, capsys):
    source = tmp_path / "Example.flow-meta.xml"
    source.write_text(
        '<m:Flow xmlns:m="http://soap.sforce.com/2006/04/metadata">\n'
        '<m:decisions><m:name>One</m:name><m:rules><m:name>Nested</m:name></m:rules></m:decisions>'
        '<m:decisions><m:name>Two</m:name></m:decisions>\n'
        '<m:status>Active</m:status><m:start><m:object>Asset</m:object><m:triggerType>RecordBeforeSave</m:triggerType></m:start>\n'
        '</m:Flow>', encoding="utf-8",
    )
    args = ["flow-outline", str(source), "--element", "decisions", "--limit", "1"]
    assert cli.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["path"] == source.as_posix()
    assert first["outline"] == [["decisions", 2, 2, [["name", 2, "One"]]]]
    assert cli.main([*args, "--offset", str(first["next_offset"])]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["outline"] == [["decisions", 2, 2, [["name", 2, "Two"]]]]
    assert "next_offset" not in second
    assert cli.main(["flow-outline", str(source), "--element", "start"]) == 0
    assert json.loads(capsys.readouterr().out)["outline"][0][3] == [
        ["object", 3, "Asset"], ["triggerType", 3, "RecordBeforeSave"],
    ]

    selected = ["flow-outline", str(source), "--element", "start", "decisions", "--limit", "2"]
    assert cli.main(selected) == 0
    page = json.loads(capsys.readouterr().out)
    assert [row[3][0][2] for row in page["outline"]] == ["One", "Two"]
    assert cli.main([*selected, "--offset", str(page["next_offset"])]) == 0
    last = json.loads(capsys.readouterr().out)
    assert [row[0] for row in last["outline"]] == ["start"]
    assert "next_offset" not in last


def test_flow_outline_resolves_api_name_with_exact_citations(tmp_path, monkeypatch, capsys):
    (tmp_path / "sfdx-project.json").write_text(
        json.dumps({"packageDirectories": [{"path": "app"}, {"path": "extra"}]}), encoding="utf-8",
    )
    path = tmp_path / "app" / "main" / "default" / "flows" / "Example.flow-meta.xml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "<Flow>\n"
        "<decisions><name>First</name></decisions>\n"
        "<start>\n"
        "<filterFormula>Example__c = false</filterFormula>\n"
        "<object>Account</object><recordTriggerType>CreateAndUpdate</recordTriggerType>\n"
        "<triggerType>RecordBeforeSave</triggerType>\n"
        "</start>\n"
        "</Flow>",
        encoding="utf-8",
    )
    outside = tmp_path / "Example.flow-meta.xml.bak"
    outside.write_text("<Flow/>", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    args = ["flow-outline", "Example", "--element", "decisions", "start", "--format", "evidence"]
    assert cli.main(args) == 0
    result = capsys.readouterr().out
    relative = path.relative_to(tmp_path).as_posix()
    assert f'{relative}:2 decisions.name = "First"' in result
    assert f'{relative}:4 start.filterFormula = "Example__c = false"' in result
    assert f'{relative}:6 start.triggerType = "RecordBeforeSave"' in result
    assert result.endswith("End of selected evidence.\n")
    assert cli.main([*args, "--limit", "1"]) == 0
    assert "Repeat with --offset 1." in capsys.readouterr().out
    assert cli.main(["flow-outline", "Example.flow-meta.xml", "--element", "start"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["path"] == relative

    duplicate = tmp_path / "extra" / "flows" / path.name
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(path.read_bytes())
    assert cli.main(args) == 1
    error = capsys.readouterr().err
    assert "Ambiguous Flow name" in error and relative in error and "extra/flows/" in error
    assert cli.main(["flow-outline", relative, "--format", "evidence"]) == 0


def test_flow_outline_evidence_marks_clipped_entry_formula(tmp_path, capsys):
    source = tmp_path / "Long.flow-meta.xml"
    source.write_text(f"<Flow>\n<start>\n<filterFormula>{'x' * 2050}</filterFormula>\n</start>\n</Flow>", encoding="utf-8")
    assert cli.main(["flow-outline", str(source), "--format", "evidence"]) == 0
    assert f"Clipped formula at {source.as_posix()}:3-3." in capsys.readouterr().out


def test_flow_outline_cli_rejects_other_metadata_and_invalid_input(tmp_path, capsys):
    source = tmp_path / "invalid.xml"
    for xml in ("<Layout/>", "<Flow><start></Flow>", "<!DOCTYPE Flow><Flow/>"):
        source.write_text(xml, encoding="utf-8")
        assert cli.main(["flow-outline", str(source)]) == 1
        out = capsys.readouterr()
        assert str(source) in out.err and not out.out
