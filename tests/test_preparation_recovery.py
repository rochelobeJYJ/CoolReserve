"""One bounded pre-Send recovery, with fake drivers and a temporary ledger."""
import contextlib
import copy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from app.core import Store, now, signature
from app.engine import Runner
from app.gentoo import automatic_profile
from app.windows import AutomationError, CaptionUnavailable, PreparationTransient, Stopped


class PreparationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.profile = automatic_profile({'exe': 'C:/SyntheticOnly/CoolMessenger.exe'})
        self.profile['contacts'] = {'fixture': {
            'expected': '합성교사(90001)(합성교사)',
            'selector': {'backend': 'win32', 'exe': 'C:/SyntheticOnly/CoolMessenger.exe',
                         'root_title': '합성 조직도', 'element': {'name': '합성교사(90001)'}}}}
        self.store.set_setting('profile', self.profile)
        self.jobs = []
        self.calls = []
        self.hook = lambda kind, job, count: None
        self.runner = None
        fixture = self

        class FakeDriver:
            def __init__(self, profile, stop):
                self.counts = {}

            def perform(self, kind, job=None):
                self.counts[kind] = self.counts.get(kind, 0) + 1
                fixture.calls.append((kind, copy.deepcopy(job)))
                fixture.hook(kind, job, self.counts[kind])

            def prepare(self, job): self.perform('prepare', job)
            def verify(self, job): self.perform('verify', job)
            def checkpoint(self): self.perform('checkpoint')
            def submit(self, job): self.perform('submit', job)
            def verify_result(self, job):
                self.perform('verify_result', job)
                return True, '합성 등록 목록 확인'

        self.runner = Runner(self.store, FakeDriver, contextlib.nullcontext)

    def tearDown(self):
        if self.runner.thread:
            self.runner.thread.join(4)
            self.assertFalse(self.runner.thread.is_alive())
        self.store.close()
        self.tmp.cleanup()

    def job(self):
        job = self.store.save({'recipient': 'fixture', 'title': f'합성 안내 {len(self.jobs)}',
                              'body': '합성 본문', 'attachments': [],
                              'scheduled': (now() + timedelta(days=2)).strftime('%Y-%m-%d 09:22')})
        self.jobs.append(job)
        return job

    def run_jobs(self, jobs, mode='register', confirmation='manual'):
        plan = self.runner.plan([job['id'] for job in jobs], mode, confirmation)
        self.assertEqual(plan['errors'], [])
        self.runner.start(plan['id'], '예약 등록' if mode == 'register' else '')
        self.runner.thread.join(4)
        self.assertFalse(self.runner.thread.is_alive())
        return self.runner.snapshot()

    def count(self, kind):
        return sum(item[0] == kind for item in self.calls)

    def test_transient_prepare_recovers_once_and_continues_remaining_jobs(self):
        jobs = [self.job(), self.job()]
        def hook(kind, job, count):
            if kind == 'prepare' and count == 1:
                raise CaptionUnavailable('합성 창 활성화 실패')
        self.hook = hook
        result = self.run_jobs(jobs)
        self.assertEqual((result['stage'], result['progress']), ('done', 2))
        self.assertEqual((self.count('prepare'), self.count('verify'), self.count('submit')), (3, 2, 2))
        snapshots = [job for kind, job in self.calls if kind == 'prepare']
        self.assertEqual([job['status'] for job in snapshots], ['draft', 'failed', 'draft'])
        self.assertEqual(signature(snapshots[0]), signature(snapshots[1]))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 2)
        self.assertEqual({self.store.get(job['id'])['status'] for job in jobs}, {'needs_review'})
        self.assertIn('보내기 전 창 응답을 다시 확인합니다 (1/1)', '\n'.join(result['log']))
        self.assertIn('창 응답 재확인 성공', '\n'.join(result['log']))

    def test_final_verification_transient_uses_same_single_recovery_budget(self):
        job = self.job()
        def hook(kind, current, count):
            if kind == 'verify' and count == 1:
                raise PreparationTransient('합성 창 목록 변경')
        self.hook = hook
        result = self.run_jobs([job])
        self.assertEqual(result['stage'], 'done')
        self.assertEqual((self.count('prepare'), self.count('verify'), self.count('submit')), (2, 2, 1))

    def test_prepare_and_final_verify_share_one_budget_and_do_not_skip_failed_job(self):
        jobs = [self.job(), self.job()]
        def hook(kind, job, count):
            if kind == 'prepare' and count == 1 or kind == 'verify':
                raise PreparationTransient('합성 창 응답 실패')
        self.hook = hook
        result = self.run_jobs(jobs)
        self.assertEqual((result['stage'], self.count('prepare'), self.count('submit')), ('failed', 2, 0))
        self.assertEqual([self.store.get(job['id'])['status'] for job in jobs], ['failed', 'draft'])
        self.assertFalse(self.store.has_attempt(signature(jobs[0])))
        self.assertIn('창 응답 재확인 중단', '\n'.join(result['log']))

    def test_repeated_transient_preparation_stops_after_two_calls(self):
        def hook(kind, job, count):
            if kind == 'prepare':
                raise PreparationTransient('합성 창 목록 변경')
        self.hook = hook
        result = self.run_jobs([self.job()])
        self.assertEqual((result['stage'], self.count('prepare'), self.count('submit')), ('failed', 2, 0))

    def test_unknown_recipient_body_timeout_and_stop_errors_never_retry(self):
        for error in (AutomationError('수신자 불일치'), AutomationError('본문 불일치'),
                      RuntimeError('unknown'), TimeoutError('timeout'), Stopped('F8')):
            self.calls.clear()
            def hook(kind, job, count):
                if kind == 'prepare':
                    raise error
            self.hook = hook
            result = self.run_jobs([self.job()])
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.count('prepare'), 1)
                self.assertEqual(self.count('submit'), 0)
                self.assertEqual(result['stage'], 'stopped' if isinstance(error, Stopped) else 'failed')

    def test_final_content_mismatch_never_retries(self):
        def hook(kind, job, count):
            if kind == 'verify':
                raise AutomationError('본문 불일치')
        self.hook = hook
        self.run_jobs([self.job()])
        self.assertEqual((self.count('prepare'), self.count('verify'), self.count('submit')), (1, 1, 0))

    def test_input_trial_and_non_gentoo_profiles_never_auto_retry(self):
        def hook(kind, job, count):
            if kind == 'prepare':
                raise PreparationTransient('합성 창 목록 변경')
        self.hook = hook
        self.run_jobs([self.job()], mode='prepare')
        self.assertEqual(self.count('prepare'), 1)
        self.calls.clear()
        profile = copy.deepcopy(self.profile)
        profile['multi_select']['kind'] = 'manual_fixture'
        profile['tested'] = True
        self.store.set_setting('profile', profile)
        self.run_jobs([self.job()])
        self.assertEqual(self.count('prepare'), 1)

    def test_send_exception_is_uncertain_and_never_retried(self):
        def hook(kind, job, count):
            if kind == 'submit':
                raise PreparationTransient('합성 전송 후 창 목록 변경')
        self.hook = hook
        job = self.job()
        self.run_jobs([job])
        self.assertEqual((self.count('prepare'), self.count('submit')), (1, 1))
        self.assertEqual(self.store.get(job['id'])['status'], 'uncertain')
        self.assertTrue(self.store.has_attempt(signature(job)))

    def test_result_check_exception_after_send_never_retries(self):
        def hook(kind, job, count):
            if kind == 'verify_result':
                raise PreparationTransient('합성 전송 후 창 목록 변경')
        self.hook = hook
        job = self.job()
        self.run_jobs([job], confirmation='automatic')
        self.assertEqual((self.count('prepare'), self.count('submit'), self.count('verify_result')), (1, 1, 1))
        self.assertEqual(self.store.get(job['id'])['status'], 'uncertain')

    def test_new_attempt_ledger_entry_blocks_recovery_even_when_status_is_preparing(self):
        def hook(kind, job, count):
            if kind == 'prepare':
                self.store.begin_submit(job)
                self.store.status(job['id'], 'preparing', '합성 상태 경합')
                raise PreparationTransient('합성 창 목록 변경')
        self.hook = hook
        self.run_jobs([self.job()])
        self.assertEqual((self.count('prepare'), self.count('submit')), (1, 0))

    def test_locked_status_blocks_recovery(self):
        for status in ('submitting', 'needs_review', 'confirmed', 'uncertain'):
            self.calls.clear()
            def hook(kind, job, count):
                if kind == 'prepare':
                    self.store.status(job['id'], status)
                    raise PreparationTransient('합성 창 목록 변경')
            self.hook = hook
            job = self.job()
            self.run_jobs([job])
            self.assertEqual((self.count('prepare'), self.count('submit')), (1, 0))
            self.assertEqual(self.store.get(job['id'])['status'], status)

    def test_changed_message_profile_or_attachment_blocks_recovery(self):
        for change in ('message', 'profile', 'attachment', 'snapshot'):
            self.calls.clear()
            self.store.set_setting('profile', self.profile)
            job = self.job()
            if change == 'attachment':
                attachment = Path(self.tmp.name) / 'fixture.txt'
                attachment.write_text('before', encoding='utf-8')
                job = self.store.save({**job, 'attachments': [str(attachment)]})
            def hook(kind, current, count):
                if kind != 'prepare':
                    return
                if change == 'message':
                    self.store.save({**current, 'title': '변경된 합성 제목'})
                elif change == 'profile':
                    profile = copy.deepcopy(self.profile)
                    profile['contacts']['fixture']['expected'] = '합성다른교사(90002)(합성다른교사)'
                    self.store.set_setting('profile', profile)
                elif change == 'attachment':
                    attachment.write_text('changed', encoding='utf-8')
                else:
                    current['body'] = '변경된 합성 본문'
                raise PreparationTransient('합성 창 목록 변경')
            self.hook = hook
            with self.subTest(change=change):
                # The current Gentoo adapter cannot upload attachments. Bypass
                # only that planning restriction to exercise the runner's
                # independent file-integrity guard with a fake driver.
                validation = patch('app.engine.profile_errors', return_value=[]) if change == 'attachment' else contextlib.nullcontext()
                with validation:
                    self.run_jobs([job])
                self.assertEqual((self.count('prepare'), self.count('submit')), (1, 0))

    def test_cancel_before_retry_never_prepares_again(self):
        def hook(kind, job, count):
            if kind == 'prepare':
                self.runner.stop.set()
                raise PreparationTransient('합성 창 목록 변경')
        self.hook = hook
        result = self.run_jobs([self.job()])
        self.assertEqual((result['stage'], self.count('prepare'), self.count('submit')), ('stopped', 1, 0))

    def test_change_during_second_preparation_is_rechecked_before_send(self):
        for change in ('snapshot', 'database', 'profile', 'ledger'):
            self.calls.clear()
            self.store.set_setting('profile', self.profile)
            def hook(kind, job, count):
                if kind != 'prepare':
                    return
                if count == 1:
                    raise PreparationTransient('합성 창 목록 변경')
                if change == 'snapshot':
                    job['body'] = '변경된 합성 내용'
                elif change == 'database':
                    self.store.save({**job, 'body': '변경된 합성 내용'})
                elif change == 'profile':
                    changed = copy.deepcopy(self.profile)
                    changed['contacts']['fixture']['expected'] = '합성다른교사(90002)(합성다른교사)'
                    self.store.set_setting('profile', changed)
                else:
                    self.store.begin_submit(job)
            self.hook = hook
            with self.subTest(change=change):
                result = self.run_jobs([self.job()])
                self.assertEqual((result['stage'], self.count('prepare'), self.count('submit')), ('failed', 2, 0))


if __name__ == '__main__':
    unittest.main()
