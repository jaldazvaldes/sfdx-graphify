// Shared visibility: node filters and canonical relationship layers compose.
const TYPE_LABELS = new Map([
  ['flow', 'Flows'], ['object', 'Objects'], ['field', 'Fields'],
  ['apexclass', 'Apex classes'], ['apexmethod', 'Apex methods'], ['apextrigger', 'Triggers'],
  ['lwc', 'Lightning components'], ['outboundmessage', 'Outbound messages'],
  ['validationrule', 'Validation rules'],
]);
const hiddenTypes = new Set();
let showTests = true;
let relationshipLayer = 'all';
let hiddenNodeIds = new Set();
let hiddenEdgeIds = new Set();
let inspectedNodeId = null;
const typeCounts = new Map();
for (const node of RAW_NODES) {
  typeCounts.set(node.kind, (typeCounts.get(node.kind) || 0) + 1);
  if (!TYPE_LABELS.has(node.kind)) TYPE_LABELS.set(node.kind, node.kind);
}
const formatCount = value => value.toLocaleString('en-US');
// Index ordered pairs once; a node panel visits only its incident edges.
const incidentEdges = new Map();
RAW_EDGES.forEach((edge, id) => {
  for (const endpoint of new Set([edge.from, edge.to])) {
    if (!incidentEdges.has(endpoint)) incidentEdges.set(endpoint, []);
    incidentEdges.get(endpoint).push(id);
  }
});

let highlightedEdgeIds = new Set();

function focusIncidentEdges(nodeId) {
  const next = new Set(nodeId !== null && !hiddenNodeIds.has(nodeId)
    ? (incidentEdges.get(nodeId) || []).filter(id => !hiddenEdgeIds.has(id)) : []);
  const updates = [];
  // The muted background is already rendered. Only links entering or leaving
  // focus need a dataset write, including when a filter hides an incident link.
  for (const id of new Set([...highlightedEdgeIds, ...next])) {
    const focused = next.has(id);
    if (focused === highlightedEdgeIds.has(id)) continue;
    const edge = RAW_EDGES[id];
    updates.push({id, width: focused ? 1.8 : edge.width,
      color: focused
        ? {...edge.color, color: '#63b3ed', highlight: '#63b3ed', hover: '#63b3ed',
          inherit: false, opacity: 0.95}
        : {...edge.color}});
  }
  highlightedEdgeIds = next;
  if (updates.length) edgesDS.update(updates);
}

function relationshipLabel(detail) {
  return detail.relation === 'references' && detail.context === 'lookup_declaration'
    ? 'lookup to' : detail.relation;
}

function relationshipTitle(details, confidence) {
  const labels = [...new Set(details.map(relationshipLabel))].sort();
  return `${labels.join(', ')} [${confidence}]`;
}

function activeRelationshipDetails(edge) {
  return relationshipLayer === 'all' ? edge.relationship_details
    : edge.relationship_details.filter(detail => detail.layer === relationshipLayer);
}

const displayedEdgeTitles = RAW_EDGES.map(edge => relationshipTitle(edge.relationship_details, edge.confidence));
let displayedRelationshipLayer = 'all';

function visibleNeighborRows(nodeId) {
  const neighbors = new Map();
  for (const id of incidentEdges.get(nodeId) || []) {
    if (hiddenEdgeIds.has(id)) continue;
    const edge = RAW_EDGES[id];
    const outgoing = edge.from === nodeId;
    const neighborId = outgoing ? edge.to : edge.from;
    if (hiddenNodeIds.has(neighborId)) continue;
    const details = activeRelationshipDetails(edge);
    if (!details.length) continue;
    if (!neighbors.has(neighborId)) neighbors.set(neighborId, new Set());
    const descriptions = neighbors.get(neighborId);
    for (const detail of details) {
      descriptions.add(`${outgoing ? 'Outgoing' : 'Incoming'}: ${relationshipLabel(detail)}`);
    }
  }
  return [...neighbors].map(([id, descriptions]) => ({id, descriptions: [...descriptions].sort()}))
    .sort((a, b) => {
      const aLabel = String(nodesDS.get(a.id)?.label || a.id);
      const bLabel = String(nodesDS.get(b.id)?.label || b.id);
      return aLabel.localeCompare(bLabel) || String(a.id).localeCompare(String(b.id));
    });
}

function isComponentVisible(node) {
  const group = nodesDS.get(node.id)?.community ?? node.community;
  return !hiddenTypes.has(node.kind) && !hiddenCommunities.has(group) &&
    (showTests || node.is_test !== true);
}

function updateVisibleCount() {
  const visible = RAW_NODES.length - hiddenNodeIds.size;
  const links = RAW_EDGES.length - hiddenEdgeIds.size;
  document.getElementById('visible-count').textContent =
    `Visible: ${formatCount(visible)} / ${formatCount(RAW_NODES.length)} components · ` +
    `${formatCount(links)} / ${formatCount(RAW_EDGES.length)} links`;
  document.getElementById('empty-graph').hidden = visible !== 0;
}

function applyComponentVisibility() {
  const nextHidden = new Set();
  const nodeUpdates = [];
  for (const node of RAW_NODES) {
    const hidden = !isComponentVisible(node);
    if (hidden) nextHidden.add(node.id);
    if (hidden !== hiddenNodeIds.has(node.id)) nodeUpdates.push({id: node.id, hidden});
  }
  const nextEdges = new Set();
  const edgeUpdates = [];
  const layerChanged = relationshipLayer !== displayedRelationshipLayer;
  RAW_EDGES.forEach((edge, id) => {
    const active = activeRelationshipDetails(edge);
    const hidden = nextHidden.has(edge.from) || nextHidden.has(edge.to) || !active.length;
    if (hidden) nextEdges.add(id);
    const update = {id};
    if (hidden !== hiddenEdgeIds.has(id)) update.hidden = hidden;
    if (layerChanged) {
      const title = relationshipTitle(active, edge.confidence);
      if (title !== displayedEdgeTitles[id]) {
        update.title = title;
        displayedEdgeTitles[id] = title;
      }
    }
    if (Object.keys(update).length > 1) edgeUpdates.push(update);
  });
  displayedRelationshipLayer = relationshipLayer;
  hiddenNodeIds = nextHidden;
  hiddenEdgeIds = nextEdges;
  if (nodeUpdates.length) nodesDS.update(nodeUpdates);
  if (edgeUpdates.length) edgesDS.update(edgeUpdates);
  if (hoveredNodeId !== null && hiddenNodeIds.has(hoveredNodeId)) {
    hoveredNodeId = null;
    container.style.cursor = 'default';
  }
  if (network.getSelectedNodes().some(id => hiddenNodeIds.has(id))) network.unselectAll();
  if (inspectedNodeId !== null) {
    if (hiddenNodeIds.has(inspectedNodeId)) {
      inspectedNodeId = null;
      document.getElementById('info-content').textContent = 'Click a visible node to inspect it';
    } else {
      showInfo(inspectedNodeId);
    }
  }
  focusIncidentEdges(inspectedNodeId);
  renderSearchResults();
  updateVisibleCount();
}

const typeList = document.getElementById('component-type-list');
for (const [kind, name] of TYPE_LABELS) {
  const count = typeCounts.get(kind) || 0;
  const row = document.createElement('label');
  row.className = 'component-type';
  const input = document.createElement('input');
  input.type = 'checkbox';
  input.className = 'component-type-cb';
  input.dataset.kind = kind;
  input.checked = count > 0;
  input.disabled = count === 0;
  input.setAttribute('aria-label', name);
  input.addEventListener('change', () => {
    if (input.checked) hiddenTypes.delete(kind); else hiddenTypes.add(kind);
    applyComponentVisibility();
  });
  const label = document.createElement('span');
  label.textContent = name;
  const total = document.createElement('span');
  total.className = 'type-count';
  total.textContent = formatCount(count);
  total.title = 'Total components of this type in the snapshot';
  row.append(input, label, total);
  typeList.appendChild(row);
}

function setAllTypes(visible) {
  hiddenTypes.clear();
  if (!visible) for (const kind of typeCounts.keys()) hiddenTypes.add(kind);
  typeList.querySelectorAll('.component-type-cb:not(:disabled)').forEach(input => { input.checked = visible; });
  applyComponentVisibility();
}
document.getElementById('types-show-all').addEventListener('click', () => setAllTypes(true));
document.getElementById('types-hide-all').addEventListener('click', () => setAllTypes(false));
const showTestsInput = document.getElementById('show-tests-cb');
const testNodeCount = document.getElementById('test-node-count');
testNodeCount.textContent = formatCount(RAW_NODES.filter(node => node.is_test === true).length);
testNodeCount.title = 'Components explicitly classified as tests by the Apex parser; unknown classifications remain visible';
showTestsInput.addEventListener('change', () => {
  showTests = showTestsInput.checked;
  applyComponentVisibility();
});
document.getElementById('relationship-layer').addEventListener('change', event => {
  relationshipLayer = event.target.value;
  applyComponentVisibility();
});
