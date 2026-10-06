const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const app = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const batch = fs.readFileSync(path.join(__dirname, '../web/batch.js'), 'utf8');
function section(start, end) {
  const first = app.indexOf(start);
  const last = app.indexOf(end, first);
  assert(first >= 0 && last > first, `Missing UI function: ${start}`);
  return app.slice(first, last);
}

function harness({mode = 'manual', errors = [], offline = false, jobs = []} = {}) {
  const calls = [], dialogs = [], toasts = [], elements = new Map();
  const state = {result_confirmation_mode: mode, connection_mode: 'automatic', offline,
    profile: {roles: {}, contacts: {}}, jobs, states: {confirmed: '예약 확인 완료'}, runner: {active: false}};
  const plan = {id: 'synthetic-plan', jobs, errors, result_confirmation_mode: mode};
  const context = {
    jobFilter: 'all', updateJobFilter() {},
    state, profile: state.profile, selected: new Set(), selectionMode: false,
    locked: new Set(['submitting', 'needs_review', 'confirmed', 'uncertain']),
    esc: value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char])),
    canOfferInputTrial: () => false, document: {querySelectorAll: () => []},
    previewJobs: rows => rows.map(row => `${row.title}: ${row.body}`).join('\n'),
    modal: (title, html, buttons) => dialogs.push({title, html, buttons}),
    closeModal() {}, refresh: async () => {}, refreshTable() {}, toast: value => toasts.push(value),
    failure: error => { throw error; },
    $: id => {
      if (!elements.has(id)) elements.set(id, {value: '', addEventListener() {}, querySelectorAll: () => [], scrollIntoView() {}, focus() {}});
      return elements.get(id);
    },
    api: async (route, payload) => {
      calls.push({route, payload});
      if (route === 'plan') return plan;
      if (route === 'run') return {started: true};
      if (route === 'batch/register') return {saved: jobs.length, job_ids: jobs.map(job => job.id), started: true, plan_errors: []};
      throw Error(`Unexpected API route: ${route}`);
    },
  };
  vm.createContext(context);
  vm.runInContext(app.slice(app.indexOf('function displayTitle('),app.indexOf('const locked=')),context);
  vm.runInContext(section('function resultConfirmationMode(', 'function inputReadiness('), context);
  vm.runInContext(section('function badge(', 'function refreshTable('), context);
  vm.runInContext(section('function details(', 'async function removeJobs('), context);
  vm.runInContext(section('async function run(', 'async function loadHistory('), context);
  vm.runInContext(batch, context);
  return {context, calls, dialogs, toasts};
}

function monthJobs() {
  return Array.from({length: 30}, (_, index) => ({id: `synthetic-${index + 1}`, status: 'draft',
    scheduled: `2026-11-${String(index + 1).padStart(2, '0')} 08:20`,
    recipient_ids: [`person-${index + 1}`, `partner-${index + 1}`],
    title: `합성 ${index + 1}일 안내`, body: `서로 다른 ${index + 1}일 본문\n다음 줄`, attachments: []}));
}

function reviewFor(jobs) {
  return {review_id: 'synthetic-review', total: jobs.length, ready: jobs.length, can_commit: true,
    rows: jobs.map((job, index) => ({row: index + 1, job,
      input: {recipient: `합성담당${index + 1}, 합성협조${index + 1}`, date: job.scheduled.slice(0, 10), time: '08:20', title: job.title, body: job.body},
      selections: job.recipient_ids.map((id, person) => ({name: person ? `합성협조${index + 1}` : `합성담당${index + 1}`,
        selected_id: id, candidates: [{id, expected: `합성계정(${index * 2 + person + 1})(합성부서)`}]})), issues: []}))};
}

test('manual mode registers thirty distinct jobs with multiple recipients without result roles', async () => {
  const jobs = monthJobs(), h = harness({jobs});
  await h.context.run('register', jobs.map(job => job.id));
  assert.equal(h.calls[0].payload.result_confirmation_mode, 'manual');
  assert.equal(h.calls[0].payload.ids.length, 30);
  const dialog = h.dialogs.at(-1), submit = dialog.buttons.find(button => button.primary);
  assert.equal(submit.disabled, false);
  assert.equal(submit.text, '30건 한 번에 등록');
  assert.match(dialog.html, /도착 여부는 조회하지 않습니다/);
  await submit.action({disabled: false});
  assert.equal(h.calls.filter(call => call.route === 'run').length, 1);
  assert.equal(h.calls.at(-1).payload.plan_id, 'synthetic-plan');
  assert.equal(h.calls.at(-1).payload.phrase, '예약 등록');
});

test('automatic mode still presents backend result adapter errors and blocks registration', async () => {
  const h = harness({mode: 'automatic', jobs: monthJobs(), errors: [{title: '합성 안내', errors: ['결과 목록 연결이 필요합니다.']}]});
  await h.context.run('register', ['synthetic-1']);
  assert.equal(h.calls[0].payload.result_confirmation_mode, 'automatic');
  assert.equal(h.dialogs.at(-1).buttons.find(button => button.primary).disabled, true);
  assert.match(h.dialogs.at(-1).html, /결과 목록 연결이 필요합니다/);
  assert.match(h.dialogs.at(-1).html, /결과 목록을 자동 대조합니다/);
});

test('manual mode does not bypass invalid recipient or input errors', async () => {
  const h = harness({jobs: monthJobs(), errors: [{title: '합성 안내', errors: ['수신자 계정을 확인하세요.']}]});
  await h.context.run('register', ['synthetic-1']);
  assert.equal(h.dialogs.at(-1).buttons.find(button => button.primary).disabled, true);
  assert.match(h.dialogs.at(-1).html, /수신자 계정을 확인하세요/);
});

test('batch editor submits all thirty reviewed multi-recipient rows with one manual registration action', async () => {
  const jobs = monthJobs(), h = harness({jobs}), review = reviewFor(jobs);
  h.context.showBatchPreview(review);
  const dialog = h.dialogs.at(-1), submit = dialog.buttons.find(button => button.text === '30건 한 번에 등록');
  assert.equal(submit.disabled, false);
  assert.equal((dialog.html.match(/class="batch-recipient"/g) || []).length, 60);
  for (const job of jobs) assert(dialog.html.includes(job.title));
  assert.match(dialog.html, /도착 여부는 조회하지 않습니다/);
  await submit.action({disabled: false});
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].route, 'batch/register');
  assert.equal(h.calls[0].payload.result_confirmation_mode, 'manual');
  assert.equal(h.calls[0].payload.review_id, review.review_id);
  assert.equal(h.context.selected.size, 30);
  assert.match(h.toasts.at(-1), /끝날 때까지 PC 사용을 잠시 멈춰 주세요/);
});

test('batch registration remains disabled offline or while row errors remain', () => {
  const jobs = monthJobs();
  for (const options of [{offline: true, valid: true}, {offline: false, valid: false}]) {
    const h = harness({jobs, offline: options.offline}), review = reviewFor(jobs);
    review.can_commit = options.valid;
    h.context.showBatchPreview(review);
    assert.equal(h.dialogs.at(-1).buttons.find(button => button.text === '30건 한 번에 등록').disabled, true);
  }
});

test('request completion and confirmation are distinct and completed requests stay locked', () => {
  const job = {...monthJobs()[0], status: 'needs_review', note: ''}, h = harness({jobs: [job]});
  assert.match(h.context.badge(job), /등록 요청 완료/);
  assert.match(h.context.badge({...job, status: 'confirmed'}), /확인 완료/);
  assert.match(h.context.badge({...job, status: 'uncertain'}), /등록 여부 불명/);
  assert.equal(h.context.runStageLabel({stage: 'done', mode: 'register', result_confirmation_mode: 'manual'}), '등록 요청 완료');
  assert.equal(h.context.runStageLabel({stage: 'done', mode: 'simulate', result_confirmation_mode: 'manual'}), '완료');
  h.context.details(job.id);
  const dialog = h.dialogs.at(-1);
  assert.match(dialog.html, /실제 접수 결과는 사용자가 확인할 수 있습니다/);
  assert(!dialog.buttons.some(button => button.text === '예약 등록'));
  const optional=dialog.buttons.find(button => button.text === '확인 기록 남기기 · 선택');
  assert(optional&&!optional.primary&&optional.quiet);
  assert.match(dialog.html,/확인 기록 남기기는 선택/);
  assert.doesNotMatch(dialog.html,/추가 확인 없이 다음 작업/);
});

test('Uncertain submission stays locked and offers a result record without a send action',()=>{
  const job={...monthJobs()[0],status:'uncertain',note:''},h=harness({jobs:[job]});
  h.context.details(job.id);const dialog=h.dialogs.at(-1);
  assert.match(dialog.html,/자동 재등록하지 않습니다/);
  assert(!dialog.buttons.some(button=>/예약 전 검토|수정/.test(button.text)));
  assert(dialog.buttons.find(button=>button.text==='등록 여부 확인 기록').primary);
  assert.equal(h.calls.length,0);
});

test('A thirty-message plan is compact and places shared preparation before individual rows',async()=>{
  const jobs=monthJobs(),h=harness({jobs});await h.context.run('register',jobs.map(job=>job.id));
  const html=h.dialogs.at(-1).html;
  assert.match(html,/등록 가능 30건 · 수정 필요 0건/);
  assert.equal((html.match(/class="review-item"/g)||[]).length,30);
  assert.equal((html.match(/data-has-issues="false" open/g)||[]).length,0);
  assert(html.indexOf('plan-registration-guide')<html.indexOf('planReviewRows'));
  assert.equal(h.calls.filter(call=>call.route==='run').length,0);
});

test('Problem rows stay expanded and cannot enable a failed registration plan',async()=>{
  const jobs=monthJobs(),h=harness({jobs,errors:[{id:jobs[4].id,title:jobs[4].title,errors:['수신자 연결 필요']}]});// One invalid row must block the entire reviewed plan.
  await h.context.run('register',jobs.map(job=>job.id));const dialog=h.dialogs.at(-1);
  assert.match(dialog.html,/등록 가능 29건 · 수정 필요 1건/);
  assert.equal((dialog.html.match(/data-has-issues="true" open/g)||[]).length,1);
  assert(dialog.buttons.find(button=>button.text==='30건 한 번에 등록').disabled);
  assert.equal(h.calls.length,1);
});

test('Offline new or existing message preview uses input-only plan and cannot start registration', async () => {
  const h=harness({jobs:monthJobs().slice(0,1),offline:true});
  await h.context.run('register',['synthetic-1']);
  assert.equal(h.calls[0].payload.mode,'simulate');
  const submit=h.dialogs.at(-1).buttons.find(button=>button.primary);
  assert.equal(submit.disabled,true);
  await assert.rejects(submit.action({disabled:false}),/모의 실행/);
  assert.equal(h.calls.length,1);
});
