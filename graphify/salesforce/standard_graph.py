"""The upstream Graphify canvas, adapted to an offline complete Salesforce graph."""
from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
import re
import tempfile

from .graphify_adapter import projection_graph
from .grouping import grouping
from .orbital_layout import orbital_positions
from .schema import DataError


def _communities(graph):
    """Compatibility entry point for connectivity navigation, never canonical facts."""
    communities, labels, _ = grouping(graph, "connectivity")
    return communities, labels


def _grouping_modes(graph, positions):
    """Color/filter partitions share one organic layout, independent of their labels."""
    from graphify.exporters.base import COMMUNITY_COLORS

    modes = {}
    partitions = {}
    for mode, label, help_text in (
        ("structure", "Salesforce structure",
         "Objects with their fields; classes with their methods. Other components are grouped by type. "
         "Tests have separate groups. Connections do not imply ownership."),
        ("connectivity", "Connectivity",
         "Groups of connected components, not Salesforce objects or business domains. "
         "Known tests are grouped separately and do not influence these communities."),
    ):
        communities, labels, descriptions = grouping(graph, mode)
        partitions[mode] = (communities, labels, descriptions)
        modes[mode] = {
            "label": label, "help": help_text,
            "legend": [
                {"cid": cid, "label": labels[cid], "description": descriptions[cid],
                 "count": len(members), "color": COMMUNITY_COLORS[cid % len(COMMUNITY_COLORS)]}
                for cid, members in communities.items()
            ],
            "placements": {node: [cid, *positions[node]]
                           for cid, members in communities.items() for node in members},
        }
    return modes, partitions


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise DataError("The upstream Graphify HTML template changed; review the offline adapter.")
    return text.replace(old, new, 1)


def _component_filters(html: str) -> str:
    """Compose navigation filters without changing upstream templates or facts."""
    controls = """<section id="component-filters" aria-labelledby="component-types-heading">
    <label class="relationship-layer-control grouping-control" for="grouping-mode">Group by
      <select id="grouping-mode" aria-describedby="grouping-help">
        <option value="structure" selected>Salesforce structure</option>
        <option value="connectivity">Connectivity</option>
      </select></label>
    <p id="grouping-help" class="graph-explanation"></p>
    <div class="type-heading"><h3 id="component-types-heading">Component types</h3>
      <div><button id="types-show-all" type="button">Show all</button>
      <button id="types-hide-all" type="button">Hide all</button></div></div>
    <div id="component-type-list"></div>
    <label class="component-type"><input id="show-tests-cb" type="checkbox" checked>
      <span>Show tests</span><span id="test-node-count" class="type-count"></span></label>
    <label class="relationship-layer-control" for="relationship-layer">Relationships
      <select id="relationship-layer" aria-describedby="relationship-layer-help">
        <option value="all" selected>All</option>
        <option value="schema">Schema</option>
        <option value="dependencies">Dependencies</option>
      </select></label>
    <p id="relationship-layer-help" class="graph-explanation">Schema: membership and lookups. Dependencies: usage in code and automation. Select a component to highlight its connections. Filters keep positions and groups fixed.</p>
    <p id="visible-count" role="status" aria-live="polite"></p>
    <p id="empty-graph" hidden>No components match the selected types and communities.</p>
  </section>
  """
    html = _replace_once(html, '<div id="info-panel">', controls + '<div id="info-panel">')
    script = files("graphify.salesforce").joinpath("viewer_static").joinpath("standard_filters.js").read_text(encoding="utf-8")
    html = _replace_once(html, "const hiddenCommunities = new Set();",
                         "const hiddenCommunities = new Set();\n" + script)
    html = _replace_once(html, "font: n.font, title: n.title,",
                         "font: n.font, title: n.title, kind: n.kind, is_test: n.is_test, group_description: n.group_description,")
    html = _replace_once(html, "  title: e.title,",
                         "  title: relationshipTitle(e.relationship_details, e.confidence),")
    html = _replace_once(html, "  if (!n) return;\n  const neighborIds = network.getConnectedNodes(nodeId);\n  const neighborItems = neighborIds.map(nid => {",
                         "  if (!n || n.hidden) return;\n  inspectedNodeId = nodeId;\n"
                         "  focusIncidentEdges(nodeId);\n"
                         "  const neighborRows = visibleNeighborRows(nodeId);\n"
                         "  const neighborIds = neighborRows.map(row => row.id);\n"
                         "  const neighborItems = neighborRows.map(row => {\n    const nid = row.id;")
    html = _replace_once(html,
                         '    return `<span class="neighbor-link" style="border-left-color:${esc(color)}" data-nid="${esc(nid)}">${esc(nb ? nb.label : nid)}</span>`;',
                         '    return `<span class="neighbor-link" style="border-left-color:${esc(color)}" data-nid="${esc(nid)}"><span class="neighbor-name">${esc(nb ? nb.label : nid)}</span><span class="neighbor-relations">${row.descriptions.map(esc).join(" · ")}</span></span>`;')
    html = _replace_once(html, "esc(n.file_type || 'unknown')", "esc(TYPE_LABELS.get(n.kind) || n.kind || 'unknown')")
    html = _replace_once(html, '  html += `<div class="field">Degree: ${n.degree}</div>`;', '''
  if (n.kind === 'apexclass' || n.kind === 'apexmethod') {
    const classification = n.is_test === true ? 'Test' : n.is_test === false ? 'Not marked as test' : 'Unknown';
    html += `<div class="field">Test classification: ${classification}</div>`;
  }
  html += `<div class="field">Degree: ${n.degree}</div>`;
  if (!neighborIds.length) html += '<div class="field">Visible neighbors (0)</div>';''')
    html = _replace_once(html, "Neighbors (${neighborIds.length})", "Visible neighbors (${neighborIds.length})")
    html = _replace_once(html, '  html += `<div class="field">Community: ${esc(n.community_name || \'\')}</div>`;',
                         '  html += `<div class="field">Group: ${esc(n.community_name || \'\')}</div>`;\n'
                         '  html += `<div class="field group-description">${esc(n.group_description || \'\')}</div>`;')
    html = _replace_once(html, "<h3>Communities</h3>", """<h3 id="groups-heading">Groups</h3>
    <input id="group-search" type="search" placeholder="Find groups..." aria-label="Find groups" aria-describedby="group-search-help">
    <p id="group-search-help" class="graph-explanation">Search this list. Checkboxes show or hide groups; click a name to zoom.</p>
    <p id="group-list-count" class="graph-explanation" role="status"></p>""")
    html = _replace_once(html, "function focusNode(nodeId) {", "function focusNode(nodeId) {\n  if (nodesDS.get(nodeId)?.hidden) return;")
    html = _replace_once(html, "} else if (hoveredNodeId === null) {", "} else if (hoveredNodeId === null) {\n    inspectedNodeId = null;\n    focusIncidentEdges(null);")
    html = _replace_once(html, "searchInput.addEventListener('input', () => {", "function renderSearchResults() {")
    html = _replace_once(html, "RAW_NODES.filter(n => n.label.toLowerCase().includes(q))",
                         "RAW_NODES.filter(n => isComponentVisible(n) && n.label.toLowerCase().includes(q))")
    html = _replace_once(html, "    searchResults.appendChild(el);\n  });\n});",
                         "    searchResults.appendChild(el);\n  });\n}\nsearchInput.addEventListener('input', renderSearchResults);")
    # A single owned control block replaces the upstream community legend. The
    # checked boundary fails explicitly when upstream changes this integration.
    start = "const selectAllCb = document.getElementById('select-all-cb');"
    end = "  legendEl.appendChild(item);\n});"
    if html.count(start) != 1 or html.count(end) != 1:
        raise DataError("The upstream Graphify legend changed; review the Salesforce controls.")
    beginning, ending = html.index(start), html.index(end) + len(end)
    group_script = files("graphify.salesforce").joinpath("viewer_static").joinpath("standard_groups.js").read_text(encoding="utf-8")
    html = html[:beginning] + group_script + html[ending:]
    html = _replace_once(html, 'placeholder="Search nodes..."', 'placeholder="Search visible nodes..."')
    return html


def standard_assets(bundle: dict, *, processes_available: bool = False) -> dict[str, tuple[bytes, str]]:
    """Render all current components with the actual standard Graphify renderer.

    The only aggregation is one directed link per ordered pair. Its individual
    canonical relationships remain available through the evidence explorer.
    Assets are returned to the registered-route server; none are published.
    """
    from graphify.exporters.html import to_html

    graph = projection_graph(bundle)
    positions = orbital_positions(graph)
    modes, partitions = _grouping_modes(graph, positions)
    communities, labels, descriptions = partitions["structure"]
    with tempfile.TemporaryDirectory(prefix="sfdx-graphify-html-") as directory:
        target = Path(directory) / "graph.html"
        # Passing the full count explicitly prevents the upstream 5,000-node
        # guard from substituting a community-level meta-graph.
        written = to_html(graph, communities, str(target), community_labels=labels,
                          node_limit=max(1, len(graph)), learning_overlay={})
        if not written:
            raise DataError("The complete Graphify page was not generated.")
        html = target.read_text(encoding="utf-8")

    match = re.search(r"const RAW_NODES = (.*);\nconst RAW_EDGES =", html)
    if match is None:
        raise DataError("The upstream Graphify node data could not be located.")
    nodes = json.loads(match.group(1))
    for node in nodes:
        node["x"], node["y"] = positions[node["id"]]
        node["kind"] = graph.nodes[node["id"]]["kind"]
        node["is_test"] = graph.nodes[node["id"]].get("is_test")
        node["group_description"] = descriptions[node["community"]]
    encoded = json.dumps(nodes).replace("<", "\\u003c")
    html = html[:match.start(1)] + encoded + html[match.end(1):]
    edge_match = re.search(r"const RAW_EDGES = (.*);\n", html)
    if edge_match is None:
        raise DataError("The upstream Graphify edge data could not be located.")
    edges = json.loads(edge_match.group(1))
    for edge in edges:
        projected = graph[edge["from"]][edge["to"]]
        edge["relationship_details"] = projected["relationship_details"]
        edge["resolution_counts"] = projected["resolution_counts"]
        edge["resolution_status"] = projected["resolution_status"]
        edge["width"] = 0.7
        edge["color"] = {"color": "#8491a5", "highlight": "#63b3ed", "hover": "#63b3ed",
                         "inherit": False, "opacity": 0.045 if len(edges) > 5000 else 0.18}
    encoded_edges = json.dumps(edges).replace("<", "\\u003c")
    html = html[:edge_match.start(1)] + encoded_edges + html[edge_match.end(1):]
    html = _replace_once(html, "const LEGEND = ", "let LEGEND = ")
    html = _replace_once(html, "// HTML-escape helper", "const GROUPING_MODES = "
                         + json.dumps(modes).replace("<", "\\u003c") + ";\n\n// HTML-escape helper")
    html = _replace_once(html, "x: 30 * Math.sqrt(i) * Math.cos(i * 2.4),", "x: n.x,")
    html = _replace_once(html, "y: 30 * Math.sqrt(i) * Math.sin(i * 2.4),", "y: n.y,")
    html = _replace_once(html, "physics: {\n    enabled: true,", "physics: {\n    enabled: false,")
    html = _replace_once(html, "smooth: { type: 'continuous', roundness: 0.2 }", "smooth: false")
    html = _replace_once(html, ' onchange="toggleAllCommunities(!this.checked)"', "")
    html = _replace_once(html, "https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js",
                         "/vis-network.min.js")
    html = _replace_once(html, "document.getElementById('info-content').innerHTML = html;", """
  document.getElementById('info-content').innerHTML = html;
  const inspect = document.createElement('a');
  inspect.className = 'evidence-link';
  inspect.href = '/explorer#' + encodeURIComponent(nodeId);
  inspect.textContent = 'Inspect dependency evidence';
  document.getElementById('info-content').appendChild(inspect);""")
    relationship_count = len(bundle["facts"]["relationships"])
    activity = bundle["manifest"].get("activity_policy", {})
    if activity.get("mode") == "exclude_explicitly_inactive":
        activity_label = ("Local inactive components excluded: "
                          f"{int(activity.get('excluded_component_count', 0)):,} · unknown status retained")
    else:
        activity_label = "All local statuses included"
    processes_link = '<a href="/processes">Process maps</a>' if processes_available else ""
    toolbar = ("<nav id=\"full-graph-toolbar\" aria-label=\"Complete graph controls\">"
               "<strong>Salesforce · complete graph</strong>"
               "<span>Orbital layout · select a component to highlight its connections</span>"
               f"<span>{len(graph):,} components · {graph.number_of_edges():,} grouped links "
               f"· {relationship_count:,} relationships · all nodes included</span>"
               f"<span>{activity_label}</span>"
               "<div><button id=\"fit-all\" type=\"button\">Fit all</button>"
               f"<a href=\"/explorer\">Detailed explorer</a>{processes_link}</div></nav>")
    html = _replace_once(html, '<div id="graph"></div>', toolbar + '\n<div id="graph"></div>')
    html = _component_filters(html)

    assets: dict[str, tuple[bytes, str]] = {}
    scripts = []
    def externalize_script(match):
        attributes, content = match.groups()
        if re.search(r"\bsrc\s*=", attributes):
            return match.group(0)
        route = f"/standard-{len(scripts)}.js"
        scripts.append(route)
        assets[route] = (content.encode("utf-8"), "text/javascript; charset=utf-8")
        return f'<script src="{route}"></script>'
    html = re.sub(r"<script([^>]*)>(.*?)</script>", externalize_script, html, flags=re.DOTALL)
    styles = []
    def externalize_style(match):
        styles.append(match.group(1))
        return '<link rel="stylesheet" href="/standard.css">'
    html = re.sub(r"<style>(.*?)</style>", externalize_style, html, flags=re.DOTALL)
    css = "\n".join(styles) + """
#full-graph-toolbar {position:absolute;top:14px;left:14px;z-index:2;max-width:calc(100% - 312px);padding:10px 13px;background:var(--panel);border:1px solid var(--border);border-radius:8px;box-shadow:0 2px 8px #0003}
#full-graph-toolbar strong,#full-graph-toolbar span {display:block}
#full-graph-toolbar strong {font-size:14px;margin-bottom:4px}
#full-graph-toolbar span {font-size:11px;color:var(--text-2)}
#full-graph-toolbar div {display:flex;gap:12px;align-items:center;margin-top:8px}
#full-graph-toolbar button {background:var(--bg);color:var(--text);border:1px solid var(--input-border);border-radius:4px;padding:4px 9px;cursor:pointer}
#full-graph-toolbar a,.evidence-link {font-size:12px;color:#77b6ed}
.evidence-link {display:block;margin-top:8px}
#component-filters {padding:12px;border-bottom:1px solid var(--border);flex-shrink:0}
.type-heading {display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;gap:6px}
.type-heading h3 {font-size:12px;color:var(--text-2);margin:0}
.type-heading div {display:flex;gap:5px}
.type-heading button {font-size:10px;background:var(--bg);color:var(--text);border:1px solid var(--input-border);border-radius:4px;padding:3px 5px;cursor:pointer}
.component-type {display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px;cursor:pointer}
.component-type input {accent-color:#4e79a7;cursor:pointer}
.component-type .type-count {margin-left:auto;color:var(--count);font-size:11px}
.component-type.pending {color:var(--text-2);cursor:default}
.component-type.pending input {cursor:default}
.relationship-layer-control {display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:12px;font-size:12px}
.relationship-layer-control select {max-width:155px;padding:4px;background:var(--bg);color:var(--text);border:1px solid var(--input-border);border-radius:4px}
.grouping-control {margin-top:0}
.group-description {font-size:11px;color:var(--text-2)}
#group-search {width:100%;box-sizing:border-box;padding:7px;background:var(--bg);color:var(--text);border:1px solid var(--input-border);border-radius:4px}
.legend-item {cursor:default;align-items:flex-start}
.legend-item .legend-cb,.legend-item .legend-dot {margin-top:4px}
.group-focus {flex:1;min-width:0;text-align:left;background:none;border:0;color:var(--text);cursor:pointer;padding:0;font:inherit}
.group-focus:hover .legend-label {text-decoration:underline}
.group-focus .legend-label {display:block;white-space:normal;overflow-wrap:anywhere}
.group-detail {display:block;font-size:10px;color:var(--text-2);line-height:1.4;margin-top:3px;overflow-wrap:anywhere}
#legend[data-mode="structure"] .group-detail {display:none}
.legend-count {padding-top:4px}
.graph-explanation {font-size:11px;line-height:1.45;color:var(--text-2);margin:7px 0}
.neighbor-name {display:block;overflow:hidden;text-overflow:ellipsis}
.neighbor-relations {display:block;white-space:normal;font-size:10px;line-height:1.4;color:var(--text-2)}
#visible-count,#empty-graph {font-size:11px;line-height:1.5;color:var(--text-2);margin:8px 0 0}
#empty-graph {color:var(--text)}
#sidebar {display:block;overflow-y:auto;flex-shrink:0}
#legend-wrap {overflow:visible;min-height:100px}
#info-panel {min-height:0}
"""
    assets["/standard.css"] = (css.encode("utf-8"), "text/css; charset=utf-8")
    assets["/"] = (html.encode("utf-8"), "text/html; charset=utf-8")
    return assets
