const assert=require('node:assert/strict'),test=require('node:test');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const batch=fs.readFileSync(path.join(__dirname,'../web/batch.js'),'utf8');
const app=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
const escape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
function reviewOf(total=30,errorRows=[]){
 const rows=Array.from({length:total},(_,i)=>({row:i+1,input:{recipient:'합성'+(i+1),date:`2026-11-${String(i+1).padStart(2,'0')}`,time:'09:22',title:'',body:`\n서로 다른 본문 ${i+1}\n둘째 줄`},job:{title:'',body:`\n서로 다른 본문 ${i+1}\n둘째 줄`,scheduled:`2026-11-${String(i+1).padStart(2,'0')} 09:22`},selections:[{name:'합성'+(i+1),selected_id:'id'+i,candidates:[{id:'id'+i,expected:`합성${i+1}(합성 부서)`}]}],issues:errorRows.includes(i+1)?[{message:'수신자를 다시 선택하세요.'}]:[]}));
 return {review_id:'synthetic-review',rows,total,ready:total-errorRows.length,can_commit:!errorRows.length};
}
function harness({offline=false,onRequest}={}){
 const elements=new Map(),requests=[],dialogs=[];let items=[],choices=[];
 function node(id,extra={}){return {id,open:false,hidden:false,checked:false,disabled:false,attrs:{},listeners:{},dataset:{},value:'',textContent:'',setAttribute(name,value){this.attrs[name]=value;},addEventListener(type,fn){this.listeners[type]=fn;},querySelectorAll(selector){return id==='batchReviewRows'&&selector==='details.batch-review-item'?items:id==='modalBody'&&selector==='.batch-choice'?choices:[];},...extra};}
 function $(id){if(!elements.has(id))elements.set(id,node(id));return elements.get(id);}
 const context={$,state:{offline},esc:escape,resultConfirmationMode:()=> 'manual',registrationGuide:()=>'<section id="batch-registration-guide">준비 안내</section>',jobFilter:'all',updateJobFilter(){},refresh:async()=>{},refreshTable(){},closeModal(){},toast(){},focusRunPanel(){},showView(){},selected:new Set(),selectionMode:false,failure:e=>{throw e;},
  modal(title,html,buttons){dialogs.push({title,html,buttons});items=[...html.matchAll(/<details class="batch-review-item"([^>]*)>/g)].map(match=>node('',{open:/\sopen(?:\s|$)/.test(match[1]),dataset:{reviewRow:match[1].match(/data-review-row="(\d+)"/)[1],hasIssues:match[1].match(/data-has-issues="(true|false)"/)[1]}}));choices=[...html.matchAll(/<select class="batch-choice" data-row="([^"]*)" data-name="([^"]*)"/g)].map(match=>node('',{dataset:{row:match[1],name:match[2]}}));},
  api:async(route,payload)=>{requests.push({route,payload:structuredClone(payload)});if(onRequest)return onRequest(route,payload);if(route==='batch/register')return {saved:30,job_ids:Array.from({length:30},(_,i)=>'job'+i),started:true};throw Error('Unexpected synthetic request '+route);},
 };
 vm.createContext(context);vm.runInContext(app.slice(app.indexOf('function displayTitle('),app.indexOf('const locked=')),context);vm.runInContext(batch,context);
 return {context,elements,requests,dialogs,get items(){return items;},get choices(){return choices;}};
}
test('Batch review puts input-validation totals and period before shared guidance and collapsed rows',()=>{
 const h=harness(),review=reviewOf(30,[2,29]),original=JSON.stringify(review);h.context.showBatchPreview(review);const html=h.dialogs[0].html;
 assert.match(html,/등록 가능 28건 · 수정 필요 2건/);assert.match(html,/2026-11-01 ~ 2026-11-30 · 한국시간/);assert(!html.includes('건 확인'));
 assert(html.indexOf('review-summary')<html.indexOf('batch-registration-guide'));assert(html.indexOf('batch-registration-guide')<html.indexOf('batchReviewRows'));
 assert.equal(h.items.length,30);assert.deepEqual(h.items.filter(item=>item.open).map(item=>item.dataset.reviewRow),['2','29']);assert.equal(JSON.stringify(review),original);
 assert(h.dialogs[0].buttons.find(button=>button.text==='30건 한 번에 등록').disabled);
});
test('A single valid row starts open and body summaries retain empty raw titles and escaped content',()=>{
 const h=harness(),review=reviewOf(1);review.rows[0].job.body='\n<합성 본문>\n다음 줄';review.rows[0].selections[0].name='합성 <계정>';h.context.showBatchPreview(review);
 const html=h.dialogs[0].html;assert.equal(h.items[0].open,true);assert.match(html,/batch-review-title">&lt;합성 본문&gt;/);assert.match(html,/합성 &lt;계정&gt;/);assert.match(html,/제목 없음/);assert.equal(review.rows[0].job.title,'');
 assert.equal(h.context.batchReviewPeriod({rows:[{job:null}]}),'예약 기간 확인 필요');
 assert.match(h.context.batchReviewPeriod({rows:[review.rows[0],{job:null}]}),/1건 예약시각 확인 필요/);
});
test('Problem-only and expand controls change presentation without deleting rows or selecting new recipients',()=>{
 const h=harness(),review=reviewOf(30,[4,22]),original=JSON.stringify(review);h.context.showBatchPreview(review);const items=h.items.slice(),filter=h.elements.get('batchIssuesOnly'),toggle=h.elements.get('batchToggleDetails');
 filter.checked=true;filter.listeners.change();assert.deepEqual(items.filter(item=>!item.hidden).map(item=>item.dataset.reviewRow),['4','22']);assert.equal(toggle.textContent,'모두 접기');
 toggle.listeners.click();assert.equal(items[3].open,false);assert.equal(items[21].open,false);assert.equal(toggle.attrs['aria-expanded'],'false');
 toggle.listeners.click();assert.equal(items[3].open,true);assert.equal(items[21].open,true);filter.checked=false;filter.listeners.change();assert.equal(items.filter(item=>!item.hidden).length,30);assert.equal(items[0].open,false);assert.equal(toggle.textContent,'모두 펼치기');
 toggle.listeners.click();assert(items.every(item=>item.open));assert.equal(h.requests.length,0);assert.equal(JSON.stringify(review),original);assert.equal(h.items[0],items[0]);
});
test('Resolving an ambiguous visible recipient rechecks every original row and keeps the original row index',async()=>{
 const review=reviewOf(30,[5]),next=reviewOf(30),h=harness({onRequest:()=>next});review.rows[4].selections[0]={name:'동명 합성',selected_id:'',candidates:[{id:'candidate-a',expected:'동명 합성(부서 가)'},{id:'candidate-b',expected:'동명 합성(부서 나)'}]};
 h.context.showBatchPreview(review);const filter=h.elements.get('batchIssuesOnly');filter.checked=true;filter.listeners.change();const select=h.choices[0];assert.equal(select.dataset.row,'4');select.value='candidate-b';await select.listeners.change();
 assert.equal(h.requests.length,1);assert.equal(h.requests[0].route,'batch/preview');assert.equal(h.requests[0].payload.rows.length,30);assert.equal(h.requests[0].payload.choices['4']['동명 합성'],'candidate-b');assert.equal(h.requests[0].payload.rows[4].title,'');assert.equal(h.dialogs.at(-1).buttons.find(b=>b.text==='30건 한 번에 등록').disabled,false);
});
test('View controls cannot change the submitted review or bypass offline and validation blocks',async()=>{
 const h=harness(),review=reviewOf(30);h.context.showBatchPreview(review);const button=h.dialogs[0].buttons.find(b=>b.text==='30건 한 번에 등록');h.elements.get('batchToggleDetails').listeners.click();await button.action({disabled:false});
 assert.equal(h.requests.length,1);assert.equal(h.requests[0].route,'batch/register');assert.equal(h.requests[0].payload.review_id,review.review_id);assert.equal(h.requests[0].payload.result_confirmation_mode,'manual');assert.equal(h.context.selected.size,30);
 for(const invalid of [harness({offline:true}),harness()]){const r=reviewOf(30);if(!invalid.context.state.offline)r.can_commit=false;invalid.context.showBatchPreview(r);assert.equal(invalid.dialogs[0].buttons.find(b=>b.text==='30건 한 번에 등록').disabled,true);}
});
