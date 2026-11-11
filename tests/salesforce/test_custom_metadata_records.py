"""Synthetic contracts for Custom Metadata record and scalar extraction."""

from __future__ import annotations

from collections import Counter
import json

from graphify.salesforce.cli import main
from graphify.salesforce.custom_metadata import extract_custom_metadata, index_custom_metadata
from graphify.salesforce.schema import FactStore
from graphify.salesforce.xml_source import METADATA_NS, XML_SCHEMA_INSTANCE_NS, parse_xml


_BASE = "force-app/main/default"


def _record(rows, name="ExampleSettings.Policy", package=_BASE):
    payload = f'''<CustomMetadata xmlns="{METADATA_NS}" xmlns:i="{XML_SCHEMA_INSTANCE_NS}"
                    xmlns:s="http://www.w3.org/2001/XMLSchema">
{rows}
</CustomMetadata>'''
    return parse_xml(f"{package}/customMetadata/{name}.md-meta.xml", payload.encode())


def _field(name, value):
    return f"<values><field>{name}</field>{value}</values>"


def _define(store, kind, name, **attrs):
    return store.ensure_component(kind, name, {"path": f"{kind}/{name}", "line": 1}, **attrs)


def _extract(store, *records):
    index_custom_metadata(list(records), store)
    extract_custom_metadata(list(records), store)


def _relationships(store, relation):
    return [edge for edge in store.relationships.values() if edge["relation"] == relation]


def test_custom_metadata_cli_pins_accessor_and_summarizes_values_before_query_limit(tmp_path, capsys):
    project = tmp_path / "project"
    snapshot = tmp_path / "snapshot"
    rules = tmp_path / "accessors.json"
    rules.write_text(json.dumps([{"class": "SampleSettings", "method": "enabled", "arity": 1,
                                 "field_argument": 1, "type": "Sample_Settings__mdt"}]), encoding="utf-8")
    sources = {
        "sfdx-project.json": '{"packageDirectories":[{"path":"force-app"}],"sourceApiVersion":"64.0"}',
        f"{_BASE}/objects/Sample_Settings__mdt/Sample_Settings__mdt.object-meta.xml":
            '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata"/>',
        f"{_BASE}/objects/Sample_Settings__mdt/fields/Enabled__c.field-meta.xml":
            '<CustomField xmlns="http://soap.sforce.com/2006/04/metadata">'
            '<fullName>Enabled__c</fullName><type>Checkbox</type></CustomField>',
        f"{_BASE}/classes/SampleSettings.cls":
            "public class SampleSettings { public static Boolean enabled(String field) { return false; } }",
        f"{_BASE}/classes/Reader.cls":
            "public class Reader { public static Boolean check() { return SampleSettings.enabled('Enabled__c'); } }",
    }
    for record, value in (("North", "true"), ("South", "false")):
        sources[f"{_BASE}/customMetadata/Sample_Settings.{record}.md-meta.xml"] = (
            f'<CustomMetadata xmlns="{METADATA_NS}" xmlns:i="{XML_SCHEMA_INSTANCE_NS}" '
            f'xmlns:s="http://www.w3.org/2001/XMLSchema"><values><field>Enabled__c</field>'
            f'<value i:type="s:boolean">{value}</value></values></CustomMetadata>'
        )
    for name, content in sources.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    assert main(["build", str(project), "--out", str(snapshot), "--custom-metadata-accessors", str(rules)]) == 0
    capsys.readouterr()
    field = "Sample_Settings__mdt.Enabled__c"
    assert main(["refs", field, "--snapshot", str(snapshot), "--limit", "1", "--source", str(project)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["freshness"]["state"] == "current"
    assert result["results"][0]["source"] == "sf:apexmethod:reader.check"
    assert result["results"][0]["evidence"][0]["rule"] == "apex.custom_metadata_accessor"
    summary = result["custom_metadata_values"]
    assert summary["record_count"] == 2
    assert {group["value"]: group["records"] for group in summary["groups"]} == {
        True: ["Sample_Settings.North"], False: ["Sample_Settings.South"],
    }
    assert main(["deps", "Sample_Settings.North", "--snapshot", str(snapshot), "--format", "brief"]) == 0
    assert "--sets_value true-->" in capsys.readouterr().out


def test_typed_custom_metadata_preserves_false_nil_empty_omission_and_lexical_text():
    values = [
        ("acme__Enabled__c", '<value i:type="s:boolean">false</value>'),
        ("acme__Blank__c", '<value i:type="s:string"/>'),
        ("acme__Reset__c", '<value i:nil="true"/>'),
        ("acme__Preserved__c", '<value i:type="s:string">  keep &amp; spaces  </value>'),
        ("acme__Count__c", '<value i:type="s:int">+0007</value>'),
        ("acme__Ratio__c", '<value i:type="s:double">4.2e1</value>'),
        ("acme__WithoutType__c", '<value>1</value>'),
        ("acme__TextWithoutType__c", '<value>true</value>'),
    ]
    document = _record("\n".join(_field(name, value) for name, value in values),
                       name="acme__ExampleSettings.acme__Policy")
    store = FactStore()
    owner = _define(store, "object", "acme__ExampleSettings__mdt")
    field_types = {
        "acme__Enabled__c": "Checkbox", "acme__Blank__c": "Text", "acme__Reset__c": "Text",
        "acme__Preserved__c": "LongTextArea", "acme__Count__c": "Number", "acme__Ratio__c": "Number",
        "acme__WithoutType__c": "Checkbox", "acme__TextWithoutType__c": "Text", "acme__Omitted__c": "Checkbox",
    }
    for name, data_type in field_types.items():
        _define(store, "field", f"acme__ExampleSettings__mdt.{name}", data_type=data_type)

    _extract(store, document)

    source = "sf:custommetadatarecord:acme__examplesettings.acme__policy"
    instance, = _relationships(store, "instance_of")
    assert (instance["source"], instance["target"], instance["qualifiers"]) == (source, owner, {})
    by_field = {edge["target"].rsplit(".", 1)[1]: edge for edge in _relationships(store, "sets_value")}
    expected = {
        "acme__enabled__c": ("boolean", False), "acme__blank__c": ("string", ""),
        "acme__reset__c": ("null", None), "acme__preserved__c": ("string", "  keep & spaces  "),
        "acme__count__c": ("number", 7), "acme__ratio__c": ("number", 42.0),
        "acme__withouttype__c": ("boolean", True), "acme__textwithouttype__c": ("string", "true"),
    }
    assert {field: (edge["qualifiers"]["value_type"], edge["qualifiers"]["value"])
            for field, edge in by_field.items()} == expected
    for row in document.root.all("values"):
        node = row.child("value")
        edge = by_field[row.value("field").casefold()]
        evidence, = [store.evidence[eid] for eid in edge["evidence_ids"]]
        assert evidence["rule"] == "custom_metadata.value"
        assert (evidence["line"], evidence["column"], evidence["locator"]) == (node.line, node.column, node.locator)
    preserved = document.root.all("values")[3].child("value")
    assert preserved.text == "keep & spaces"
    assert preserved.lexical_text == "  keep & spaces  "
    assert not store.diagnostics


def test_exact_string_mentions_require_a_unique_defined_target():
    store = FactStore()
    _define(store, "object", "ExampleSettings__mdt")
    expected = {_define(store, "apexclass", "UsePolicy"), _define(store, "flow", "RunPolicy"),
                _define(store, "field", "Account.Region__c")}
    _define(store, "apexclass", "AmbiguousName")
    _define(store, "flow", "AmbiguousName")
    store.ensure_component("apexclass", "ExternalPolicy")
    strings = ["UsePolicy", "RunPolicy", "Account.Region__c", "prefix UsePolicy", " RunPolicy ",
               "AmbiguousName", "ExternalPolicy", "Region__c"]
    rows = []
    for number, value in enumerate(strings):
        name = f"Reference{number}__c"
        _define(store, "field", f"ExampleSettings__mdt.{name}", data_type="Text")
        rows.append(_field(name, f'<value i:type="s:string">{value}</value>'))

    _extract(store, _record("\n".join(rows)))

    edges = _relationships(store, "mentions")
    assert {edge["target"] for edge in edges} == expected
    for edge in edges:
        evidence, = [store.evidence[eid] for eid in edge["evidence_ids"]]
        assert edge["qualifiers"] == {}
        assert evidence["rule"] == "custom_metadata.exact_text_mention"
    assert Counter(d["code"] for d in store.diagnostics) == {"CUSTOM_METADATA_MENTION_AMBIGUOUS": 1}


def test_malformed_and_duplicate_values_stay_diagnostic_without_guessing():
    store = FactStore()
    _define(store, "object", "ExampleSettings__mdt")
    for name in ("Repeated__c", "BadType__c", "BadQName__c", "BadNumber__c", "BadNil__c", "Nested__c"):
        _define(store, "field", f"ExampleSettings__mdt.{name}")
    rows = [
        _field("Repeated__c", '<value i:type="s:boolean">true</value>'),
        _field("repeated__c", '<value i:type="s:boolean">false</value>'),
        _field("BadType__c", '<value xmlns:s="urn:not-xsd" i:type="s:boolean">true</value>'),
        _field("BadQName__c", '<value i:type="s:invalid:boolean">true</value>'),
        _field("BadNumber__c", '<value i:type="s:double">1e3000</value>'),
        _field("BadNil__c", '<value i:nil="true">nonempty</value>'),
        _field("Nested__c", '<value><child/></value>'),
        _field("OtherObject.External__c", '<value i:type="s:boolean">false</value>'),
        _field("Legacy__c", '<value i:type="s:boolean">false</value>'),
        _field("Untyped__c", '<value>true</value>'),
    ]

    _extract(store, _record("\n".join(rows)))

    assert {edge["target"].rsplit(".", 1)[1]: edge["qualifiers"]
            for edge in _relationships(store, "sets_value")} == {
        "legacy__c": {"value_type": "boolean", "value": False},
        "untyped__c": {"value_type": "string", "value": "true"},
    }
    assert Counter(d["code"] for d in store.diagnostics) == {
        "CUSTOM_METADATA_VALUE_DUPLICATE": 1, "CUSTOM_METADATA_VALUE_INVALID": 5,
        "CUSTOM_METADATA_FIELD_UNRESOLVED": 1, "CUSTOM_METADATA_FIELD_UNDEFINED": 2,
        "CUSTOM_METADATA_VALUE_UNTYPED": 1,
    }


def test_duplicate_records_and_ambiguous_type_do_not_emit_values():
    store = FactStore()
    _define(store, "object", "ExampleSettings__mdt")
    store.ensure_component("object", "ExampleSettings__mdt", {"path": "other/object", "line": 1})
    first = _record("", name="ExampleSettings.Repeated")
    second = _record("", name="ExampleSettings.Repeated", package="other/main/default")
    ambiguous_type = _record("", name="ExampleSettings.Unique")
    wrong_root = parse_xml(f"{_BASE}/customMetadata/ExampleSettings.Invalid.md-meta.xml", b"<Other/>")
    invalid_name = _record("", name="ExampleSettings__mdt.Invalid")

    _extract(store, first, second, ambiguous_type, wrong_root, invalid_name)

    assert not store.relationships
    assert Counter(d["code"] for d in store.diagnostics) == {
        "CUSTOM_METADATA_RECORD_AMBIGUOUS": 2, "CUSTOM_METADATA_TYPE_AMBIGUOUS": 1,
        "CUSTOM_METADATA_IDENTITY_UNRESOLVED": 2,
    }

