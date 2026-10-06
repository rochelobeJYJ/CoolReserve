#!/usr/bin/env python3
"""Local-only desktop service for 쿨 예약실. Run: python server.py --open"""
from __future__ import annotations

import argparse
import base64
import contextlib
import errno
import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

# Set before importing COM libraries; all workers join the persistent MTA.
sys.coinit_flags = 0
VERSION = '1.4.1'
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from app.core import (Store, now, STATES, expand_repeat, export_csv, validate_job,
                      DEFAULT_RESULT_CONFIRMATION_MODE, validate_result_confirmation_mode)
from app.engine import Runner
from app.importers import import_file
from app.windows import ROLE_LABELS, capture, diagnose, WindowsDriver, com_session, automation_runtime, is_gentoo_profile
from app.gentoo import inspect_compose, build_profile, inspect_contacts, initialize_profile, bind_gentoo_self
from app.sheets import SourceReader, read_table_csv, read_table_xlsx
from app.workflows import WorkflowService
from app.batch import BatchService, table_file
from app.recipients import resolve as resolve_recipients
from app.privacy import diagnostics, portable_config


class AlreadyRunning(RuntimeError):
    """The data directory is already owned by another CoolReserve instance."""


class InstanceLock:
    def __init__(self, path):
        self.f = open(path, 'a+b')
        try:
            # Windows byte locks deny reads as well as writes through other
            # handles. Acquire first; the range may extend beyond an empty file.
            # The lock file is never read, rewritten, truncated or removed.
            self.f.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.f.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise AlreadyRunning('already running') from exc
            raise

    def close(self):
        self.f.close()


def publish_session(path: Path, payload: dict):
    """Publish a complete session while the caller owns the instance lock."""
    temporary = path.with_name(f'.session-{os.getpid()}-{secrets.token_hex(8)}.tmp')
    try:
        temporary.write_text(json.dumps(payload), encoding='utf-8')
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def running_session_url(directory: Path, wait_seconds: float = 2.0) -> str | None:
    """Wait briefly for the lock owner's authenticated local server to be ready."""
    deadline = time.monotonic() + wait_seconds
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            saved = json.loads((directory / 'session.json').read_text(encoding='utf-8'))
            if not isinstance(saved, dict) or not isinstance(saved.get('url'), str):
                return None
            url = saved['url']
            parsed = urlsplit(url)
            if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                    or parsed.port is None or parsed.path != '/' or parsed.query
                    or parsed.username is not None or parsed.password is not None
                    or not parsed.fragment):
                return None
            request = urllib.request.Request(f'http://127.0.0.1:{parsed.port}/api/state',
                                             headers={'X-Cool-Token': parsed.fragment})
            with opener.open(request, timeout=min(.3, remaining)) as response:
                state = json.load(response)
            if (isinstance(state, dict) and state.get('version')
                    and state.get('data_dir')
                    and Path(state['data_dir']).resolve() == directory.resolve()):
                return url
        except urllib.error.HTTPError as exc:
            exc.close()
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(min(.05, max(0, deadline - time.monotonic())))


class Service:
    def __init__(self, data_dir: Path, offline=False):
        self.store = Store(data_dir)
        self.runner = Runner(self.store)
        self.token = secrets.token_urlsafe(32)
        self.mutation_lock = threading.RLock()
        self.server = None
        self.sources = SourceReader(data_dir)
        self.workflows = WorkflowService(self.store)
        self.batch = BatchService(self.store)
        self.offline = offline
        with self.store.lock, self.store.db:
            self.store.db.execute('CREATE TABLE IF NOT EXISTS source_reads(id TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def remember_source(self, snapshot):
        snapshot['source']['source_id'] = snapshot['source'].get('source_id', snapshot['source'].get('id', ''))
        sid = secrets.token_urlsafe(20)
        snapshot['snapshot_id'] = sid
        with self.store.lock, self.store.db:
            self.store.db.execute('INSERT INTO source_reads VALUES (?,?)', (sid, json.dumps(snapshot, ensure_ascii=False)))
            self.store.db.execute('DELETE FROM source_reads WHERE rowid NOT IN (SELECT rowid FROM source_reads ORDER BY rowid DESC LIMIT 30)')
        return {'snapshot_id': sid, 'snapshot': snapshot, 'tabs': snapshot.get('tabs', []),
                'connection': self.sources.connection_status()}

    def state(self):
        jobs = self.store.list_jobs(include_archived=False)
        for j in jobs:
            j['errors'] = validate_job(j, check_files=False)
        profile = self.store.setting('profile', {'roles': {}, 'contacts': {}, 'tested': False})
        return {'version': VERSION, 'now': now().strftime('%Y-%m-%d %H:%M'), 'jobs': jobs,
                'archived_count': len(self.store.list_archived()), 'past_cleanup_count': self.store.past_cleanup_count(),
                'profile': profile, 'connection_mode':'automatic' if is_gentoo_profile(profile) else 'manual',
                'needs_organization_refresh':not self.offline and profile.get('inventory_version') != '1.2.0',
                'offline':self.offline,
                'result_confirmation_mode':DEFAULT_RESULT_CONFIRMATION_MODE,
                'runner': self.runner.snapshot(), 'states': STATES, 'roles': ROLE_LABELS,
                'data_dir': str(self.store.directory), 'platform': sys.platform}

    def dispatch(self, path: str, body: dict):
        if path == '/api/stop':
            self.runner.cancel()
            return {'ok': True}
        if path == '/api/plan':
            if self.offline and body.get('mode', 'simulate') != 'simulate':
                raise ValueError('모의 실행 모드입니다. 실제 쿨메신저 입력과 등록은 실행하지 않습니다.')
            return self.runner.plan(body.get('ids', []), body.get('mode', 'simulate'),
                                    body.get('result_confirmation_mode', DEFAULT_RESULT_CONFIRMATION_MODE))
        with self.mutation_lock:
            if self.runner.busy():
                raise ValueError('실행 중에는 메시지와 화면 연결을 변경할 수 없습니다. 중지 후 다시 시도하세요.')
            if self.offline and path in ('/api/capture', '/api/read-role', '/api/diagnose', '/api/gentoo/inspect', '/api/gentoo/initialize', '/api/gentoo/contacts', '/api/gentoo/connect', '/api/gentoo/self', '/api/gentoo/discard-empty-draft'):
                raise ValueError('모의 실행 모드에서는 실제 쿨메신저 화면을 읽거나 조작하지 않습니다.')
            if path == '/api/archive-past':
                return self.store.archive_past(body.get('ids'))
            if path == '/api/archives':
                return {'jobs': self.store.list_archived()}
            if path == '/api/restore-archived':
                return self.store.restore_archived(body.get('ids'))
            if path == '/api/recipients/resolve':
                names, choices = body.get('names'), body.get('choices', {})
                if (not isinstance(names, (str, list))
                        or isinstance(names, list) and any(not isinstance(name, str) for name in names)
                        or not isinstance(choices, dict)
                        or any(not isinstance(key, str) or not isinstance(value, str) for key, value in choices.items())):
                    raise ValueError('수신자 명단과 계정 선택 형식을 확인하세요.')
                contacts = self.store.setting('profile', {}).get('contacts', {})
                result = resolve_recipients(names, contacts, choices)
                result['can_apply'] = not result['issues']
                if result['issues']:
                    result['ids'], result['bindings'] = [], {}
                return result
            if path == '/api/batch/preview':
                return self.batch.preview(body.get('rows'), body.get('choices'))
            if path == '/api/batch/import':
                data = base64.b64decode(body.get('data', ''), validate=True)
                return self.batch.preview(table_file(body.get('name', ''), data), body.get('choices'))
            if path == '/api/batch/commit':
                return self.batch.commit(body.get('review_id', ''))
            if path == '/api/batch/register':
                if self.offline:
                    raise ValueError('모의 실행 모드에서는 실제 예약을 등록하지 않습니다.')
                if body.get('phrase') != '예약 등록':
                    raise ValueError('전체 예약 등록 승인이 필요합니다.')
                confirmation = validate_result_confirmation_mode(
                    body.get('result_confirmation_mode', DEFAULT_RESULT_CONFIRMATION_MODE))
                result = self.batch.commit(body.get('review_id', ''))
                plan = self.runner.plan(result['job_ids'], 'register', confirmation)
                if plan['errors']:
                    return dict(result, started=False, plan_errors=plan['errors'], result_confirmation_mode=confirmation)
                self.runner.start(plan['id'], '예약 등록')
                return dict(result, started=True, result_confirmation_mode=confirmation)
            if path == '/api/source/status':
                return self.sources.connection_status()
            if path == '/api/source/connect':
                if self.server is None:
                    raise ValueError('서버가 준비되지 않았습니다.')
                return self.sources.begin_oauth(body.get('client_json_path'), f'http://127.0.0.1:{self.server.server_port}/oauth/callback')
            if path == '/api/source/disconnect':
                return self.sources.disconnect()
            if path == '/api/source/read':
                result = self.remember_source(self.sources.read(body))
                self.store.set_setting('source_config', {k: body[k] for k in ('kind','url','tab_id','range') if k in body})
                return result
            if path == '/api/source/cached':
                with self.store.lock:
                    cached = self.store.db.execute('SELECT payload FROM source_reads ORDER BY rowid DESC LIMIT 1').fetchone()
                if not cached:
                    raise ValueError('이 PC에서 읽어 둔 자료가 없습니다. 파일이나 시트를 먼저 불러오세요.')
                snapshot = json.loads(cached['payload'])
                snapshot['source'].update(cached=True, access='saved_snapshot')
                return self.remember_source(snapshot)
            if path == '/api/source/import':
                raw = base64.b64decode(body.get('data', ''), validate=True)
                if len(raw) > 5 * 1024 * 1024:
                    raise ValueError('자료 파일은 5MB 이하여야 합니다.')
                kind = body.get('kind', '')
                if kind not in ('csv', 'xlsx'):
                    raise ValueError('CSV 또는 XLSX 자료를 선택하세요.')
                values = read_table_csv(raw) if kind == 'csv' else read_table_xlsx(raw, body.get('sheet_name', ''))
                import hashlib
                source_id = 'file:' + str(body.get('name', '자료')) + ':' + str(body.get('sheet_name', ''))
                snapshot = {'source': {'kind':kind,'source_id':source_id,'title':body.get('name','자료'),
                                       'tab_name':body.get('sheet_name',''), 'range':'파일 전체'},
                            'values':values,'read_at':now().isoformat(timespec='seconds'),
                            'hash':hashlib.sha256(raw).hexdigest()}
                return self.remember_source(snapshot)
            if path == '/api/workflows/presets':
                return self.workflows.presets()
            if path == '/api/workflows/list':
                recipes = self.workflows.list_recipes()
                return {'recipes':recipes, 'source':self.store.setting('source_config',{})}
            if path == '/api/workflows/save':
                return self.workflows.save_recipe(body)
            if path == '/api/workflows/preview':
                with self.store.lock:
                    row = self.store.db.execute('SELECT payload FROM source_reads WHERE id=?', (body.get('snapshot_id',''),)).fetchone()
                if not row:
                    raise ValueError('자료를 다시 불러오세요. 저장된 읽기 자료를 찾지 못했습니다.')
                recipe = body.get('recipe','lunch')
                if body.get('contact_mapping') is not None:
                    if not isinstance(recipe,dict):
                        recipe = self.workflows._recipe(recipe)
                    recipe = dict(recipe,contact_mapping=body['contact_mapping'])
                return self.workflows.preview(json.loads(row[0]), recipe, body.get('from'), body.get('to'))
            if path == '/api/workflows/commit':
                return self.workflows.commit(body.get('preview_id',''))
            if path == '/api/job':
                return self.store.save(body)
            if path == '/api/remove':
                self.store.remove(body.get('ids', [])); return {'ok': True}
            if path == '/api/import':
                raw = base64.b64decode(body.get('data', ''), validate=True)
                jobs, errors = import_file(body.get('name', ''), raw)
                return {'jobs': jobs, 'errors': errors}
            if path == '/api/import/commit':
                jobs = body.get('jobs', [])
                if not isinstance(jobs, list) or len(jobs) > 500:
                    raise ValueError('메시지는 500건 이하여야 합니다.')
                saved = self.store.save_batch(jobs)
                return {'saved': len(saved), 'errors': []}
            if path == '/api/repeat':
                return {'jobs': expand_repeat(body['job'], body['end'], body['weekdays'], body.get('excluded', [])), 'errors': []}
            if path == '/api/profile':
                profile = body.get('profile')
                if not isinstance(profile, dict) or not isinstance(profile.get('roles'), dict) or not isinstance(profile.get('contacts'), dict):
                    raise ValueError('화면 연결 형식이 잘못되었습니다.')
                if any(k not in ROLE_LABELS for k in profile['roles']):
                    raise ValueError('알 수 없는 화면 요소 역할입니다.')
                previous = self.store.setting('profile', {})
                unchanged = (previous.get('roles', {}) == profile['roles']
                             and previous.get('multi_select') == profile.get('multi_select')
                             and all({k:c.get(k) for k in ('selector','expected','expected_result')} ==
                                     {k:profile['contacts'].get(cid,{}).get(k) for k in ('selector','expected','expected_result')}
                                     for cid,c in previous.get('contacts',{}).items()))
                # Client flags cannot grant readiness. Adding an unrelated
                # contact does not invalidate already-tested input controls.
                profile['tested'] = bool(unchanged and previous.get('tested'))
                profile.pop('tested_multi', None)
                if unchanged and previous.get('tested_multi'):
                    profile['tested_multi'] = True
                self.store.set_setting('profile', profile)
                return {'ok': True}
            if path == '/api/capture':
                return capture(body.get('backend', 'uia'), body.get('role', ''), 4)
            if path == '/api/read-role':
                with com_session():
                    d = WindowsDriver(self.store.setting('profile', {}), threading.Event())
                    return {'text': d.text(body['role'])}
            if path == '/api/gentoo/discard-empty-draft':
                job = self.store.get(body.get('job_id', ''))
                if job['status'] != 'failed':
                    raise ValueError('보내기 전에 실패한 작업의 빈 작성창만 정리할 수 있습니다.')
                from app.draft_recovery import discard_empty_draft
                with com_session():
                    d = WindowsDriver(self.store.setting('profile', {}), threading.Event())
                    return discard_empty_draft(d, hwnd=body.get('hwnd'), pid=body.get('pid'),
                                               recipient=body.get('recipient'))
            if path == '/api/diagnose':
                return diagnose()
            if path == '/api/gentoo/inspect':
                return inspect_compose()
            if path == '/api/gentoo/initialize':
                result = initialize_profile(self.store.setting('profile', {}))
                self.store.set_setting('profile', result.pop('profile'))
                return dict(result, ok=True)
            if path == '/api/gentoo/self':
                profile = bind_gentoo_self(self.store.setting('profile', {}), body.get('contact_id', ''))
                self.store.set_setting('profile', profile)
                return {'ok': True, 'recipient': profile['self_binding']['expected']}
            if path == '/api/gentoo/contacts':
                return inspect_contacts(self.store.setting('profile',{}))
            if path == '/api/gentoo/connect':
                return build_profile(body.get('alias', '본인 테스트'))
            if path == '/api/review':
                self.store.review(body['id'], body.get('note', ''))
                return {'ok': True}
            if path == '/api/run':
                self.runner.start(body.get('plan_id', ''), body.get('phrase', ''))
                return {'ok': True}
            if path == '/api/demo':
                # Demonstration records are local draft data only. No external contacts are looked up.
                start = (now() + timedelta(days=1)).replace(hour=8, minute=20, second=0, microsecond=0)
                examples = [
                    ('본인 테스트', '예약전송 확인용 메시지', '예약 기능을 확인하는 테스트 메시지입니다.\n실제 수신자는 화면 연결에서 먼저 확인하세요.'),
                    ('2학년 담임', '교육자료 배부 안내', '선생님, 안녕하세요.\n오늘 조회시간에 교육자료를 배부해 주시기 바랍니다.\n협조해 주셔서 감사합니다.'),
                    ('업무 담당자', '자료 제출일 안내', '안녕하세요.\n내일까지 자료 제출을 부탁드립니다.\n이미 제출하신 선생님께서는 참고만 해주세요.'),
                ]
                count = 0
                for i, (recipient, title, text) in enumerate(examples):
                    try:
                        self.store.save({'scheduled': (start + timedelta(days=i)).strftime('%Y-%m-%d %H:%M'),
                                         'recipient': recipient, 'title': title, 'body': text})
                        count += 1
                    except ValueError:
                        pass
                return {'saved': count}
            if path == '/api/shutdown':
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return {'ok': True}
        raise ValueError('지원하지 않는 요청입니다.')


def make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'CoolReserve/1.0'
        def log_message(self, fmt, *args):
            # No tokens, recipient names or message contents in HTTP logs.
            pass

        def reply(self, status: int, data: bytes, mime='application/json; charset=utf-8', attachment=None, inline_style=False):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            styles = "'self' 'unsafe-inline'" if inline_style else "'self'"
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src " + styles + "; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if attachment:
                self.send_header('Content-Disposition', f'attachment; filename="{attachment}"')
            self.end_headers()
            self.wfile.write(data)

        def json(self, obj, code=200):
            self.reply(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'))

        def trusted(self):
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host:
                return False
            origin = self.headers.get('Origin')
            if origin is not None and origin != f'http://{host}':
                return False
            return secrets.compare_digest(self.headers.get('X-Cool-Token', ''), service.token)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/oauth/callback':
                if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                    self.json({'error':'접근 권한이 없습니다.'},403); return
                try:
                    params = parse_qs(urlsplit(self.path).query)
                    if params.get('error'):
                        raise ValueError('Google 연결이 취소되었습니다. 앱으로 돌아가 다시 연결하세요.')
                    service.sources.finish_oauth(params.get('code',[''])[0], params.get('state',[''])[0])
                    message = 'Google 시트를 연결했습니다. 이 창을 닫고 쿨 예약실에서 자료를 다시 불러오세요.'
                    status = 200
                except ValueError:
                    message = 'Google 연결을 완료하지 못했습니다. 앱으로 돌아가 다시 연결하세요.'
                    status = 400
                self.reply(status, ('<!doctype html><html lang="ko"><meta charset="utf-8"><title>Google 연결</title><p>'+message+'</p></html>').encode(), 'text/html; charset=utf-8'); return
            if path.startswith('/api/'):
                if not self.trusted():
                    self.json({'error': '접근 권한이 없습니다. 프로그램을 다시 실행하세요.'}, 403); return
                if path == '/api/state':
                    self.json(service.state()); return
                if path == '/api/events':
                    self.json(service.store.events()); return
                if path == '/api/export':
                    self.reply(200, export_csv(service.store.list_jobs(), True), 'text/csv; charset=utf-8', 'cool-reservations.csv'); return
                if path == '/api/diagnostics':
                    self.reply(200, json.dumps(diagnostics(service.store, VERSION),ensure_ascii=False,indent=2).encode('utf-8'), attachment='coolreserve-diagnostics.json'); return
                if path == '/api/config-template':
                    self.reply(200, json.dumps(portable_config(service.store.setting('profile',{})),ensure_ascii=False,indent=2).encode('utf-8'), attachment='coolreserve-config-template.json'); return
                if path == '/api/template':
                    preset = parse_qs(urlsplit(self.path).query).get('preset', [''])[0]
                    if preset not in ('', 'lunch'):
                        self.json({'error': '알 수 없는 양식입니다.'}, 400); return
                    filename = 'lunch_preset.xlsx' if preset == 'lunch' else 'reservation_template.xlsx'
                    template = ROOT / 'templates' / filename
                    if not template.is_file():
                        self.json({'error': '양식 파일이 없습니다.'}, 404); return
                    self.reply(200, template.read_bytes(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', filename); return
                self.json({'error': '요청 경로를 찾지 못했습니다.'}, 404); return
            if path == '/google-connection.html':
                guide = ROOT / 'docs' / 'google-connection.html'
                if guide.is_file():
                    self.reply(200, guide.read_bytes(), 'text/html; charset=utf-8', inline_style=True); return
            permitted = {'/': 'index.html', '/app.js': 'app.js', '/batch.js': 'batch.js', '/style.css': 'style.css', '/favicon.svg': 'favicon.svg'}
            if path not in permitted:
                self.json({'error': '찾을 수 없습니다.'}, 404); return
            file = ROOT / 'web' / permitted[path]
            self.reply(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

        def do_POST(self):
            if not self.trusted():
                self.json({'error': '접근 권한이 없습니다.'}, 403); return
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                self.json({'error': 'JSON 요청만 받습니다.'}, 415); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > 15 * 1024 * 1024:
                    raise ValueError('요청 크기가 너무 큽니다.')
                body = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('잘못된 요청 형식입니다.')
                self.json(service.dispatch(urlsplit(self.path).path, body))
            except (ValueError, KeyError, TypeError) as e:
                self.json({'error': str(e)}, 400)
            except Exception as e:
                self.json({'error': str(e) or type(e).__name__}, 500)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--offline', action='store_true', help='쿨메신저를 읽거나 조작하지 않는 모의 실행 모드')
    args = parser.parse_args()
    directory = args.data_dir or Path(os.getenv('LOCALAPPDATA') or Path.home()) / 'CoolReserve'
    directory.mkdir(parents=True, exist_ok=True)
    session = directory / 'session.json'
    try:
        lock = InstanceLock(directory / 'instance.lock')
    except AlreadyRunning:
        if args.open:
            existing_url = running_session_url(directory)
            if existing_url:
                webbrowser.open(existing_url)
            else:
                print('이미 실행 중인 프로그램의 화면을 아직 확인하지 못했습니다. 잠시 후 START.cmd를 다시 실행하세요.')
        print('CoolReserve is already running.'); return
    service = server = None
    runtime = contextlib.ExitStack()
    try:
        # A previous crash may have left a session for a server that is gone.
        session.unlink(missing_ok=True)
        runtime.enter_context(contextlib.nullcontext() if args.offline else automation_runtime())
        service = Service(directory, offline=args.offline)
        server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(service))
        server.daemon_threads = True
        service.server = server
        url = f'http://127.0.0.1:{server.server_port}/#{service.token}'
        publish_session(session, {'url': url, 'pid': os.getpid()})
        print(f'CoolReserve local service: http://127.0.0.1:{server.server_port}/', flush=True)
        print('Close from the app menu, or press Ctrl+C in this window.', flush=True)
        if args.open:
            webbrowser.open(url)
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        if service:
            service.runner.cancel()
    finally:
        try:
            if service:
                service.runner.stop.set()
                if service.runner.thread:
                    service.runner.thread.join(30)
            if server:
                server.server_close()
            if service and (not service.runner.thread or not service.runner.thread.is_alive()):
                service.store.close()
        finally:
            # Keep ownership until session removal, including failed startup.
            try:
                session.unlink(missing_ok=True)
            finally:
                try:
                    runtime.close()
                finally:
                    lock.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
