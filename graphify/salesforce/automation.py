"""Local save automation and platform event declarations with source evidence."""
from __future__ import annotations

from collections import Counter

from .activity import inactive_components
from .inventory import identity
from .schema import API_NAME, component_id, relationship_resolution


_FLOW_TIMINGS = {
    "RecordBeforeSave": "before_save",
    "RecordAfterSave": "after_save",
    "RecordBeforeDelete": "before_delete",
}
_SAVE_EVENTS = {"before_insert", "before_update", "after_insert", "after_update", "after_undelete"}
_ENTRY_SCHEMA = 13


def _platform_event(component):
    return component["kind"] == "object" and component["api_name"].casefold().endswith("__e")


def _leaf(node, tag):
    values = node.all(tag)
    return values[0] if len(values) == 1 and not values[0].children and values[0].text else None


def _location(occurrence):
    return {key: occurrence[key] for key in ("path", "locator", "line", "column", "end_line", "end_column", "content_hash")
            if key in occurrence}


def _flow_start(document, store, source):
    starts = document.root.all("start")
    if len(starts) != 1:
        return
    start = starts[0]
    kind, owner = _leaf(start, "triggerType"), _leaf(start, "object")
    if kind is None or owner is None or not API_NAME.fullmatch(owner.text):
        return
    timing = _FLOW_TIMINGS.get(kind.text)
    is_event = owner.text.casefold().endswith("__e")
    if not ((kind.text == "PlatformEvent" and is_event) or (timing and not is_event)):
        return
    location = document.location(owner)
    target = store.resolve_component("object", owner.text, location)
    if not target:
        return
    qualifiers = {"trigger_type": kind.text, "local_status": source.get("local_status") or "Unknown"}
    proof = {"basis": "flow_start_metadata", "trigger_type_location": document.location(kind)}
    status = _leaf(document.root, "status")
    if status is not None:
        proof["status_location"] = document.location(status)
    if is_event:
        store.relation(source["id"], target, "subscribes", location, "flow.platform_event_start",
                       qualifiers=qualifiers, resolution=proof)
        return
    qualifiers["timing"] = timing
    record_event = _leaf(start, "recordTriggerType")
    if record_event is not None:
        qualifiers["record_trigger_type"] = record_event.text
        proof["record_trigger_type_location"] = document.location(record_event)
    trigger_order = _leaf(document.root, "triggerOrder")
    if trigger_order is not None and trigger_order.text.isascii() and trigger_order.text.isdigit():
        if len(trigger_order.text) <= 4 and 1 <= int(trigger_order.text) <= 2000:
            qualifiers["trigger_order"] = int(trigger_order.text)
            proof["trigger_order_location"] = document.location(trigger_order)
    store.relation(source["id"], target, "triggered_by", location, "flow.record_trigger_start",
                   qualifiers=qualifiers, resolution=proof)


def _flow_create_locations(document):
    for create in document.root.all("recordCreates"):
        operand = create.all("object") + create.all("inputReference")
        upsert = create.all("doesUpsert")
        if (len(operand) == 1 and not operand[0].children and operand[0].text
                and (not upsert or (len(upsert) == 1 and not upsert[0].children and upsert[0].text in {"false", "0"}))):
            yield document.path, operand[0].locator


def _trigger_entry_point(store, edge, source, target):
    events = edge["qualifiers"].get("events") or []
    if not events or (_platform_event(target) and events != ["after_insert"]):
        return
    qualifiers = {"events": events, "local_status": source.get("local_status") or "Unknown"}
    if _platform_event(target):
        relation, rule = "subscribes", "trigger.platform_event_declaration"
    else:
        relation, rule = "triggered_by", "trigger.record_declaration"
        phases = {event.split("_", 1)[0] for event in events}
        qualifiers["timing"] = next(iter(phases)) if len(phases) == 1 else "before_after"
    for evidence_id in edge["evidence_ids"]:
        occurrence = store.evidence[evidence_id]
        proof = {**occurrence["resolution"]}
        if source.get("status_location") is not None:
            proof["status_location"] = source["status_location"]
        store.relation(source["id"], target["id"], relation, _location(occurrence), rule,
                       qualifiers=qualifiers, resolution=proof)


def extract_entry_points(documents, store, *, excluded=()):
    """Add entry-point relations while keeping existing write and reference IDs."""
    flow_creates = set()
    for document in documents:
        if document.root.tag not in {"Flow", "ValidationRule"}:
            continue
        kind, name = identity(document)
        source = store.components[component_id(kind, name)]
        if source["id"] in excluded or source["definition_status"] != "defined":
            continue
        if kind == "flow":
            _flow_start(document, store, source)
            flow_creates.update(_flow_create_locations(document))
        elif kind == "validationrule":
            location = document.location(document.root)
            target = store.resolve_component("object", source["owner"], location)
            if target and not _platform_event(store.components[target]):
                active = _leaf(document.root, "active")
                proof = {"basis": "validation_rule_owner"}
                if active is not None:
                    proof["status_location"] = document.location(active)
                store.relation(source["id"], target, "triggered_by", location, "validation_rule.record_save",
                               qualifiers={"timing": "validation", "local_status": source["local_status"]},
                               resolution=proof)
    for edge in list(store.relationships.values()):
        relation = edge["relation"]
        if relation not in {"references", "writes"}:
            continue
        source, target = store.components[edge["source"]], store.components[edge["target"]]
        if target["kind"] != "object" or source["definition_status"] != "defined" or source["id"] in excluded:
            continue
        if (relation == "references" and source["kind"] == "apextrigger"
                and edge["qualifiers"].get("operation") == "trigger"):
            _trigger_entry_point(store, edge, source, target)
        elif (relation == "writes" and source["kind"] == "flow" and _platform_event(target)
              and edge["qualifiers"].get("operation") == "create"):
            for evidence_id in edge["evidence_ids"]:
                occurrence = store.evidence[evidence_id]
                if (occurrence["rule"] == "flow.record_operation"
                        and (occurrence["path"], occurrence["locator"]) in flow_creates):
                    store.relation(source["id"], target["id"], "publishes", _location(occurrence),
                                   "flow.platform_event_create",
                                   qualifiers={"operation": "publish", "effect": "event_bus",
                                               "local_status": source.get("local_status") or "Unknown"},
                                   resolution=occurrence["resolution"])


def _save_automation(incoming, components):
    entry_points = {}
    for edge in incoming:
        if edge["relation"] != "triggered_by":
            continue
        qualifiers = edge["qualifiers"]
        kind = components[edge["source"]]["kind"]
        if (qualifiers.get("timing") == "before_delete"
                or (kind == "apextrigger" and not (_SAVE_EVENTS & set(qualifiers.get("events", ()))))):
            continue
        entry_points[edge["source"]] = kind, qualifiers
    statuses = Counter(q["local_status"] for _, q in entry_points.values())
    active = [(kind, q) for kind, q in entry_points.values() if q["local_status"] == "Active"]
    kinds = Counter(kind for kind, _ in active)
    timing = Counter(q["timing"] for kind, q in active if kind == "flow")
    return {"state": "indexed", "active_count": len(active), "status_counts": dict(sorted(statuses.items())),
            "active_by_kind": {kind: kinds[kind] for kind in ("flow", "apextrigger", "validationrule")},
            "active_flows_by_timing": {phase: timing[phase] for phase in ("before_save", "after_save")},
            "interpretation": "Local declarations for record save. Runtime eligibility and transaction order depend on the org and record."}


def _event_direction(publishers, candidates, subscribers):
    if subscribers:
        if publishers:
            return "local_publisher_and_subscriber"
        return "candidate_local_publisher" if candidates else "possible_external_publisher"
    if publishers or candidates:
        return "possible_external_subscriber"
    return "unresolved"


def _publication_methods_without_callers(publishers, components, relationships):
    methods = {source for source in publishers if components[source]["kind"] == "apexmethod"}
    if not methods:
        return []
    calls = [(edge["source"], edge["target"]) for edge in relationships
             if edge["relation"] == "calls" and edge["target"] in methods and edge["source"] != edge["target"]
             and components[edge["source"]].get("is_test") is not True]
    inactive = inactive_components(components[source] for source, _ in calls)
    called = {target for source, target in calls if source not in inactive}
    return [components[source]["api_name"] for source in sorted(methods - called)]


def _event_context(incoming, components, permissions_included, relationships):
    by_kind = {kind: set() for kind in ("publishes", "candidate_publishes", "subscribes")}
    create_sets = set()
    sources = {edge["source"]: components[edge["source"]] for edge in incoming}
    inactive = inactive_components(sources.values())
    for edge in incoming:
        source, relation = sources[edge["source"]], edge["relation"]
        if source["id"] in inactive or source.get("is_test") is True:
            continue
        if relation == "publishes" and relationship_resolution(edge)["status"] == "candidate":
            by_kind["candidate_publishes"].add(source["id"])
        elif relation in {"publishes", "subscribes"}:
            by_kind[relation].add(source["id"])
        elif (permissions_included and relation == "grants" and source["kind"] == "permissionset"
              and edge["qualifiers"].get("permission") == "create"):
            create_sets.add(source["api_name"])
    publishers, candidates, subscribers = (sorted(by_kind[key]) for key in ("publishes", "candidate_publishes", "subscribes"))
    return {"state": "indexed", "direction": _event_direction(publishers, candidates, subscribers),
            "publishers": publishers, "candidate_publishers": candidates, "subscribers": subscribers,
            "permissions_included": permissions_included,
            "create_permission_sets": sorted(create_sets, key=str.casefold),
            "apex_publication_methods_without_indexed_callers": _publication_methods_without_callers(
                publishers + candidates, components, relationships),
            "interpretation": "Static production evidence from this checkout. External participants, grant assignments and delivery need org or integration evidence."}


def reference_context(bundle, target, relationships, components):
    if target["kind"] != "object":
        return {}
    event = _platform_event(target)
    key = "platform_event" if event else "save_automation"
    if bundle["manifest"]["schema_version"] < _ENTRY_SCHEMA:
        return {key: {"state": "unavailable"}}
    relevant = {"publishes", "subscribes", "grants"} if event else {"triggered_by"}
    incoming = [edge for edge in relationships if edge["target"] == target["id"] and edge["relation"] in relevant]
    if event:
        permissions = bundle["manifest"]["coverage"].get("permission_selection", {}).get("included", False)
        return {key: _event_context(incoming, components, permissions, relationships)}
    return {key: _save_automation(incoming, components)}


def reference_lines(result):
    event = result.get("platform_event")
    if event is not None:
        if event["state"] == "unavailable":
            return ["Rebuild this snapshot to index platform event publishers and subscribers."]
        direction = event["direction"].replace("_", " ")
        lines = [f"Event direction (local static evidence): {direction}. "
                 f"{len(event['publishers'])} publishers, {len(event['candidate_publishers'])} candidate publishers, "
                 f"{len(event['subscribers'])} subscribers."]
        uncalled = event["apex_publication_methods_without_indexed_callers"]
        if uncalled:
            names = ", ".join(uncalled[:8])
            more = f" (+{len(uncalled) - 8} more)" if len(uncalled) > 8 else ""
            lines.append(f"Apex publication methods without indexed production callers include {names}{more}.")
        if not event["publishers"]:
            if event["permissions_included"]:
                sets = event["create_permission_sets"]
                if sets:
                    visible = ", ".join(sets[:8])
                    more = f" (+{len(sets) - 8} more)" if len(sets) > 8 else ""
                    lines.append(f"Declared permission sets with Create: {visible}{more}. User assignments require org evidence.")
                else:
                    lines.append("Declared permission sets with Create: 0 in this snapshot.")
            else:
                lines.append("Use build --include-permissions to inspect declared Create grants.")
        return lines
    save = result.get("save_automation")
    if save is None:
        return []
    if save["state"] == "unavailable":
        return ["Rebuild this snapshot to index record-save automation."]
    kinds, phases = save["active_by_kind"], save["active_flows_by_timing"]
    lines = [f"Active record-save automation (local metadata): {kinds['flow']} flows "
             f"({phases['before_save']} before-save, {phases['after_save']} after-save), "
             f"{kinds['apextrigger']} Apex triggers, {kinds['validationrule']} validation rules."]
    other = [f"{count} {status}" for status, count in save["status_counts"].items() if status != "Active"]
    if other:
        lines.append("Other declared statuses: " + ", ".join(other) + ".")
    return lines
