"""Deterministic tests. Windows UI calls are replaced by a recording test driver.
Run from the project folder: python -m unittest discover -s tests -v
"""
import base64
import contextlib
import copy
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from http.server import ThreadingHTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core import Store, clean_job, validate_job, now, add_months, expand_repeat, signature, export_csv, file_info
from app.importers import import_file, read_xlsx
from app.engine import Runner
from app.windows import Stopped, profile_errors
from server import Service, make_handler


def job(**changes):
    d={'scheduled':(now()+timedelta(days=1)).strftime('%Y-%m-%d %H:%M'), 'recipient':'본인 테스트',
       'title':'테스트', 'body':'첫째 줄\n둘째 줄', 'attachments':[]}
    d.update(changes)
    return clean_job(d)


def profile():
    sel={'exe':'C:\\CoolMessenger\\CoolMessenger.exe'}
    return {'tested':True, 'roles':{k:dict(sel) for k in ('title','body','recipient_read','scheduled_check','send','datetime')},
            'contacts':{'본인 테스트':{'expected':'본인 이름', 'selector':dict(sel)}}}


def minimal_xlsx(cell, date1904=False):
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr('xl/workbook.xml',f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><workbookPr date1904="{int(date1904)}"/><sheets><sheet name="예약목록" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1">'+cell+'</row></sheetData></worksheet>')
    return buf.getvalue()

class CoreTests(unittest.TestCase):
    def test_leap_month(self):
        self.assertEqual(add_months(datetime(2024,1,31),1), datetime(2024,2,29))
    def test_nonleap_month(self):
        self.assertEqual(add_months(datetime(2026,1,31),1), datetime(2026,2,28))
    def test_too_soon(self):
        self.assertTrue(validate_job(job(scheduled=now().strftime('%Y-%m-%d %H:%M'))))
    def test_too_far(self):
        self.assertTrue(validate_job(job(scheduled=(now()+timedelta(days=120)).strftime('%Y-%m-%d %H:%M'))))
    def test_valid(self):
        self.assertEqual(validate_job(job()),[])
    def test_missing_recipient(self):
        self.assertTrue(validate_job(job(recipient='')))
    def test_missing_body(self):
        self.assertTrue(validate_job(job(body='')))
    def test_missing_attachment(self):
        self.assertTrue(validate_job(job(attachments=['/not-a-real-file-58227'])) )
    def test_repeat_exclusion(self):
        base=job(scheduled='2026-10-05 08:20')
        result=expand_repeat(base,'2026-10-19',[0],['2026-10-12'],datetime(2026,9,29))
        self.assertEqual([j['scheduled'] for j in result],['2026-10-05 08:20','2026-10-19 08:20'])
    def test_repeat_no_days(self):
        with self.assertRaises(ValueError): expand_repeat(job(),'2026-10-10',[],[])
    def test_repeat_reversed(self):
        with self.assertRaises(ValueError): expand_repeat(job(scheduled='2026-10-05 08:20'),'2026-10-01',[0],[])
    def test_body_newlines(self):
        self.assertEqual(job(body='a\r\nb\rc')['body'],'a\nb\nc')
    def test_signature_ignores_id(self):
        self.assertEqual(signature(job()),signature(job()))
    def test_signature_changes_with_time(self):
        self.assertNotEqual(signature(job()),signature(job(scheduled='2026-10-20 10:30')))
    def test_file_change_detected(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'f.txt'; p.write_text('one'); a=file_info(str(p));p.write_text('two')
            self.assertNotEqual(a['sha256'],file_info(str(p))['sha256'])

class ImportTests(unittest.TestCase):
    def test_csv_bom_multiline(self):
        original=job(); jobs,errors=import_file('x.csv',export_csv([original]))
        self.assertEqual(errors,[]);self.assertEqual(jobs[0]['body'],original['body'])
    def test_csv_cp949(self):
        raw='예약일시,받는사람,제목,내용\n2026-10-01 09:00,본인,제목,내용'.encode('cp949')
        self.assertEqual(import_file('x.csv',raw)[0][0]['recipient'],'본인')
    def test_csv_injection_export(self):
        output=export_csv([job(title='=2+2',body='@SUM(A1)')]).decode('utf-8-sig')
        self.assertIn("'=2+2",output);self.assertIn("'@SUM(A1)",output)
    def test_unknown_extension(self):
        with self.assertRaises(ValueError): import_file('x.xls',b'')
    def test_missing_header(self):
        with self.assertRaises(ValueError): import_file('x.csv',b'a,b\n1,2')
    def test_duplicate_header(self):
        with self.assertRaises(ValueError): import_file('x.csv','예약일시,받는사람,제목,내용,내용'.encode())
    def test_wrong_date_partial(self):
        rows,errors=import_file('x.csv','예약일시,받는사람,제목,내용\n잘못된날짜,나,제목,본문'.encode())
        self.assertEqual(len(rows),0);self.assertEqual(len(errors),1)
    def test_xlsx_formula_rejected(self):
        with self.assertRaises(ValueError): read_xlsx(minimal_xlsx('<c r="A1"><f>1+1</f><v>2</v></c>'))
    def test_xlsx_serial_minute_rounding(self):
        rows=read_xlsx(minimal_xlsx('<c r="A1"><v>46296.34722222222</v></c>'))
        self.assertEqual(rows[0][0],'2026-10-01 08:20')
    def test_xlsx_1904(self):
        rows=read_xlsx(minimal_xlsx('<c r="A1"><v>44834.34722222222</v></c>',True))
        self.assertEqual(rows[0][0],'2026-10-01 08:20')
    def test_template(self):
        p=Path(__file__).resolve().parents[1]/'templates/reservation_template.xlsx'
        rows,errors=import_file(p.name,p.read_bytes())
        self.assertEqual(errors,[]);self.assertEqual(rows,[])
        self.assertEqual(read_xlsx(p.read_bytes())[0], ['받는사람','예약날짜','예약시간','제목','내용','첨부파일'])
    def test_xlsx_entities_rejected(self):
        raw=minimal_xlsx('<c r="A1" t="inlineStr"><is><t>ok</t></is></c>')
        inp=zipfile.ZipFile(io.BytesIO(raw)); out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            for name in inp.namelist():
                data=inp.read(name)
                if name.endswith('sheet1.xml'):data=b'<!DOCTYPE a [<!ENTITY x "boom">]>'+data
                z.writestr(name,data)
        with self.assertRaises(ValueError):read_xlsx(out.getvalue())

class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name);self.store=Store(self.path)
    def tearDown(self):
        self.store.close();self.tmp.cleanup()

class StoreTests(StoreFixture):
    def test_roundtrip(self):
        j=self.store.save(job());self.assertEqual(self.store.get(j['id'])['body'],j['body'])
    def test_duplicate_rejected(self):
        self.store.save(job())
        with self.assertRaises(ValueError):self.store.save(job())
    def test_edit(self):
        j=self.store.save(job()); j['title']='수정'; self.store.save(j)
        self.assertEqual(self.store.get(j['id'])['title'],'수정')
    def test_attempt_blocks_second_submit(self):
        j=self.store.save(job());self.store.begin_submit(j)
        with self.assertRaises(ValueError):self.store.begin_submit(j)
    def test_attempt_blocks_edit(self):
        j=self.store.save(job());self.store.begin_submit(j);j['title']='변경'
        with self.assertRaises(ValueError):self.store.save(j)
    def test_atomic_delete(self):
        a=self.store.save(job(title='a'));b=self.store.save(job(title='b'));self.store.begin_submit(b)
        with self.assertRaises(ValueError):self.store.remove([a['id'],b['id']])
        self.assertEqual(len(self.store.list_jobs()),2)
    def test_crash_recovery(self):
        a=self.store.save(job(title='a'));b=self.store.save(job(title='b'))
        self.store.begin_submit(a);self.store.status(b['id'],'preparing')
        self.store.close();self.store=Store(self.path)
        self.assertEqual(self.store.get(a['id'])['status'],'uncertain')
        self.assertEqual(self.store.get(b['id'])['status'],'failed')
    def test_review_requires_note(self):
        j=self.store.save(job());self.store.begin_submit(j);self.store.finish_submit(j,'uncertain','테스트')
        with self.assertRaises(ValueError):self.store.review(j['id'],'')
        self.store.review(j['id'],'본인 2026-10-01 08:20 확인')
        self.assertEqual(self.store.get(j['id'])['status'],'confirmed')

class FakeDriver:
    def __init__(self, rec, stop, fail='', checked=False): self.rec=rec;self.stop=stop;self.fail=fail;self.checked=checked
    def op(self,name):
        self.rec.append(name)
        if self.fail==name:raise RuntimeError('의도된 테스트 오류: '+name)
    def checkpoint(self):
        if self.stop.is_set():raise Stopped('테스트 중지')
    def prepare(self,j):self.op('prepare')
    def verify(self,j):self.op('verify')
    def submit(self,j):self.op('submit')
    def verify_result(self,j):self.op('result');return self.checked,'모의 결과'

class RunnerTests(StoreFixture):
    def setup_runner(self,fail='',checked=False):
        local=profile()
        from app.verification import RESULT_ROLES
        for role in RESULT_ROLES:local['roles'][role]=dict(local['roles']['title'])
        self.store.set_setting('profile',local);self.calls=[]
        def factory(p,s):
            self.calls.append('factory');return FakeDriver(self.calls,s,fail,checked)
        self.runner=Runner(self.store,factory,contextlib.nullcontext)
        return self.store.save(job())
    def run_mode(self,j,mode,phrase='',confirmation='manual'):
        plan=self.runner.plan([j['id']],mode,confirmation)
        self.assertEqual(plan['errors'],[])
        self.runner.start(plan['id'],phrase);self.runner.thread.join(5)
        self.assertFalse(self.runner.busy())
    def test_simulation_never_constructs_driver(self):
        j=self.setup_runner();self.run_mode(j,'simulate')
        self.assertEqual(self.calls,[]);self.assertEqual(self.store.get(j['id'])['status'],'draft')
    def test_prepare_never_submits(self):
        j=self.setup_runner();self.run_mode(j,'prepare')
        self.assertEqual(self.calls,['factory','prepare']);self.assertFalse(self.store.has_attempt(signature(j)))
        self.assertEqual(self.store.get(j['id'])['status'],'prepared')
    def test_live_requires_phrase(self):
        j=self.setup_runner();plan=self.runner.plan([j['id']],'register')
        with self.assertRaises(ValueError):self.runner.start(plan['id'],'yes')
        self.assertEqual(self.calls,[])
    def test_live_requires_input_test(self):
        j=self.setup_runner();p=profile();p['tested']=False;self.store.set_setting('profile',p)
        self.assertTrue(self.runner.plan([j['id']],'register')['errors'])
    def test_submit_unknown_is_locked(self):
        j=self.setup_runner(fail='submit');self.run_mode(j,'register','예약 등록')
        self.assertEqual(self.store.get(j['id'])['status'],'uncertain')
        self.assertTrue(self.runner.plan([j['id']],'register')['errors'])
        self.assertEqual(self.calls.count('submit'),1)
    def test_prefill_error_never_attempts(self):
        j=self.setup_runner(fail='prepare');self.run_mode(j,'register','예약 등록')
        self.assertFalse(self.store.has_attempt(signature(j)));self.assertNotIn('submit',self.calls)
    def test_readback_error_never_attempts(self):
        j=self.setup_runner(fail='verify');self.run_mode(j,'register','예약 등록')
        self.assertFalse(self.store.has_attempt(signature(j)));self.assertNotIn('submit',self.calls)
    def test_unverified_result_needs_review(self):
        j=self.setup_runner();self.run_mode(j,'register','예약 등록','automatic')
        self.assertEqual(self.store.get(j['id'])['status'],'needs_review')
    def test_verified_result(self):
        j=self.setup_runner(checked=True);self.run_mode(j,'register','예약 등록','automatic')
        self.assertEqual(self.store.get(j['id'])['status'],'confirmed')
    def test_result_error_is_uncertain(self):
        j=self.setup_runner(fail='result');self.run_mode(j,'register','예약 등록','automatic')
        self.assertEqual(self.store.get(j['id'])['status'],'uncertain')
    def test_edited_plan_is_rejected(self):
        j=self.setup_runner();plan=self.runner.plan([j['id']],'register');j['title']='changed';self.store.save(j)
        with self.assertRaises(ValueError):self.runner.start(plan['id'],'예약 등록')
    def test_profile_change_rejected(self):
        j=self.setup_runner();plan=self.runner.plan([j['id']],'register');p=profile();p['tested']=False;self.store.set_setting('profile',p)
        with self.assertRaises(ValueError):self.runner.start(plan['id'],'예약 등록')
    def test_expired_plan_rejected(self):
        j=self.setup_runner();plan=self.runner.plan([j['id']],'simulate');self.runner.plans[plan['id']]['at']-=301
        with self.assertRaises(ValueError):self.runner.start(plan['id'],'')
    def test_duplicate_ids_rejected(self):
        j=self.setup_runner()
        with self.assertRaises(ValueError):self.runner.plan([j['id'],j['id']],'simulate')
    def test_attachment_change_after_review(self):
        j=self.setup_runner();p=self.path/'a.txt';p.write_text('before');j['attachments']=[str(p)];self.store.save(j)
        plan=self.runner.plan([j['id']],'simulate');p.write_text('after')
        self.runner.start(plan['id'],'');self.runner.thread.join(5)
        self.assertIn('바뀌',self.runner.snapshot()['message']);self.assertEqual(self.calls,[])
    def test_batch_stops_after_error(self):
        a=self.setup_runner(fail='submit');b=self.store.save(job(title='second'))
        plan=self.runner.plan([a['id'],b['id']],'register');self.runner.start(plan['id'],'예약 등록');self.runner.thread.join(5)
        self.assertEqual(self.calls.count('submit'),1);self.assertEqual(self.store.get(b['id'])['status'],'draft')

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Service(Path(self.tmp.name))
        self.http=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.s));self.s.server=self.http
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.http.server_port)
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join();self.s.store.close();self.tmp.cleanup()
    def req(self,path,body=None,token=True,**headers):
        if token:headers['X-Cool-Token']=self.s.token
        if body is not None:headers['Content-Type']='application/json'
        r=urllib.request.Request(self.url+path,json.dumps(body).encode() if body is not None else None,headers)
        try:
            with urllib.request.urlopen(r) as resp:return resp.status,resp.read()
        except urllib.error.HTTPError as e:return e.code,e.read()
    def test_no_token_denied(self):self.assertEqual(self.req('/api/state',token=False)[0],403)
    def test_bad_origin_denied(self):self.assertEqual(self.req('/api/state',Origin='https://example.invalid')[0],403)
    def test_bad_host_denied(self):self.assertEqual(self.req('/api/state',Host='example.invalid')[0],403)
    def test_state_ok(self):self.assertEqual(self.req('/api/state')[0],200)
    def test_static_public_no_secrets(self):
        code,body=self.req('/',token=False);self.assertEqual(code,200);self.assertNotIn(self.s.token.encode(),body)
    def test_path_traversal_denied(self):self.assertEqual(self.req('/../server.py')[0],404)
    def test_save_and_export(self):
        self.assertEqual(self.req('/api/job',job())[0],200)
        self.assertIn('테스트'.encode(),self.req('/api/export')[1])
    def test_template_download(self):
        code,b=self.req('/api/template');self.assertEqual(code,200);self.assertTrue(zipfile.is_zipfile(io.BytesIO(b)))
    def test_demo_does_not_submit(self):
        self.assertEqual(self.req('/api/demo',{})[0],200)
        self.assertEqual([j['status'] for j in self.s.store.list_jobs()],['draft']*3)
    def test_import_preview_does_not_save(self):
        payload={'name':'t.csv','data':base64.b64encode(export_csv([job()])).decode()}
        self.assertEqual(self.req('/api/import',payload)[0],200)
        self.assertEqual(len(self.s.store.list_jobs()),0)
    def test_linux_diagnosis(self):
        code,data=self.req('/api/diagnose',{});self.assertEqual(code,200)
        if sys.platform!='win32':self.assertFalse(json.loads(data)['windows'])

if __name__=='__main__':unittest.main()
