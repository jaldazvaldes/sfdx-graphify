"""Independent synthetic fixtures for the bounded Flow contract."""

import pytest

from graphify.salesforce.flow import extract_flow
from graphify.salesforce.schema import FactStore
from graphify.salesforce.xml_source import parse_xml


def analyze(body, *, name="SyntheticFlow", version="64.0", store=None):
    xml = f'<Flow xmlns="http://soap.sforce.com/2006/04/metadata"><apiVersion>{version}</apiVersion><processType>AutoLaunchedFlow</processType>{body}</Flow>'
    doc = parse_xml(f"force-app/main/default/flows/{name}.flow-meta.xml", xml.encode())
    store = store or FactStore()
    store.ensure_component("flow", name, doc.location(doc.root))
    extract_flow(doc, store)
    return store.finish(), store.diagnostics


def fields(facts):
    names = {item["id"]: item["api_name"] for item in facts["components"] if item["kind"] == "field"}
    return {(names[item["target"]], item["relation"]) for item in facts["relationships"] if item["target"] in names}


def codes(diagnostics):
    return {item["code"] for item in diagnostics}


def variable(name, owner, collection=False):
    return f'<variables><name>{name}</name><dataType>SObject</dataType><objectType>{owner}</objectType><isCollection>{str(collection).lower()}</isCollection></variables>'


def assignment(destination, source):
    return f'<assignments><name>AssignValue</name><assignmentItems><assignToReference>{destination}</assignToReference><operator>Assign</operator><value><elementReference>{source}</elementReference></value></assignmentItems></assignments>'


def test_start_filter_fields_and_operand_keep_owners_and_repeated_evidence():
    facts, diagnostics = analyze('''
      <start><object>Account</object><triggerType>RecordAfterSave</triggerType><recordTriggerType>Update</recordTriggerType>
        <filters><field>Name</field><operator>EqualTo</operator><value><stringValue>Contact.LastName</stringValue></value></filters>
        <filters><field>Name</field><operator>NotEqualTo</operator><value><elementReference>$Record__Prior.Name</elementReference></value></filters>
      </start><description>Contact.Email</description>''')
    assert fields(facts) == {("Account.Name", "filters")}
    filter_edges = [edge for edge in facts["relationships"] if edge["relation"] == "filters"]
    current = next(edge for edge in filter_edges if "record_context" not in edge["qualifiers"])
    assert len(current["evidence_ids"]) == 2
    assert any(edge["qualifiers"].get("record_context") == "prior" for edge in filter_edges)
    assert not diagnostics
    assert all(item["line"] and item["locator"] and item["content_hash"] for item in facts["evidence"])


def test_automatic_lookup_output_is_typed_before_later_reference():
    facts, diagnostics = analyze(variable("Destination", "Contact") + '''
      <recordLookups><name>LookupAccount</name><object>Account</object><storeOutputAutomatically>true</storeOutputAutomatically>
        <getFirstRecordOnly>true</getFirstRecordOnly><queriedFields>Name</queriedFields>
        <filters><field>Id</field><operator>EqualTo</operator><value><stringValue>fictional</stringValue></value></filters>
      </recordLookups>''' + assignment("Destination.LastName", "LookupAccount.Name"))
    assert fields(facts) == {("Account.Name", "reads"), ("Account.Id", "filters"), ("Contact.LastName", "writes")}
    assert not diagnostics
    source = next(item for item in facts["evidence"] if item["rule"] == "flow.assignment_source")
    assert source["resolution"]["basis"] == "automatic_get_records_output"
    assert source["resolution"]["collection"] is False
    assert set(source["resolution"]["declaration"]) == {"path", "locator"}


def test_explicit_outputs_do_not_turn_scalar_destinations_into_records():
    facts, diagnostics = analyze('''
      <variables><name>ScalarName</name><dataType>String</dataType><isCollection>false</isCollection></variables>
      <recordLookups><name>Lookup</name><object>Account</object><storeOutputAutomatically>false</storeOutputAutomatically>
        <outputAssignments><field>Name</field><assignToReference>ScalarName</assignToReference></outputAssignments>
      </recordLookups>''' + variable("Target", "Contact") + assignment("Target.LastName", "ScalarName.Name"))
    assert fields(facts) == {("Account.Name", "reads"), ("Contact.LastName", "writes")}
    assert "FLOW_RECORD_TYPE_UNRESOLVED" in codes(diagnostics)


def test_declared_explicit_record_output_and_namespaces():
    facts, diagnostics = analyze(variable("Saved", "pkg__Sample__c") + '''
      <recordLookups><name>Lookup</name><object>pkg__Sample__c</object><outputReference>Saved</outputReference>
        <queriedFields>pkg__Label__c</queriedFields></recordLookups>
      <recordUpdates><name>Update</name><object>pkg__Sample__c</object>
        <inputAssignments><field>pkg__Label__c</field><value><elementReference>Saved.pkg__Label__c</elementReference></value></inputAssignments>
      </recordUpdates>''')
    assert fields(facts) == {("pkg__Sample__c.pkg__Label__c", "reads"), ("pkg__Sample__c.pkg__Label__c", "writes")}
    assert not diagnostics
    assert any(edge["qualifiers"].get("effect") == "memory" for edge in facts["relationships"])
    assert any(edge["qualifiers"].get("effect") == "database" for edge in facts["relationships"])


def test_whole_record_updates_and_deletes_do_not_invent_field_writes():
    facts, diagnostics = analyze(variable("Rows", "Account", True) + '''
      <recordUpdates><name>UpdateRows</name><inputReference>Rows</inputReference></recordUpdates>
      <recordDeletes><name>DeleteRows</name><inputReference>Rows</inputReference></recordDeletes>''')
    assert fields(facts) == set()
    writes = [edge for edge in facts["relationships"] if edge["relation"] == "writes"]
    assert {edge["qualifiers"]["operation"] for edge in writes} == {"update", "delete"}
    assert all(edge["qualifiers"]["field_set"] == "unknown" and edge["qualifiers"]["collection"] for edge in writes)
    assert not diagnostics


def test_assignment_is_memory_write_and_same_named_fields_stay_distinct():
    facts, diagnostics = analyze(variable("Left", "Account") + variable("Right", "Contact") + assignment("Left.Name", "Right.Name"))
    assert fields(facts) == {("Account.Name", "writes"), ("Contact.Name", "reads")}
    assert all(edge["qualifiers"]["effect"] == "memory" for edge in facts["relationships"] if edge["relation"] == "writes")
    assert not diagnostics


def test_literal_write_evidence_keeps_memory_and_database_effects():
    facts, _ = analyze(variable("Current", "Account") + """
        <assignments><name>Prepare</name><assignmentItems>
          <assignToReference>Current.Stage__c</assignToReference><operator>Assign</operator>
          <value><stringValue>Ready</stringValue></value>
        </assignmentItems></assignments>
        <recordUpdates><name>Persist</name><object>Account</object><inputAssignments>
          <field>Stage__c</field><value><stringValue>Saved</stringValue></value>
        </inputAssignments></recordUpdates>""")
    evidence = {item["id"]: item for item in facts["evidence"]}
    writes = [edge for edge in facts["relationships"] if edge["target"] == "sf:field:account.stage__c"]
    values = {edge["qualifiers"]["effect"]: evidence[edge["evidence_ids"][0]]["resolution"]["assigned_value"]
              for edge in writes}
    assert values == {"memory": {"type": "string", "value": "Ready"},
                      "database": {"type": "string", "value": "Saved"}}


def test_calls_use_literal_targets_and_keep_missing_targets_referenced_only():
    facts, diagnostics = analyze('''
      <subflows><name>RunChild</name><flowName>ChildFlow</flowName></subflows>
      <actionCalls><name>RunAction</name><actionType>apex</actionType><actionName>pkg__ExampleAction</actionName></actionCalls>
      <actionCalls><name>Dynamic</name><actionType>apex</actionType><actionName>{!ActionName}</actionName></actionCalls>
      <actionCalls><name>Other</name><actionType>emailSimple</actionType><actionName>emailSimple</actionName></actionCalls>''')
    targets = {item["id"]: item for item in facts["components"]}
    calls = [edge for edge in facts["relationships"] if edge["relation"] == "calls"]
    assert {(targets[edge["target"]]["kind"], targets[edge["target"]]["api_name"]) for edge in calls} == {
        ("flow", "ChildFlow"), ("apexclass", "pkg__ExampleAction")}
    assert all(targets[edge["target"]]["definition_status"] == "referenced_only" for edge in calls)
    assert codes(diagnostics) == {"FLOW_CALL_TARGET_DYNAMIC", "FLOW_ACTION_UNSUPPORTED"}


@pytest.mark.parametrize("reference,expected", [
    ("Row.Owner.Name", "FLOW_RELATIONSHIP_PATH_UNSUPPORTED"),
    ("Row[0].Name", "FLOW_REFERENCE_DYNAMIC"),
    ("Missing.Name", "FLOW_SYMBOL_UNRESOLVED"),
])
def test_unresolved_paths_never_truncate_or_guess(reference, expected):
    facts, diagnostics = analyze(variable("Row", "Account") + assignment("Row.Name", reference))
    assert fields(facts) == {("Account.Name", "writes")}
    assert expected in codes(diagnostics)


def test_collection_field_projection_and_duplicate_symbols_are_reported():
    facts, diagnostics = analyze(variable("Rows", "Account", True) + variable("Duplicate", "Account") + variable("Duplicate", "Contact") +
                                 assignment("Rows.Name", "Duplicate.Name"))
    assert fields(facts) == set()
    assert {"FLOW_COLLECTION_FIELD_UNSUPPORTED", "FLOW_SYMBOL_AMBIGUOUS"} <= codes(diagnostics)


def test_formula_unknown_nested_and_unused_reference_context_are_explicit():
    facts, diagnostics = analyze('''
      <start><object>Account</object><filterFormula>$Record.Name = "example"</filterFormula></start>
      <formulas><name>FormulaValue</name><dataType>String</dataType><expression>$Record.Name</expression></formulas>
      <newContainer><filterFormula>$Record.Name</filterFormula><newChild>value</newChild></newContainer>
      <processMetadataValues><name>UnknownReferenceContext</name><value><elementReference>$Record.Name</elementReference></value></processMetadataValues>
      <loops><name>LoopRows</name><collectionReference>Rows</collectionReference></loops>''', version="65.0")
    assert fields(facts) == {("Account.Name", "filters"), ("Account.Name", "reads")}
    assert {"FLOW_CONSTRUCT_UNSUPPORTED", "FLOW_XML_ELEMENT_UNSUPPORTED", "FLOW_API_VERSION_UNTESTED", "FLOW_REFERENCE_CONTEXT_UNSUPPORTED"} <= codes(diagnostics)
    formula_diagnostics = [item for item in diagnostics if item.get("construct") == "filterFormula"]
    assert len(formula_diagnostics) == 1
    assert formula_diagnostics[0]["code"] == "FLOW_FORMULA_CONTEXT_UNSUPPORTED"
    assert len([item for item in diagnostics if item["code"] == "FLOW_FORMULA_ANALYSIS_BOUNDED"]) == 2
    assert all(item["location"]["locator"] for item in diagnostics)


def test_whitespace_changes_positions_but_not_occurrence_identity():
    body = variable("Row", "Account") + assignment("Row.Name", "Row.Name")
    first, _ = analyze(body)
    second, _ = analyze(body.replace("><", ">\n  <"))
    assert {item["id"] for item in first["relationships"]} == {item["id"] for item in second["relationships"]}
    assert {item["id"] for item in first["evidence"]} == {item["id"] for item in second["evidence"]}
    assert {item["content_hash"] for item in first["evidence"]} != {item["content_hash"] for item in second["evidence"]}


def test_ambiguous_component_targets_are_not_silently_selected():
    store = FactStore()
    store.ensure_component("apexclass", "ExampleAction", {"path": "one/ExampleAction.cls", "locator": "file"})
    store.ensure_component("apexclass", "ExampleAction", {"path": "two/ExampleAction.cls", "locator": "file"})
    facts, diagnostics = analyze('<actionCalls><name>Call</name><actionType>apex</actionType><actionName>ExampleAction</actionName></actionCalls>', store=store)
    assert not any(edge["relation"] == "calls" for edge in facts["relationships"])
    assert "ambiguous_component" in codes(diagnostics)


def test_unsupported_nested_record_shape_does_not_invent_field_write():
    facts, diagnostics = analyze('''
      <recordDeletes><name>DeleteRows</name><object>Account</object>
        <inputAssignments><field>Name</field><value><stringValue>Example</stringValue></value></inputAssignments>
      </recordDeletes>''')
    assert fields(facts) == set()
    assert "FLOW_XML_ELEMENT_UNSUPPORTED" in codes(diagnostics)


def test_generic_records_and_unsupported_automatic_create_outputs_are_visible():
    facts, diagnostics = analyze('''
      <variables><name>GenericRow</name><dataType>SObject</dataType></variables>
      <recordCreates><name>CreateRow</name><object>Account</object><storeOutputAutomatically>true</storeOutputAutomatically></recordCreates>''')
    assert fields(facts) == set()
    assert codes(diagnostics) == {"FLOW_RECORD_TYPE_UNRESOLVED", "FLOW_CREATE_OUTPUT_TYPING_UNSUPPORTED"}
