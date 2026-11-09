// Execute the shipped explorer script with deferred HTTP responses and a small DOM double.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
function element() {
  return {
    children: [], listeners: {}, dataset: {}, hidden: false, textContent: '', value: '',
    classList: {add() {}, remove() {}, toggle() {}},
    addEventListener(type, callback) { this.listeners[type] = callback; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
  };
}
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  },
  createElement: element,
  querySelectorAll: () => [],
};
const requests = [];
const sandbox = {
  assert, scenario, document, URLSearchParams, setTimeout, clearTimeout,
  location: {hash: ''}, requests,
  fetch(url) {
    return new Promise((resolve, reject) => requests.push({
      url, reject, respond(data) { resolve({ok: true, json: async () => data}); },
    }));
  },
};
const checks = `
(async () => {
  state.meta = {comparison: {before_snapshot_id: 'snapshot:before', after_snapshot_id: 'snapshot:after'}};
  state.focus = 'sf:apexmethod:sample.run';
  state.edge = {id: 'view:edge'};
  $('canonical-relation').value = 'rel:sample';
  function payload(path, snapshot='after', offset=0) {
    return {relationship: {qualifiers: {}}, snapshot, offset, total: 201,
      results: [{path, line: offset + 1, locator: 'synthetic', rule: 'fixture'}], next_offset: null};
  }
  function firstPath() { return $('evidence-rows').children[0].children[0].textContent; }
  if (scenario === 'snapshot') {
    const old = showEvidence(), oldRequest = requests.at(-1);
    state.evidenceSnapshot = 'before';
    const latest = showEvidence(), latestRequest = requests.at(-1);
    assert.match(oldRequest.url, /snapshot=after/);
    assert.match(latestRequest.url, /snapshot=before/);
    latestRequest.respond(payload('before.cls', 'before'));
    await latest;
    oldRequest.respond(payload('after.cls'));
    await old;
    assert.equal(firstPath(), 'before.cls', 'An older snapshot response must not overwrite the current one');
    assert.match($('qualifiers').textContent, /^Before snapshot/);
    assert.equal($('evidence-snapshot').textContent, 'Show after evidence');
  } else if (scenario === 'page') {
    const old = showEvidence(), oldRequest = requests.at(-1);
    state.evidenceOffset = 100;
    const latest = showEvidence(), latestRequest = requests.at(-1);
    assert.match(latestRequest.url, /offset=100/);
    latestRequest.respond(payload('page-two.cls', 'after', 100));
    await latest;
    oldRequest.respond(payload('page-one.cls'));
    await old;
    assert.equal(firstPath(), 'page-two.cls', 'An older page response must not overwrite a newer page');
    assert.match($('evidence-page').textContent, /101/);
  } else if (scenario === 'new-view') {
    const old = showEvidence(), oldRequest = requests.at(-1);
    void focus('sf:apexmethod:other.run');
    assert.match(requests.at(-1).url, /^\\/api\\/view/);
    oldRequest.reject(new Error('stale source error'));
    await old;
    assert.equal($('error').textContent, '', 'An obsolete request error must not be shown for the new component');
    assert.equal($('evidence-content').hidden, true);
    assert.equal($('evidence-empty').hidden, false);
  } else if (scenario === 'diagnostics') {
    $('component-diagnostics').hidden = false;
    $('diagnostic-more').dataset.offset = '100';
    const old = showDiagnosticsPage(), oldRequest = requests.at(-1);
    void focus('sf:apexmethod:other.run');
    oldRequest.respond({results: [{code: 'STALE', message: 'Old record'}], offset: 100, total: 101, next_offset: null});
    await old;
    assert.equal($('component-diagnostics').hidden, true, 'Old diagnostics must disappear while the new view loads');
    assert.equal($('diagnostic-rows').children.length, 0);
  } else if (scenario === 'diagnostic-snapshot') {
    const context = {snapshot_id: 'snapshot:before', total: 1, code_counts: {SYNTHETIC: 1},
      returned_count: 1, omitted_count: 0, diagnostics: [], interpretation: 'File scope'};
    renderDiagnostics(context);
    assert.match($('diagnostic-summary').textContent, /Before snapshot/);
    renderDiagnostics({...context, snapshot_id: 'snapshot:after'});
    assert.match($('diagnostic-summary').textContent, /After snapshot/);
  } else {
    throw new Error('Unknown scenario: ' + scenario);
  }
})()
`;
Promise.resolve(vm.runInNewContext(source + '\n' + checks, sandbox, {timeout: 5000}))
  .then(() => process.stdout.write('ok\n'))
  .catch(error => { console.error(error); process.exitCode = 1; });
