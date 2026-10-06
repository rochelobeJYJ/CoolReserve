const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const app=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const html=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
function section(start,end){const first=app.indexOf(start),last=app.indexOf(end,first);assert(first>=0&&last>first);return app.slice(first,last);}
function harness(extra={}){
 const ctx={state:{offline:false,connection_mode:'manual',jobs:[]},profile:{contacts:{},roles:{}},initializationBusy:false,initializationError:'',updateRequired:()=>false,locked:new Set(),esc:String,structuredClone,...extra};
 vm.createContext(ctx);vm.runInContext(section('function displayTitle(','const locked='),ctx);vm.runInContext(section('function inputReadiness(','function canOfferInputTrial('),ctx);return ctx;
}
test('Fresh account shows preparation and one saved connection action without an automatic live read',()=>{
 const ctx=harness();ctx.state.needs_organization_refresh=true;
 const ready=ctx.inputReadiness();assert(ready.show&&ready.firstUse);assert.equal(ready.action,'조직도 계정 불러오기');assert.match(ready.status,/로그인.*조직도/);assert.equal(ctx.shouldInitializeOnBoot(),false);
 assert.match(html,/id="setupSteps"[^>]*>[\s\S]*쿨메신저에 로그인[\s\S]*조직도 창 열기/);
});
test('Saved automatic connection remains ready and only refreshes an outdated inventory',()=>{
 const ctx=harness();ctx.profile.contacts={synthetic:{expected:'합성 계정'}};ctx.state.connection_mode='automatic';ctx.state.needs_organization_refresh=false;
 assert.equal(ctx.inputReadiness().show,false);assert.equal(ctx.shouldInitializeOnBoot(),false);
 ctx.state.needs_organization_refresh=true;assert.equal(ctx.shouldInitializeOnBoot(),true);
 ctx.state.offline=true;assert.equal(ctx.shouldInitializeOnBoot(),false);assert.match(ctx.inputReadiness().status,/비활성화/);
});
test('Fresh offline mode explains setup without claiming to have loaded any accounts',()=>{
 const ctx=harness();ctx.state.offline=true;const ready=ctx.inputReadiness();assert(ready.firstUse);assert.match(ready.hint,/모의 실행 모드/);assert.equal(ctx.shouldInitializeOnBoot(),false);
});
test('Empty title receives a display-only label while a real title remains unchanged',()=>{
 const ctx=harness(),job={title:'',body:'합성 본문'};assert.equal(ctx.displayTitle(job.title),'제목 없음');assert.equal(job.title,'');assert.equal(ctx.displayTitle('  합성 제목  '),'  합성 제목  ');
 const input=html.match(/<input id="title"[^>]*>/)[0];assert(!/required/.test(input));assert.match(html,/제목 · 선택<input id="title"/);
});

test('Untitled messages have distinct body summaries without adding a real title',()=>{
 const ctx=harness(),job={title:'',body:'\n  합성 첫 안내  \n다음 줄'};assert.equal(ctx.messageSummary(job),'합성 첫 안내');assert.equal(job.title,'');assert.equal(job.body,'\n  합성 첫 안내  \n다음 줄');
 assert.equal(ctx.messageSummary({title:'합성 제목',body:'다른 본문'}),'합성 제목');assert.equal(ctx.messageSummary({title:'',body:'A'.repeat(110)}).length,90);
});
test('General is the first selected recipe while saved lunch time and mappings survive selecting its preset',async()=>{
 const general={id:'general',preset:'general',name:'일반 안내',contact_mapping:{}};
 const lunch={id:'lunch',preset:'lunch',name:'중식 지도',send:{time:'09:22'},contact_mapping:{합성가:'synthetic'}};
 const ctx=harness({wf:{from:'2026-11-01',to:'2026-11-30',presets:[],source:{kind:'xlsx'}},$:()=>({innerHTML:''}),renderWorkflow(){},api:async route=>route==='workflows/list'?{recipes:[lunch,general]}:route==='workflows/presets'?[]:{connected:false}});
 vm.runInContext(section('async function openWorkflow(','function wfSourceLabel('),ctx);vm.runInContext(section('function workflowPresetOptions(','function wfDataHTML('),ctx);
 await ctx.openWorkflow();assert.equal(ctx.wf.recipe.id,'general');assert.equal(ctx.wf.source.kind,'xlsx');const select=ctx.workflowPresetOptions();assert.match(select,/<optgroup label="일반 메시지">/);assert.match(select,/<optgroup label="선택 프리셋">/);assert.match(select,/<option value="general" selected>/);assert.match(select,/<option value="lunch" >/);
 const saved=ctx.wf.presets.find(item=>item.id==='lunch').recipe;assert.equal(saved.send.time,'09:22');assert.equal(saved.contact_mapping.합성가,'synthetic');
});
test('Lunch template download is explicit and generic data rendering does not expose its action',()=>{
 assert.match(app,/wf\.recipe\?\.preset==='lunch'\?'<button[^>]*data-wf="lunchTemplate"/);
 assert.match(app,/action==='lunchTemplate'\)\{await download\('template\?preset=lunch','중식지도_프리셋.xlsx'\)/);
});
