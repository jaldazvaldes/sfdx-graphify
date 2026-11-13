import pytest

from graphify.salesforce import apex_source_check, cli
from graphify.salesforce.apex_source_check import check_apex_lines
from graphify.salesforce.schema import DataError


def test_apex_line_checks_comments_mixed_lines_and_string_tokens_once(tmp_path, monkeypatch):
    relative = "classes/Example.cls"
    path = tmp_path / relative
    path.parent.mkdir()
    path.write_bytes(
        b"class Example {\r\n"
        b"    void apply(Order order) {\r\n"
        b"        /* old code\r\n"
        b"        order.Status = 'Cancelled';\r\n"
        b"\r\n"
        b"        */\r\n"
        b"        order.Status = '/* data */'; // comment\r\n"
        b"        String text = '// data';\r\n"
        b"\r\n"
        b"        // order.Status = 'Draft';\r\n"
        b"    }\r\n"
        b"}\r\n"
    )
    parse_source = apex_source_check._parse_source
    parses = []

    def count_parse(parser, data):
        parses.append(data)
        return parse_source(parser, data)

    monkeypatch.setattr(apex_source_check, "_parse_source", count_parse)
    requested = [7, 4, 5, 6, 8, 9, 10, 1, 12, 13, 4]
    citations = [f"{relative}:{line}" for line in requested]
    citations[-1] = citations[-1].replace("/", "\\")
    rows = check_apex_lines(tmp_path, citations)
    assert [(row["path"], row["line"]) for row in rows] == [(relative, line) for line in requested]
    assert [row["state"] for row in rows] == [
        "mixed", "comment", "comment", "comment", "code", "blank", "comment",
        "code", "code", "unknown", "comment",
    ]
    assert len(parses) == 1


def test_apex_line_checks_fail_closed_when_syntax_or_file_is_unavailable(tmp_path):
    (tmp_path / "Invalid.cls").write_bytes(b"public class Invalid { void bad( {")
    (tmp_path / "NonUtf8.cls").write_bytes(b"\xff")
    (tmp_path / "Example.trigger").write_text("trigger Example on Account (before insert) {}", encoding="utf-8")
    rows = check_apex_lines(tmp_path, ["Invalid.cls:1", "Missing.cls:1", "NonUtf8.cls:1", "Example.trigger:1"])
    assert [row["state"] for row in rows] == ["unknown", "unknown", "unknown", "code"]
    assert all(row["reason"] for row in rows[:3])


def test_cli_reports_unverified_reason_and_preserves_other_citations(tmp_path, capsys):
    (tmp_path / "Example.cls").write_text("class Example {}\n// old example\n", encoding="utf-8")
    command = ["check-apex", "--source", str(tmp_path), "Example.cls:2"]
    assert cli.main(command) == 0
    assert capsys.readouterr().out.strip() == "Example.cls:2 comment"

    assert cli.main([*command, "Missing.cls:1"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Example.cls:2 comment"
    assert lines[1].startswith("Missing.cls:1 unknown (Apex source is unavailable")


@pytest.mark.parametrize("citation", ["../Example.cls:1", "/Example.cls:1", "C:/Example.cls:1",
                                       r"..\Example.cls:1", r"\\host\share\Example.cls:1",
                                       "Example.cls:0", "Example.cls", "Example.xml:1"])
def test_apex_line_checks_reject_unsafe_paths_and_bad_citations(tmp_path, citation):
    with pytest.raises(DataError):
        check_apex_lines(tmp_path, [citation])
