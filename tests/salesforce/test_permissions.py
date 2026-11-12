"""Synthetic local permission declarations through build, query, and focused views."""
from __future__ import annotations

import hashlib
import json

import pytest

from graphify.salesforce import query, snapshot
from graphify.salesforce.cli import main
from graphify.salesforce.schema import SCHEMA_VERSION, component_id
from graphify.salesforce.service import build_project


PACKAGE = "force-app/main/default"
PROFILE_PATH = f"{PACKAGE}/profiles/Sample Support.profile-meta.xml"
PERMISSION_SET_PATH = f"{PACKAGE}/permissionsets/SampleSupport.permissionset-meta.xml"
PROFILE = """<Profile xmlns="http://soap.sforce.com/2006/04/metadata">
    <fieldPermissions>
        <editable>false</editable>
        <field>Account.Name</field>
        <readable>true</readable>
    </fieldPermissions>
    <fieldPermissions>
        <editable>false</editable>
        <field>Account.Name</field>
        <readable>true</readable>
    </fieldPermissions>
    <objectPermissions>
        <allowCreate>false</allowCreate>
        <allowDelete>false</allowDelete>
        <allowEdit>false</allowEdit>
        <allowRead>true</allowRead>
        <modifyAllRecords>false</modifyAllRecords>
        <object>Account</object>
        <viewAllRecords>false</viewAllRecords>
    </objectPermissions>
</Profile>
"""
PERMISSION_SET = """<PermissionSet xmlns="http://soap.sforce.com/2006/04/metadata">
    <label>Sample Support</label>
    <fieldPermissions>
        <editable>true</editable>
        <field>Account.Name</field>
    </fieldPermissions>
    <objectPermissions>
        <allowCreate>true</allowCreate>
        <allowDelete>true</allowDelete>
        <allowEdit>true</allowEdit>
        <allowRead>true</allowRead>
        <modifyAllRecords>true</modifyAllRecords>
        <object>Account</object>
        <viewAllFields>true</viewAllFields>
        <viewAllRecords>true</viewAllRecords>
    </objectPermissions>
    <classAccesses>
        <apexClass>SampleAction</apexClass>
        <enabled>true</enabled>
    </classAccesses>
    <classAccesses>
        <apexClass>UnassignedAction</apexClass>
        <enabled>false</enabled>
    </classAccesses>
</PermissionSet>
"""
OBJECT_FLAGS = {
    "read": "allowRead",
    "create": "allowCreate",
    "edit": "allowEdit",
    "delete": "allowDelete",
    "view_all_records": "viewAllRecords",
    "modify_all_records": "modifyAllRecords",
    "view_all_fields": "viewAllFields",
}


def build(tmp_path, files, **build_options):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sfdx-project.json").write_text(json.dumps({
        "packageDirectories": [{"path": "force-app"}], "sourceApiVersion": "64.0",
    }), encoding="utf-8")
    for relative, content in files.items():
        file = source / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content.encode("utf-8"))
    output = tmp_path / "snapshot"
    bundle = build_project(source, out=output, **build_options)
    assert snapshot.load(output) == bundle
    return source, output, bundle


@pytest.fixture
def permission_project(tmp_path):
    return build(tmp_path, {
        PROFILE_PATH: PROFILE,
        PERMISSION_SET_PATH: PERMISSION_SET,
        f"{PACKAGE}/classes/SampleAction.cls": """public class SampleAction {
    public static void inspectFields() {
        List<Account> accounts = [SELECT Name FROM Account];
    }
}
""",
    }, include_permissions=True)


def test_local_grants_record_positive_boolean_evidence(permission_project):
    source, _, bundle = permission_project
    assert bundle["manifest"]["schema_version"] == SCHEMA_VERSION
    profile = component_id("profile", "Sample Support")
    permission_set = component_id("permissionset", "SampleSupport")
    field = component_id("field", "Account.Name")
    account = component_id("object", "Account")
    apex_class = component_id("apexclass", "SampleAction")
    expected = {
        (profile, field, "read"): ("Account.Name", "readable", 2),
        (profile, account, "read"): ("Account", "allowRead", 1),
        (permission_set, field, "edit"): ("Account.Name", "editable", 1),
        (permission_set, apex_class, "execute"): ("SampleAction", "enabled", 1),
    }
    expected.update({
        (permission_set, account, action): ("Account", flag, 1)
        for action, flag in OBJECT_FLAGS.items()
    })

    grants = [row for row in bundle["facts"]["relationships"] if row["relation"] == "grants"]
    assert len(grants) == len(expected)
    components = {row["id"]: row for row in bundle["facts"]["components"]}
    assert components[profile]["kind"] == "profile"
    assert components[permission_set]["kind"] == "permissionset"
    assert {components[cid]["definition_status"] for cid in (profile, permission_set)} == {"defined"}
    assert not any(row["code"].startswith("PERMISSION_") for row in bundle["diagnostics"])

    proof_by_id = {row["id"]: row for row in bundle["facts"]["evidence"]}
    for row in grants:
        action = row["qualifiers"]["permission"]
        target, flag, count = expected[row["source"], row["target"], action]
        assert row["qualifiers"] == {"permission": action}
        occurrences = [proof_by_id[eid] for eid in row["evidence_ids"]]
        assert len(occurrences) == count
        assert len({proof["locator"] for proof in occurrences}) == count
        expected_path = PROFILE_PATH if row["source"] == profile else PERMISSION_SET_PATH
        content = (source / expected_path).read_bytes()
        for proof in occurrences:
            assert proof["path"] == expected_path
            assert proof["content_hash"] == hashlib.sha256(content).hexdigest()
            assert proof["locator"].endswith(f"/{flag}")
            assert content.decode("utf-8").splitlines()[proof["line"] - 1].strip() == f"<{flag}>true</{flag}>"
            assert proof["resolution"] == {"basis": "explicit_permission_boolean", "declared_target": target}


def test_grants_are_available_through_explicit_queries_and_views(permission_project, capsys):
    _, output, bundle = permission_project
    profile = component_id("profile", "Sample Support")
    permission_set = component_id("permissionset", "SampleSupport")
    field = component_id("field", "Account.Name")
    apex_method = component_id("apexmethod", "SampleAction.inspectFields")

    assert {(row["source"], row["relation"]) for row in query.impact(bundle, "Account.Name")["results"]} == {
        (apex_method, "reads"),
    }

    args = ["--snapshot", str(output)]
    assert main(["refs", "Account.Name", "--relation", "grants", *args]) == 0
    incoming = json.loads(capsys.readouterr().out)["results"]
    assert {(row["source"], row["qualifiers"]["permission"]) for row in incoming} == {
        (profile, "read"), (permission_set, "edit"),
    }
    assert main(["deps", profile, "--relation", "grants", *args]) == 0
    outgoing = json.loads(capsys.readouterr().out)["results"]
    assert {row["target"] for row in outgoing} == {field, component_id("object", "Account")}

    assert main(["impact", "Account.Name", "--relation", "grants", *args]) == 0
    permission_impact = json.loads(capsys.readouterr().out)["results"]
    assert {row["id"] for row in permission_impact} == {row["id"] for row in incoming}

    assert main(["view", "Account.Name", "--mode", "permissions", *args]) == 0
    permissions = json.loads(capsys.readouterr().out)
    assert {(edge["source"], edge["target"], edge["relation"]) for edge in permissions["edges"]} == {
        (profile, field, "grants"), (permission_set, field, "grants"),
    }
    assert main(["view", "Account.Name", "--mode", "impact", *args]) == 0
    functional = json.loads(capsys.readouterr().out)
    assert {(edge["source"], edge["target"], edge["relation"]) for edge in functional["edges"]} == {
        (apex_method, field, "reads"),
    }


def test_mixed_declarations_suppress_only_the_conflicting_operation(tmp_path):
    xml = """<PermissionSet>
    <fieldPermissions>
        <editable>true</editable>
        <field>Account.Name</field>
        <readable>true</readable>
    </fieldPermissions>
    <fieldPermissions>
        <editable>true</editable>
        <field>Account.Name</field>
        <readable>false</readable>
    </fieldPermissions>
    <classAccesses>
        <apexClass>SampleAction</apexClass>
        <enabled>maybe</enabled>
    </classAccesses>
    <fieldPermissions>
        <editable>true</editable>
        <field>Account.Parent.Name</field>
        <readable>true</readable>
    </fieldPermissions>
</PermissionSet>
"""
    _, _, bundle = build(tmp_path, {PERMISSION_SET_PATH: xml}, include_permissions=True)
    grant, = query.deps(bundle, "SampleSupport", relation="grants")["results"]
    assert grant["target"] == component_id("field", "Account.Name")
    assert grant["qualifiers"] == {"permission": "edit"}
    assert len(grant["evidence"]) == 2
    diagnostics = [row for row in bundle["diagnostics"] if row["code"].startswith("PERMISSION_")]
    assert {row["code"] for row in diagnostics} == {
        "PERMISSION_DECLARATION_CONFLICT", "PERMISSION_BOOLEAN_INVALID", "PERMISSION_TARGET_UNRESOLVED",
    }
    assert all(row["location"]["path"] == PERMISSION_SET_PATH for row in diagnostics)


def test_task_and_event_grants_keep_their_declared_field_scope(tmp_path):
    activity_field = f"{PACKAGE}/objects/Activity/fields/Note__c.field-meta.xml"
    xml = """<PermissionSet>
    <fieldPermissions>
        <editable>false</editable>
        <field>Task.Note__c</field>
        <readable>true</readable>
    </fieldPermissions>
    <fieldPermissions>
        <editable>true</editable>
        <field>Event.Note__c</field>
        <readable>false</readable>
    </fieldPermissions>
</PermissionSet>
"""
    _, _, bundle = build(tmp_path, {
        activity_field: "<CustomField><type>Text</type></CustomField>",
        PERMISSION_SET_PATH: xml,
    }, include_permissions=True)
    task, = query.refs(bundle, "Task.Note__c", relation="grants")["results"]
    event, = query.refs(bundle, "Event.Note__c", relation="grants")["results"]
    assert task["target"] == component_id("field", "Task.Note__c")
    assert event["target"] == component_id("field", "Event.Note__c")
    assert task["qualifiers"] == {"permission": "read"}
    assert event["qualifiers"] == {"permission": "edit"}
    assert query.refs(bundle, "Activity.Note__c", relation="grants")["results"] == []


def test_default_skips_permission_xml_but_keeps_fingerprints_and_literal_hints(tmp_path):
    field = f"{PACKAGE}/objects/Account/fields/Name.field-meta.xml"
    credential = f"{PACKAGE}/namedCredentials/SampleService.namedCredential-meta.xml"
    invalid_profile = "<Profile><field>Account.Name</field>"
    source, _, default = build(tmp_path, {
        field: "<CustomField><type>Text</type></CustomField>",
        credential: "<NamedCredential/>",
        PROFILE_PATH: invalid_profile,
        PERMISSION_SET_PATH: PERMISSION_SET,
    })
    default_manifest = default["manifest"]
    assert default_manifest["coverage"]["permission_selection"] == {
        "included": False, "selected_file_count": 2, "analyzed_file_count": 0,
        "skipped_file_count": 2, "failed_file_count": 0,
    }
    assert default_manifest["coverage"]["analyzed_files"] == 2
    assert "include_permissions" not in default_manifest["scope"]
    assert not any(row["code"].startswith("PERMISSION_") for row in default["diagnostics"])
    assert not any(row["kind"] in {"profile", "permissionset"} for row in default["facts"]["components"])
    assert query.refs(default, "Account.Name", relation="grants")["results"] == []
    inputs = default_manifest["inputs"]
    for path, data in ((PROFILE_PATH, invalid_profile), (PERMISSION_SET_PATH, PERMISSION_SET)):
        assert inputs[path] == hashlib.sha256(data.encode()).hexdigest()
    literal = default_manifest["literal_blind_spots"]["qualified"]["account.name"]
    assert literal["occurrence_count"] == 2
    assert set(literal["example_paths"]) == {PROFILE_PATH, PERMISSION_SET_PATH}

    enabled = build_project(source, out=tmp_path / "with-permissions", include_permissions=True)
    enabled_manifest = enabled["manifest"]
    assert enabled_manifest["inputs"] == inputs
    assert enabled_manifest["scope"] == {**default_manifest["scope"], "include_permissions": True}
    assert enabled_manifest["coverage"]["permission_selection"] == {
        "included": True, "selected_file_count": 2, "analyzed_file_count": 1,
        "skipped_file_count": 0, "failed_file_count": 1,
    }
    assert enabled_manifest["coverage"]["analyzed_files"] == 3
    assert [row["location"]["path"] for row in enabled["diagnostics"]
            if row["code"] == "PERMISSION_METADATA_UNRESOLVED"] == [PROFILE_PATH]
    assert len(query.refs(enabled, "Account.Name", relation="grants")["results"]) == 1

    for bundle in (default, enabled):
        assert any(row["kind"] == "namedcredential" and row["definition_status"] == "defined"
                   for row in bundle["facts"]["components"])
