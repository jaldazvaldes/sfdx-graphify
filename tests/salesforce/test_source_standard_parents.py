"""Exercise the same synthetic parent paths through SOQL and typed Flow references."""

import hashlib

import pytest

from graphify.salesforce.apex import extract_apex, index_apex
from graphify.salesforce.flow import extract_flow
from graphify.salesforce.inventory import extract_inventory, index_definitions
from graphify.salesforce.schema import FactStore
from graphify.salesforce.xml_source import parse_xml


def extract(engine, owner, path, *metadata):
    store = FactStore()
    documents = [parse_xml(name, text.encode()) for name, text in metadata]
    index_definitions(documents, store)
    extract_inventory(documents, store)
    if engine == "soql":
        source = "classes/ParentProbe.cls"
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
    ("Case", "RecordType.DeveloperName", "recordtype.developername", (("case.recordtypeid", "recordtype"),)),
    ("Lead", "CreatedBy.Email", "user.email", (("lead.createdbyid", "user"),)),
    ("Contact", "RecordType.Name", "recordtype.name", (("contact.recordtypeid", "recordtype"),)),
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
