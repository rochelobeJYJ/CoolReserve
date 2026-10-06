const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function section(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert(first >= 0 && last > first);
  return source.slice(first, last);
}

function harness() {
  const handlers = {}, events = [], content = {};
  let mutations = 0, mutating = false, onReplace = () => {};
  Object.defineProperty(content, 'innerHTML', {set() {
    if (mutating) throw Error('Cannot replace the node during its blur handler');
    mutations++;
    mutating = true;
    try { onReplace(); } finally { mutating = false; }
  }});
  const context = {
    view: 'workflow',
    wf: {step: 2, recipe: {header_rows: 2}, preview: {can_commit: true}, dirty: false,
      from: '2026-11-01', to: '2026-11-30', source: {url: 'synthetic-source'}},
    $: () => content, document: {addEventListener: (type, handler) => handlers[type] = handler},
    wfTextHTML: () => '<input id="wfHeaderRows">', wfDataHTML: () => '<input id="wfMonth">',
    wfReviewHTML: () => '<p>synthetic review</p>', wfErrors: () => '',
    takeWfSettings: () => { context.wf.recipe.header_rows = Number(events.at(-1).value); },
  };
  vm.createContext(context);
  vm.runInContext(section('function renderWorkflow(', 'function takeWfSettings('), context);
  vm.runInContext(section('function workflowMonthRange(', 'function workflowMonthValue('), context);
  vm.runInContext(section("document.addEventListener('change',", "document.addEventListener('input',"), context);
  vm.runInContext(section("document.addEventListener('input',", 'let pollingInFlight='), context);
  function change(id, value) { const target = {id, value}; events.push(target); handlers.change({target}); }
  return {context, handlers, change, mutations: () => mutations, onReplace: callback => {onReplace = callback;}};
}

test('Header redraw ignores change and input re-entry from the removed focused element', () => {
  const h = harness();
  h.onReplace(() => {
    h.change('wfHeaderRows', '2');
    h.handlers.input({target: {id: 'wfUrl', value: 'stale-source'}});
    h.context.renderWorkflow();
  });
  h.change('wfHeaderRows', '1');
  assert.equal(h.mutations(), 1);
  assert.equal(h.context.wf.recipe.header_rows, 1);
  assert.equal(h.context.wf.source.url, 'synthetic-source');
  assert.equal(h.context.wf.rendering, false);
});

test('Month redraw cannot be overwritten by the old month change during replacement', () => {
  const h = harness();
  h.context.wf.step = 1;
  h.onReplace(() => h.change('wfMonth', '2026-11'));
  h.change('wfMonth', '2026-12');
  assert.equal(h.mutations(), 1);
  assert.equal(h.context.wf.from, '2026-12-01');
  assert.equal(h.context.wf.to, '2026-12-31');
});

test('Unchanged header and month changes keep the focused controls and approved preview', () => {
  const h = harness(), preview = h.context.wf.preview;
  h.change('wfHeaderRows', '2');
  h.change('wfMonth', '2026-11');
  assert.equal(h.mutations(), 0);
  assert.equal(h.context.wf.preview, preview);
  assert.equal(h.context.wf.dirty, false);
});

test('A failed DOM replacement releases the render guard for later recovery', () => {
  const h = harness();
  h.onReplace(() => { throw Error('synthetic renderer failure'); });
  assert.throws(() => h.context.renderWorkflow(), /synthetic renderer failure/);
  assert.equal(h.context.wf.rendering, false);
  h.onReplace(() => {});
  h.context.renderWorkflow();
  assert.equal(h.mutations(), 2);
});
