'use strict';

let batchRows = [], batchChoices = {}, batchReview = null;
const batchFields = [['recipient','받는 사람'],['date','예약 날짜'],['time','시간'],['title','제목'],['body','내용']];

function batchInput(raw) {
  const schedule = String(raw.scheduled || raw['예약일시'] || '').split(' ');
  const day=String(raw.date ?? raw['예약날짜'] ?? schedule[0] ?? '').split(' ')[0];
  let clock=String(raw.time ?? raw['예약시간'] ?? schedule[1] ?? '');
  if(clock.trim()&&Number.isFinite(Number(clock))&&Number(clock)>=0&&Number(clock)<1){const minutes=Math.round(Number(clock)*1440);clock=`${String(Math.floor(minutes/60)).padStart(2,'0')}:${String(minutes%60).padStart(2,'0')}`;}
  return {recipient:raw.recipient ?? raw['받는사람'] ?? '',date:day,
    time:clock,title:raw.title ?? raw['제목'] ?? '',
    body:raw.body ?? raw['내용'] ?? '',attachments:raw.attachments ?? raw['첨부파일'] ?? ''};
}

function parsePastedTable(text) {
  const rows = []; let row = [], cell = '', quoted = false;
  for (let i=0;i<text.length;i++) {
    const c=text[i];
    if(c==='"' && (quoted || !cell)) {if(quoted&&text[i+1]==='"'){cell+='"';i++;}else quoted=!quoted;}
    else if(!quoted && (c==='\t'||c==='\n'||c==='\r')) {
      row.push(cell);cell='';
      if(c!=='\t'){if(c==='\r'&&text[i+1]==='\n')i++;rows.push(row);row=[];}
    } else cell+=c;
  }
  if(quoted)throw Error('붙여 넣은 내용의 따옴표를 확인하세요.');
  row.push(cell);rows.push(row);
  while(rows.length&&!rows.at(-1).some(value=>value.trim()))rows.pop();
  if(!rows.some(values=>values.some(value=>value.trim())))throw Error('복사한 표를 붙여 넣으세요.');
  if(rows.length>510)throw Error('표는 열 이름을 포함해 510행 이하여야 합니다.');
  return rows;
}

function pastedTableCSV(text){
  return parsePastedTable(text).map(row=>row.map(cell=>'"'+cell.replaceAll('"','""')+'"').join(',')).join('\r\n');
}

function parseBatchPaste(text) {
  const rows=parsePastedTable(text).filter(row=>row.some(value=>value.trim()));
  const canonical={'받는사람':'recipient','예약날짜':'date','예약시간':'time','시간':'time','제목':'title','내용':'body','첨부파일':'attachments','예약일시':'scheduled',recipient:'recipient',date:'date',time:'time',title:'title',body:'body',attachments:'attachments',scheduled:'scheduled'};
  let fields=[...batchFields.map(([key])=>key),'attachments'];
  const header=(rows[0]||[]).map(value=>canonical[value.replace(/\s/g,'')]||'');
  const completeHeader=['recipient','body'].every(key=>header.includes(key))&&(header.includes('scheduled')||['date','time'].every(key=>header.includes(key)));
  if(completeHeader){
    const named=header.filter(Boolean);
    if(new Set(named).size!==named.length)throw Error('붙여 넣은 열 이름이 중복되었습니다.');
    fields=header;rows.shift();
  }
  if(rows.some(values=>values.slice(fields.length).some(value=>value.trim())))throw Error('붙여 넣은 열 개수를 확인하세요. 받는사람·예약날짜·예약시간·제목·내용·첨부파일 순서입니다.');
  if(!rows.length||rows.length>500)throw Error('1~500행을 붙여 넣으세요.');
  return rows.map(values=>batchInput(Object.fromEntries(fields.map((key,i)=>[key,values[i]||'']).filter(([key])=>key))));
}

function takeBatchRows(){
  batchRows=[...document.querySelectorAll('.batch-row')].map(el=>Object.fromEntries([...el.querySelectorAll('[data-field]')].map(input=>[input.dataset.field,input.value])));
}

function renderBatchEditor(){
  $('batchRows').innerHTML=batchRows.map((row,i)=>`<fieldset class="batch-row" data-row="${i}"><legend>${i+1}행</legend><div class="batch-fields">${batchFields.map(([key,label])=>`<label>${label}${key==='title'?' · 선택':''}${key==='recipient'?'<small>쉼표 또는 줄바꿈으로 구분</small>':''}${['recipient','body'].includes(key)?`<textarea data-field="${key}" rows="${key==='body'?3:2}" maxlength="${key==='body'?30000:2000}" aria-label="${i+1}행 ${label}">${esc(row[key]||'')}</textarea>`:`<input data-field="${key}" type="${key==='date'?'date':key==='time'?'time':'text'}" value="${esc(row[key]||'')}" aria-label="${i+1}행 ${label}" ${key==='title'?'maxlength="200"':''}>`}</label>`).join('')}</div><details><summary>첨부파일</summary><label>전체 경로<textarea data-field="attachments" rows="1">${esc(Array.isArray(row.attachments)?row.attachments.join('\n'):row.attachments||'')}</textarea></label></details><button type="button" class="button quiet small batch-remove" data-row="${i}">행 삭제</button></fieldset>`).join('');
  $('batchCount').textContent=`${batchRows.length}행 · 한국시간`;
  $('batchRows').querySelectorAll('.batch-remove').forEach(button=>button.addEventListener('click',()=>{takeBatchRows();batchRows.splice(Number(button.dataset.row),1);renderBatchEditor();}));
}

function openBatchEditor(){
  if(state.runner.active)throw Error('진행 중인 작업이 끝난 뒤 입력하세요.');
  if(!batchRows.length)batchRows=[batchInput({})];
  renderBatchEditor();$('batchError').textContent='';$('batchEditor').showModal();
}

function batchReviewPeriod(review){
  const dates=review.rows.map(row=>String(row.job?.scheduled||'').match(/^(\d{4}-\d{2}-\d{2}) /)?.[1]).filter(Boolean).sort();
  if(!dates.length)return '예약 기간 확인 필요';
  const span=dates[0]===dates.at(-1)?dates[0]:dates[0]+' ~ '+dates.at(-1),missing=review.rows.length-dates.length;
  return span+' · 한국시간'+(missing?` · ${missing}건 예약시각 확인 필요`:'');
}
function batchReviewRowHTML(row,total){
  const input=batchInput(row.input),job=row.job||input,title=String(job.title??''),names=(row.selections||[]).map(selection=>selection.name),recipients=names.length?names.slice(0,2).join(' · ')+(names.length>2?` 외 ${names.length-2}명`:''):String(input.recipient||'수신자 확인 필요');
  const issues=row.issues||[],scheduled=row.job?.scheduled||[input.date,input.time].filter(Boolean).join(' ')||'예약시각 확인 필요';
  return `<details class="batch-review-item" data-review-row="${row.row}" data-has-issues="${issues.length>0}" ${issues.length||total===1?'open':''}><summary><span class="batch-review-meta">${row.row}행 · ${esc(scheduled)} · ${esc(recipients)}</span><span class="batch-review-title">${esc(messageSummary(job))}</span>${!title.trim()?'<small class="hint">제목 없음</small>':''}${issues.length?`<span class="batch-review-issue">수정 필요 ${issues.length}개</span>`:''}</summary><div class="draft-body">${row.selections.map(selection=>`<div class="batch-recipient"><strong>${esc(selection.name)}</strong>${selection.candidates.length>1?`<label>실제 계정 선택<select class="batch-choice" data-row="${row.row-1}" data-name="${esc(selection.name)}"><option value="">소속과 전체 표시를 보고 선택하세요</option>${selection.candidates.map(candidate=>`<option value="${esc(candidate.id)}" ${selection.selected_id===candidate.id?'selected':''}>${esc(candidate.expected||candidate.label)}</option>`).join('')}</select></label>`:selection.selected_id?`<span>${esc(selection.candidates[0]?.expected||'')}</span>`:'<span class="error">계정 연결을 확인하세요.</span>'}</div>`).join('')}<pre>${esc(row.job?.body??input.body)}</pre>${row.job?.attachments?.length?`<p class="hint">첨부: ${row.job.attachments.map(esc).join(' · ')}</p>`:''}${issues.length?`<div class="error-box">${issues.map(issue=>`<p>${esc(issue.message)}</p>`).join('')}</div>`:''}</div></details>`;
}
function wireBatchReviewControls(){
  const items=[...$('batchReviewRows').querySelectorAll('details.batch-review-item')],filter=$('batchIssuesOnly'),toggle=$('batchToggleDetails');
  const visible=()=>items.filter(item=>!item.hidden);
  const sync=()=>{const shown=visible(),expanded=shown.length>0&&shown.every(item=>item.open);toggle.textContent=expanded?'모두 접기':'모두 펼치기';toggle.disabled=!shown.length;toggle.setAttribute('aria-expanded',String(expanded));$('batchReviewEmpty').hidden=shown.length>0;};
  filter.addEventListener('change',()=>{for(const item of items)item.hidden=filter.checked&&item.dataset.hasIssues!=='true';sync();});
  toggle.addEventListener('click',()=>{const shown=visible(),expand=!shown.every(item=>item.open);for(const item of shown)item.open=expand;sync();});
  for(const item of items)item.addEventListener('toggle',sync);
  if(items.length)sync();
}

function showBatchPreview(review){
  batchReview=review;batchRows=review.rows.map(row=>batchInput(row.input));
  const problems=review.total-review.ready;
  const confirmationMode=resultConfirmationMode();
  const allOpen=review.rows.every(row=>review.total===1||row.issues.length>0);
  const body=`<section class="review-summary" aria-label="예약 검토 요약"><strong>등록 가능 ${review.ready}건 · 수정 필요 ${problems}건</strong><p>${esc(batchReviewPeriod(review))}</p><p class="hint">입력값과 계정 연결을 검사한 결과입니다. 수신자·시각·내용을 확인하세요.</p></section>${state.offline?'<p class="notice">모의 실행 모드입니다. 실제 쿨메신저에 등록하지 않습니다.</p>':''}${registrationGuide(review.rows.map(row=>row.job||{}),{result_confirmation_mode:confirmationMode},'batch-registration-guide')}<div class="review-controls"><label><input id="batchIssuesOnly" type="checkbox" ${problems?'':'disabled'}> 수정 필요한 항목만</label><button id="batchToggleDetails" type="button" class="button quiet small" aria-controls="batchReviewRows" aria-expanded="${allOpen}">${allOpen?'모두 접기':'모두 펼치기'}</button></div><div id="batchReviewRows" class="batch-preview">${review.rows.map(row=>batchReviewRowHTML(row,review.total)).join('')}</div><p id="batchReviewEmpty" class="hint" hidden>수정이 필요한 항목이 없습니다.</p>`;
  modal('전체 미리보기',body,[{text:'입력 수정',action:()=>{closeModal();openBatchEditor();}},
    {text:'초안만 저장',disabled:!review.can_commit,action:async button=>{button.disabled=true;try{const result=await api('batch/commit',{review_id:review.review_id});jobFilter='all';updateJobFilter();closeModal();await refresh();selected=new Set(result.job_ids);selectionMode=true;refreshTable();toast(`${result.saved}건을 초안으로 저장했습니다.`);batchRows=[];batchChoices={};}catch(error){button.disabled=false;throw error;}}},
    {text:`${review.total}건 한 번에 등록`,describedBy:'batch-registration-guide',primary:true,disabled:!review.can_commit||state.offline,action:async button=>{button.disabled=true;try{const result=await api('batch/register',{review_id:review.review_id,phrase:'예약 등록',result_confirmation_mode:confirmationMode});jobFilter='all';updateJobFilter();closeModal();await refresh();selected=new Set(result.job_ids);selectionMode=true;refreshTable();batchRows=[];batchChoices={};if(result.started){toast(`${result.saved}건의 예약 등록을 시작했습니다. ${confirmationMode==='manual'?'끝날 때까지 PC 사용을 잠시 멈춰 주세요.':'등록 결과를 건별로 자동 대조합니다.'}`);focusRunPanel();}else modal('초안 저장 · 등록 전 확인 필요',`<p>초안 ${result.saved}건을 저장했습니다. 아래 항목을 해결한 뒤 목록에서 다시 미리보기 하세요.</p><div class="error-box">${result.plan_errors.map(item=>item.errors.map(message=>`<p>${esc(message)}</p>`).join('')).join('')}</div>`,[{text:'닫기',action:closeModal},{text:'쿨메신저 연결',action:()=>{closeModal();showView('settings');}}]);}catch(error){button.disabled=false;throw error;}}}]);
  wireBatchReviewControls();
  $('modalBody').querySelectorAll('.batch-choice').forEach(select=>select.addEventListener('change',async()=>{
    const row=select.dataset.row;batchChoices[row]=batchChoices[row]||{};
    if(select.value)batchChoices[row][select.dataset.name]=select.value;else delete batchChoices[row][select.dataset.name];
    try{const next=await api('batch/preview',{rows:batchRows,choices:batchChoices});closeModal();showBatchPreview(next);}catch(error){failure(error);}
  }));
}

$('batchRows').addEventListener('input',event=>{const row=event.target.closest('.batch-row'),field=event.target.dataset.field;if(row&&field&&batchRows[Number(row.dataset.row)])batchRows[Number(row.dataset.row)][field]=event.target.value;});
$('batchBtn').addEventListener('click',()=>{try{openBatchEditor();}catch(error){failure(error);}});
$('batchAddRow').addEventListener('click',()=>{takeBatchRows();if(batchRows.length>=500)return;$('batchError').textContent='';batchRows.push(batchInput({}));renderBatchEditor();});
$('batchPasteApply').addEventListener('click',()=>{try{batchRows=parseBatchPaste($('batchPaste').value);batchChoices={};renderBatchEditor();$('batchPaste').value='';$('batchError').textContent='';}catch(error){$('batchError').textContent=error.message;}});
$('batchForm').addEventListener('submit',async event=>{event.preventDefault();takeBatchRows();const button=$('batchPreviewBtn');button.disabled=true;try{batchChoices={};const review=await api('batch/preview',{rows:batchRows});$('batchEditor').close();showBatchPreview(review);}catch(error){$('batchError').textContent=error.message;}finally{button.disabled=false;}});
$('diagnosticExport').addEventListener('click',()=>download('diagnostics','쿨예약실_진단정보.json').catch(failure));
$('configExport').addEventListener('click',()=>download('config-template','쿨예약실_연결양식.json').catch(failure));

if(typeof module!=='undefined')module.exports={parseBatchPaste,batchInput,parsePastedTable,pastedTableCSV};
