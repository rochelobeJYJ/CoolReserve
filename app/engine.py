from __future__ import annotations
import contextlib
import copy
import hashlib
import json
import secrets
import threading
import time
from datetime import datetime
from .core import (Store, LOCKED, signature, validate_job, file_info, now, recipient_ids,
                   DEFAULT_RESULT_CONFIRMATION_MODE, validate_result_confirmation_mode)
from .windows import WindowsDriver, profile_errors, com_session, Stopped, is_gentoo_profile, PreparationTransient
from .privacy import safe_error, diagnostic
from .recipients import equivalent_attempt
from .verification import available as verification_available


class Runner:
    """Batch runner with per-run approval, crash-safe attempts, and an explicit dry-run."""
    def __init__(self, store: Store, driver_factory=WindowsDriver, context_factory=com_session):
        self.store = store
        self.driver_factory = driver_factory
        self.context_factory = context_factory
        self.guard = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.state = {'active': False, 'mode': '', 'current': '', 'progress': 0, 'total': 0, 'message': '실행 대기', 'log': [],
                      'stage':'idle','elapsed_ms':0,'report':None,
                      'result_confirmation_mode':DEFAULT_RESULT_CONFIRMATION_MODE}
        self.plans = {}

    def snapshot(self):
        with self.guard:
            return json.loads(json.dumps(self.state))

    def busy(self):
        return bool(self.state['active'])

    def log(self, message, update_message=True):
        with self.guard:
            if update_message:
                self.state['message'] = message
            self.state['log'] = (self.state['log'] + [f'{now():%H:%M:%S}  {message}'])[-150:]

    def plan(self, ids: list[str], mode: str,
             result_confirmation_mode: str = DEFAULT_RESULT_CONFIRMATION_MODE) -> dict:
        result_confirmation_mode = validate_result_confirmation_mode(result_confirmation_mode)
        if mode not in ('simulate', 'inspect', 'prepare', 'register'):
            raise ValueError('잘못된 실행 방식입니다.')
        if not ids or len(ids) != len(set(ids)) or len(ids) > 500:
            raise ValueError('실행할 메시지를 중복 없이 선택하세요. 최대 500건입니다.')
        if mode in ('prepare', 'inspect') and len(ids) != 1:
            raise ValueError('입력 시험·작성창 대조는 한 번에 1건만 실행합니다.')
        profile = self.store.setting('profile', {'roles': {}, 'contacts': {}})
        automatic = is_gentoo_profile(profile)
        jobs, errors, files = [], [], {}
        for jid in ids:
            j = self.store.get(jid)
            jobs.append(j)
            problems = validate_job(j)
            if mode != 'simulate':
                if mode != 'inspect' and (j['status'] in LOCKED or equivalent_attempt(self.store, j, profile)):
                    problems.append('이미 등록을 시도한 기록이 있어 자동 재등록하지 않습니다.')
                ids_for_job = recipient_ids(j)
                for contact_id in ids_for_job:
                    problems += profile_errors(profile, contact_id, bool(j['attachments']), read_only=mode=='inspect')
                    evidence = j.get('recipient_bindings', {}).get(contact_id)
                    contact = profile.get('contacts', {}).get(contact_id, {})
                    if evidence and {k: evidence.get(k) for k in ('selector', 'expected')} != {k: contact.get(k) for k in ('selector', 'expected')}:
                        problems.append(f'수신자 연결이 초안 저장 후 변경되었습니다: {contact_id}. 시트 미리보기를 다시 확인하세요.')
                if mode != 'inspect' and len(ids_for_job) > 1 and not profile.get('multi_select'):
                    problems.append('여러 수신자를 한 작성창에 넣는 화면 연결이 필요합니다.')
                if mode == 'register' and not automatic and not profile.get('tested'):
                    problems.append('“1건 자동 입력”을 먼저 성공시켜 주세요. 수동 화면 연결의 동작 확인이 필요합니다.')
                if mode == 'register' and not automatic and len(ids_for_job) > 1 and not profile.get('tested_multi'):
                    problems.append('여러 수신자의 “1건 자동 입력”을 먼저 성공시켜 주세요.')
                if (mode == 'register' and result_confirmation_mode == 'automatic' and len(ids) > 1
                        and not verification_available(profile, bool(j['attachments']))):
                    problems.append('전체 등록에는 예약 목록 대조 연결이 필요합니다. 쿨메신저 연결에서 결과 대조를 먼저 준비하세요.')
            try:
                files[jid] = [file_info(p) for p in j['attachments']]
            except (OSError, ValueError) as e:
                problems.append(str(e))
            if problems:
                errors.append({'id': jid, 'title': j['title'], 'errors': problems})
        token = secrets.token_urlsafe(24)
        plan = {'id': token, 'mode': mode, 'jobs': jobs, 'profile': profile, 'files': files, 'errors': errors,
                'result_confirmation_mode': result_confirmation_mode, 'at': time.monotonic()}
        with self.guard:
            self.plans = {k: v for k, v in self.plans.items() if time.monotonic() - v['at'] < 300}
            self.plans[token] = plan
        return {'id': token, 'mode': mode, 'jobs': jobs, 'errors': errors, 'expires_in': 300,
                'result_confirmation_mode': result_confirmation_mode}

    def start(self, plan_id: str, phrase: str):
        with self.guard:
            if self.busy():
                raise ValueError('이미 실행 중입니다.')
            plan = self.plans.pop(plan_id, None)
            if not plan or time.monotonic() - plan['at'] > 300:
                raise ValueError('검토가 만료되었습니다. 다시 미리보기 해주세요.')
            if plan['errors']:
                raise ValueError('검토 오류를 먼저 수정하세요.')
            mode = plan['mode']
            if mode == 'register' and phrase != '예약 등록':
                raise ValueError('확인 문구 “예약 등록”을 정확히 입력하세요.')
            if self.store.setting('profile', {'roles': {}, 'contacts': {}}) != plan['profile']:
                raise ValueError('화면 연결이 변경되었습니다. 다시 검토하세요.')
            for j in plan['jobs']:
                current = self.store.get(j['id'])
                if mode not in ('simulate','inspect') and (current['status'] in LOCKED or equivalent_attempt(self.store, current, plan['profile'])):
                    raise ValueError('이미 등록을 시도한 메시지입니다. 재등록을 차단했습니다.')
                if signature(j) != signature(current):
                    raise ValueError('메시지가 변경되었습니다. 다시 검토하세요.')
            self.stop.clear()
            self.state = {'active': True, 'mode': mode, 'current': '', 'progress': 0,
                          'total': len(plan['jobs']), 'message': '실행 준비', 'log': [],
                          'stage':'validating','elapsed_ms':0,'report':None,
                          'result_confirmation_mode':plan['result_confirmation_mode']}
            self.thread = threading.Thread(target=self._run, args=(plan,), daemon=True)
            self.thread.start()

    def _check_preparation_recovery(self, plan, original, original_profile, expected_files, *snapshots):
        """Recheck the reviewed identity and ledger before/after one recovery."""
        if self.stop.is_set():
            raise Stopped('사용자가 중지했습니다.')
        current_job = self.store.get(original['id'])
        if (current_job['status'] in LOCKED
                or equivalent_attempt(self.store, current_job, original_profile)):
            raise ValueError('등록 시도 기록이 있어 자동 재개하지 않습니다.')
        if any(job['id'] != original['id'] or signature(job) != signature(original)
               for job in (*snapshots, current_job)):
            raise ValueError('메시지가 변경되어 자동 재개하지 않습니다. 다시 검토하세요.')
        if (plan['profile'] != original_profile
                or self.store.setting('profile', {}) != original_profile):
            raise ValueError('화면 연결이 변경되어 자동 재개하지 않습니다. 다시 검토하세요.')
        problems = validate_job(original)
        if problems:
            raise ValueError('\n'.join(problems))
        if [file_info(p) for p in original['attachments']] != expected_files:
            raise ValueError('첨부파일이 변경되어 자동 재개하지 않습니다.')

    def _run(self, plan: dict):
        mode = plan['mode']
        driver = None
        current = None
        submitting = False
        submitted_count = 0
        result_confirmation_mode = plan['result_confirmation_mode']
        started = time.monotonic()
        try:
            context = contextlib.nullcontext() if mode == 'simulate' else self.context_factory()
            with context:
                if mode != 'simulate':
                    driver = self.driver_factory(plan['profile'], self.stop)
                for i, j in enumerate(plan['jobs']):
                    current = j
                    submitting = False
                    if self.stop.is_set():
                        raise Stopped('사용자가 중지했습니다.')
                    self.state['current'] = j['id']
                    self.log(f'{i+1}/{len(plan["jobs"])}행 처리 중')
                    problems = validate_job(j)
                    if problems:
                        raise ValueError('\n'.join(problems))
                    fresh_files = [file_info(p) for p in j['attachments']]
                    if fresh_files != plan['files'][j['id']]:
                        raise ValueError('검토 이후 첨부파일이 바뀌었습니다. 다시 검토하세요.')
                    if mode == 'simulate':
                        self.log('모의 실행 통과 · 쿨메신저를 조작하지 않았습니다.')
                    elif mode == 'inspect':
                        self.state['stage'] = 'reading'
                        verify = getattr(driver, 'verify_existing', None) or driver.verify
                        verify(j)
                        checks = ['recipient','title','body','scheduled']
                        checks += [key for key, role in (('cc','cc_read'),('attachments','attach_list'))
                                   if role in plan['profile'].get('roles',{})]
                        self.state['report'] = {'passed':True, 'read_only':True, 'job_id':j['id'],
                                                'checks':checks}
                        self.log('작성창 대조 성공 · 화면 입력·클릭·전송 없음. 자동 입력 시험과 예약 등록은 별도입니다.')
                    else:
                        self.state['stage'] = 'preparing'
                        self.store.status(j['id'], 'preparing', '화면 입력 및 읽기 대조 중')
                        original = copy.deepcopy(j)
                        original_profile = copy.deepcopy(plan['profile'])
                        preparing_job = j
                        for recovery in range(2):
                            try:
                                driver.prepare(preparing_job)
                                if mode == 'register':
                                    self.state['stage'] = 'verifying'
                                    driver.verify(preparing_job)
                                    problems = validate_job(j)
                                    if problems:
                                        raise ValueError('\n'.join(problems))
                                    if [file_info(p) for p in j['attachments']] != fresh_files:
                                        raise ValueError('입력 도중 첨부파일이 변경되어 전송을 차단했습니다.')
                                    driver.checkpoint()
                                if recovery:
                                    self._check_preparation_recovery(
                                        plan, original, original_profile, fresh_files, j, preparing_job)
                                    self.log('창 응답 재확인 성공 · 수신자·내용·예약일시를 다시 대조했습니다.')
                                break
                            except PreparationTransient as exc:
                                if recovery:
                                    self.log('창 응답 재확인 중단 · ' + safe_error(exc))
                                    raise
                                if mode != 'register' or not is_gentoo_profile(original_profile):
                                    raise
                                self._check_preparation_recovery(
                                    plan, original, original_profile, fresh_files, j)
                                driver.checkpoint()
                                self.log('보내기 전 창 응답을 다시 확인합니다 (1/1)')
                                self.log('재확인 사유 · ' + safe_error(exc), update_message=False)
                                self.store.status(j['id'], 'preparing', '보내기 전 창 응답을 다시 확인합니다 (1/1)')
                                if self.stop.wait(.3):
                                    raise Stopped('사용자가 중지했습니다.')
                                self.state['stage'] = 'preparing'
                                # Only this in-memory snapshot is marked failed.
                                # The driver rechecks every field before its one
                                # permitted empty-draft recovery. No Send occurs
                                # anywhere inside this bounded preparation loop.
                                preparing_job = {**copy.deepcopy(original), 'status': 'failed'}
                            except Exception as exc:
                                if recovery:
                                    self.log('창 응답 재확인 중단 · ' + safe_error(exc))
                                raise
                        if mode == 'prepare':
                            self.store.status(j['id'], 'prepared', '수신자·내용·예약일시를 입력하고 읽기 대조했습니다. 보내기는 누르지 않았습니다.')
                            p = self.store.setting('profile', {})
                            p['tested'] = True
                            if len(recipient_ids(j)) > 1:
                                p['tested_multi'] = True
                            self.store.set_setting('profile', p)
                            self.log('입력 시험 성공 · 같은 메시지의 예약 등록을 이어서 실행할 수 있습니다. 전송하지 않았습니다.')
                        else:
                            self.store.begin_submit(j)
                            submitting = True
                            self.state['stage'] = 'submitting'
                            driver.submit(j)
                            submitted_count += 1
                            if result_confirmation_mode == 'manual':
                                # submit() returns only after Send and closure of
                                # this composer. This is not a receipt or delivery.
                                note = '보내기와 작성창 종료를 확인했습니다. 등록 결과는 사용자가 확인합니다. 자동 재등록하지 않습니다.'
                                self.store.finish_submit(j, 'needs_review', note)
                                submitting = False
                                self.state['report'] = {'passed': True, 'result_confirmation_mode': 'manual',
                                                        'submitted_count': submitted_count, 'receipt_checked': False}
                                self.log('등록 요청 완료 · 결과는 사용자가 확인합니다.')
                            else:
                                self.state['stage'] = 'checking_result'
                                checked, note = driver.verify_result(j)
                                self.store.finish_submit(j, 'confirmed' if checked else 'needs_review', note)
                                self.log('예약 목록 대조 완료' if checked else '등록 시도 후 예약 목록 대조가 필요합니다.')
                                submitting = False
                                if not checked:
                                    self.state['progress'] = i + 1
                                    self.state['stage'] = 'needs_review'
                                    self.log('나머지 처리를 보류했습니다. 예약 목록을 확인하기 전에는 재등록하지 마세요.')
                                    return
                    self.state['progress'] = i + 1
                if mode != 'inspect':
                    self.log('처리를 마쳤습니다.' if mode != 'simulate' else '모의 실행을 마쳤습니다. 실제 예약은 등록하지 않았습니다.')
                self.state['stage'] = 'done'
        except Exception as e:
            note = safe_error(e)
            detail = diagnostic(e)
            self.state['report'] = {'passed': False, 'read_only': mode == 'inspect',
                                    'job_id': current['id'] if current else '',
                                    'error': note, 'diagnostic': detail}
            if mode == 'register':
                self.state['report'].update(result_confirmation_mode=result_confirmation_mode,
                                            submitted_count=submitted_count, receipt_checked=False)
            if current and mode not in ('simulate','inspect'):
                if submitting:
                    self.store.finish_submit(current, 'uncertain', note + ' 자동 재시도 금지: 쿨메신저에서 등록 여부를 먼저 확인하세요.')
                elif self.store.get(current['id'])['status'] not in LOCKED:
                    self.store.status(current['id'], 'failed', note)
            self.log('중단 · ' + note)
            frame = detail['frames'][-1] if detail['frames'] else None
            location = f"{frame['file']}:{frame['line']} {frame['function']}" if frame else 'external'
            self.log(f"진단 · {detail['exception']} / {location}", update_message=False)
            self.state['stage'] = 'stopped' if isinstance(e, Stopped) else 'failed'
        finally:
            with self.guard:
                self.state['active'] = False
                self.state['current'] = ''
                self.state['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            self.log(f'소요 시간 {(time.monotonic() - started):.2f}초', update_message=False)

    def cancel(self):
        self.stop.set()
        self.log('중지 요청을 받았습니다. 진행 중인 Windows 호출이 끝나면 멈춥니다.')
