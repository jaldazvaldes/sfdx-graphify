"""Exercise the same synthetic parent paths through SOQL and typed Flow references."""

import hashlib
from pathlib import Path

import pytest

from graphify.salesforce import query
from graphify.salesforce.apex import extract_apex, index_apex
from graphify.salesforce.flow import extract_flow
from graphify.salesforce.inventory import extract_inventory, index_definitions
from graphify.salesforce.schema import FactStore
from graphify.salesforce.service import build_project
from graphify.salesforce.xml_source import parse_xml


def extract(engine, owner, path, *metadata):
    store = FactStore()
    documents = [parse_xml(name, text.encode()) for name, text in metadata]
    index_definitions(documents, store)
    extract_inventory(documents, store)
    if engine in {"apex", "soql"}:
        source = "classes/ParentProbe.cls"
        if engine == "apex":
            code = (f"public class ParentProbe {{ public void inspectRecords({owner} record) {{ "
                    f"String observed = record.{path}; }} }}")
        else:
            code = ("public class ParentProbe {\n    public void inspectRecords() {\n"
                    f"        List<{owner}> records = [SELECT {path} FROM {owner}];\n    }}\n}}")
        extract_apex(index_apex({source: code.encode()}, store), store)
        component = "sf:apexmethod:parentprobe.inspectrecords"
    else:
        source = "flows/ParentProbe.flow-meta.xml"
        code = (f"<Flow><apiVersion>64.0</apiVersion><start><object>{owner}</object></start>\n"
                "<decisions><name>Inspect</name><rules><name>Present</name><conditions>\n"
                f"    <leftValueReference>$Record.{path}</leftValueReference>\n"
                "<operator>IsNull</operator><rightValue><booleanValue>false</booleanValue></rightValue>"
                "</conditions></rules></decisions></Flow>")
        document = parse_xml(source, code.encode())
        component = store.ensure_component("flow", "ParentProbe", document.location(document.root))
        extract_flow(document, store)
    edges = [edge for edge in store.relationships.values()
             if edge["source"] == component and edge["target"].startswith("sf:field:")]
    return store, edges, source, code


@pytest.mark.parametrize("engine", ["soql", "flow"])
@pytest.mark.parametrize("owner,path,target,hops", [
    ("Case", "Account.Name", "account.name", (("case.accountid", "account"),)),
    ("Case", "Contact.LastName", "contact.lastname", (("case.contactid", "contact"),)),
    ("EmailMessage", "Parent.Contact.LastName", "contact.lastname", (
        ("emailmessage.parentid", "case"), ("case.contactid", "contact"),
    )),
    ("Opportunity", "Account.Name", "account.name", (("opportunity.accountid", "account"),)),
    ("Opportunity", "Contact.Account.Name", "account.name", (
        ("opportunity.contactid", "contact"), ("contact.accountid", "account"),
    )),
    ("Order", "Account.RecordType.Name", "recordtype.name", (
        ("order.accountid", "account"), ("account.recordtypeid", "recordtype"),
    )),
    ("Order", "BillToContact.LastName", "contact.lastname", (("order.billtocontactid", "contact"),)),
    ("Order", "ShipToContact.LastName", "contact.lastname", (("order.shiptocontactid", "contact"),)),
    ("Order", "CreatedBy.Profile.Name", "profile.name", (
        ("order.createdbyid", "user"), ("user.profileid", "profile"),
    )),
    ("User", "Profile.Name", "profile.name", (("user.profileid", "profile"),)),
    ("User", "UserRole.Name", "userrole.name", (("user.userroleid", "userrole"),)),
    ("Case", "RecordType.DeveloperName", "recordtype.developername", (("case.recordtypeid", "recordtype"),)),
    ("Lead", "CreatedBy.Email", "user.email", (("lead.createdbyid", "user"),)),
    ("Contact", "RecordType.Name", "recordtype.name", (("contact.recordtypeid", "recordtype"),)),
    ("OrderItem", "Order.Account.Name", "account.name", (
        ("orderitem.orderid", "order"), ("order.accountid", "account"),
    )),
    ("OrderItem", "Product2.Name", "product2.name", (("orderitem.product2id", "product2"),)),
    ("OrderItem", "PricebookEntry.Product2.Name", "product2.name", (
        ("orderitem.pricebookentryid", "pricebookentry"), ("pricebookentry.product2id", "product2"),
    )),
    ("PricebookEntry", "Product2.Name", "product2.name", (("pricebookentry.product2id", "product2"),)),
    ("CampaignMember", "Campaign.Name", "campaign.name", (("campaignmember.campaignid", "campaign"),)),
    ("CampaignMember", "Contact.LastName", "contact.lastname", (("campaignmember.contactid", "contact"),)),
    ("CampaignMember", "Lead.Email", "lead.email", (("campaignmember.leadid", "lead"),)),
    ("CampaignMember", "RecordType.Name", "recordtype.name", (("campaignmember.recordtypeid", "recordtype"),)),
    ("Asset", "CreatedBy.Name", "user.name", (("asset.createdbyid", "user"),)),
    ("Asset", "RecordType.Name", "recordtype.name", (("asset.recordtypeid", "recordtype"),)),
    ("Task", "CreatedBy.Email", "user.email", (("task.createdbyid", "user"),)),
    ("Task", "RecordType.Name", "recordtype.name", (("task.recordtypeid", "recordtype"),)),
    ("Event", "Account.Name", "account.name", (("event.accountid", "account"),)),
    ("Event", "CreatedBy.Email", "user.email", (("event.createdbyid", "user"),)),
    ("User", "CreatedBy.Email", "user.email", (("user.createdbyid", "user"),)),
])
def test_documented_paths_preserve_terminal_field_and_each_hop(engine, owner, path, target, hops):
    store, edges, source, code = extract(engine, owner, path)
    assert not store.diagnostics
    indexed = {edge["target"]: edge for edge in edges}
    expected = [{"field": "sf:field:" + field, "target": "sf:object:" + parent,
                 "basis": "standard_schema"} for field, parent in hops]
    assert set(indexed) == {"sf:field:" + target, *(step["field"] for step in expected)}
    assert len(indexed) == len(edges)
    terminal = indexed["sf:field:" + target]
    assert terminal["relation"] == ("reads" if engine == "soql" else "filters")
    occurrence, = [store.evidence[eid] for eid in terminal["evidence_ids"]]
    assert occurrence["path"] == source
    assert occurrence["line"] == 3
    assert occurrence["content_hash"] == hashlib.sha256(code.encode()).hexdigest()
    assert occurrence["resolution"]["steps"] == expected
    for number, step in enumerate(expected, 1):
        edge = indexed[step["field"]]
        assert edge["relation"] == "reads"
        proof, = [store.evidence[eid] for eid in edge["evidence_ids"]]
        assert proof["locator"] == f"{occurrence['locator']}/navigation[{number}]"
        assert proof["resolution"]["relationship_path"] == path
        assert proof["resolution"]["navigation_step"] == {"index": number, **step}


@pytest.mark.parametrize("engine", ["soql", "flow"])
@pytest.mark.parametrize("owner,path", [
    ("Order", "Contact.Name"), ("Case", "Owner.Name"), ("Task", "Who.Name"),
    ("Task", "What.Name"), ("Sample__c", "RecordType.Name"), ("Sample__c", "CreatedBy.Name"),
    ("ContentDocumentLink", "CreatedBy.Name"), ("User", "RecordType.Name"),
    ("Event", "Who.Name"), ("Event", "What.Name"), ("CampaignMember", "LeadOrContact.Name"),
    ("EmailMessage", "RelatedTo.Name"), ("EmailMessage", "From.Name"),
])
def test_unknown_parent_paths_have_diagnostics_without_partial_field_facts(engine, owner, path):
    store, edges, _, _ = extract(engine, owner, path)
    assert not edges
    code = "APEX_SOQL_FIELD_PATH_UNSUPPORTED" if engine == "soql" else "FLOW_RELATIONSHIP_PATH_UNSUPPORTED"
    assert any(row["code"] == code for row in store.diagnostics)


@pytest.mark.parametrize("engine", ["soql", "flow"])
@pytest.mark.parametrize("body", [
    "<type>Text</type>",
    "<type>Lookup</type><referenceTo>Account</referenceTo>",
    "<type>Lookup</type><referenceTo>Contact</referenceTo><referenceTo>Account</referenceTo>",
])
def test_standard_fallback_cannot_override_local_contradictions(engine, body):
    metadata = ("objects/Case/fields/ContactId.field-meta.xml", f"<CustomField>{body}</CustomField>")
    store, edges, _, _ = extract(engine, "Case", "Contact.LastName", metadata)
    assert not edges
    code = "APEX_SOQL_FIELD_PATH_UNSUPPORTED" if engine == "soql" else "FLOW_RELATIONSHIP_PATH_UNSUPPORTED"
    assert any(row["code"] == code for row in store.diagnostics)


@pytest.mark.parametrize("engine", ["soql", "flow"])
def test_custom_object_with_declared_lookup_can_use_a_known_standard_parent(engine):
    metadata = ("objects/Sample__c/fields/Reviewer__c.field-meta.xml",
                "<CustomField><type>Lookup</type><referenceTo>User</referenceTo></CustomField>")
    store, edges, _, _ = extract(engine, "Sample__c", "Reviewer__r.Profile.Name", metadata)
    assert {edge["target"] for edge in edges} == {
        "sf:field:sample__c.reviewer__c", "sf:field:user.profileid", "sf:field:profile.name",
    }
    terminal = next(edge for edge in edges if edge["target"] == "sf:field:profile.name")
    proof, = [store.evidence[eid] for eid in terminal["evidence_ids"]]
    assert [step["basis"] for step in proof["resolution"]["steps"]] == ["declared_lookup", "standard_schema"]


@pytest.mark.parametrize("engine", ["soql", "flow"])
@pytest.mark.parametrize("owner", ["Event", "Task"])
def test_activity_metadata_keeps_the_lookup_endpoint_canonical_and_the_source_owner(engine, owner):
    path = "objects/Activity/fields/Reviewer__c.field-meta.xml"
    xml = "<CustomField><type>Lookup</type><referenceTo>Contact</referenceTo></CustomField>"
    store, edges, _, _ = extract(engine, owner, "Reviewer__r.Account.Name", (path, xml))
    lookup = "sf:field:activity.reviewer__c"
    assert {edge["target"] for edge in edges} == {lookup, "sf:field:contact.accountid", "sf:field:account.name"}
    assert f"sf:field:{owner.casefold()}.reviewer__c" not in store.components
    terminal = next(edge for edge in edges if edge["target"] == "sf:field:account.name")
    evidence, = [store.evidence[eid] for eid in terminal["evidence_ids"]]
    inherited, standard = evidence["resolution"]["steps"]
    assert inherited["field"] == lookup
    assert inherited["target"] == "sf:object:contact"
    assert inherited["basis"] == "declared_lookup"
    assert inherited["source_owner"] == owner
    assert inherited["schema_source"]["path"] == path
    assert inherited["schema_source"]["content_hash"] == hashlib.sha256(xml.encode()).hexdigest()
    assert standard["field"] == "sf:field:contact.accountid"


@pytest.mark.parametrize("engine", ["soql", "flow"])
@pytest.mark.parametrize("other", [
    ("objects/Event/fields/Reviewer__c.field-meta.xml", "<CustomField><type>Text</type></CustomField>"),
    ("other/objects/Activity/fields/Reviewer__c.field-meta.xml",
     "<CustomField><type>Lookup</type><referenceTo>Account</referenceTo></CustomField>"),
])
def test_activity_metadata_does_not_replace_owner_specific_or_ambiguous_field_definitions(engine, other):
    activity = ("objects/Activity/fields/Reviewer__c.field-meta.xml",
                "<CustomField><type>Lookup</type><referenceTo>Contact</referenceTo></CustomField>")
    store, edges, _, _ = extract(engine, "Event", "Reviewer__r.LastName", activity, other)
    assert not edges
    code = "APEX_SOQL_FIELD_PATH_UNSUPPORTED" if engine == "soql" else "FLOW_RELATIONSHIP_PATH_UNSUPPORTED"
    assert any(row["code"] == code for row in store.diagnostics)


@pytest.mark.parametrize("engine", ["apex", "soql", "flow"])
@pytest.mark.parametrize("owner", ["Event", "Task"])
def test_direct_activity_field_aliases_share_evidence_across_refs_and_impact(engine, owner):
    metadata = ("objects/Activity/fields/Details__c.field-meta.xml", "<CustomField><type>Text</type></CustomField>")
    store, edges, source, code = extract(engine, owner, "Details__c", metadata)
    edge, = edges
    assert edge["target"] == "sf:field:activity.details__c"
    occurrence, = [store.evidence[eid] for eid in edge["evidence_ids"]]
    assert occurrence["path"] == source
    assert occurrence["content_hash"] == hashlib.sha256(code.encode()).hexdigest()
    assert occurrence["resolution"]["object"] == owner
    assert f"sf:field:{owner.casefold()}.details__c" not in store.components

    bundle = {"manifest": {"snapshot_id": "synthetic"}, "facts": store.finish(), "diagnostics": []}
    for name in ("Activity.Details__c", "Event.Details__c", "sf:field:task.details__c"):
        assert query.resolve_component(bundle, name)["id"] == edge["target"]
        assert [row["id"] for row in query.refs(bundle, name)["results"] if row["relation"] != "contains"] == [edge["id"]]
        assert [row["id"] for row in query.impact(bundle, name, depth=1)["results"]] == [edge["id"]]


def test_concrete_or_legacy_exact_child_field_wins_over_activity_alias():
    activity = ("objects/Activity/fields/Details__c.field-meta.xml", "<CustomField><type>Text</type></CustomField>")
    event = ("objects/Event/fields/Details__c.field-meta.xml", "<CustomField><type>Text</type></CustomField>")
    store, edges, _, _ = extract("soql", "Event", "Details__c", activity, event)
    assert {edge["target"] for edge in edges} == {"sf:field:event.details__c"}
    legacy_task = store.ensure_component("field", "Task.Details__c")
    bundle = {"manifest": {"snapshot_id": "synthetic", "schema_version": 8},
              "facts": store.finish(), "diagnostics": []}
    assert query.resolve_component(bundle, "Event.Details__c")["id"] == "sf:field:event.details__c"
    assert query.resolve_component(bundle, "Task.Details__c")["id"] == legacy_task
    assert query.resolve_component(bundle, legacy_task)["id"] == legacy_task
    assert query.resolve_component(bundle, "Activity.Details__c")["id"] == "sf:field:activity.details__c"


def record_type(owner="Sample__c", body="<active>false</active>"):
    return (f"objects/{owner}/recordTypes/Example.recordType-meta.xml",
            f"<RecordType><fullName>Example</fullName>{body}</RecordType>")


@pytest.mark.parametrize("engine", ["soql", "flow"])
@pytest.mark.parametrize("path,lookup,target", [
    ("RecordType.Name", "recordtypeid", "recordtype.name"),
    ("CreatedBy.Name", "createdbyid", "user.name"),
])
def test_local_record_type_establishes_the_owners_standard_system_field(engine, path, lookup, target):
    metadata_path, xml = record_type()
    store, edges, _, _ = extract(engine, "Sample__c", path, (metadata_path, xml))
    assert not store.diagnostics
    assert {edge["target"] for edge in edges} == {f"sf:field:sample__c.{lookup}", f"sf:field:{target}"}
    for edge in edges:
        occurrence, = [store.evidence[eid] for eid in edge["evidence_ids"]]
        step, = occurrence["resolution"]["steps"]
        assert step["schema_source"]["path"] == metadata_path
        assert step["schema_source"]["content_hash"] == hashlib.sha256(xml.encode()).hexdigest()


@pytest.mark.parametrize("engine", ["soql", "flow"])
def test_local_custom_object_enables_created_by_without_a_record_type(engine):
    metadata = ("objects/Sample__c/Sample__c.object-meta.xml", "<CustomObject><label>Sample</label></CustomObject>")
    store, edges, _, _ = extract(engine, "Sample__c", "CreatedBy.Name", metadata)
    assert {edge["target"] for edge in edges} == {"sf:field:sample__c.createdbyid", "sf:field:user.name"}
    for edge in edges:
        occurrence, = [store.evidence[eid] for eid in edge["evidence_ids"]]
        assert occurrence["resolution"]["steps"][0]["schema_source"]["path"] == metadata[0]


@pytest.mark.parametrize("engine", ["soql", "flow"])
@pytest.mark.parametrize("owner,path,metadata", [
    ("Sample__c", "RecordType.Name", (record_type(body="<active>unknown</active>"),)),
    ("Sample__c", "RecordType.Name", (record_type(), ("other/" + record_type()[0], record_type()[1]))),
    ("Sample__c", "RecordType.Name", (record_type(), (
        "objects/Sample__c/fields/RecordTypeId.field-meta.xml",
        "<CustomField><type>Lookup</type><referenceTo>User</referenceTo></CustomField>",
    ))),
    ("Sample__c", "CreatedBy.Name", (
        ("objects/Sample__c/Sample__c.object-meta.xml",
         "<CustomObject><customSettingsType>Hierarchy</customSettingsType></CustomObject>"),
    )),
    ("Sample__mdt", "RecordType.Name", (record_type("Sample__mdt"),)),
    ("Sample__x", "RecordType.Name", (record_type("Sample__x"),)),
    ("Sample__e", "RecordType.Name", (record_type("Sample__e"),)),
    ("Sample__b", "RecordType.Name", (record_type("Sample__b"),)),
])
def test_local_metadata_cannot_override_unresolved_conflicting_or_special_objects(engine, owner, path, metadata):
    store, edges, _, _ = extract(engine, owner, path, *metadata)
    assert not edges
    code = "APEX_SOQL_FIELD_PATH_UNSUPPORTED" if engine == "soql" else "FLOW_RELATIONSHIP_PATH_UNSUPPORTED"
    assert any(row["code"] == code for row in store.diagnostics)


@pytest.mark.parametrize("invalid_xml", [
    "<RecordType><active>true</active>",
    "<RecordType><fullName>Mismatch</fullName><active>true</active></RecordType>",
    '<?xml version="1.0" encoding="unsupported-encoding"?><RecordType/>',
    '<?xml version="1.0" encoding="utf-7"?><RecordType/>',
])
def test_build_selects_and_hashes_valid_record_type_after_invalid_peer(tmp_path, invalid_xml):
    source = tmp_path / "source"
    package = source / "force-app"
    package.mkdir(parents=True)
    (source / "sfdx-project.json").write_text('{"packageDirectories":[{"path":"force-app"}]}', encoding="utf-8")
    metadata_path, xml = record_type()
    invalid_path, _ = record_type("Unresolved__c")
    files = {
        metadata_path: xml,
        invalid_path: invalid_xml,
        "classes/ParentProbe.cls": (
            "public class ParentProbe { public void probe() { "
            "List<Sample__c> rows = [SELECT RecordType.Name FROM Sample__c]; "
            "List<Unresolved__c> unresolved = [SELECT RecordType.Name FROM Unresolved__c]; } }"
        ),
    }
    for relative, content in files.items():
        file = package / Path(relative)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
    bundle = build_project(source, out=tmp_path / "snapshot")
    path = f"force-app/{metadata_path}"
    terminal, = [row for row in bundle["facts"]["relationships"] if row["target"] == "sf:field:recordtype.name"]
    occurrence, = [row for row in bundle["facts"]["evidence"] if row["id"] in terminal["evidence_ids"]]
    definition = occurrence["resolution"]["steps"][0]["schema_source"]
    assert definition["path"] == path
    assert bundle["manifest"]["inputs"][path] == definition["content_hash"]
    unresolved, = [row for row in bundle["diagnostics"] if row["code"] == "RECORD_TYPE_METADATA_UNRESOLVED"]
    invalid_path = f"force-app/{invalid_path}"
    assert unresolved["location"]["path"] == invalid_path
    assert unresolved["location"]["content_hash"] == bundle["manifest"]["inputs"][invalid_path]
    assert any(row["code"] == "APEX_SOQL_FIELD_PATH_UNSUPPORTED" for row in bundle["diagnostics"])
    assert not any(row["id"] == "sf:field:unresolved__c.recordtypeid" for row in bundle["facts"]["components"])
    assert not any(row["code"] == "unsupported_metadata" for row in bundle["diagnostics"])
