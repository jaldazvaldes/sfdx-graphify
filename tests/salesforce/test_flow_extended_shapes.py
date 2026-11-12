"""Typed Flow screen, template, iteration, and transformation dependencies."""

from graphify.salesforce.flow import extract_flow
from graphify.salesforce.schema import FactStore
from graphify.salesforce.xml_source import parse_xml


def _analyze(body):
    xml = f'''<Flow xmlns="http://soap.sforce.com/2006/04/metadata">
      <apiVersion>64.0</apiVersion>{body}
    </Flow>'''
    document = parse_xml("flows/Extended.flow-meta.xml", xml.encode())
    store = FactStore()
    store.ensure_component("flow", "Extended", document.location(document.root))
    extract_flow(document, store)
    return store


def _field_facts(store):
    return {(edge["target"], edge["relation"], store.evidence[ev]["rule"])
            for edge in store.relationships.values() for ev in edge["evidence_ids"]
            if edge["target"].startswith("sf:field:")}


def test_collection_filter_binds_declarative_operands_and_typed_loop_outputs():
    store = _analyze('''
      <variables><name>Items</name><dataType>SObject</dataType><isCollection>true</isCollection><objectType>OrderItem</objectType></variables>
      <variables><name>Current</name><dataType>SObject</dataType><objectType>OrderItem</objectType></variables>
      <screens><name>Screen</name><connector><targetReference>Loop</targetReference></connector><fields>
        <name>Table</name><fieldType>ComponentInstance</fieldType><extensionName>flowruntime:datatable</extensionName>
        <storeOutputAutomatically>true</storeOutputAutomatically>
        <dataTypeMappings><typeName>T</typeName><typeValue>OrderItem</typeValue></dataTypeMappings>
      </fields></screens>
      <loops><name>Loop</name><collectionReference>Table.editedRows</collectionReference>
        <nextValueConnector><targetReference>Filter</targetReference></nextValueConnector>
        <noMoreValuesConnector><targetReference>LoopFiltered</targetReference></noMoreValuesConnector>
      </loops>
      <collectionProcessors><name>Filter</name><collectionProcessorType>FilterCollectionProcessor</collectionProcessorType>
        <assignNextValueToReference>Current</assignNextValueToReference><collectionReference>Items</collectionReference>
        <conditionLogic>and</conditionLogic><conditions><operator>NotEqualTo</operator>
          <leftValueReference>Current.PricebookEntryId</leftValueReference>
          <rightValue><elementReference>Loop.Product2Id</elementReference></rightValue>
        </conditions><connector><targetReference>Loop</targetReference></connector>
      </collectionProcessors>
      <loops><name>LoopFiltered</name><collectionReference>Filter</collectionReference>
        <nextValueConnector><targetReference>Assign</targetReference></nextValueConnector>
        <noMoreValuesConnector><targetReference>After</targetReference></noMoreValuesConnector>
      </loops>
      <assignments><name>Assign</name><assignmentItems><assignToReference>LoopFiltered.Quantity</assignToReference>
        <operator>Assign</operator><value><numberValue>1</numberValue></value>
      </assignmentItems><connector><targetReference>Update</targetReference></connector></assignments>
      <recordUpdates><name>Update</name><inputReference>LoopFiltered</inputReference>
        <connector><targetReference>LoopFiltered</targetReference></connector>
      </recordUpdates>
      <assignments><name>After</name><assignmentItems><assignToReference>LoopFiltered.Outside__c</assignToReference>
        <operator>Assign</operator><value><numberValue>1</numberValue></value>
      </assignmentItems></assignments>
    ''')
    assert _field_facts(store) == {
        ("sf:field:orderitem.pricebookentryid", "filters", "flow.collection_filter_left_operand"),
        ("sf:field:orderitem.product2id", "filters", "flow.collection_filter_right_operand"),
        ("sf:field:orderitem.quantity", "writes", "flow.assignment_destination"),
    }
    assert any(edge["target"] == "sf:object:orderitem" and edge["relation"] == "writes"
               and edge["qualifiers"]["effect"] == "database" for edge in store.relationships.values())
    assert {item["code"] for item in store.diagnostics} == {"FLOW_LOOP_ITEM_SCOPE_UNRESOLVED"}


def test_loop_current_item_requires_a_typed_collection_and_no_bypass():
    store = _analyze('''
      <variables><name>Rows</name><dataType>SObject</dataType><objectType>Account</objectType><isCollection>true</isCollection></variables>
      <startElementReference>Branch</startElementReference>
      <decisions><name>Branch</name><defaultConnector><targetReference>Inside</targetReference></defaultConnector>
        <rules><name>Iterate</name><connector><targetReference>Loop</targetReference></connector></rules>
      </decisions>
      <loops><name>Loop</name><collectionReference>Rows</collectionReference>
        <nextValueConnector><targetReference>Inside</targetReference></nextValueConnector>
      </loops>
      <assignments><name>Inside</name><assignmentItems><assignToReference>Loop.Guessed__c</assignToReference>
        <operator>Assign</operator><value><numberValue>1</numberValue></value>
      </assignmentItems></assignments>
    ''')
    assert _field_facts(store) == set()
    assert "FLOW_LOOP_ITEM_SCOPE_UNRESOLVED" in {item["code"] for item in store.diagnostics}


def test_screen_and_rich_text_template_emit_typed_references_only():
    store = _analyze('''
      <start><object>Account</object></start>
      <textTemplates><name>Greeting</name><text>Customer's &lt;a href="{!$Record.Website}"&gt;{!$Record.Name}&lt;/a&gt; Account.Fake__c</text></textTemplates>
      <screens><name>Review</name><fields><name>Region</name><fieldType>Region</fieldType><fields>
        <name>Notes</name><fieldType>InputField</fieldType><fieldText>Hello {!$Record.Description}</fieldText>
        <defaultValue><elementReference>$Record.Fax</elementReference></defaultValue>
        <validationRule><formulaExpression>LEN({!$Record.AccountNumber}) &gt; 0</formulaExpression></validationRule>
        <visibilityRule><conditionLogic>and</conditionLogic><conditions>
          <leftValueReference>$Record.Industry</leftValueReference><operator>IsNull</operator>
          <rightValue><booleanValue>false</booleanValue></rightValue>
        </conditions></visibilityRule>
      </fields><fields><name>Phone</name><fieldType>ObjectProvided</fieldType><objectFieldReference>$Record.Phone</objectFieldReference></fields>
      <fields><name>Label</name><fieldType>DisplayText</fieldType><fieldText>{!Greeting}</fieldText></fields></fields></screens>
    ''')
    assert _field_facts(store) == {
        ("sf:field:account.website", "reads", "flow.text_template_reference"),
        ("sf:field:account.name", "reads", "flow.text_template_reference"),
        ("sf:field:account.description", "reads", "flow.screen_text_reference"),
        ("sf:field:account.fax", "reads", "flow.screen_default"),
        ("sf:field:account.accountnumber", "filters", "flow.formula_reference"),
        ("sf:field:account.industry", "filters", "flow.screen_visibility_left_operand"),
        ("sf:field:account.phone", "references", "flow.screen_object_field"),
    }
    assert all("merge_reference" in item["resolution"] and "/reference[" in item["locator"]
               for item in store.evidence.values() if item["rule"] == "flow.text_template_reference")
    assert {item["code"] for item in store.diagnostics} == {"FLOW_FORMULA_ANALYSIS_BOUNDED"}


def test_transform_map_has_typed_input_and_memory_write_with_unsupported_action_diagnostic():
    store = _analyze('''
      <start><object>Lead</object></start>
      <variables><name>Leads</name><dataType>SObject</dataType><objectType>Lead</objectType><isCollection>true</isCollection></variables>
      <formulas><name>OutsideTransform</name><dataType>String</dataType><expression>{!Leads[$EachItem].Unscoped__c}</expression></formulas>
      <transforms><name>NewAccounts</name><dataType>SObject</dataType><objectType>Account</objectType><isCollection>true</isCollection>
        <transformValues>
          <transformValueActions><transformType>Map</transformType><outputFieldApiName>Name</outputFieldApiName>
            <value><elementReference>Leads[$EachItem].Company</elementReference></value>
          </transformValueActions>
          <transformValueActions><transformType>Map</transformType><outputFieldApiName>Description</outputFieldApiName>
            <value><formulaDataType>String</formulaDataType><formulaExpression>TRIM({!Leads[$EachItem].Description}) &amp; "{!Leads[$EachItem].Literal__c}" /* {!Leads[$EachItem].Comment__c} */</formulaExpression></value>
          </transformValueActions>
          <transformValueActions><transformType>InnerJoin</transformType><outputFieldApiName>Unsupported__c</outputFieldApiName></transformValueActions>
        </transformValues>
      </transforms>
      <recordCreates><name>Save</name><inputReference>NewAccounts</inputReference></recordCreates>
    ''')
    assert _field_facts(store) == {
        ("sf:field:lead.company", "reads", "flow.transform_input"),
        ("sf:field:lead.description", "reads", "flow.formula_reference"),
        ("sf:field:account.name", "writes", "flow.transform_output"),
        ("sf:field:account.description", "writes", "flow.transform_output"),
    }
    assert all(edge["qualifiers"]["effect"] == "memory" for edge in store.relationships.values()
               if edge["target"].startswith("sf:field:account."))
    assert {item["code"] for item in store.diagnostics} == {
        "FLOW_FORMULA_ANALYSIS_BOUNDED", "FLOW_FORMULA_SYNTAX_UNSUPPORTED", "FLOW_TRANSFORM_ACTION_UNSUPPORTED",
    }
    issue, = (item for item in store.diagnostics if item["code"] == "FLOW_FORMULA_SYNTAX_UNSUPPORTED")
    assert "OutsideTransform" in issue["location"]["locator"]
