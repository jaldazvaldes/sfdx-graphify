"""Validation rule dependencies with local activation and lexical formula evidence.

The formula scanner finds candidate fields; it does not evaluate a formula or
claim that an indexed rule is deployed. Unsupported globals and parent paths
remain diagnostics instead of being interpreted as fields of the rule's object.
"""
from __future__ import annotations

from .formula_refs import formula_references
from .relationship_paths import resolve_field_path
from .schema import API_NAME, component_id

_DESCRIPTIVE = {"fullName", "active", "description", "errorConditionFormula", "errorDisplayField", "errorMessage"}
_GLOBAL_OBJECTS = {"$user": "User", "$profile": "Profile", "$organization": "Organization", "$userrole": "UserRole"}
# Computed context properties are not SObject fields despite their global prefix.
_GLOBAL_NON_FIELDS = {("$user", "uitheme"), ("$user", "uithemedisplayed"), ("$profile", "licensetype")}
# Explicit non-polymorphic relationships, opt-in only for validation formulas.
# Salesforce Object Reference: standard-objects/object-{order,orderitem,
# campaignmember,case,opportunity}.html under:
# https://developer.salesforce.com/docs/platform/object-reference/references/
_STANDARD_FORMULA_PARENTS = {
    ("order", "account"): ("AccountId", "Account"),
    ("order", "shiptocontact"): ("ShipToContactId", "Contact"),
    ("orderitem", "order"): ("OrderId", "Order"),
    ("orderitem", "product2"): ("Product2Id", "Product2"),
    ("campaignmember", "contact"): ("ContactId", "Contact"),
    ("case", "account"): ("AccountId", "Account"),
    ("opportunity", "account"): ("AccountId", "Account"),
}


def definition_metadata(document, store):
    """Classify only one explicit XML boolean; retain uncertain definitions."""
    statuses = document.root.all("active")
    values = {"true": "Active", "1": "Active", "false": "Inactive", "0": "Inactive"}
    if len(statuses) == 1 and not statuses[0].children and statuses[0].text in values:
        return {"local_status": values[statuses[0].text]}
    code = "VALIDATION_RULE_STATUS_AMBIGUOUS" if len(statuses) > 1 else "VALIDATION_RULE_STATUS_UNKNOWN"
    for node in statuses or [document.root]:
        store.diagnostic(code, "A unique explicit local active boolean is required to classify rule activation.",
                         document.location(node), state="ambiguous" if len(statuses) > 1 else "unresolved",
                         candidates=[item.text for item in statuses])
    return {"local_status": "Unknown"}


def _field_context(reference, owner):
    """Return a typed object and field path, without guessing from token spelling."""
    parts = reference.split(".")
    if not reference.startswith("$"):
        return owner, reference, {"basis": "validation_rule_object", "object": owner}
    global_name = parts[0].casefold()
    if not all(API_NAME.fullmatch(part) for part in parts[1:]):
        return None
    if global_name in _GLOBAL_OBJECTS and len(parts) == 2:
        if (global_name, parts[1].casefold()) in _GLOBAL_NON_FIELDS:
            return None
        target = _GLOBAL_OBJECTS[global_name]
        return target, parts[1], {"basis": "explicit_global_object", "symbol": parts[0], "object": target}
    if global_name == "$setup" and len(parts) == 3:
        return parts[1], parts[2], {"basis": "explicit_setup_reference", "object": parts[1]}
    if global_name == "$custommetadata" and len(parts) == 4:
        return parts[1], parts[3], {"basis": "explicit_custom_metadata_reference", "object": parts[1], "record": parts[2]}
    if global_name == "$objecttype" and len(parts) == 4 and parts[2].casefold() == "fields":
        return parts[1], parts[3], {"basis": "explicit_object_type_field", "object": parts[1]}
    return None


def _resolve_formula_field(store, owner, field_path):
    """Resolve a bounded standard vocabulary and documented RecordType fields.

    RecordType availability depends on object configuration. This establishes a
    syntactic dependency, not whether that object has record types in an org.
    Local conflicts must block the standard fallback rather than be overridden.
    """
    resolved = resolve_field_path(store, owner, field_path, standard_parents=_STANDARD_FORMULA_PARENTS)
    parts = field_path.split(".")
    if (len(parts) < 2 or parts[-2].casefold() != "recordtype"
            or parts[-1].casefold() not in {"id", "name", "developername"}):
        return resolved
    prefix_steps = []
    if len(parts) > 2:
        # A terminal Id lets the shared resolver establish just the preceding
        # owner and lookup hops, without creating a fabricated field or edge.
        prefix = resolve_field_path(store, owner, ".".join([*parts[:-2], "Id"]),
                                    standard_parents=_STANDARD_FORMULA_PARENTS)
        if prefix is None:
            return None
        owner, prefix_steps = prefix["owner"], prefix["steps"]
    owner_id = component_id("object", owner)
    field_id = component_id("field", owner + ".RecordTypeId")
    target_id = component_id("object", "RecordType")
    for cid in (owner_id, field_id, target_id):
        if cid in store.components and store.is_ambiguous(cid):
            return None
    declaration = store.components.get(field_id)
    if (declaration is not None and declaration.get("definition_status") != "referenced_only"
            and declaration.get("data_type") not in {None, "", "Lookup"}):
        return None
    # The shared resolver has already built this immutable lookup-only index.
    targets = store._parent_path_targets.get(field_id, set())
    if targets and targets != {target_id}:
        return None
    if resolved is not None:
        return resolved if resolved["owner"].casefold() == "recordtype" else None
    return {"owner": "RecordType", "field": parts[-1],
            "steps": [*prefix_steps, {"field": field_id, "target": target_id, "basis": "standard_formula_record_type"}]}


def _formula(document, node, store, source, owner):
    location = document.location(node)
    store.diagnostic("VALIDATION_RULE_FORMULA_ANALYSIS_BOUNDED",
                     "Literal field references are indexed; formula evaluation and dynamic dependencies are not modeled.",
                     location, severity="info")
    if node.children or not node.text:
        store.diagnostic("VALIDATION_RULE_FORMULA_INVALID", "A nonempty scalar formula expression is required.",
                         location, state="unresolved")
        return
    references, issues = formula_references(node.text)
    for issue in issues:
        store.diagnostic("VALIDATION_RULE_FORMULA_SYNTAX_UNSUPPORTED", "Formula syntax exceeds lexical reference coverage.",
                         location, issue=issue)
    for number, reference in enumerate(references, 1):
        occurrence = {**location, "locator": f"{node.locator}/reference[{number}]"}
        context = _field_context(reference.text, owner)
        if context is None:
            store.diagnostic("VALIDATION_RULE_GLOBAL_UNSUPPORTED", "This global formula reference has no supported typed field target.",
                             occurrence, reference=reference.text)
            continue
        target_owner, field_path, resolution = context
        resolved = _resolve_formula_field(store, target_owner, field_path)
        if resolved is None:
            store.diagnostic("VALIDATION_RULE_FIELD_PATH_UNRESOLVED", "The complete field path is not uniquely established.",
                             occurrence, state="unresolved", reference=reference.text, object=target_owner)
            continue
        target = store.field(resolved["owner"], resolved["field"], occurrence)
        if target is None:
            continue
        resolution = {**resolution, "reference": reference.text,
                      "relationship_path": field_path, "steps": resolved["steps"],
                      "formula_reference": {
                          "expression_locator": node.locator, "decoded_character_offset": reference.offset,
                          "source_position_basis": "containing_xml_expression_element",
                      }}
        store.relation(source, target, "reads", occurrence, "validation_rule.formula_reference",
                       resolution=resolution, qualifiers={"context": "validation_formula"})
        for step_number, step in enumerate(resolved["steps"], 1):
            # Reaching the terminal field also depends on every lookup value.
            # Keep those reads distinguishable so reverse queries can find a
            # consumer without treating navigation as a direct formula operand.
            declaration = store.components.get(step["field"])
            api_name = declaration["api_name"] if declaration else step["field"].split(":", 2)[2]
            lookup_owner, lookup_field = api_name.split(".", 1)
            navigation_location = {**occurrence, "locator": f"{occurrence['locator']}/navigation[{step_number}]"}
            lookup = store.field(lookup_owner, lookup_field, navigation_location)
            if lookup is None:
                continue
            store.relation(source, lookup, "reads", navigation_location, "validation_rule.formula_navigation",
                           resolution={**resolution, "navigation_step": {"index": step_number, **step}},
                           qualifiers={"context": "validation_formula_navigation"})


def extract_validation_rule(document, store):
    from .inventory import identity
    kind, name = identity(document)
    if kind != "validationrule":
        return
    root = document.root
    source = store.resolve_component(kind, name, document.location(root))
    if source is None:
        return
    owner = name.split(".", 1)[0]
    owner_id = store.resolve_component("object", owner, document.location(root))
    if owner_id:
        store.relation(owner_id, source, "contains", document.location(root), "validation_rule.owner")
    formulas = root.all("errorConditionFormula")
    if len(formulas) != 1:
        for node in formulas or [root]:
            store.diagnostic("VALIDATION_RULE_FORMULA_AMBIGUOUS" if formulas else "VALIDATION_RULE_FORMULA_MISSING",
                             "A validation rule requires one error condition formula.", document.location(node),
                             state="ambiguous" if formulas else "unresolved")
    else:
        _formula(document, formulas[0], store, source, owner)
    displays = root.all("errorDisplayField")
    if len(displays) > 1:
        for node in displays:
            store.diagnostic("VALIDATION_RULE_DISPLAY_FIELD_AMBIGUOUS", "Multiple error display fields prevent unique resolution.",
                             document.location(node), state="ambiguous")
    elif displays and (displays[0].text or displays[0].children):
        node = displays[0]
        if node.children or not API_NAME.fullmatch(node.text):
            store.diagnostic("VALIDATION_RULE_DISPLAY_FIELD_UNSUPPORTED", "Error display field must be a literal direct field.",
                             document.location(node))
        else:
            target = store.field(owner, node.text, document.location(node))
            if target:
                store.relation(source, target, "references", document.location(node), "validation_rule.error_display_field",
                               qualifiers={"context": "validation_error_display"})
    for node in root.children:
        if node.tag not in _DESCRIPTIVE:
            store.diagnostic("VALIDATION_RULE_CONSTRUCT_UNSUPPORTED", "Validation rule element is outside extraction coverage.",
                             document.location(node), construct=node.tag)
