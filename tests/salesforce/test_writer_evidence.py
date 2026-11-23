"""Field-writer citations and exact local Flow statuses in one CLI query."""
import hashlib

from graphify.salesforce import cli, snapshot


def test_writer_evidence_scopes_source_and_reports_exact_flow_status(tmp_path, monkeypatch, capsys):
    active = "force-app/flows/Set_Name.flow-meta.xml"
    stale = "force-app/flows/Older_Name.flow-meta.xml"
    missing = "force-app/flows/Missing_Status.flow-meta.xml"
    invalid = "force-app/flows/Invalid_Status.flow-meta.xml"
    apex = "force-app/classes/UpdateName.cls"
    sources = {
        active: b'<Flow xmlns="http://soap.sforce.com/2006/04/metadata">\n'
                b'  <start><status>IgnoreNested</status></start>\n'
                b'  <status>Active</status>\n</Flow>',
        stale: b"<Flow><status>Draft</status></Flow>",
        missing: b"<Flow><status/>\n<status>Draft</status>\n</Flow>",
        invalid: b"<Flow><status>Active</Flow>",
        apex: b"class UpdateName {}",
    }
    for path, body in sources.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    hashes = {path: hashlib.sha256(body).hexdigest() for path, body in sources.items()}
    (tmp_path / stale).write_bytes(b"<Flow><status>Obsolete</status></Flow>")
    focus = {"id": "sf:field:account.name", "kind": "field", "api_name": "Account.Name"}
    result = {"snapshot_id": "snapshot:example", "component": focus, "freshness": {"state": "unknown"},
              "coverage": {"status": "partial"}, "truncated": True,
              "activity_policy": {"mode": "exclude_explicitly_inactive"}, "results": []}
    for path, name, line, kind, effect in ((active, "set_name", 2, "flow", "database"),
                                           (active, "set_name", 4, "flow", "database"),
                                           (stale, "older_name", 72, "flow", "database"),
                                           (missing, "missing_status", 1, "flow", "database"),
                                           (invalid, "invalid_status", 1, "flow", "database"),
                                           (apex, "updatename", 1, "apexclass", "memory")):
        result["results"].append({"source": f"sf:{kind}:{name}", "target": focus["id"], "relation": "writes",
                                  "qualifiers": {"effect": effect}, "evidence": [{"path": path, "line": line}]})
    monkeypatch.setattr(snapshot, "load", lambda path: {"manifest": {"inputs": hashes}})

    def dispatch(_bundle, command, name, **options):
        assert command == "refs" and name == focus["api_name"]
        assert options["source"] is None
        return result.copy()

    monkeypatch.setattr(cli.query, "dispatch", dispatch)
    argv = ["refs", "Account.Name", "--relation", "writes", "--format", "evidence",
            "--flow-status", "--snapshot", "unused"]
    assert cli.main(argv) == 1
    assert "--source <checkout>" in capsys.readouterr().err
    assert cli.main([*argv, "--source", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    rows = output.splitlines()
    status = f'Declared Flow status: "Active" ({active}:3)'
    assert [row for row in rows if row.startswith(active)] == [
        f"{active}:2 writes [database] | {status}",
        f"{active}:4 writes [database] | {status}",
    ]
    assert f"{apex}:1 writes [memory]" in rows
    assert "IgnoreNested" not in output
    assert f"{stale}:72" not in output and "source differs from the snapshot" in output
    assert (f"{missing}:1 writes [database] | Declared Flow status: unknown "
            "(no unique, nonempty top-level status)") in rows
    assert f"{invalid}:1 writes [database] | Declared Flow status: unknown (invalid XML)" in rows
    assert "Snapshot excludes explicitly inactive Flows" in output
    assert "Result limit reached" in output
