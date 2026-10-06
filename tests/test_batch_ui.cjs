const assert=require('node:assert/strict');
const test=require('node:test');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const context={module:{exports:{}},$:()=>({addEventListener(){}})};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/batch.js'),'utf8'),context);
const {parseBatchPaste,batchInput}=context.module.exports;
test('Named pasted tables may omit title and retain multiline content with one or multiple recipients',()=>{
  for(const header of ['받는사람\t예약날짜\t예약시간\t내용','recipient\tdate\ttime\tbody']){
    const rows=parseBatchPaste(header+'\n"합성가, 합성나"\t2026-11-20\t09:22\t"첫 줄\n둘째 줄"');
    assert.equal(rows.length,1);assert.equal(rows[0].title,'');assert.equal(rows[0].body,'첫 줄\n둘째 줄');assert.equal(rows[0].recipient,'합성가, 합성나');
  }
  const combined=parseBatchPaste('받는사람\t예약일시\t내용\n합성가\t2026-11-20 09:22\t합성 본문')[0];
  assert.equal(combined.title,'');assert.equal(combined.time,'09:22');
});
test('Excel quoted multiline content stays in one message row',()=>{
  const rows=parseBatchPaste('받는사람\t예약날짜\t예약시간\t제목\t내용\r\n"합성가, 합성나"\t2026-10-20\t15:30\t합성제목\t"첫 줄\n\n둘째 줄"');
  assert.equal(rows.length,1);assert.equal(rows[0].recipient,'합성가, 합성나');assert.equal(rows[0].body,'첫 줄\n\n둘째 줄');
});
test('Unclosed Excel quote is rejected instead of dropping rows',()=>{
  assert.throws(()=>parseBatchPaste('합성가\t2026-10-20\t15:30\t합성제목\t"누락된 따옴표'));
});
test('Imported Excel date and time survive returning to the editor',()=>{
  const row=batchInput({'예약날짜':'2026-10-20 00:00','예약시간':'0.5','받는사람':'합성가'});
  assert.equal(row.date,'2026-10-20');assert.equal(row.time,'12:00');
});

test('A first message containing a header word is kept as message data',()=>{
  const rows=parseBatchPaste('합성가\t2026-11-20\t15:30\t제목\t첫 메시지 본문\n합성나\t2026-11-21\t15:30\t둘째\t둘째 본문');
  assert.equal(rows.length,2);assert.equal(rows[0].title,'제목');assert.equal(rows[0].recipient,'합성가');
});

test('Headerless sixth attachment column and combined schedule headers are retained',()=>{
  const plain=parseBatchPaste('합성가\t2026-11-20\t15:30\t합성제목\t합성본문\tC:\\Synthetic\\sample.pdf');
  assert.equal(plain[0].attachments,'C:\\Synthetic\\sample.pdf');
  const headed=parseBatchPaste('받는사람\t예약일시\t제목\t내용\n합성가\t2026-11-20 15:30\t합성제목\t합성본문');
  assert.equal(headed[0].date,'2026-11-20');assert.equal(headed[0].time,'15:30');
});

test('Duplicate headers and unexpected extra data are rejected instead of discarded',()=>{
  assert.throws(()=>parseBatchPaste('받는사람\t예약날짜\t예약시간\t제목\t내용\t내용\n합성가\t2026-11-20\t15:30\t제목\t본문\t잘못된추가본문'),/중복/);
  assert.throws(()=>parseBatchPaste('합성가\t2026-11-20\t15:30\t제목\t본문\t파일\t누락될데이터'),/열 개수/);
});
