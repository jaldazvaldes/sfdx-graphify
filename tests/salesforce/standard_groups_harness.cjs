// Execute the shipped grouping/filter scripts against small DOM and vis doubles.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const filtersSource = fs.readFileSync(process.argv[2], 'utf8');
const groupsSource = fs.readFileSync(process.argv[3], 'utf8');
const scenario = process.argv[4];

function element(tagName = 'div') {
  const item = {
    tagName: tagName.toUpperCase(), children: [], listeners: {}, dataset: {},
    checked: true, disabled: false, hidden: false, value: '', style: {},
    className: '', title: '', parentElement: null, attributes: {},
    addEventListener(type, callback) {
      (this.listeners[type] ||= []).push(callback);
    },
    dispatchEvent(event) {
      event.target ||= this;
      event.stopPropagation ||= () => {};
      for (const callback of this.listeners[event.type] || []) callback(event);
      if (event.type === 'click' && this.onclick) this.onclick(event);
      return true;
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name] ?? null; },
    append(...children) {
      for (const child of children) {
        child.parentElement = this;
        this.children.push(child);
      }
    },
    appendChild(child) { this.append(child); return child; },
    prepend(child) { child.parentElement = this; this.children.unshift(child); },
    replaceChildren(...children) { this.children = []; this._text = ''; this.append(...children); },
    remove() {
      if (this.parentElement) {
        this.parentElement.children = this.parentElement.children.filter(child => child !== this);
      }
    },
    querySelectorAll(selector) { return descend(this).filter(child => matches(child, selector)); },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    contains(child) { return this === child || descend(this).includes(child); },
    click() { this.dispatchEvent({type: 'click'}); },
  };
  item.classList = {
    contains(name) { return item.className.split(/\s+/).includes(name); },
    add(name) { if (!this.contains(name)) item.className = (item.className + ' ' + name).trim(); },
    remove(name) { item.className = item.className.split(/\s+/).filter(x => x !== name).join(' '); },
    toggle(name, force) {
      const enabled = force === undefined ? !this.contains(name) : force;
      if (enabled) this.add(name); else this.remove(name);
      return enabled;
    },
  };
  Object.defineProperty(item, 'textContent', {
    get() { return (this._text || '') + this.children.map(child => child.textContent).join(''); },
    set(value) { this.children = []; this._text = String(value); },
  });
  Object.defineProperty(item, 'innerHTML', {
    get() { return this._html || ''; },
    set(value) {
      // Literal labels belong in textContent (or must be escaped before HTML).
      assert.doesNotMatch(String(value), /<\s*(?:script|img)\b/i,
        'Graph labels must never become executable markup');
      this.children = [];
      this._html = String(value);
    },
  });
  return item;
}
function descend(root) { return root.children.flatMap(child => [child, ...descend(child)]); }
function matches(item, selector) {
  if (selector.includes(',')) return selector.split(',').some(part => matches(item, part.trim()));
  if (selector.endsWith(':not(:disabled)')) {
    return !item.disabled && matches(item, selector.replace(':not(:disabled)', ''));
  }
  if (selector.startsWith('.')) return item.classList.contains(selector.slice(1));
  return item.tagName.toLowerCase() === selector.toLowerCase();
}
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  },
  createElement: element,
  createDocumentFragment() { return element('fragment'); },
  querySelectorAll(selector) {
    const every = new Set([...elements.values()].flatMap(root => [root, ...descend(root)]));
    return [...every].filter(item => matches(item, selector));
  },
};
function dataset(rows) {
  const items = new Map(rows.map(row => [row.id, structuredClone(row)]));
  return {
    get(id) {
      if (id === undefined) return [...items.values()];
      if (Array.isArray(id)) return id.map(key => items.get(key));
      return items.get(id);
    },
    update(changes) {
      for (const change of Array.isArray(changes) ? changes : [changes]) {
        assert.ok(items.has(change.id), 'Grouping may update existing nodes only');
        Object.assign(items.get(change.id), change);
      }
    },
    getIds() { return [...items.keys()]; },
  };
}
const fits = [];
const initial = JSON.stringify(input);
const sandbox = {
  assert, scenario, RAW_NODES: input.nodes, RAW_EDGES: input.edges,
  GROUPING_MODES: input.modes, LEGEND: input.modes.structure.legend,
  document, hiddenCommunities: new Set(), hoveredNodeId: null,
  nodesDS: dataset(input.nodes),
  edgesDS: dataset(input.edges.map((edge, id) => ({...edge, id}))),
  container: {style: {}},
  network: {
    getSelectedNodes: () => [], unselectAll: () => {},
    fit: options => fits.push(options), redraw: () => {},
  },
  fits, showInfo: () => {}, renderSearchResults: () => {},
  requestAnimationFrame: callback => callback(),
  Event: function(type) { this.type = type; },
};

const checks = `
function emit(id, type, value) {
  const control = document.getElementById(id);
  if (typeof value === 'boolean') control.checked = value;
  else if (value !== undefined) control.value = value;
  control.dispatchEvent({type});
}
function visibleNodes() {
  return Array.from(RAW_NODES).filter(node => !nodesDS.get(node.id).hidden).map(node => node.id).sort();
}
function visiblePairs() {
  return Array.from(RAW_EDGES).filter((edge, id) => !edgesDS.get(id).hidden)
    .map(edge => edge.from + '>' + edge.to).sort();
}
function toggleType(kind, checked) {
  const control = document.getElementById('component-type-list')
    .querySelectorAll('.component-type-cb').find(item => item.dataset.kind === kind);
  assert.ok(control, kind);
  control.checked = checked;
  control.dispatchEvent({type: 'change'});
}
applyComponentVisibility();
const allNodes = visibleNodes();
const allPairs = visiblePairs();
assert.equal(allNodes.length, 8);
assert.equal(allPairs.length, 7);

if (scenario === 'switching_filters') {
  // A group hidden in one layout must not hide an unrelated group with the
  // same numeric id after switching to a different partition.
  hiddenCommunities.add(GROUPING_MODES.structure.placements.first[0]);
  applyComponentVisibility();
  assert.ok(!visibleNodes().includes('first'));
  emit('show-tests-cb', 'change', false);
  emit('relationship-layer', 'change', 'dependencies');
  toggleType('field', false);
  switchGrouping('connectivity');
  assert.equal(showTests, false);
  assert.equal(relationshipLayer, 'dependencies');
  assert.ok(hiddenTypes.has('field'));
  assert.ok(visibleNodes().includes('first'));
  assert.ok(!visibleNodes().includes('test'));
  assert.ok(visibleNodes().includes('unknown'));
  assert.ok(!visibleNodes().includes('first_field'));
  assert.deepEqual(visiblePairs(), []);
  for (const node of RAW_NODES) {
    const [cid, x, y] = GROUPING_MODES.connectivity.placements[node.id];
    assert.equal(nodesDS.get(node.id).community, cid);
    assert.equal(nodesDS.get(node.id).x, x);
    assert.equal(nodesDS.get(node.id).y, y);
  }
  switchGrouping('structure');
  toggleType('field', true);
  emit('show-tests-cb', 'change', true);
  emit('relationship-layer', 'change', 'all');
  toggleAllCommunities(false);
  assert.deepEqual(visibleNodes(), allNodes);
  assert.deepEqual(visiblePairs(), allPairs);
} else if (scenario === 'legend_focus') {
  emit('group-search', 'input', 'First');
  let rows = document.getElementById('legend').children;
  assert.ok(rows.length > 0);
  assert.ok(rows.every(row => row.textContent.toLowerCase().includes('first')));
  assert.deepEqual(visibleNodes(), allNodes, 'Searching the legend must not filter the map');
  toggleType('field', false);
  const firstGroup = GROUPING_MODES.structure.placements.first[0];
  focusGroup(firstGroup);
  assert.deepEqual(fits.at(-1).nodes, ['first'], 'Focus must exclude nodes hidden by other filters');
  const row = rows.find(item => item.querySelector('.legend-label').textContent.includes('First'));
  assert.ok(row);
  const checkbox = row.querySelector('.legend-cb');
  checkbox.checked = false;
  checkbox.dispatchEvent({type: 'change'});
  assert.ok(!visibleNodes().includes('first'));
  const previousFits = fits.length;
  focusGroup(firstGroup);
  assert.equal(fits.length, previousFits, 'A fully hidden group must not change the viewport');
  assert.ok(document.getElementById('select-all-cb').indeterminate);
  emit('group-search', 'input', 'no-such-group');
  assert.equal(document.getElementById('legend').children.length, 0);
  assert.ok(document.getElementById('group-list-count').textContent.startsWith('0 / '));
  emit('select-all-cb', 'change', false);
  assert.deepEqual(visibleNodes(), [], 'Select all acts on all groups, including search-hidden rows');
  emit('select-all-cb', 'change', true);
  toggleType('field', true);
  assert.deepEqual(visibleNodes(), allNodes);
  assert.deepEqual(visiblePairs(), allPairs);
} else if (scenario === 'literal_labels') {
  const cid = GROUPING_MODES.structure.placements.hostile[0];
  const literal = GROUPING_MODES.structure.legend.find(group => group.cid === cid).label;
  assert.ok(document.getElementById('legend').textContent.includes(literal));
  switchGrouping('connectivity');
  switchGrouping('structure');
  assert.ok(document.getElementById('legend').textContent.includes(literal));
} else {
  throw new Error('Unknown test scenario: ' + scenario);
}
`;
vm.runInNewContext(filtersSource + '\n' + groupsSource + '\n' + checks, sandbox, {timeout: 5000});
assert.equal(JSON.stringify(input), initial, 'Grouping must not mutate canonical browser payloads');
console.log('ok');
