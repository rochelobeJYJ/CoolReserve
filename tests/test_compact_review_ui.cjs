const assert=require('node:assert/strict'),test=require('node:test');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const app=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function harness(extra={}){
  const context={state:{result_confirmation_mode:'manual'},esc:escape,previewJobs:jobs=>jobs.map(job=>`<pre>${escape(job.body)}</pre>`).join(''),...extra};
  vm.createContext(context);
  vm.runInContext(app.slice(app.indexOf('function displayTitle('),app.indexOf('const locked=')),context);
  vm.runInContext(app.slice(app.indexOf('function resultConfirmationMode('),app.indexOf('function inputReadiness(')),context);
  return context;
}
const jobs=()=>Array.from({length:30},(_,i)=>({id:'synthetic'+i,title:'',body:`\n합성 ${i+1}번째 본문\n두 번째 줄`,recipient:'합성 수신자',scheduled:'2026-11-10 09:22'}));

test('Single blank-title review remains identifiable without mutating the persisted title',()=>{
  const ctx=harness(),job=jobs()[0];job.body='\n<합성 첫 줄>\n두 번째 줄';const before=JSON.stringify(job);
  const html=ctx.registrationPreview([job]);assert.match(html,/data-has-issues="false" open/);
  assert.match(html,/review-title">&lt;합성 첫 줄&gt;<small>제목 없음<\/small>/);
  assert.equal(JSON.stringify(job),before);assert.equal(ctx.messageSummary({title:'지정한 제목',body:'본문'}),'지정한 제목');
});

test('Thirty-message review opens only erroneous rows and shows complete error messages',()=>{
  const ctx=harness(),input=jobs(),original=JSON.stringify(input);
  const good=ctx.registrationPreview(input);assert.equal((good.match(/<details /g)||[]).length,30);assert.equal((good.match(/\sopen>/g)||[]).length,0);
  const errors=[{id:'synthetic16',errors:['합성 수신자 연결 필요','합성 예약시각 수정 필요']}];
  const bad=ctx.registrationPreview(input,errors);assert.equal((bad.match(/\sopen>/g)||[]).length,1);
  assert.match(bad,/data-has-issues="true" open/);for(const message of errors[0].errors)assert(bad.includes(message));
  assert.match(ctx.reviewSummary(input,errors),/등록 가능 29건 · 수정 필요 1건/);assert.equal(JSON.stringify(input),original);
});

test('Issue filtering preserves row disclosure and toggles only currently visible details',()=>{
  function row(hasIssues,open){const classes=new Set();return {dataset:{hasIssues:String(hasIssues)},open,listeners:{},addEventListener(type,fn){this.listeners[type]=fn;},classList:{contains:name=>classes.has(name),toggle(name,on){on?classes.add(name):classes.delete(name);}}};}
  const rows=[row(false,false),row(true,true),row(false,true)],elements={};
  for(const id of ['planIssuesOnly','planToggleDetails'])elements[id]={checked:false,textContent:'',attrs:{},listeners:{},setAttribute(name,value){this.attrs[name]=value;},addEventListener(type,fn){this.listeners[type]=fn;}};
  const ctx=harness({document:{querySelectorAll:()=>rows},$:id=>elements[id],api:()=>{throw Error('Filter must not request data');}});
  ctx.bindReviewControls('plan');elements.planIssuesOnly.checked=true;elements.planIssuesOnly.listeners.change();
  assert.deepEqual(rows.map(r=>r.classList.contains('hidden')),[true,false,true]);assert.deepEqual(rows.map(r=>r.open),[false,true,true]);
  elements.planToggleDetails.listeners.click();assert.deepEqual(rows.map(r=>r.open),[false,false,true]);
  elements.planIssuesOnly.checked=false;elements.planIssuesOnly.listeners.change();assert(rows.every(r=>!r.classList.contains('hidden')));
  assert.deepEqual(rows.map(r=>r.open),[false,false,true]);
});

test('Run feedback distinguishes input ownership, stop reasons, and completed requests',()=>{
  const ctx=harness(),base={mode:'register',progress:4,total:30,result_confirmation_mode:'manual'};
  const active=ctx.runDisplay({...base,active:true,stage:'preparing'});assert.equal(active.tone,'busy');assert(active.controlsPC);assert.match(active.title,/현재 5번째/);
  const stopped=ctx.runDisplay({...base,active:false,stage:'stopped',message:'fallback',report:{error:'합성 중단 사유'}});assert.equal(stopped.tone,'warning');assert.equal(stopped.reason,'합성 중단 사유');assert.match(stopped.status,/PC를 사용할/);assert.doesNotMatch(stopped.status,/처리가 끝났습니다/);
  const unknown=ctx.runDisplay({...base,active:false,stage:'failed',report:{error:'합성 등록 여부 불명'}});assert.equal(unknown.reason,'합성 등록 여부 불명');assert.equal(unknown.tone,'warning');
  const done=ctx.runDisplay({...base,active:false,stage:'done',progress:30,report:{error:'이전 오류'}});assert.equal(done.tone,'success');assert.equal(done.reason,'');assert.match(done.title,/등록 요청 완료/);
  const reading=ctx.runDisplay({...base,active:true,mode:'inspect',stage:'reading'});assert.equal(reading.tone,'info');assert.equal(reading.controlsPC,false);
});

function focusHarness(){
  const focused=[];
  const currentRow={dataset:{id:'synthetic-row'},isConnected:true,closest:()=>null,focus:()=>focused.push('current-row')};
  const removedRow={dataset:{id:'synthetic-row'},isConnected:false,focus:()=>focused.push('removed-row')};
  const nodes={modal:{open:false},editor:{open:false},addBtn:{closest:()=>null,focus:()=>focused.push('add-button')}};
  const context={modalReturnFocus:null,editorReturnFocus:null,$:id=>nodes[id],document:{querySelectorAll:selector=>selector==='.row-open'?[currentRow]:[]}};
  vm.createContext(context);vm.runInContext(app.slice(app.indexOf('function restoreModalFocus('),app.indexOf('function showView(')),context);
  return {context,nodes,focused,currentRow,removedRow};
}

test('Review close locates the current row after a poll replaced the original opener',()=>{
  const h=focusHarness();h.context.modalReturnFocus={element:h.removedRow,rowId:'synthetic-row'};
  h.context.restoreModalFocus();assert.deepEqual(h.focused,['current-row']);assert.equal(h.context.modalReturnFocus,null);
});

test('Discard waits for both dialogs to close and returns to the current row or new-message action',()=>{
  const h=focusHarness();h.nodes.modal.open=true;h.context.editorReturnFocus={element:h.removedRow,rowId:'synthetic-row'};
  h.context.restoreEditorFocus();assert.deepEqual(h.focused,[]);assert(h.context.editorReturnFocus);
  h.nodes.modal.open=false;
  h.context.modalReturnFocus={element:{isConnected:true,closest:()=>h.nodes.editor,focus:()=>h.focused.push('closed-editor')}};
  h.context.restoreModalFocus();assert.deepEqual(h.focused,['current-row']);assert.equal(h.context.editorReturnFocus,null);
  h.context.editorReturnFocus={element:h.removedRow,id:'addBtn'};h.context.restoreEditorFocus();assert.deepEqual(h.focused,['current-row','add-button']);
});
