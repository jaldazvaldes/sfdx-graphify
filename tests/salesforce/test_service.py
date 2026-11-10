"""End-to-end contracts using only fictional source created in temporary folders."""

from __future__ import annotations

import builtins
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from graphify.salesforce.schema import DataError, FactStore, canonical_json, validate_bundle
from graphify.salesforce.xml_source import parse_xml


FLOW = '''<?xml version="1.0" encoding="UTF-8"?>
<Flow xmlns="http://soap.sforce.com/2006/04/metadata">
  <apiVersion>64.0</apiVersion><processType>AutoLaunchedFlow</processType>
  <label>Sample account flow</label><status>Active</status>
  <start><object>Account</object><triggerType>RecordAfterSave</triggerType>
    <recordTriggerType>Update</recordTriggerType>
    <filters><field>Name</field><operator>EqualTo</operator>
      <value><stringValue>Sample</stringValue></value></filters>
  </start>
  <recordLookups><name>LookupContact</name><object>Contact</object>
    <getFirstRecordOnly>true</getFirstRecordOnly><storeOutputAutomatically>true</storeOutputAutomatically>
    <filters><field>AccountId</field><operator>EqualTo</operator>
      <value><elementReference>$Record.Id</elementReference></value></filters>
    <queriedFields>LastName</queriedFields>
  </recordLookups>
  <assignments><name>CopyName</name><assignmentItems>
    <assignToReference>$Record.Description</assignToReference><operator>Assign</operator>
    <value><elementReference>LookupContact.LastName</elementReference></value>
  </assignmentItems></assignments>
</Flow>'''


def project(tmp_path, name="source"):
    root = tmp_path / name
    root.mkdir()
    (root / "sfdx-project.json").write_text(json.dumps({
        "packageDirectories": [{"path": "force-app", "default": True}], "sourceApiVersion": "64.0",
    }), encoding="utf-8")
    directory = root / "force-app/main/default/flows"
    directory.mkdir(parents=True)
    (directory / "Sample.flow-meta.xml").write_text(FLOW, encoding="utf-8", newline="\n")
    return root


def test_cli_uses_the_saved_snapshot_for_the_current_project(tmp_path, monkeypatch, capsys):
    from graphify.salesforce import cli, viewer

    cache = tmp_path / "private-cache"
    monkeypatch.setenv("LOCALAPPDATA", str(cache))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    root = project(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["refs", "Account.Description"]) == 1
    assert "Run sfdx-graphify build ." in json.loads(capsys.readouterr().out)["error"]

    assert cli.main(["build", "."]) == 0
    built = json.loads(capsys.readouterr().out)
    output = Path(built["output"])
    assert output.is_relative_to(cache)
    assert cli.main(["refs", "Account.Description"]) == 0
    refs = json.loads(capsys.readouterr().out)
    assert refs["snapshot_id"] == built["snapshot_id"]
    assert refs["results"]
    assert refs["freshness"]["state"] == "unknown"

    def check_serve(snapshot_dir, *, port, before):
        assert snapshot_dir == output

    monkeypatch.setattr(viewer, "serve", check_serve)
    assert cli.main(["serve"]) == 0

    monkeypatch.chdir(project(tmp_path, "other-checkout"))
    assert cli.main(["refs", "Account.Description"]) == 1
    assert "No saved snapshot" in json.loads(capsys.readouterr().out)["error"]
    assert cli.main(["refs", "Account.Description", "--snapshot", str(output)]) == 0


def test_full_build_query_deletion_and_stale_source(tmp_path):
    from graphify.salesforce.service import build_project
    from graphify.salesforce import query, snapshot
    root, output = project(tmp_path), tmp_path / "snapshots"
    bundle = build_project(root, out=output)
    assert snapshot.load(output) == bundle
    refs = query.refs(bundle, "Account.Name", relation="filters", source=root)
    assert refs["freshness"]["state"] == "current"
    assert len(refs["results"]) == 1
    assert refs["results"][0]["evidence"][0]["line"] == 7
    assert "SOURCE.json" not in bundle["manifest"]["inputs"]
    assert not list(output.rglob("*.md"))
    flow = root / "force-app/main/default/flows/Sample.flow-meta.xml"
    flow.write_text(FLOW.replace("<field>Name</field>", "<field>Industry</field>"), encoding="utf-8")
    assert query.freshness(bundle, root)["state"] == "stale"
    updated = build_project(root, out=output)
    assert "sf:field:account.name" not in {c["id"] for c in updated["facts"]["components"]}
    flow.unlink()
    empty = build_project(root, out=output)
    assert empty["facts"] == {"components": [], "relationships": [], "evidence": []}
    assert snapshot.load(output) == empty


def test_parse_failure_preserves_previous_snapshot(tmp_path):
    from graphify.salesforce.service import build_project
    from graphify.salesforce.snapshot import load
    root, output = project(tmp_path), tmp_path / "output"
    good = build_project(root, out=output)
    pointer = (output / "CURRENT.json").read_bytes()
    (root / "force-app/main/default/flows/Sample.flow-meta.xml").write_text("<Flow>", encoding="utf-8")
    with pytest.raises(DataError, match="Malformed XML"):
        build_project(root, out=output)
    assert (output / "CURRENT.json").read_bytes() == pointer
    assert load(output) == good


def test_identical_inputs_have_identical_canonical_outputs(tmp_path):
    from graphify.salesforce.service import build_project
    first, second = project(tmp_path, "first"), project(tmp_path, "second")
    a = build_project(first, out=tmp_path / "out-a")
    b = build_project(second, out=tmp_path / "out-b")
    assert canonical_json(a) == canonical_json(b)
    assert build_project(first, out=tmp_path / "out-a") == a
    validate_bundle(a)
    a["manifest"]["coverage"]["analyzed_files"] += 1
    with pytest.raises(DataError, match="Snapshot identity"):
        validate_bundle(a)


def test_inventory_lookups_namespaces_and_duplicate_definitions(tmp_path):
    from graphify.salesforce.service import build_project
    root = project(tmp_path)
    fields = root / "force-app/main/default/objects/pkg__Sample__c/fields"
    fields.mkdir(parents=True)
    (fields / "pkg__Account__c.field-meta.xml").write_text(
        '<CustomField><fullName>pkg__Account__c</fullName><type>Lookup</type>'
        '<referenceTo>Account</referenceTo></CustomField>', encoding="utf-8")
    result = build_project(root, out=tmp_path / "out")
    cid = "sf:field:pkg__sample__c.pkg__account__c"
    component = next(c for c in result["facts"]["components"] if c["id"] == cid)
    assert component["namespace"] == "pkg"
    assert component["definition_status"] == "defined"
    assert any(r["source"] == cid and r["target"] == "sf:object:account"
               and r["relation"] == "references" for r in result["facts"]["relationships"])
    second = root / "force-app/other/flows"
    second.mkdir(parents=True)
    (second / "Sample.flow-meta.xml").write_text(FLOW, encoding="utf-8")
    conflict = build_project(root, out=tmp_path / "out")
    assert any(d["code"] == "duplicate_definition" for d in conflict["diagnostics"])
    assert not any(r["source"] == "sf:flow:sample" for r in conflict["facts"]["relationships"])


@pytest.mark.parametrize("payload", [
    b'<!DOCTYPE Flow [<!ENTITY x "secret">]><Flow>&x;</Flow>',
    b'<!DOCTYPE Flow SYSTEM "https://example.invalid/evil.dtd"><Flow/>',
    b'<Flow>' + b'<nested>' * 129 + b'</nested>' * 129 + b'</Flow>',
])
def test_xml_limits_and_entities_rejected(payload):
    with pytest.raises(DataError):
        parse_xml("flows/Sample.flow-meta.xml", payload)


def test_location_and_evidence_identity_survive_line_shifts():
    from graphify.salesforce.flow import extract_flow
    records = []
    for source in (FLOW, FLOW.replace("  <assignments>", "\n\n  <assignments>")):
        document = parse_xml("flows/Sample.flow-meta.xml", source.encode())
        store = FactStore()
        extract_flow(document, store)
        records.append(store.finish())
    assert [r["id"] for r in records[0]["relationships"]] == [r["id"] for r in records[1]["relationships"]]
    assert [e["id"] for e in records[0]["evidence"]] == [e["id"] for e in records[1]["evidence"]]
    assert {e["content_hash"] for e in records[0]["evidence"]} == {hashlib.sha256(FLOW.encode()).hexdigest()}


def test_all_cli_paths_are_local_with_provider_configuration(tmp_path, monkeypatch, capsys):
    root, output = project(tmp_path), tmp_path / "out"
    classes = root / "force-app/main/default/classes"
    classes.mkdir()
    (classes / "SampleAction.cls").write_text('''public class SampleAction {
      @InvocableMethod public static void run(List<Account> records) {
        List<Contact> contacts = [SELECT Id, LastName FROM Contact WHERE AccountId != null];
        insert new Contact(LastName='Synthetic');
      }
    }''', encoding="utf-8")
    flow = root / "force-app/main/default/flows/Sample.flow-meta.xml"
    flow.write_text(FLOW.replace('</Flow>', '''<actionCalls><name>RunSample</name>
      <actionType>apex</actionType><actionName>SampleAction</actionName></actionCalls></Flow>'''),
      encoding="utf-8")
    existing_semantic_modules = {name for name in sys.modules if name == "graphify.llm"}
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "MOONSHOT_API_KEY",
                 "AWS_ACCESS_KEY_ID", "GRAPHIFY_BACKEND"):
        monkeypatch.setenv(name, "offline-test-sentinel")
    original_run = subprocess.run

    def checked_run(argv, *args, **kwargs):
        assert isinstance(argv, list) and argv[0] == "git", "Unexpected agent or other subprocess"
        assert not kwargs.get("shell"), "Shell execution is forbidden"
        return original_run(argv, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Network access attempted")

    class NetworkDeniedSocket(socket.socket):
        def __init__(self, *args, **kwargs):
            forbidden()

    original_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        assert name not in {"graphify.llm", "openai", "anthropic", "boto3", "graphify.cli", "graphify.ingest"}
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", checked_run)
    monkeypatch.setattr(socket, "socket", NetworkDeniedSocket)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(builtins, "__import__", checked_import)
    from graphify.salesforce.cli import main
    from graphify.salesforce.snapshot import load
    assert main(["build", str(root), "--out", str(output)]) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["coverage"]["evidence_count"] > 0
    for command in ("refs", "impact"):
        assert main([command, "Account.Name", "--snapshot", str(output), "--source", str(root)]) == 0
        assert json.loads(capsys.readouterr().out)["results"]
    rid = load(output)["facts"]["relationships"][0]["id"]
    assert main(["explain", rid, "--snapshot", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["results"]
    assert main(["status", "--snapshot", str(output), "--source", str(root), "--require-current"]) == 0
    assert json.loads(capsys.readouterr().out)["freshness"]["state"] == "current"
    assert main(["export", "graphify", "--snapshot", str(output), "--out", str(tmp_path / "projection")]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert Path(exported["output"]).is_file()
    assert main(["view", "Contact.LastName", "--snapshot", str(output), "--depth", "2"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert {"sf:flow:sample", "sf:apexmethod:sampleaction.run", "sf:field:contact.lastname"} <= {
        node["id"] for node in view["nodes"]}
    assert any(edge["relation"] == "calls" for edge in view["edges"])
    assert any(evidence["path"].endswith("SampleAction.cls")
               for edge in view["edges"] for evidence in edge["evidence"])
    assert main(["view", "Contact.LastName", "--snapshot", str(output),
                 "--depth", "2", "--format", "text"]) == 0
    text = capsys.readouterr().out
    assert "Freshness: unknown" in text and "--calls-->" in text and "SampleAction.cls:" in text
    assert {name for name in sys.modules if name == "graphify.llm"} == existing_semantic_modules


def test_empty_first_build_fails_and_leaves_no_pointer(tmp_path):
    from graphify.salesforce.service import build_project
    root = project(tmp_path)
    (root / "force-app/main/default/flows/Sample.flow-meta.xml").unlink()
    with pytest.raises(DataError, match="No supported metadata"):
        build_project(root, out=tmp_path / "out")
    assert not (tmp_path / "out/CURRENT.json").exists()


def test_changed_selection_is_not_mistaken_for_complete_deletion(tmp_path):
    from graphify.salesforce.service import build_project
    from graphify.salesforce.snapshot import load
    root, output = project(tmp_path), tmp_path / "out"
    before = build_project(root, out=output)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True)
    with pytest.raises(DataError, match="No supported metadata"):
        build_project(root, out=output)
    assert load(output) == before
    assert (root / "force-app/main/default/flows/Sample.flow-meta.xml").exists()


def test_field_namespace_is_independent_of_object_namespace():
    store = FactStore()
    extension = store.ensure_component("field", "base__Thing__c.ext__Field__c")
    standard = store.ensure_component("field", "base__Thing__c.Name")
    assert store.components[extension]["namespace"] == "ext"
    assert store.components[standard]["namespace"] is None
    assert store.components[extension]["owner_namespace"] == "base"


def test_provenance_cannot_disagree_with_analyzed_input(tmp_path):
    from graphify.salesforce.service import build_project
    root = project(tmp_path)
    bundle = build_project(root, out=tmp_path / "out")
    bundle["facts"]["evidence"][0]["content_hash"] = "0" * 64
    with pytest.raises(DataError, match="Provenance content hash"):
        validate_bundle(bundle)
