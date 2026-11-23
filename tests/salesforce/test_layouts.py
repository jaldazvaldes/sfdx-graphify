"""Salesforce DX page layout identities and field placement evidence."""

from copy import deepcopy

import pytest

from graphify.salesforce import query
from graphify.salesforce.schema import DataError, component_id, validate_bundle
from graphify.salesforce.service import build_project
from graphify.salesforce.snapshot import load
from tests.salesforce.test_storage import project, put


def test_layout_placements_round_trip_with_xml_locations(tmp_path):
    root = project(tmp_path / "source", flow=False)
    prefix = "force-app/main/default"
    first = f"{prefix}/layouts/Widget__c-Widget %28Service%2FHelp%29.layout-meta.xml"
    second = f"{prefix}/layouts/Widget__c-Widget - Admin.layout-meta.xml"
    field_file = f"{prefix}/objects/Widget__c/fields/Code__c.field-meta.xml"
    put(root, field_file, "<CustomField><fullName>Code__c</fullName><type>Text</type></CustomField>")
    body = """<Layout xmlns="http://soap.sforce.com/2006/04/metadata">
    <layoutSections><layoutColumns>
        <layoutItems><behavior>Readonly</behavior>
            <field>Code__c</field></layoutItems>
        <layoutItems><field>Name</field></layoutItems>
        <layoutItems><field>Code__c</field></layoutItems>
    </layoutColumns></layoutSections>
    <relatedLists><field>Other__c</field></relatedLists>
    <summaryLayout><field>Decoy__c</field></summaryLayout>
    </Layout>"""
    put(root, first, body)
    put(root, second, body)
    output = tmp_path / "snapshot"
    build_project(root, out=output)
    bundle = load(output)

    rows = query.refs(bundle, "Widget__c.Code__c", relation="places_field")["results"]
    assert {row["source"] for row in rows} == {
        component_id("layout", "Widget__c-Widget %28Service%2FHelp%29"),
        component_id("layout", "Widget__c-Widget - Admin"),
    }
    assert {(site["path"], site["line"]) for row in rows for site in row["evidence"]} == {
        (path, line) for path in (first, second) for line in (4, 6)
    }
    for row in rows:
        assert all(site["locator"].endswith("/field") for site in row["evidence"])
    standard = query.refs(bundle, "Widget__c.Name", relation="places_field")
    assert standard["component"]["definition_status"] == "referenced_only"
    assert len(standard["results"]) == 2
    owner = query.deps(bundle, "Widget__c", relation="contains")
    assert {row["target"] for row in owner["results"]} == {row["source"] for row in rows} | {"sf:field:widget__c.code__c"}
    assert {c["api_name"] for c in bundle["facts"]["components"] if c["kind"] == "field"} == {"Widget__c.Code__c", "Widget__c.Name"}

    old = deepcopy(bundle)
    old["manifest"]["schema_version"] = 13
    with pytest.raises(DataError, match="layouts require snapshot schema version"):
        validate_bundle(old)


def test_invalid_or_ambiguous_layouts_do_not_introduce_placements(tmp_path):
    root = project(tmp_path / "source", flow=False)
    body = """<Layout><layoutSections><layoutColumns>
        <layoutItems><field>Contact.Code__c</field></layoutItems>
        <layoutItems><field>Name</field><field>Email</field></layoutItems>
        <layoutItems><field>Nested<tag/>Text</field></layoutItems>
        <layoutItems><emptySpace>true</emptySpace></layoutItems>
        </layoutColumns></layoutSections></Layout>"""
    put(root, "force-app/layouts/Account-Incomplete.layout-meta.xml", body)
    put(root, "force-app/layouts/Account-Wrong.layout-meta.xml", "<Layout><fullName>Contact-Wrong</fullName></Layout>")
    put(root, "force-app/layouts/Account-Broken.layout-meta.xml", "<Layout>")
    duplicate = "<Layout><layoutSections><layoutColumns><layoutItems><field>Name</field></layoutItems></layoutColumns></layoutSections></Layout>"
    for package in ("a", "b"):
        put(root, f"force-app/{package}/layouts/Account-Duplicate.layout-meta.xml", duplicate)
    output = tmp_path / "snapshot"
    build_project(root, out=output)
    bundle = load(output)
    assert not any(row["relation"] == "places_field" for row in bundle["facts"]["relationships"])
    assert {c["api_name"] for c in bundle["facts"]["components"] if c["kind"] == "layout"} == {"Account-Incomplete", "Account-Duplicate"}
    codes = [item["code"] for item in bundle["diagnostics"]]
    assert codes.count("LAYOUT_FIELD_UNRESOLVED") == 3
    assert codes.count("LAYOUT_METADATA_UNRESOLVED") == 2
    assert "duplicate_definition" in codes


def test_task_layout_can_place_a_shared_activity_field(tmp_path):
    root = project(tmp_path / "source", flow=False)
    put(root, "force-app/objects/Activity/fields/Shared__c.field-meta.xml", "<CustomField><type>Text</type></CustomField>")
    path = "force-app/layouts/Task-Team.layout-meta.xml"
    put(root, path, """<Layout><layoutSections><layoutColumns>
    <layoutItems><field>Shared__c</field></layoutItems>
    </layoutColumns></layoutSections></Layout>""")
    output = tmp_path / "snapshot"
    build_project(root, out=output)
    bundle = load(output)
    row, = query.refs(bundle, "Activity.Shared__c", relation="places_field")["results"]
    assert row["source"] == component_id("layout", "Task-Team")
    assert [(site["path"], site["line"]) for site in row["evidence"]] == [(path, 2)]
