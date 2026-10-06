const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const app = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const source = app.slice(app.indexOf('let pollingInFlight=false;'), app.indexOf('async function boot()'));

function harness({hidden = false, active = false, request} = {}) {
  const clock = {textContent: ''};
  const initial = {now: '2026-10-06 09:00', connection_mode: 'automatic', profile: {}, jobs: [], runner: {active, stage: active ? 'preparing' : 'done'}};
  let calls = 0;
  const context = {
    state: structuredClone(initial), profile: {}, polling: 1,
    document: {hidden, addEventListener(_name, callback) { this.onVisible = callback; }},
    $: () => clock,
    api: async () => { calls++; return request ? request() : {...initial, now: '2026-10-06 09:01'}; },
    applyState: next => { context.state = next; clock.textContent = next.now.replaceAll('-', '.'); },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return {context, clock, calls: () => calls};
}

test('background registration observes completion without a reload', async () => {
  const h = harness({hidden: true, active: true});
  h.context.api = async () => ({...h.context.state, runner: {active: false, stage: 'needs_review'}});
  await h.context.pollState();
  assert.equal(h.context.state.runner.stage, 'needs_review');
  assert.equal(h.context.state.runner.active, false);
});

test('idle background stays quiet and returning updates the clock', async () => {
  const h = harness({hidden: true});
  await h.context.pollState();
  assert.equal(h.calls(), 0);
  h.context.document.hidden = false;
  h.context.document.onVisible();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.calls(), 1);
  assert.equal(h.clock.textContent, '2026.10.06 09:01');
});

test('slow or failed status requests do not overlap or freeze later polling', async () => {
  let release;
  let calls = 0;
  const h = harness({request: () => {
    calls++;
    if (calls === 1) return new Promise((_resolve, reject) => { release = reject; });
    return h.context.state;
  }});
  const pending = h.context.pollState();
  await h.context.pollState();
  assert.equal(calls, 1);
  release(new Error('temporary unavailable'));
  await pending;
  await h.context.pollState();
  assert.equal(calls, 2);
});
