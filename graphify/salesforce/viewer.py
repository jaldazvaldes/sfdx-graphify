"""Standard full Graphify map with optional offline evidence-focused views."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from importlib.resources import files
import ipaddress
from pathlib import Path
import socket
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, urlsplit

from .schema import DataError, canonical_json, component_id, relationship_resolution, stable_id
from .snapshot import load

if TYPE_CHECKING:
    from http.server import ThreadingHTTPServer

NODE_LIMIT = 100
EDGE_LIMIT = 200
SEARCH_LIMIT = 25
EVIDENCE_LIMIT = 100
MODES = frozenset({"impact", "automation", "schema"})
_RULE_KINDS = frozenset({"validationrule", "workflowrule"})
AUTOMATION_KINDS = frozenset({"flow", "apexclass", "apexmethod", "apextrigger", "lwc", "outboundmessage"}) | _RULE_KINDS
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
       "connect-src 'self'; img-src 'self'; font-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
GRAPH_CSP = CSP.replace("style-src 'self'", "style-src 'self' 'unsafe-inline'")


def _flow_status(component: dict) -> str:
    if component.get("definition_status") == "ambiguous":
        return "ambiguous"
    status = str(component.get("local_status") or "unknown")
    return "unknown" if status.casefold() == "unknown" else status


def _proof(evidence: dict) -> dict:
    """Extractor stamps identify tooling, not a change to source evidence."""
    return {key: value for key, value in evidence.items() if key != "extractor_version"}


class SnapshotView:
    """Indexes built once; HTTP requests never read or rescan analyzed source."""

    def __init__(self, bundle: dict, before: dict | None = None):
        self.bundle = bundle
        self.before = before
        self.after_components = {item["id"]: item for item in bundle["facts"]["components"]}
        self.before_components = ({item["id"]: item for item in before["facts"]["components"]}
                                  if before else {})
        self.components = {**self.before_components, **self.after_components}
        self.after_relations = {item["id"]: item for item in bundle["facts"]["relationships"]}
        self.before_relations = ({item["id"]: item for item in before["facts"]["relationships"]}
                                 if before else {})
        self.after_evidence = {item["id"]: item for item in bundle["facts"]["evidence"]}
        self.before_evidence = ({item["id"]: item for item in before["facts"]["evidence"]}
                                if before else {})
        self.relations = {**self.before_relations, **self.after_relations}
        self.incoming: dict[str, list[str]] = defaultdict(list)
        self.outgoing: dict[str, list[str]] = defaultdict(list)
        self.fields: dict[str, set[str]] = defaultdict(set)
        self.methods: dict[str, dict[str, str]] = defaultdict(dict)
        self.rule_members: dict[str, dict[str, str]] = defaultdict(dict)
        self.rule_owners: dict[str, dict[str, str]] = defaultdict(dict)
        for cid, item in self.components.items():
            if item["kind"] == "field":
                owner = component_id("object", item["api_name"].split(".", 1)[0])
                if owner in self.components:
                    self.fields[owner].add(cid)
        self.changes = {}
        for rid, item in sorted(self.relations.items()):
            self.incoming[item["target"]].append(rid)
            self.outgoing[item["source"]].append(rid)
            if (item["relation"] == "contains"
                    and self.components[item["source"]]["kind"] == "apexclass"
                    and self.components[item["target"]]["kind"] == "apexmethod"):
                self.methods[item["source"]][item["target"]] = rid
            if (item["relation"] == "contains"
                    and self.components[item["source"]]["kind"] == "object"
                    and self.components[item["target"]]["kind"] in _RULE_KINDS):
                self.rule_members[item["source"]][item["target"]] = rid
                self.rule_owners[item["target"]][item["source"]] = rid
            if before is None:
                change = "current"
            elif rid not in self.after_relations:
                change = "removed"
            elif rid not in self.before_relations:
                change = "added"
            else:
                old = [_proof(self.before_evidence[eid]) for eid in self.before_relations[rid]["evidence_ids"]]
                new = [_proof(self.after_evidence[eid]) for eid in self.after_relations[rid]["evidence_ids"]]
                change = "unchanged" if old == new else "evidence_changed"
            self.changes[rid] = change
        self.statuses = sorted({_flow_status(item) for item in
                                [*self.before_components.values(), *self.after_components.values()]
                                if item["kind"] == "flow"}, key=str.casefold)
        self.metadata = self._metadata()

    def _metadata(self) -> dict:
        manifest = self.bundle["manifest"]
        coverage = manifest.get("coverage", {})
        diagnostics = self.bundle.get("diagnostics", [])
        unsupported_paths = manifest.get("scope", {}).get("unsupported_paths", [])
        states = Counter(item.get("state", "unknown") for item in diagnostics
                         if item.get("code") not in {"unsupported_metadata", "unsupported_metadata_type", "unsupported_source_type"})
        comparison = None
        if self.before:
            old = self.before["manifest"]
            previous_inputs, current_inputs = old.get("inputs", {}), manifest.get("inputs", {})
            previous_scope, current_scope = old.get("scope", {}), manifest.get("scope", {})
            scope_changes = sorted(key for key in previous_scope.keys() | current_scope.keys()
                                   if previous_scope.get(key) != current_scope.get(key))
            before_settings = {key: old.get(key) for key in
                               ("schema_version", "extractor_version", "source_revision", "dirty", "input_mode", "activity_policy")}
            after_settings = {key: manifest.get(key) for key in before_settings}
            comparison = {
                "before_snapshot_id": old["snapshot_id"], "after_snapshot_id": manifest["snapshot_id"],
                "before": before_settings, "after": after_settings, "scope_changed_keys": scope_changes,
                "inputs": {"added": len(current_inputs.keys() - previous_inputs.keys()),
                           "removed": len(previous_inputs.keys() - current_inputs.keys()),
                           "changed": sum(current_inputs[path] != previous_inputs[path]
                                          for path in current_inputs.keys() & previous_inputs.keys())},
                "extraction_changed": any(before_settings[key] != after_settings[key]
                                          for key in ("schema_version", "extractor_version")) or bool(scope_changes)
                                      or ((before_settings.get("activity_policy") or {}).get("mode")
                                          != (after_settings.get("activity_policy") or {}).get("mode")),
                "relationship_changes": dict(sorted(Counter(self.changes.values()).items())),
                "meaning": "Static snapshot comparison; added facts can reflect extraction coverage, not source changes. No deployed change is inferred.",
            }
        return {
            "snapshot_id": manifest["snapshot_id"],
            "schema_version": manifest.get("schema_version"),
            "extractor_version": manifest.get("extractor_version", "unknown"),
            "source_revision": manifest.get("source_revision"),
            "dirty": manifest.get("dirty"),
            "input_mode": manifest.get("input_mode", "unknown"),
            "freshness": "unknown",
            "freshness_note": "This loaded snapshot is not rechecked against live source.",
            "component_count": len(self.after_components),
            "relationship_count": len(self.after_relations),
            "flow_statuses": self.statuses,
            "coverage": {
                "status": coverage.get("status", "unknown"),
                "analyzed_files": coverage.get("analyzed_files", 0),
                "unsupported_inventory_files": len(unsupported_paths),
                "resolution_notes": dict(sorted(states.items())),
                "tested_api_versions": coverage.get("tested_api_versions", []),
                "untested_api_versions": coverage.get("untested_api_versions", []),
                "limitations": coverage.get("limitations", []),
                "activity_policy": {key: value for key, value in
                                    manifest.get("activity_policy", {"mode": "legacy_all_statuses"}).items()
                                    if key != "excluded_components"},
            },
            "comparison": comparison,
        }

    def _node(self, cid: str, layer: int = 0) -> dict:
        item = self.components[cid]
        if self.before is None:
            change = "current"
        elif cid not in self.after_components:
            change = "removed"
        elif cid not in self.before_components:
            change = "added"
        else:
            old, new = self.before_components[cid], self.after_components[cid]
            old_sources = {entry["path"]: entry["content_hash"] for entry in old.get("definitions", [])}
            new_sources = {entry["path"]: entry["content_hash"] for entry in new.get("definitions", [])}
            if old == new:
                change = "unchanged"
            elif any(old_sources[path] != new_sources[path] for path in old_sources.keys() & new_sources.keys()):
                change = "source_changed"
            else:
                change = "index_metadata_changed"
        result = {"id": cid, "label": item["api_name"], "kind": item["kind"], "layer": layer,
                  "definition_status": item["definition_status"], "change": change}
        if "is_test" in item:
            result["is_test"] = item["is_test"]
        if item["kind"] == "apextrigger" and item["definition_status"] != "ambiguous":
            for key in ("trigger_object", "trigger_events", "local_status", "api_version"):
                if key in item:
                    result[key] = item[key]
        if item["kind"] == "flow" or item["kind"] in _RULE_KINDS:
            result["local_status"] = _flow_status(item)
            if cid in self.before_components:
                result["before_local_status"] = _flow_status(self.before_components[cid])
            if item["kind"] == "flow" and item["definition_status"] != "ambiguous":
                for key in ("process_type", "api_version", "trigger_type", "record_trigger_type", "trigger_object"):
                    if key in item:
                        result[key] = item[key]
        result["definitions"] = item.get("definitions", [])
        if "metadata_by_definition" in item:
            result["metadata_by_definition"] = item["metadata_by_definition"]
        return result

    def search(self, text: str, *, limit: int = SEARCH_LIMIT) -> dict:
        needle = text.strip().casefold()
        if len(needle) > 160:
            raise DataError("Search is limited to 160 characters.")
        if not needle:
            return {"results": [], "total": 0, "truncated": False}
        matches = [cid for cid, item in self.components.items()
                   if needle in item["api_name"].casefold() or needle in cid.casefold()]
        matches.sort(key=lambda cid: (self.components[cid]["api_name"].casefold() != needle,
                                     self.components[cid]["kind"], self.components[cid]["api_name"].casefold(), cid))
        cap = max(1, min(limit, SEARCH_LIMIT))
        return {"results": [self._node(cid) for cid in matches[:cap]],
                "total": len(matches), "truncated": len(matches) > cap}

    def _allowed(self, relation: dict, mode: str) -> bool:
        kind = relation["relation"]
        if mode == "schema":
            return (all(self.components[relation[key]]["kind"] in {"object", "field"}
                        for key in ("source", "target"))
                    and (kind == "contains" or (kind == "references" and
                         relation.get("qualifiers", {}).get("context") == "lookup_declaration")))
        if kind == "contains":
            return (mode == "automation"
                    and self.components[relation["source"]]["kind"] == "object"
                    and self.components[relation["target"]]["kind"] in _RULE_KINDS)
        if mode == "automation":
            return any(self.components[relation[key]]["kind"] in AUTOMATION_KINDS
                       for key in ("source", "target"))
        return True

    def view(self, component: str, *, mode: str = "impact", depth: int = 1,
             status: str = "all") -> dict:
        if mode not in MODES:
            raise DataError("Mode must be impact, automation, or schema.")
        if not 1 <= depth <= 3:
            raise DataError("Depth must be between 1 and 3.")
        if status != "all" and status not in self.statuses:
            raise DataError("Unknown local Flow status.")
        if component not in self.components:
            raise DataError("No matching component in the loaded snapshots.")
        focus = self.components[component]
        fields = self.fields.get(component, set()) if mode != "schema" and focus["kind"] == "object" else set()
        methods = self.methods.get(component, {}) if mode != "schema" and focus["kind"] == "apexclass" else {}
        members = {**methods, **self.rule_members.get(component, {})} if mode != "schema" else {}
        owners = self.rule_owners.get(component, {}) if mode != "schema" else {}
        owned = fields | methods.keys()
        seeds = {component, *owned}
        levels = {component: 0}
        distance = {seed: 0 for seed in seeds}
        signed = {seed: 0 for seed in seeds}
        queue = deque(sorted(seeds))
        selected = set()
        filtered_edges, filtered_flows = set(), set()
        depth_boundary = set()

        def mapped(cid):
            return component if cid in owned else cid

        while queue:
            current = queue.popleft()
            candidates = [(rid, -1) for rid in self.incoming[current]]
            if mode != "impact":
                candidates += [(rid, 1) for rid in self.outgoing[current]]
            for rid, direction in candidates:
                relation = self.relations[rid]
                if not self._allowed(relation, mode):
                    continue
                status_components = (self.before_components if self.changes[rid] == "removed"
                                     else self.after_components)
                rejected = [cid for cid in (relation["source"], relation["target"])
                            if self.components[cid]["kind"] == "flow" and status != "all"
                            and _flow_status(status_components.get(cid, self.components[cid])) != status]
                if rejected:
                    filtered_edges.add(rid)
                    filtered_flows.update(rejected)
                    continue
                if distance[current] >= depth:
                    if rid not in selected:
                        depth_boundary.add(rid)
                    continue
                selected.add(rid)
                other = relation["source"] if direction == -1 else relation["target"]
                if other not in distance:
                    distance[other] = distance[current] + 1
                    signed[other] = signed[current] + direction
                    queue.append(other)
                for cid in (relation["source"], relation["target"]):
                    if mapped(cid) not in levels:
                        levels[mapped(cid)] = signed.get(cid, signed[current] + direction)

        rank = sorted((cid for cid in levels if cid != component),
                      key=lambda cid: (abs(levels[cid]), self.components[cid]["kind"],
                                       self.components[cid]["api_name"].casefold(), cid))
        shown = {component, *rank[:NODE_LIMIT - 1]}
        groups = defaultdict(list)
        for rid in sorted(selected):
            relation = self.relations[rid]
            source, target = mapped(relation["source"]), mapped(relation["target"])
            if source in shown and target in shown:
                groups[source, target, relation["relation"], self.changes[rid]].append(relation)
        ordered_groups = sorted(groups, key=lambda key: (
            min(abs(levels[key[0]]), abs(levels[key[1]])), key))
        edges = []
        visible_canonical = set()
        for key in ordered_groups[:EDGE_LIMIT]:
            source, target, kind, change = key
            relationships = groups[key]
            records = []
            preview = []
            total_evidence = 0
            for relation in relationships:
                rid = relation["id"]
                visible_canonical.add(rid)
                evidence_index = self.before_evidence if change == "removed" else self.after_evidence
                records.append({"id": rid, "source": relation["source"], "target": relation["target"],
                                "source_label": self.components[relation["source"]]["api_name"],
                                "target_label": self.components[relation["target"]]["api_name"],
                                "qualifiers": relation.get("qualifiers", {}),
                                "resolution": relationship_resolution(relation),
                                "evidence_count": len(relation["evidence_ids"])})
                total_evidence += len(relation["evidence_ids"])
                for eid in relation["evidence_ids"][:max(0, 12 - len(preview))]:
                    preview.append({**evidence_index[eid], "qualifiers": relation.get("qualifiers", {}),
                                    "relationship_resolution": relationship_resolution(relation)})
            candidate_count = sum(record["resolution"]["status"] == "candidate" for record in records)
            edges.append({"id": stable_id("view", key), "source": source, "target": target,
                          "relation": kind, "change": change, "relationships": records,
                          "resolution_counts": {"candidate": candidate_count, "extracted": len(records) - candidate_count},
                          "resolution_status": ("mixed" if 0 < candidate_count < len(records)
                                                else "candidate" if candidate_count else "extracted"),
                          "relationship_count": len(records), "evidence_count": total_evidence,
                          "evidence": preview, "evidence_truncated": total_evidence > len(preview)})
        note = ("Incoming dependencies identify candidates to inspect, not proven runtime effects."
                if mode == "impact" else
                "References around the selected component; contains gives membership context, not execution order or causality."
                if mode == "automation" else "Declared field ownership and lookup references only.")
        return {"snapshot_id": self.manifest_id, "focus": self._node(component),
                "mode": mode, "depth": depth, "status_filter": status,
                "nodes": [self._node(cid, levels[cid]) for cid in sorted(shown, key=lambda cid: (levels[cid], cid))],
                "edges": edges, "interpretation": note, "freshness": "unknown",
                "field_targets_grouped": len(fields), "method_members_grouped": len(methods),
                "ownership_context": {
                    "meaning": "Declared membership is navigation context, not a causal dependency.",
                    "members": [{"component": self._node(cid), "relationship_id": members[cid]}
                                for cid in sorted(members)[:50]],
                    "total": len(members), "truncated": len(members) > 50,
                    "owners": [{"component": self._node(cid), "relationship_id": owners[cid]}
                               for cid in sorted(owners)[:50]],
                    "owner_total": len(owners), "owners_truncated": len(owners) > 50,
                },
                "diagnostic_context": self.component_diagnostics(component),
                "filters": {"hidden_flow_count": len(filtered_flows), "hidden_relationship_count": len(filtered_edges)},
                "truncation": {"nodes_omitted": len(levels) - len(shown),
                               "edge_groups_omitted": max(0, len(groups) - EDGE_LIMIT),
                               "relationships_omitted": len(selected - visible_canonical),
                               "depth_boundary_relationships": len(depth_boundary - selected),
                               "node_limit": NODE_LIMIT, "edge_limit": EDGE_LIMIT},
                "comparison": self.metadata["comparison"]}

    @property
    def manifest_id(self):
        return self.bundle["manifest"]["snapshot_id"]

    def component_diagnostics(self, component: str, *, mode: str = "summary") -> dict:
        from .query import diagnostic_context
        if component not in self.components:
            raise DataError("No matching component in the loaded snapshots.")
        bundle = self.bundle if component in self.after_components else self.before
        assert bundle is not None
        return {**diagnostic_context(bundle, self.components[component], [], mode=mode),
                "snapshot_id": bundle["manifest"]["snapshot_id"], "component_id": component}

    def diagnostics_page(self, component: str, *, offset: int = 0) -> dict:
        if offset < 0:
            raise DataError("Invalid diagnostics page.")
        context = self.component_diagnostics(component, mode="full")
        records = context.pop("diagnostics")
        return {"context": context, "results": records[offset:offset + EVIDENCE_LIMIT],
                "total": len(records), "offset": offset,
                "next_offset": offset + EVIDENCE_LIMIT if offset + EVIDENCE_LIMIT < len(records) else None}

    def evidence(self, rid: str, *, snapshot: str = "after", offset: int = 0) -> dict:
        if snapshot not in {"before", "after"} or offset < 0:
            raise DataError("Invalid evidence page.")
        relations = self.before_relations if snapshot == "before" else self.after_relations
        index = self.before_evidence if snapshot == "before" else self.after_evidence
        if rid not in relations:
            raise DataError("No matching relationship in that snapshot.")
        relation = relations[rid]
        identifiers = relation["evidence_ids"]
        return {"relationship": relation, "resolution": relationship_resolution(relation), "snapshot": snapshot,
                "results": [index[eid] for eid in identifiers[offset:offset + EVIDENCE_LIMIT]],
                "total": len(identifiers), "offset": offset,
                "next_offset": offset + EVIDENCE_LIMIT if offset + EVIDENCE_LIMIT < len(identifiers) else None}


def build_view(bundle: dict, component: str, *, mode: str = "impact", depth: int = 1,
               status: str = "all", before: dict | None = None) -> dict:
    """Pure canonical view construction, shared by the local server and CLI."""
    return SnapshotView(bundle, before).view(component, mode=mode, depth=depth, status=status)


def create_server(snapshot: Path, host: str = "127.0.0.1", port: int = 8767,
                  *, before: Path | None = None) -> ThreadingHTTPServer:
    """Load verified snapshots once and bind only to an explicit loopback address."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class ViewerServer(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, address, handler, model):
            self.model = model
            super().__init__(address, handler)

    address = "127.0.0.1" if host == "localhost" else host
    try:
        parsed_address = ipaddress.ip_address(address)
    except ValueError as exc:
        raise DataError("The viewer host must be a loopback IP address or localhost.") from exc
    if not parsed_address.is_loopback or not 0 <= port <= 65535:
        raise DataError("The viewer requires a loopback address and a valid local port.")
    model = SnapshotView(load(snapshot), load(before) if before is not None else None)
    resources = files("graphify.salesforce").joinpath("viewer_static")
    from .standard_graph import standard_assets

    graph_assets = standard_assets(model.bundle)
    assets = {**graph_assets,
              "/explorer": (resources.joinpath("index.html").read_bytes(), "text/html; charset=utf-8"),
              "/vis-network.min.js": (resources.joinpath("vis-network.min.js").read_bytes(),
                                      "text/javascript; charset=utf-8"),
              "/viewer.css": (resources.joinpath("viewer.css").read_bytes(), "text/css; charset=utf-8"),
              "/viewer.js": (resources.joinpath("viewer.js").read_bytes(), "text/javascript; charset=utf-8")}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, content, media="application/json; charset=utf-8", *, csp=CSP):
            payload = content if isinstance(content, bytes) else canonical_json(content).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Security-Policy", csp)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            actual_port = cast(tuple[str, int], self.server.server_address)[1]
            literal = f"[{address}]" if parsed_address.version == 6 else address
            hosts = {f"{literal}:{actual_port}", f"localhost:{actual_port}"}
            if self.headers.get("Host", "").lower() not in hosts:
                return self.respond(403, {"error": "Local host required."})
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + value for value in hosts}:
                return self.respond(403, {"error": "Same-origin requests only."})
            if len(self.path) > 8192:
                return self.respond(414, {"error": "Request URL is too long."})
            try:
                target = urlsplit(self.path)
                raw = parse_qs(target.query, keep_blank_values=True, max_num_fields=8)
                if any(len(values) != 1 for values in raw.values()):
                    raise DataError("Duplicate query parameters are not supported.")
                params = {key: values[0] for key, values in raw.items()}
                if target.path in assets and not params:
                    content, media = assets[target.path]
                    return self.respond(200, content, media,
                                        csp=GRAPH_CSP if target.path in graph_assets else CSP)
                if target.path == "/api/meta" and not params:
                    return self.respond(200, model.metadata)
                if target.path == "/api/search" and set(params) <= {"query"}:
                    return self.respond(200, model.search(params.get("query", "")))
                if target.path == "/api/view" and set(params) <= {"component", "mode", "depth", "status"}:
                    value = model.view(params.get("component", ""), mode=params.get("mode", "impact"),
                                       depth=int(params.get("depth", "1")), status=params.get("status", "all"))
                    return self.respond(200, value)
                if target.path == "/api/evidence" and set(params) <= {"relationship", "snapshot", "offset"}:
                    value = model.evidence(params.get("relationship", ""), snapshot=params.get("snapshot", "after"),
                                           offset=int(params.get("offset", "0")))
                    return self.respond(200, value)
                if target.path == "/api/diagnostics" and set(params) <= {"component", "offset"}:
                    return self.respond(200, model.diagnostics_page(params.get("component", ""),
                                                                   offset=int(params.get("offset", "0"))))
                return self.respond(404, {"error": "Route not found."})
            except (DataError, ValueError) as exc:
                return self.respond(400, {"error": str(exc)})

    if parsed_address.version == 6:
        class IPv6ViewerServer(ViewerServer):
            address_family = socket.AF_INET6
        return IPv6ViewerServer((address, port), Handler, model)
    return ViewerServer((address, port), Handler, model)


def serve(snapshot: Path, host: str = "127.0.0.1", port: int = 8767,
          *, before: Path | None = None) -> None:
    server = create_server(snapshot, host, port, before=before)
    bound_host, bound_port = cast(tuple[str, int], server.server_address)[:2]
    literal = f"[{bound_host}]" if ":" in bound_host else bound_host
    print(f"Local Salesforce viewer: http://{literal}:{bound_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
