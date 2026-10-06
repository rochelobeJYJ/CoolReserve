const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const app=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const begin=app.indexOf('function detectWorkflowHeaderRow('),end=app.indexOf('function workflowMonthRange(',begin);
const lunch={id:'lunch',preset:'lunch',header_rows:2,columns:{date:'날짜',first:'최종_중식1',second:'최종_중식2',exclude:'제외일',first_only:'고사(1차만)'}};
const header=['날짜','최종_중식1','최종_중식2','제외일','고사(1차만)'];
function harness(values,kind='google_sheets'){
 const elements=new Map(),requests=[],handlers={};
 const context={Blob,structuredClone,view:'workflow',wf:{recipe:structuredClone(lunch),presets:[{id:'lunch',recipe:structuredClone(lunch)}],source:{kind},snapshot:{values},snapshotId:'old',preview:{approved:true},connection:{}},
 $:id=>{if(!elements.has(id))elements.set(id,{value:'',files:[{size:1,name:'synthetic.xlsx'}]});return elements.get(id);},
 api:async(route,payload)=>{requests.push({route,payload});return {snapshot_id:'synthetic',snapshot:{values,source:{kind}}};},
 fileBase64:async()=>'',pastedTableCSV:()=>'',toast(){},renderWorkflow(){},
 document:{addEventListener:(event,fn)=>{handlers[event]=fn;},querySelectorAll:()=>[]},
 };
 vm.createContext(context);vm.runInContext(app.slice(begin,end),context);
 const eventStart=app.indexOf("document.addEventListener('change',event=>{const el=event.target;if(view!=='workflow'");
 const eventEnd=app.indexOf("document.addEventListener('input'",eventStart);
 vm.runInContext(app.slice(eventStart,eventEnd),context);
 return {context,requests,handlers};
}

test('Lunch template header in row one is detected on Google, Excel, CSV, and pasted sources',async()=>{
 for(const kind of ['google_sheets','xlsx','csv','paste']){
  const h=harness([header,['2026-11-03','합성가','합성나',false,false]],kind);
  await h.context.readWorkflowSource();
  assert.equal(h.context.wf.recipe.header_rows,1,kind);
  assert.match(h.context.wf.headerNotice,/1행/);
  assert.equal(h.requests.length,1);
 }
});

test('Existing lunch metadata row and header in row two remain intact',async()=>{
 const values=[['연도',2026,'월',11],header,['3','합성가','합성나',false,false]];
 const h=harness(values);await h.context.readWorkflowSource();
 assert.equal(h.context.wf.recipe.header_rows,2);
 assert.deepEqual(h.context.wf.snapshot.values,values);
 assert.match(h.context.wf.headerNotice,/2행/);
});

test('Only a unique exact full header within the first ten rows is accepted',()=>{
 const h=harness([]),detect=h.context.detectWorkflowHeaderRow;
 assert.equal(detect([['자료제목'],['날짜','최종_중식1','최종_중식2'],header],lunch),3);
 assert.equal(detect([header,header],lunch),0);
 assert.equal(detect([['날짜','최종_중식 1','최종_중식2','제외일','고사(1차만)']],lunch),0);
 assert.equal(detect([Array(5).fill('날짜'),header.concat('날짜')],lunch),0);
 assert.equal(detect([...Array(9).fill(['제목']),header],lunch),10);
 assert.equal(detect([...Array(10).fill(['제목']),header],lunch),0);
});

test('Uncertain detection leaves a manually chosen header row unchanged',()=>{
 const h=harness([header,header]);h.context.wf.recipe.header_rows=5;
 assert.equal(h.context.applyWorkflowHeaderRow(),0);
 assert.equal(h.context.wf.recipe.header_rows,5);
 assert.equal(h.context.wf.headerNotice,'');
});

test('Changing to lunch after reading a source also detects its header before preview',()=>{
 const h=harness([header,['2026-11-03','합성가','합성나',false,false]]);
 h.context.wf.recipe={preset:'general',header_rows:1,columns:{date:'업무일',recipient:'담당자'}};
 h.handlers.change({target:{id:'wfPreset',value:'lunch'}});
 assert.equal(h.context.wf.recipe.header_rows,1);
 assert.equal(h.context.wf.preview,null);
 assert.equal(h.context.wf.dirty,true);
});

test('General workflow detection uses configured exact column names without guessing similar headers',()=>{
 const h=harness([]),recipe={preset:'general',header_rows:1,columns:{date:'업무 날짜',recipient:'명단',title:'제목',body:'안내 내용',id:'',exclude:''}};
 assert.equal(h.context.detectWorkflowHeaderRow([['회사업무'],['업무 날짜','명단','제목','안내 내용']],recipe),2);
 assert.equal(h.context.detectWorkflowHeaderRow([['업무날짜','명단','제목','안내 내용']],recipe),0);
 assert.equal(h.context.detectWorkflowHeaderRow([['자료 제목'],['업무 날짜','명단','안내 내용']],recipe),2);
});
