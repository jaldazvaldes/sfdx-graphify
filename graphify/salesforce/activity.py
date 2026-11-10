"""Explicit local activation policy; never infer inactivity from missing callers."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from .schema import FactStore


_INACTIVE_STATUSES = {"flow": frozenset({"Draft", "InvalidDraft", "Obsolete"}),
                      "apextrigger": frozenset({"Inactive", "Deleted"}),
                      "validationrule": frozenset({"Inactive"}),
                      "workflowrule": frozenset({"Inactive"})}
_LOCATION_KEYS = ("path", "locator", "line", "column", "end_line", "end_column", "content_hash")


def inactive_components(components: Iterable[dict], *, include_inactive: bool = False) -> dict:
    """Exclude only uniquely defined components with recognized inactive metadata."""
    if include_inactive:
        return {}
    return {
        component["id"]: component
        for component in components
        if component.get("definition_status") == "defined"
        and len(component.get("definitions", [])) == 1
        and component.get("local_status") in _INACTIVE_STATUSES.get(component["kind"], frozenset())
    }


def remove_inactive(store: FactStore, excluded: dict, *, include_inactive: bool = False) -> dict:
    """Remove excluded endpoints, retaining diagnostic proof of incoming references.

    Callers skip extraction of excluded definitions before this step. This keeps
    their otherwise unreferenced targets out without deleting genuine inventory
    definitions or implicit owners of retained field references.
    """
    excluded = {} if include_inactive else {
        cid: component for cid, component in excluded.items() if cid in store.components
    }
    removed_relationships = []
    for relationship in store.relationships.values():
        source, target = relationship["source"], relationship["target"]
        if source not in excluded and target not in excluded:
            continue
        removed_relationships.append(relationship["id"])
        if source in excluded or target not in excluded:
            continue
        for evidence_id in relationship["evidence_ids"]:
            occurrence = store.evidence[evidence_id]
            location = {key: occurrence[key] for key in _LOCATION_KEYS if key in occurrence}
            store.diagnostic(
                "inactive_dependency_excluded",
                "A retained component references a target excluded by its explicit local inactive status.",
                location, state="excluded", source_component=source, target_component=target,
                local_status=excluded[target]["local_status"], relation=relationship["relation"],
                relationship_id=relationship["id"], evidence_id=evidence_id, rule=occurrence["rule"],
            )
    for rid in removed_relationships:
        del store.relationships[rid]
    for cid in excluded:
        del store.components[cid]
    used_evidence = {eid for relationship in store.relationships.values()
                     for eid in relationship["evidence_ids"]}
    store.evidence = {eid: occurrence for eid, occurrence in store.evidence.items()
                      if eid in used_evidence}
    # A Workflow file is shared by multiple rules and other actions.
    skipped_paths = {definition["path"] for component in excluded.values() if component["kind"] != "workflowrule"
                     for definition in component["definitions"]}
    return {
        "mode": "include_inactive" if include_inactive else "exclude_explicitly_inactive",
        "excluded_component_count": len(excluded),
        "excluded_counts": dict(sorted(Counter(c["kind"] for c in excluded.values()).items())),
        "excluded_status_counts": dict(sorted(Counter(c["local_status"] for c in excluded.values()).items())),
        "excluded_components": [{key: component[key] for key in
                                 ("id", "kind", "api_name", "local_status", "definitions")}
                                for _, component in sorted(excluded.items())],
        "skipped_definition_files": len(skipped_paths),
        "omitted_relationship_count": len(removed_relationships),
        "unknown_status_policy": "retain",
        "local_metadata_only": True,
    }
