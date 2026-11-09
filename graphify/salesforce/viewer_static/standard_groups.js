// Salesforce navigation lives outside the upstream renderer and canonical graph.
const selectAllCb = document.getElementById('select-all-cb');
const legendEl = document.getElementById('legend');
const groupingInput = document.getElementById('grouping-mode');
const groupSearch = document.getElementById('group-search');
let groupingMode = 'structure';
let groupMembers = new Map();

function indexGroupMembers(mode) {
  groupMembers = new Map();
  for (const [id, [cid]] of Object.entries(mode.placements)) {
    if (!groupMembers.has(cid)) groupMembers.set(cid, []);
    groupMembers.get(cid).push(id);
  }
}

function updateSelectAllState() {
  const hidden = hiddenCommunities.size;
  selectAllCb.checked = hidden === 0;
  selectAllCb.indeterminate = hidden > 0 && hidden < LEGEND.length;
}

function focusGroup(cid) {
  const ids = (groupMembers.get(cid) || []).filter(id => !hiddenNodeIds.has(id));
  if (ids.length) network.fit({nodes: ids, animation: false});
}

function renderGroupLegend() {
  const query = groupSearch.value.trim().toLowerCase();
  legendEl.dataset.mode = groupingMode;
  legendEl.replaceChildren();
  let shown = 0;
  for (const group of LEGEND) {
    if (query && !`${group.label} ${group.description}`.toLowerCase().includes(query)) continue;
    shown++;
    const item = document.createElement('div');
    item.className = 'legend-item' + (hiddenCommunities.has(group.cid) ? ' dimmed' : '');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'legend-cb';
    checkbox.checked = !hiddenCommunities.has(group.cid);
    checkbox.setAttribute('aria-label', `Show group ${group.label}`);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) hiddenCommunities.delete(group.cid);
      else hiddenCommunities.add(group.cid);
      item.classList.toggle('dimmed', !checkbox.checked);
      applyComponentVisibility();
      updateSelectAllState();
    });
    const dot = document.createElement('span');
    dot.className = 'legend-dot';
    dot.style.background = group.color;
    const focus = document.createElement('button');
    focus.type = 'button';
    focus.className = 'group-focus';
    focus.setAttribute('aria-label', `Zoom to group ${group.label}`);
    focus.title = group.description + ' Zoom to visible components in this group.';
    focus.addEventListener('click', () => focusGroup(group.cid));
    const label = document.createElement('span');
    label.className = 'legend-label';
    label.textContent = group.label;
    const description = document.createElement('span');
    description.className = 'group-detail';
    description.textContent = group.description;
    focus.append(label, description);
    const count = document.createElement('span');
    count.className = 'legend-count';
    count.textContent = formatCount(group.count);
    count.title = 'Total components in this group';
    item.append(checkbox, dot, focus, count);
    legendEl.appendChild(item);
  }
  document.getElementById('group-list-count').textContent =
    `${formatCount(shown)} / ${formatCount(LEGEND.length)} groups`;
  updateSelectAllState();
}

function toggleAllCommunities(hide) {
  hiddenCommunities.clear();
  if (hide) for (const group of LEGEND) hiddenCommunities.add(group.cid);
  applyComponentVisibility();
  renderGroupLegend();
}

function switchGrouping(modeName) {
  const mode = GROUPING_MODES[modeName];
  if (!mode || modeName === groupingMode) return;
  groupingMode = modeName;
  groupingInput.value = modeName;
  LEGEND = mode.legend;
  const groups = new Map(LEGEND.map(group => [group.cid, group]));
  const updates = RAW_NODES.map(node => {
    const [cid] = mode.placements[node.id];
    const group = groups.get(cid);
    return {
      id: node.id, community: cid, community_name: group.label,
      group_description: group.description,
      color: {background: group.color, border: group.color,
        highlight: {background: '#ffffff', border: group.color}},
    };
  });
  // Group selections belong to a partition; type/test/relationship filters don't.
  hiddenCommunities.clear();
  nodesDS.update(updates);
  indexGroupMembers(mode);
  document.getElementById('grouping-help').textContent = mode.help +
    ' Grouping changes colors and filters, not positions. Changing grouping shows all groups; other filters stay as selected.';
  updateGroupStats();
  renderGroupLegend();
  applyComponentVisibility();
}

function updateGroupStats() {
  document.getElementById('stats').textContent = `${formatCount(RAW_NODES.length)} components · ` +
    `${formatCount(RAW_EDGES.length)} links · ${formatCount(LEGEND.length)} groups`;
}

groupingInput.addEventListener('change', () => switchGrouping(groupingInput.value));
groupSearch.addEventListener('input', renderGroupLegend);
selectAllCb.addEventListener('change', () => toggleAllCommunities(!selectAllCb.checked));
document.getElementById('fit-all').addEventListener('click', () => network.fit({animation: false}));
LEGEND = GROUPING_MODES[groupingMode].legend;
indexGroupMembers(GROUPING_MODES[groupingMode]);
document.getElementById('grouping-help').textContent = GROUPING_MODES[groupingMode].help +
  ' Grouping changes colors and filters, not positions. Changing grouping shows all groups; other filters stay as selected.';
renderGroupLegend();
updateVisibleCount();
updateGroupStats();
requestAnimationFrame(() => network.fit({animation: false}));
