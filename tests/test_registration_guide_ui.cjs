const assert=require('node:assert/strict'),test=require('node:test'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
function harness(){const calls=[];const context={state:{result_confirmation_mode:'manual',jobs:[]},locked:new Set(['submitting','needs_review','confirmed','uncertain']),esc:x=>String(x??''),$:()=>({focus:options=>calls.push(['focus',options]),scrollIntoView:options=>calls.push(['scroll',options])})};vm.createContext(context);vm.runInContext(source.slice(source.indexOf('function resultConfirmationMode('),source.indexOf('function inputReadiness(')),context);vm.runInContext(source.slice(source.indexOf('function workflowPendingJobs('),source.indexOf('function wfReviewHTML(')),context);return {context,calls};}

test('Registration guidance uses one concise checklist without another confirmation gate',()=>{const h=harness(),html=h.context.registrationGuide(Array.from({length:15},()=>({status:'draft'})),{result_confirmation_mode:'manual'},'fixture-guide');assert.equal((html.match(/<li>/g)||[]).length,3);for(const phrase of ['15건','로그인','조직도','표시 필터','따로 보관','마우스·키보드','화면 잠금·절전','F8','Windows PC','도착 여부는 조회하지 않습니다'])assert(html.includes(phrase),phrase);assert(!/checkbox|<button/.test(html));assert.match(html,/id="fixture-guide"/);});

test('Prepared single message keeps its verified compose window while a new batch asks to close drafts',()=>{const h=harness();assert.match(h.context.registrationGuide([{status:'prepared'}]),/이 작성창 하나만 남기세요/);assert.match(h.context.registrationGuide([{status:'draft'}]),/필요한 작성창은 프로그램이 엽니다/);});

test('Progress distinguishes current work, successful request completion, and partial stop',()=>{const h=harness();const active=h.context.runDisplay({active:true,mode:'register',progress:4,total:15,stage:'preparing'});assert.match(active.title,/4\/15건 처리 완료 · 현재 5번째/);assert.match(active.status,/지금은 PC 사용을 멈춰/);assert(active.controlsPC);const done=h.context.runDisplay({active:false,mode:'register',progress:15,total:15,stage:'done',elapsed_ms:34560,result_confirmation_mode:'manual'});assert.match(done.title,/등록 요청 완료/);assert.match(done.title,/34.6초/);assert.match(done.status,/이제 PC를 사용할/);assert(!done.controlsPC);const stopped=h.context.runDisplay({active:false,mode:'register',progress:4,total:15,stage:'failed'});assert.match(stopped.title,/4\/15/);assert.match(stopped.status,/작업이 멈췄습니다/);assert.doesNotMatch(stopped.status,/처리가 끝났습니다/);});

test('Workflow count excludes previously submitted and running messages before presenting register label',()=>{const h=harness();h.context.state.jobs=[{id:'old',status:'needs_review'},{id:'busy',status:'preparing'}];const jobs=h.context.workflowPendingJobs({candidates:[{job:{id:'new',status:'draft'}},{job:{id:'old',status:'draft'}},{job:{id:'busy',status:'draft'}},{job:{id:'confirmed',status:'confirmed'}}]});assert.deepEqual([...jobs].map(j=>j.id),['new']);});

test('Starting processing focuses the labelled progress panel before revealing it',()=>{const h=harness();h.context.focusRunPanel();assert.deepEqual(h.calls.map(item=>item[0]),['focus','scroll']);assert.equal(h.calls[0][1].preventScroll,true);});

test('A stopped run keeps its error visible with warning styling and does not appear successful',()=>{
  const h=harness(),run={active:false,mode:'register',progress:4,total:30,stage:'failed',message:'진단 상세',report:{error:'합성 작성창 응답을 읽을 수 없습니다.'}};
  const stopped=h.context.runDisplay(run);assert.equal(stopped.tone,'warning');assert.equal(stopped.reason,run.report.error);assert(stopped.stopped);assert(!stopped.controlsPC);
  const done=h.context.runDisplay({...run,stage:'done',report:{passed:true}});assert.equal(done.tone,'success');assert.equal(done.reason,'');assert(!done.stopped);
  assert.equal(h.context.runDisplay({...run,active:true,stage:'preparing'}).tone,'busy');
});
