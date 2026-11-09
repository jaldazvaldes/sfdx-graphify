"""Resolve parent paths only through declared lookups or explicit standard links.

This is a deliberately small schema vocabulary, not suffix-based object guessing.
Polymorphic relationships and child subqueries are not selected by name.
"""
from __future__ import annotations

from .schema import API_NAME, component_id


# Standard, non-polymorphic parent relationships. Owner, Who and What are
# intentionally omitted: their possible target types vary by source object.
# Salesforce Object Reference: standard-objects/object-{account,contact,asset}.html
# https://developer.salesforce.com/docs/platform/object-reference/references/
_STANDARD_PARENTS = {
    ("account", "parent"): ("ParentId", "Account"),
    ("contact", "account"): ("AccountId", "Account"),
    ("contact", "reportsto"): ("ReportsToId", "Contact"),
    ("asset", "account"): ("AccountId", "Account"),
    ("asset", "contact"): ("ContactId", "Contact"),
    ("asset", "product2"): ("Product2Id", "Product2"),
}


def resolve_field_path(store, owner: str, path: str, *,
                       standard_parents: dict[tuple[str, str], tuple[str, str]] | None = None) -> dict | None:
    """Return the exact terminal field and schema hops, or None with no mutation.

    Consumers can opt into additional documented parent pairs. The default
    vocabulary and behavior remain unchanged for existing extractors.
    """
    # Inventory finishes before dependency extraction. Index only those schema
    # edges once; scanning every dependency edge for every hop is quadratic.
    lookup_targets = getattr(store, "_parent_path_targets", None)
    if lookup_targets is None:
        lookup_targets = {}
        for edge in store.relationships.values():
            if edge["relation"] == "references" and edge["qualifiers"].get("context") == "lookup_declaration":
                lookup_targets.setdefault(edge["source"], set()).add(edge["target"])
        store._parent_path_targets = lookup_targets
    parts = path.split(".")
    if not API_NAME.fullmatch(owner) or not parts or not all(API_NAME.fullmatch(p) for p in parts):
        return None
    current, steps = owner, []
    for parent in parts[:-1]:
        object_id = component_id("object", current)
        if object_id in store.components and store.is_ambiguous(object_id):
            return None
        custom = parent.casefold().endswith("__r")
        field_name = parent[:-3] + "__c" if custom else parent + "Id"
        field_id = component_id("field", current + "." + field_name)
        declaration = store.components.get(field_id)
        if declaration and declaration.get("definition_status") == "ambiguous":
            return None
        pair = (current.casefold(), parent.casefold())
        standard = (standard_parents or {}).get(pair) or _STANDARD_PARENTS.get(pair)
        # Account.ParentId can be represented by Hierarchy metadata. This is a
        # single verified self-reference, not a general alias for Lookup.
        hierarchy = (pair == ("account", "parent") and declaration is not None
                     and declaration.get("data_type") == "Hierarchy")
        compatible_types = {None, "", "Lookup", "Hierarchy"} if hierarchy else {None, "", "Lookup"}
        # Opt-in formula vocabulary must not override contradictory local XML.
        if (standard_parents is not None and standard and declaration is not None
                and declaration.get("definition_status") != "referenced_only"
                and declaration.get("data_type") not in compatible_types):
            return None
        matches = lookup_targets.get(field_id, set())
        if len(matches) > 1:
            return None
        if matches:
            target = store.components[next(iter(matches))]
            if target["kind"] != "object" or target.get("definition_status") == "ambiguous":
                return None
            if ((standard_parents is not None or hierarchy) and standard
                    and target["id"] != component_id("object", standard[1])):
                return None
            steps.append({"field": field_id, "target": target["id"], "basis": "declared_lookup"})
            current = target["api_name"]
        else:
            # Standard-field customization XML may declare Lookup and a filter
            # without repeating the platform referenceTo. The explicit standard
            # map supplies that target; incompatible local types still block it.
            if (custom or not standard or (declaration is not None
                    and declaration.get("definition_status") != "referenced_only"
                    and declaration.get("data_type") not in compatible_types)):
                return None
            field_name, target_name = standard
            target_id = component_id("object", target_name)
            if target_id in store.components and store.is_ambiguous(target_id):
                return None
            steps.append({"field": component_id("field", current + "." + field_name),
                          "target": target_id, "basis": "standard_schema"})
            current = target_name
    return {"owner": current, "field": parts[-1], "steps": steps}
