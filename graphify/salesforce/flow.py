"""Bounded, deterministic Salesforce Flow dependency extraction.

The handlers follow the public Metadata API Flow reference. They deliberately
do not evaluate formulas or model execution order.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from dataclasses import replace

from .schema import TESTED_API_VERSIONS, canonical_json
from .symbols import API_NAME, RESOURCE_NAME, index_symbols
from .formula_refs import formula_references
from .relationship_paths import SOURCE_STANDARD_PARENTS, add_navigation_reads, resolve_field_path

_BASE = {"name", "label", "description", "locationX", "locationY", "connector", "faultConnector", "processMetadataValues"}
_OP = _BASE | {"object", "filters", "filterLogic"}
_VALUE = {"elementReference", "stringValue", "booleanValue", "numberValue", "dateValue", "dateTimeValue"}
_UNSUPPORTED = {
    "formula": "Formula expressions are outside v0.1 expression coverage.",
    "screens": "Screen expression contexts are outside v0.1 coverage.",
    "loops": "Loop alias propagation is outside v0.1 coverage.",
    "collectionProcessors": "Collection processing is outside v0.1 coverage.",
    "collectionFilter": "Collection filtering is outside v0.1 coverage.",
    "dynamicChoiceSets": "Dynamic choice resources are outside v0.1 coverage.",
    "choices": "Choice resources are outside v0.1 coverage.",
    "textTemplates": "Text template expression parsing is outside v0.1 coverage.",
    "apexPluginCalls": "Apex plug-in calls are outside v0.1 coverage.",
    "waits": "Wait event expressions are outside v0.1 coverage.",
    "recordRollbacks": "Transaction rollback semantics are outside v0.1 coverage.",
    "relatedRecords": "Related record lookup traversal is outside v0.1 coverage.",
    "scheduledPaths": "Scheduled path expressions are outside v0.1 coverage.",
    "transforms": "Transform expressions are outside v0.1 coverage.",
}
_CHILDREN = {
    "Flow": {"apiVersion", "fullName", "label", "description", "interviewLabel", "processType", "status", "runInMode",
             "start", "startElementReference", "variables", "constants", "recordLookups", "recordCreates", "recordUpdates",
             "recordDeletes", "assignments", "decisions", "actionCalls", "subflows", "processMetadataValues", "environments",
             "isAdditionalPermissionRequiredToRun", "isTemplate", "isOverridable", "areMetricsLoggedToDataCloud",
             "triggerOrder", "migratedFromWorkflowRuleName", "timeZoneSidKey", "builder", "sourceTemplate", "apiVersionRuntime", "formulas"}
            | set(_UNSUPPORTED),
    "start": _BASE | {"object", "triggerType", "recordTriggerType", "filters", "filterLogic", "filterFormula",
                      "doesRequireRecordChangedToMeetCriteria", "schedule",
                      "scheduledPaths", "segment", "eventName", "eventType"},
    "variables": {"name", "description", "dataType", "objectType", "isCollection", "isInput", "isOutput", "value", "scale", "apexClass"},
    "constants": {"name", "description", "dataType", "value", "scale"},
    "recordLookups": _OP | {"outputAssignments", "outputReference", "queriedFields", "storeOutputAutomatically",
                           "getFirstRecordOnly", "assignNullValuesIfNoRecordsFound", "sortField", "sortOrder",
                           "getRecordsLimit", "relatedRecords"},
    "recordCreates": _OP | {"inputReference", "inputAssignments", "assignRecordIdToReference", "doesUpsert",
                           "upsertExternalIdField", "doesRollbackOnError", "storeOutputAutomatically"},
    "recordUpdates": _OP | {"inputReference", "inputAssignments"},
    "recordDeletes": _OP | {"inputReference"},
    "filters": {"field", "operator", "value"},
    "inputAssignments": {"field", "name", "value"},
    "outputAssignments": {"field", "name", "assignToReference"},
    "inputParameters": {"name", "value"},
    "outputParameters": {"name", "assignToReference"},
    "value": _VALUE,
    "rightValue": _VALUE,
    "getRecordsLimit": _VALUE,
    "assignments": _BASE | {"assignmentItems"},
    "assignmentItems": {"assignToReference", "operator", "value"},
    "decisions": _BASE | {"defaultConnector", "defaultConnectorLabel", "rules"},
    "rules": {"name", "label", "description", "processMetadataValues", "conditionLogic", "conditions",
              "connector", "doesRequireRecordChangedToMeetCriteria"},
    "conditions": {"processMetadataValues", "leftValueReference", "operator", "rightValue"},
    "actionCalls": _BASE | {"actionType", "actionName", "inputParameters", "outputParameters", "storeOutputAutomatically",
                           "flowTransactionModel", "nameSegment", "versionSegment"},
    "subflows": _BASE | {"flowName", "inputAssignments", "outputAssignments", "storeOutputAutomatically"},
    "connector": {"targetReference", "isGoTo"},
    "faultConnector": {"targetReference", "isGoTo"},
    "defaultConnector": {"targetReference", "isGoTo"},
    "processMetadataValues": {"name", "value"},
    "schedule": {"frequency", "startDate", "startTime"},
    "formulas": {"name", "description", "dataType", "expression", "scale"},
    "textTemplates": {"name", "description", "text", "isViewedAsPlainText"},
}


class _Extractor:
    def __init__(self, document, store, source):
        self.doc = document
        self.store = store
        self.source = source
        self.symbols = index_symbols(document, store)
        self.handled_references = set()
        self.handled_formulas = set()
        self.formula_stack = set()
        self.formula_done = set()
        self.formula_tokens = {}

    def diagnostic(self, code, message, node, state="unsupported", **extra):
        self.store.diagnostic(code, message, self.doc.location(node), state=state, **extra)

    def object_edge(self, owner, relation, node, rule, qualifiers=None, resolution=None):
        if not owner or not API_NAME.fullmatch(owner):
            self.diagnostic("FLOW_OBJECT_UNRESOLVED", "The operation does not establish a literal object type.", node,
                            "unresolved" if not owner else "dynamic")
            return
        target = self.store.resolve_component("object", owner, self.doc.location(node))
        if target:
            self.store.relation(self.source, target, relation, self.doc.location(node), rule,
                                resolution=resolution or {"basis": "explicit_object", "object": owner}, qualifiers=qualifiers)

    def field_edge(self, owner, field, relation, node, rule, qualifiers=None, resolution=None):
        self.handled_references.add(node.locator)
        if not owner:
            self.diagnostic("FLOW_FIELD_OWNER_UNRESOLVED", "The field's owning object is not established.", node, "unresolved")
            return
        if "." in (field or ""):
            resolved = resolve_field_path(self.store, owner, field, standard_parents=SOURCE_STANDARD_PARENTS)
            if resolved is None:
                self.diagnostic("FLOW_RELATIONSHIP_PATH_UNSUPPORTED", "The complete parent relationship path is not uniquely established.", node)
                return
            resolution = {**(resolution or {"basis": "explicit_object", "object": owner}),
                          "relationship_path": field, "steps": resolved["steps"]}
            owner, field = resolved["owner"], resolved["field"]
        elif not API_NAME.fullmatch(field or ""):
            self.diagnostic("FLOW_FIELD_DYNAMIC", "The field name is not a literal API name.", node, "dynamic")
            return
        location = self.doc.location(node)
        target = self.store.field(owner, field, location)
        if target is None:
            return
        resolution = resolution or {"basis": "explicit_object", "object": owner}
        if node.locator in self.formula_tokens:
            resolution = {**resolution, "formula_reference": self.formula_tokens[node.locator]}
        self.store.relation(self.source, target, relation, location, rule,
                            resolution=resolution, qualifiers=qualifiers)
        if "steps" in resolution:
            add_navigation_reads(self.store, self.source, location, resolution, rule="flow.relationship_navigation",
                                 qualifiers={"context": "flow_relationship_navigation"})

    def reference(self, node, relation="reads", rule="flow.direct_reference", qualifiers=None):
        if node is None:
            return
        self.handled_references.add(node.locator)
        value = node.text.strip()
        if value.startswith("{!") and value.endswith("}"):
            value = value[2:-1].strip()
        parts = value.split(".")
        if parts[0].casefold() == "$setup" and len(parts) == 3 and all(API_NAME.fullmatch(p) for p in parts[1:]):
            self.field_edge(parts[1], parts[2], relation, node, rule, qualifiers,
                            {"basis": "explicit_setup_reference", "object": parts[1]})
            return
        if parts[0].casefold() == "$custommetadata" and len(parts) == 4 and all(API_NAME.fullmatch(p) for p in parts[1:]):
            self.field_edge(parts[1], parts[3], relation, node, rule, qualifiers,
                            {"basis": "explicit_custom_metadata_reference", "object": parts[1], "record": parts[2]})
            return
        if not parts or not RESOURCE_NAME.fullmatch(parts[0]) or not all(API_NAME.fullmatch(p) for p in parts[1:]):
            self.diagnostic("FLOW_REFERENCE_DYNAMIC", "Only a literal resource or direct resource field is supported.", node, "dynamic")
            return
        symbol = self.symbols.resolve(parts[0], node, require_record=len(parts) >= 2)
        if symbol is None:
            return
        if symbol.basis == "formulas":
            if relation == "writes":
                self.diagnostic("FLOW_FORMULA_DESTINATION_UNSUPPORTED", "A formula resource is not a writable record destination.", node)
                return
            self.formula_resource(symbol.name, relation, qualifiers)
            return
        if symbol.basis in ("textTemplates", "dynamicChoiceSets", "choices", "unsupported_call_output", "unsupported_create_output"):
            self.diagnostic("FLOW_RESOURCE_DEPENDENCIES_UNSUPPORTED", "The referenced resource requires an unsupported dependency handler.", node)
            return
        if not symbol.object_type:
            return  # A declared scalar carries no object/field dependency.
        basis = {"basis": symbol.basis, "symbol": symbol.name, "object": symbol.object_type,
                 "collection": symbol.collection,
                 "declaration": {key: symbol.location[key] for key in ("path", "locator")}}
        qualified = dict(qualifiers or {})
        if parts[0].casefold() in ("$record", "$record__prior"):
            qualified["record_context"] = "prior" if parts[0].casefold() == "$record__prior" else "current"
        if len(parts) >= 2:
            if symbol.collection:
                self.diagnostic("FLOW_COLLECTION_FIELD_UNSUPPORTED", "Direct collection field projection requires unsupported collection semantics.", node)
                return
            self.field_edge(symbol.object_type, ".".join(parts[1:]), relation, node, rule, qualified, basis)
        else:
            qualified.update({"field_set": "unknown", "collection": symbol.collection})
            self.object_edge(symbol.object_type, relation, node, rule, qualified, basis)

    def formula(self, node, relation, qualifiers=None):
        """Record typed lexical dependencies; formula truth and evaluation stay unknown."""
        self.handled_formulas.add(node.locator)
        references, issues = formula_references(node.text)
        self.diagnostic("FLOW_FORMULA_ANALYSIS_BOUNDED",
                        "Only literal typed formula references are indexed; evaluation and dynamic dependencies are not modeled.",
                        node, severity="info")
        for issue in issues:
            self.diagnostic("FLOW_FORMULA_SYNTAX_UNSUPPORTED", "Formula syntax prevents complete reference extraction.",
                            node, construct=issue)
        for index, reference in enumerate(references, 1):
            token = replace(node, text=reference.text, children=[], locator=f"{node.locator}/reference[{index}]")
            self.formula_tokens[token.locator] = {
                "expression_locator": node.locator, "decoded_character_offset": reference.offset,
                "source_position_basis": "containing_xml_expression_element",
            }
            self.reference(token, relation, "flow.formula_reference", qualifiers)

    def formula_resource(self, name, relation="reads", qualifiers=None):
        resources = [node for node in self.doc.root.all("formulas") if node.value("name").casefold() == name.casefold()]
        if len(resources) != 1:
            return  # Symbol resolution reports duplicates before entering this handler.
        resource = resources[0]
        if name.casefold() in self.formula_stack:
            self.diagnostic("FLOW_FORMULA_CYCLE", "Formula resources refer to each other recursively.", resource, "ambiguous")
            return
        qualified = {**(qualifiers or {}), "formula_resource": name}
        cache_key = (name.casefold(), relation, canonical_json(qualified))
        if cache_key in self.formula_done:
            return
        if len(self.formula_stack) >= 64:
            self.diagnostic("FLOW_FORMULA_DEPTH_UNSUPPORTED", "Formula resource expansion exceeds the supported nesting depth.", resource)
            return
        expressions = resource.all("expression")
        if len(expressions) != 1:
            self.diagnostic("FLOW_FORMULA_EXPRESSION_UNRESOLVED", "Formula resource needs a unique expression.", resource, "unresolved")
            return
        self.formula_stack.add(name.casefold())
        try:
            self.formula(expressions[0], relation, qualified)
            self.formula_done.add(cache_key)
        finally:
            self.formula_stack.remove(name.casefold())

    def formulas(self):
        for resource in self.doc.root.all("formulas"):
            name = resource.value("name")
            symbol = self.symbols.resolve(name, resource)
            if symbol and symbol.basis == "formulas":
                self.formula_resource(name)

    def value(self, container, relation="reads", rule="flow.value_reference", qualifiers=None):
        if container is None:
            return
        for child in container.children:
            if child.tag == "elementReference":
                self.reference(child, relation, rule, qualifiers)
            elif child.tag not in _VALUE:
                self.diagnostic("FLOW_VALUE_UNSUPPORTED", "This value representation is outside v0.1 coverage.", child)

    def filters(self, node, owner, operation):
        for item in node.all("filters"):
            field = item.child("field")
            if field is not None:
                self.field_edge(owner, field.text.strip(), "filters", field, "flow.filter_field", {"operation": operation})
            self.value(item.child("value"), "filters", "flow.filter_operand", {"operation": operation})

    def decisions(self):
        """Extract direct rule operands, without evaluating outcome or rule order."""
        for decision in self.doc.root.all("decisions"):
            for rule in decision.all("rules"):
                context = {"operation": "decision", "decision": decision.value("name"),
                           "rule": rule.value("name"), "condition_logic": rule.value("conditionLogic")}
                changed = rule.value("doesRequireRecordChangedToMeetCriteria")
                if changed:
                    context["record_changed_to_meet_criteria"] = changed
                for condition in rule.all("conditions"):
                    # Never prefer one operand when malformed XML repeats it.
                    left, right = condition.all("leftValueReference"), condition.all("rightValue")
                    repeated_value = right and sum(child.tag in _VALUE for child in right[0].children) > 1
                    if len(left) > 1 or len(right) > 1 or len(condition.all("operator")) > 1 or repeated_value:
                        self.diagnostic("FLOW_DECISION_CONDITION_AMBIGUOUS",
                                        "A decision condition repeats a scalar operand or operator.",
                                        condition, "ambiguous")
                        continue
                    qualifiers = {**context, "operator": condition.value("operator")}
                    if left:
                        self.reference(left[0], "filters", "flow.decision_left_operand",
                                       {**qualifiers, "operand": "left"})
                    else:
                        self.diagnostic("FLOW_DECISION_CONDITION_UNSUPPORTED",
                                        "A direct decision condition requires a left resource reference; nested conditions are outside coverage.",
                                        condition)
                    if right:
                        self.value(right[0], "filters", "flow.decision_right_operand",
                                   {**qualifiers, "operand": "right"})

    def start(self):
        start = self.doc.root.child("start")
        if start is None:
            if self.doc.root.value("processType") == "Workflow":
                self.process_builder_start()
            return
        owner = start.value("object")
        if owner:
            self.object_edge(owner, "references", start.child("object"), "flow.trigger_object", {
                "operation": "trigger", "trigger_type": start.value("triggerType"), "record_trigger_type": start.value("recordTriggerType"),
            })
        self.filters(start, owner, "start")
        formulas = start.all("filterFormula")
        if len(formulas) > 1:
            self.diagnostic("FLOW_FILTER_FORMULA_AMBIGUOUS", "Start repeats the formula expression.", start, "ambiguous")
        elif formulas:
            self.formula(formulas[0], "filters", {"operation": "start", "context": "filter_formula"})

    def process_builder_start(self):
        metadata = {}
        for item in self.doc.root.all("processMetadataValues"):
            names = item.all("name")
            if any(node.text in {"ObjectType", "TriggerType"} for node in names):
                if len(names) != 1:
                    self.diagnostic("FLOW_PROCESS_TRIGGER_AMBIGUOUS", "Process Builder metadata repeats the entry name.", item, "ambiguous")
                    return
                metadata.setdefault(names[0].text, []).append(item)
        owners = metadata.get("ObjectType", [])
        triggers = metadata.get("TriggerType", [])
        if not owners:
            return
        if len(owners) != 1 or len(triggers) > 1:
            self.diagnostic("FLOW_PROCESS_TRIGGER_AMBIGUOUS", "Process Builder trigger metadata is repeated.",
                            owners[0], "ambiguous")
            return
        values = owners[0].all("value")
        if len(values) != 1 or len(values[0].children) != 1 or values[0].children[0].tag != "stringValue":
            self.diagnostic("FLOW_PROCESS_TRIGGER_UNRESOLVED", "Process Builder object must be a literal string.", owners[0], "unresolved")
            return
        value = values[0]
        owner = value.children[0].text
        trigger_type = ""
        if triggers:
            trigger_values = triggers[0].all("value")
            if (len(trigger_values) != 1 or len(trigger_values[0].children) != 1
                    or trigger_values[0].children[0].tag != "stringValue"):
                self.diagnostic("FLOW_PROCESS_TRIGGER_AMBIGUOUS", "Process Builder trigger type needs one literal value.", triggers[0], "ambiguous")
                return
            trigger_type = trigger_values[0].children[0].text
        self.object_edge(owner, "references", value.children[0], "flow.process_builder_trigger_object",
                         {"operation": "trigger", "trigger_type": trigger_type, "process_type": "Workflow"},
                         {"basis": "process_metadata_object_type", "object": owner})
        if API_NAME.fullmatch(owner):
            self.store.components[self.source]["trigger_object"] = owner
            self.store.components[self.source]["trigger_type"] = trigger_type or None

    def declarations(self):
        for variable in self.doc.root.all("variables"):
            symbol = self.symbols.resolve(variable.value("name"), variable)
            if symbol and symbol.object_type:
                self.object_edge(symbol.object_type, "references", variable.child("objectType"), "flow.variable_type",
                                 {"operation": "declaration", "collection": symbol.collection})
            self.value(variable.child("value"), rule="flow.variable_default")
        for constant in self.doc.root.all("constants"):
            self.value(constant.child("value"), rule="flow.constant_value")

    def operation_owner(self, node):
        owner = node.value("object")
        input_node = node.child("inputReference")
        symbol = None
        if input_node is not None:
            self.handled_references.add(input_node.locator)
            reference = input_node.text.strip()
            if not RESOURCE_NAME.fullmatch(reference):
                self.diagnostic("FLOW_RECORD_INPUT_UNSUPPORTED", "Record operations require a direct record resource.", input_node)
            else:
                symbol = self.symbols.resolve(reference, input_node, require_record=True)
            if symbol and symbol.object_type:
                if owner and owner.casefold() != symbol.object_type.casefold():
                    self.diagnostic("FLOW_OBJECT_TYPE_CONFLICT", "The explicit object conflicts with the input record type.", node, "ambiguous")
                    return None, None
                owner = owner or symbol.object_type
        return owner, symbol

    def operations(self):
        for tag, operation in (("recordLookups", "lookup"), ("recordCreates", "create"),
                               ("recordUpdates", "update"), ("recordDeletes", "delete")):
            for node in self.doc.root.all(tag):
                owner, symbol = self.operation_owner(node)
                if tag == "recordCreates" and node.value("doesUpsert") == "true":
                    operation_name = "upsert"
                else:
                    operation_name = operation
                relation = "reads" if tag == "recordLookups" else "writes"
                qualifier: dict[str, str | bool] = {"operation": operation_name, "effect": "database"}
                explicit_fields = node.all("queriedFields") if tag == "recordLookups" else node.all("inputAssignments")
                qualifier["field_set"] = "explicit" if explicit_fields else "unknown"
                if symbol:
                    qualifier["collection"] = symbol.collection
                elif tag == "recordLookups" and node.value("storeOutputAutomatically") == "true":
                    qualifier["collection"] = node.value("getFirstRecordOnly") != "true"
                location_node = node.child("object") or node.child("inputReference") or node
                self.object_edge(owner, relation, location_node, "flow.record_operation", qualifier)
                self.filters(node, owner, operation_name)
                for field in node.all("queriedFields") if tag == "recordLookups" else []:
                    self.field_edge(owner, field.text.strip(), "reads", field, "flow.queried_field", {"operation": operation_name})
                sort_field = node.child("sortField") if tag == "recordLookups" else None
                if sort_field is not None:
                    self.field_edge(owner, sort_field.text.strip(), "references", sort_field, "flow.sort_field", {"operation": "sort"})
                for item in node.all("inputAssignments") if tag in ("recordCreates", "recordUpdates") else []:
                    field = item.child("field")
                    if field is not None:
                        self.field_edge(owner, field.text.strip(), "writes", field, "flow.record_input_field", {"operation": operation_name, "effect": "database"})
                    self.value(item.child("value"), "reads", "flow.record_input_value", {"operation": operation_name})
                for item in node.all("outputAssignments") if tag == "recordLookups" else []:
                    field = item.child("field")
                    if field is not None:
                        self.field_edge(owner, field.text.strip(), "reads", field, "flow.record_output_field", {"operation": operation_name})
                    self.reference(item.child("assignToReference"), "writes", "flow.record_output_destination", {"operation": "assignment", "effect": "memory"})
                output = node.child("outputReference") if tag == "recordLookups" else None
                if output is not None:
                    self.handled_references.add(output.locator)
                    target = self.symbols.resolve(output.text.strip(), output, require_record=True)
                    if target and target.object_type and owner and target.object_type.casefold() != owner.casefold():
                        self.diagnostic("FLOW_OUTPUT_TYPE_CONFLICT", "The declared output resource type conflicts with the queried object.", output, "ambiguous")
                    elif target:
                        self.reference(output, "writes", "flow.record_output_reference", {"operation": "assignment", "effect": "memory"})
                if tag == "recordCreates":
                    self.reference(node.child("assignRecordIdToReference"), "writes", "flow.created_id_destination", {"operation": "assignment", "effect": "memory"})
                if tag == "recordLookups":
                    self.value(node.child("getRecordsLimit"), rule="flow.lookup_limit")
                upsert = node.child("upsertExternalIdField") if tag == "recordCreates" else None
                if upsert is not None:
                    self.field_edge(owner, upsert.text.strip(), "filters", upsert, "flow.upsert_key", {"operation": operation_name})

    def assignments(self):
        for assignment in self.doc.root.all("assignments"):
            for item in assignment.all("assignmentItems"):
                operator = item.value("operator")
                if operator not in ("Assign", "Add", "Subtract", "Multiply", "Divide"):
                    self.diagnostic("FLOW_ASSIGNMENT_OPERATOR_UNSUPPORTED", "This assignment operator is outside scalar/direct-record v0.1 coverage.", item)
                    continue
                qualifiers = {"operation": "assignment", "effect": "memory", "operator": operator}
                target = item.child("assignToReference")
                if target is not None and operator != "Assign" and "." not in target.text:
                    symbol = self.symbols.resolve(target.text.strip(), target)
                    if symbol and (symbol.collection or symbol.object_type):
                        self.diagnostic("FLOW_ASSIGNMENT_OPERATOR_UNSUPPORTED", "Collection and whole-record arithmetic is outside v0.1 coverage.", item)
                        continue
                self.reference(target, "writes", "flow.assignment_destination", qualifiers)
                if operator != "Assign":
                    self.reference(target, "reads", "flow.assignment_existing_value", qualifiers)
                self.value(item.child("value"), "reads", "flow.assignment_source", qualifiers)

    def calls(self):
        for tag in ("subflows", "actionCalls"):
            for node in self.doc.root.all(tag):
                kind = "subflow" if tag == "subflows" else node.value("actionType")
                target_node = node.child("flowName" if tag == "subflows" else "actionName")
                target_parts = target_node.text.strip().split(".") if target_node is not None else []
                expected_parts = 2 if kind == "outboundMessage" else 1
                if kind not in ("subflow", "apex", "outboundMessage"):
                    self.diagnostic("FLOW_ACTION_UNSUPPORTED", "Only literal subflow, Apex and outbound message actions are linked.", node)
                elif target_node is None or len(target_parts) != expected_parts or not all(API_NAME.fullmatch(part) for part in target_parts):
                    self.diagnostic("FLOW_CALL_TARGET_DYNAMIC", "The call target is missing or is not a literal API name.", target_node or node, "dynamic")
                else:
                    component_kind = {"subflow": "flow", "apex": "apexclass", "outboundMessage": "outboundmessage"}[kind]
                    target = self.store.resolve_component(component_kind, target_node.text.strip(), self.doc.location(target_node))
                    if target:
                        self.store.relation(self.source, target, "calls", self.doc.location(target_node), "flow.literal_call",
                                            resolution={"basis": "literal_target"}, qualifiers={"action_kind": kind})
                for item in node.all("inputAssignments") + node.all("inputParameters"):
                    self.value(item.child("value"), "reads", "flow.call_input", {"operation": "call_input", "action_kind": kind})
                for item in node.all("outputAssignments") + node.all("outputParameters"):
                    self.reference(item.child("assignToReference"), "writes", "flow.call_output", {"operation": "assignment", "effect": "memory", "action_kind": kind})

    def coverage(self):
        version = self.doc.root.value("apiVersion")
        if version not in TESTED_API_VERSIONS:
            self.diagnostic("FLOW_API_VERSION_UNTESTED", "Flow API version is outside the synthetic tested baseline; shape-based extraction continues.",
                            self.doc.root.child("apiVersion") or self.doc.root, version=version or None)
        process = self.doc.root.value("processType")
        if process and process not in ("Flow", "AutoLaunchedFlow"):
            self.diagnostic("FLOW_PROCESS_TYPE_UNTESTED", "The Flow process type is outside the tested baseline; shape-based extraction continues.", self.doc.root.child("processType"))

        def visit(node, inherited_unsupported=False):
            expected = _CHILDREN.get(node.tag, set())
            for child in node.children:
                unsupported = child.tag in _UNSUPPORTED
                unknown = child.tag not in expected
                if child.tag in {"filterFormula", "expression"} and child.locator not in self.handled_formulas:
                    self.diagnostic("FLOW_FORMULA_CONTEXT_UNSUPPORTED", "This formula occurs outside a supported expression context.",
                                    child, construct=child.tag)
                elif unsupported:
                    self.diagnostic("FLOW_CONSTRUCT_UNSUPPORTED", _UNSUPPORTED[child.tag], child, construct=child.tag)
                elif unknown and not inherited_unsupported:
                    self.diagnostic("FLOW_XML_ELEMENT_UNSUPPORTED", "The XML element is not classified by the v0.1 handler for this parent.", child,
                                    construct=child.tag, parent_construct=node.tag)
                elif child.tag in {"elementReference", "leftValueReference", "assignToReference", "inputReference", "outputReference", "field"} and child.locator not in self.handled_references and not inherited_unsupported:
                    self.diagnostic("FLOW_REFERENCE_CONTEXT_UNSUPPORTED", "This reference occurs outside a supported dependency context.", child,
                                    construct=child.tag, parent_construct=node.tag)
                # Do not lose recognized unsupported descendants inside an unknown tree.
                visit(child, inherited_unsupported or unsupported or unknown)

        visit(self.doc.root)


def extract_flow(document, store):
    """Extract supported dependency occurrences without evaluating the Flow."""
    if document.root.tag != "Flow":
        store.diagnostic("FLOW_ROOT_INVALID", "A Flow file must have a Flow root element.", document.location(document.root), state="unresolved", severity="error")
        return
    name = PurePosixPath(document.path.replace("\\", "/")).name.removesuffix(".flow-meta.xml")
    source = store.resolve_component("flow", name, document.location(document.root))
    if source is None:
        return
    extractor = _Extractor(document, store, source)
    extractor.start()
    extractor.decisions()
    extractor.declarations()
    extractor.formulas()
    extractor.operations()
    extractor.assignments()
    extractor.calls()
    extractor.coverage()
