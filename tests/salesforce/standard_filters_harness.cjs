// Run the actual browser filtering code against small DOM/vis dataset doubles.
// The fixture covers mixed semantic layers, reverse arcs, and filter composition.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

function element() {
  return {
    children: [], listeners: {}, dataset: {}, checked: true, disabled: false,
    value: 'all', textContent: '', style: {},
    addEventListener(type, callback) { this.listeners[type] = callback; },
    setAttribute(name, value) { this[name] = value; },
    append(...children) { this.children.push(...children); },
    appendChild(child) { this.children.push(child); },
    querySelectorAll() {
      return this.children.flatMap(row => row.children || []).filter(
        child => child.className === 'component-type-cb' && !child.disabled);
    },
  };
}
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  },
  createElement() { return element(); },
};
function dataset(rows) {
  const items = new Map(rows.map(row => [row.id, structuredClone(row)]));
  const writes = [];
  return {
    writes,
    get(id) { return items.get(id); },
    update(changes) {
      writes.push(...changes);
      for (const change of changes) Object.assign(items.get(change.id), change);
    },
  };
}
const initial = JSON.stringify(input);
const sandbox = {
  assert, scenario, RAW_NODES: input.nodes, RAW_EDGES: input.edges,
  document, hiddenCommunities: new Set(), hoveredNodeId: null,
  nodesDS: dataset(input.nodes),
  edgesDS: dataset(input.edges.map((edge, id) => ({...edge, id}))),
  container: {style: {}}, network: {getSelectedNodes: () => [], unselectAll: () => {}},
  showInfo: () => {}, renderSearchResults: () => {},
};
const checks = `
function chooseLayer(value) {
  const select = document.getElementById('relationship-layer');
  select.value = value;
  select.listeners.change({target: select});
}
function toggleTests(value) {
  const control = document.getElementById('show-tests-cb');
  control.checked = value;
  control.listeners.change({target: control});
}
function toggleType(kind, value) {
  const control = document.getElementById('component-type-list').querySelectorAll()
    .find(input => input.dataset.kind === kind);
  assert.ok(control, kind);
  control.checked = value;
  control.listeners.change({target: control});
}
function visiblePairs() {
  return Array.from(RAW_EDGES).filter((edge, id) => !edgesDS.get(id).hidden)
    .map(edge => edge.from + '>' + edge.to).sort();
}
function visibleNodes() {
  return Array.from(RAW_NODES).filter(node => !nodesDS.get(node.id).hidden).map(node => node.id).sort();
}
function neighborDescriptions(id) {
  return Object.fromEntries(visibleNeighborRows(id).map(row => [row.id, [...row.descriptions].sort()]));
}
function inspect(id) {
  inspectedNodeId = id;
  focusIncidentEdges(id);
}
function focusedPairs() {
  return Array.from(highlightedEdgeIds).map(id => RAW_EDGES[id].from + '>' + RAW_EDGES[id].to).sort();
}
function assertBaseline() {
  RAW_EDGES.forEach((edge, id) => {
    assert.equal(edgesDS.get(id).width, edge.width);
    assert.equal(JSON.stringify(edgesDS.get(id).color), JSON.stringify(edge.color));
  });
}
applyComponentVisibility();
const allPairs = visiblePairs();
const allNodes = visibleNodes();
assert.equal(relationshipLayer, 'all');
assert.equal(allPairs.length, scenario === 'validation_rules' ? 8 : 6);
assert.equal(allNodes.length, scenario === 'validation_rules' ? 7 : 6);

if (scenario === 'layers') {
  chooseLayer('schema');
  assert.deepEqual(visiblePairs(), ['field>target', 'owner>field']);
  assert.deepEqual(visibleNodes(), allNodes, 'layers must retain nodes, including isolates');
  assert.deepEqual(neighborDescriptions('field'), {
    owner: ['Incoming: contains'], target: ['Outgoing: lookup to'],
  });
  const mixedId = RAW_EDGES.findIndex(edge => edge.from === 'owner' && edge.to === 'field');
  assert.match(edgesDS.get(mixedId).title, /contains/);
  assert.doesNotMatch(edgesDS.get(mixedId).title, /reads/);
  chooseLayer('dependencies');
  assert.deepEqual(visiblePairs(), allPairs, 'both mixed pairs must retain their dependency relation');
  assert.match(edgesDS.get(mixedId).title, /reads/);
  assert.doesNotMatch(edgesDS.get(mixedId).title, /contains/);
  assert.deepEqual(neighborDescriptions('target'), {field: ['Incoming: references']});
  chooseLayer('all');
  assert.deepEqual(visiblePairs(), allPairs);
  assert.deepEqual(visibleNodes(), allNodes);
} else if (scenario === 'direction') {
  const rows = neighborDescriptions('field');
  assert.deepEqual(rows.owner, ['Incoming: contains', 'Incoming: reads', 'Outgoing: writes']);
  assert.deepEqual(rows.target, ['Outgoing: lookup to', 'Outgoing: references']);
  chooseLayer('schema');
  assert.deepEqual(neighborDescriptions('owner'), {field: ['Outgoing: contains']});
  chooseLayer('dependencies');
  assert.deepEqual(neighborDescriptions('owner'), {field: ['Incoming: writes', 'Outgoing: reads']});
} else if (scenario === 'combined_filters') {
  chooseLayer('dependencies');
  toggleTests(false);
  assert.ok(!visibleNodes().includes('test'));
  assert.ok(visibleNodes().includes('unknown'), 'unknown test classification must remain');
  assert.ok(!visiblePairs().includes('test>field'));
  hiddenCommunities.add(1);
  applyComponentVisibility();
  assert.deepEqual(visibleNodes(), ['field', 'owner', 'unknown']);
  assert.deepEqual(visiblePairs(), ['field>owner', 'owner>field', 'unknown>field']);
  toggleType('field', false);
  assert.deepEqual(visiblePairs(), []);
  toggleType('field', true);
  hiddenCommunities.clear();
  applyComponentVisibility();
  document.getElementById('types-hide-all').listeners.click();
  assert.deepEqual(visibleNodes(), []);
  assert.deepEqual(visiblePairs(), []);
  document.getElementById('types-show-all').listeners.click();
  assert.equal(relationshipLayer, 'dependencies');
  assert.equal(showTests, false);
  assert.equal(visiblePairs().length, 5);
  toggleTests(true);
  chooseLayer('all');
  assert.deepEqual(visiblePairs(), allPairs);
  assert.deepEqual(visibleNodes(), allNodes);
} else if (scenario === 'focus_styles') {
  const titles = RAW_EDGES.map((_, id) => edgesDS.get(id).title);
  edgesDS.writes.length = 0;
  inspect('owner');
  assert.deepEqual(focusedPairs(), ['field>owner', 'owner>field']);
  assert.equal(edgesDS.writes.length, 2, 'focus writes only incident edges');
  for (const id of highlightedEdgeIds) {
    assert.equal(edgesDS.get(id).color.opacity, 0.95);
    assert.equal(edgesDS.get(id).width, 1.8);
  }
  edgesDS.writes.length = 0;
  focusIncidentEdges('owner');
  assert.equal(edgesDS.writes.length, 0, 'duplicate showInfo calls do not rewrite styles');
  inspect('target');
  assert.deepEqual(focusedPairs(), ['field>target']);
  assert.equal(edgesDS.writes.length, 3, 'selection touches only old/new incident sets');
  for (const update of edgesDS.writes) {
    assert.deepEqual(Object.keys(update).sort(), ['color', 'id', 'width']);
  }
  assert.deepEqual(RAW_EDGES.map((_, id) => edgesDS.get(id).title), titles);
  assert.deepEqual(visiblePairs(), allPairs, 'emphasis never hides relationships');
  inspect(null);
  assert.deepEqual(focusedPairs(), []);
  assertBaseline();
} else if (scenario === 'focus_filters') {
  inspect('field');
  assert.deepEqual(focusedPairs(), allPairs);
  chooseLayer('schema');
  assert.deepEqual(focusedPairs(), ['field>target', 'owner>field']);
  assert.deepEqual(visiblePairs(), focusedPairs());
  const mixedId = RAW_EDGES.findIndex(edge => edge.from === 'owner' && edge.to === 'field');
  assert.match(edgesDS.get(mixedId).title, /contains/);
  assert.doesNotMatch(edgesDS.get(mixedId).title, /reads/);
  inspect('unknown');
  assert.deepEqual(focusedPairs(), [], 'a visible isolate clears previous emphasis');
  assertBaseline();
  chooseLayer('dependencies');
  assert.deepEqual(focusedPairs(), ['unknown>field']);
  inspect('test');
  assert.deepEqual(focusedPairs(), ['test>field']);
  toggleTests(false);
  assert.equal(inspectedNodeId, null, 'hiding the inspected node clears focus');
  assert.deepEqual(focusedPairs(), []);
  assertBaseline();
  const testEdge = RAW_EDGES.findIndex(edge => edge.from === 'test');
  assert.equal(edgesDS.get(testEdge).hidden, true, 'clearing emphasis must preserve visibility');
  inspect('field');
  assert.ok(!focusedPairs().includes('test>field'));
  toggleTests(true);
  assert.ok(focusedPairs().includes('test>field'));
  inspect(null);
  chooseLayer('all');
  assertBaseline();
  assert.deepEqual(visiblePairs(), allPairs);
  assert.deepEqual(visibleNodes(), allNodes);
} else if (scenario === 'validation_empty') {
  const row = document.getElementById('component-type-list').children.find(
    row => row.children[0].dataset.kind === 'validationrule');
  assert.equal(row.children[0].disabled, true);
  assert.equal(row.children[2].textContent, '0');
  assert.doesNotMatch(row.children[2].title, /not extracted/i);
} else if (scenario === 'validation_rules') {
  const row = document.getElementById('component-type-list').children.find(
    row => row.children[0].dataset.kind === 'validationrule');
  assert.equal(row.children[0].disabled, false);
  assert.equal(row.children[0].checked, true);
  assert.equal(row.children[2].textContent, '1');
  inspect('rule');
  assert.deepEqual(focusedPairs(), ['owner>rule', 'rule>field']);
  toggleType('validationrule', false);
  assert.equal(inspectedNodeId, null);
  assert.ok(!visibleNodes().includes('rule'));
  assert.ok(!visiblePairs().includes('rule>field'));
  assert.ok(!visiblePairs().includes('owner>rule'));
  assert.equal(visiblePairs().length, 6);
  toggleType('validationrule', true);
  chooseLayer('dependencies');
  assert.ok(visiblePairs().includes('rule>field'));
  assert.ok(!visiblePairs().includes('owner>rule'));
  chooseLayer('schema');
  assert.ok(!visiblePairs().includes('rule>field'));
  assert.ok(visiblePairs().includes('owner>rule'));
  chooseLayer('all');
  assert.deepEqual(visiblePairs(), allPairs);
  assert.deepEqual(visibleNodes(), allNodes);
} else {
  throw new Error('Unknown test scenario: ' + scenario);
}
`;
vm.runInNewContext(source + '\n' + checks, sandbox, {timeout: 5000});
assert.equal(JSON.stringify(input), initial, 'filters must not mutate RAW metadata');
console.log('ok');
