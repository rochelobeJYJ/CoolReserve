# SPDX-License-Identifier: MIT
"""Offline private-worker contract. Every provider/UI operation uses mocks."""
import contextlib
import copy
import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from app.core import now
from app.gentoo import automatic_profile
from app.teacherdeck_worker import PrivateAdapter, PrivateError, GentooProvider, MAX_LINE, handle_request, serve
from app.windows import Stopped, WindowsDriver


def profile():
    p = automatic_profile({'exe': 'C:/SyntheticOnly/CoolMessenger.exe'})
    p['contacts'] = {cid: {'expected': f'합성교사({account})(합성부서)',
                          'selector': {'backend': 'win32', 'exe': 'C:/SyntheticOnly/CoolMessenger.exe',
                                       'root_title': '합성 조직도', 'element': {'name': f'합성교사({account})'}}}
                     for cid, account in (('one', '90001'), ('two', '90002'))}
    p['self_binding'] = {'contact_id': 'one', 'expected': p['contacts']['one']['expected'],
                         'login_marker': {'label': '합성 본인'}}
    for role in ('file_name', 'file_open'):
        p['roles'][role] = dict(p['roles']['attach'])
    p['roles']['attach_list']['reader'] = 'synthetic_verified_attachment_inventory'
    p['tested'] = True
    p['tested_multi'] = True
    return p


class FakeProvider:
    def __init__(self):
        self.marker = {'label': '합성 본인'}
        self.checks = 0

    def connect(self, previous):
        return profile(), copy.deepcopy(self.marker)

    def check(self, p, marker):
        self.checks += 1
        if marker != self.marker:
            raise PrivateError('CONNECTION_CHANGED')


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        self.hook = lambda kind, job: None
        self.provider = FakeProvider()
        fixture = self

        class Driver:
            def __init__(self, p, stop):
                self.stop = stop

            def call(self, kind, job=None):
                fixture.calls.append(kind)
                fixture.hook(kind, job)
                if self.stop.is_set():
                    raise Stopped('synthetic stop')

            def prepare(self, job): self.call('prepare', job)
            def verify(self, job): self.call('verify', job)
            def verify_existing(self, job): self.call('inspect', job)
            def checkpoint(self): self.call('checkpoint')
            def submit(self, job):
                self.call('submit', job)
                if not fixture.adapter.store.has_attempt(__import__('app.core', fromlist=['signature']).signature(job)):
                    raise AssertionError('begin_submit must commit before submission')
            def verify_result(self, job):
                self.call('verify-result', job)
                return True, 'synthetic verified'

        self.factory = Driver
        self.adapter = PrivateAdapter(self.tmp.name, provider=self.provider,
                                      driver_factory=Driver, context_factory=contextlib.nullcontext)
        self.connect()

    def tearDown(self):
        if self.adapter:
            self.adapter.close()
        self.tmp.cleanup()

    def connect(self):
        self.connection = self.adapter.connect({})

    def message(self, **changes):
        m = {'id': 'synthetic', 'recipientIds': ['one'], 'title': '', 'body': '합성 본문',
             'scheduledAt': (now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'),
             'attachmentPaths': [], 'contentRevision': 1}
        m.update(changes)
        return m

    def plan(self, message=None, mode='register', **extra):
        return self.adapter.plan({'connectionId': self.connection['connectionId'],
                                  'messages': [message or self.message()], 'mode': mode, **extra})

    def start(self, plan, confirmed=True, binding=None):
        return self.adapter.start({'planId': plan['planId'], 'confirmed': confirmed,
                                   'accountBinding': binding or self.connection['accountBinding']})

    def finish(self, run):
        self.adapter.runner.thread.join(3)
        self.assertFalse(self.adapter.runner.thread.is_alive())
        return self.adapter.status(run)

    def test_simulate_is_local_and_optional_empty_title_is_preserved(self):
        checks = self.provider.checks
        p = self.plan(self.message(title=None), mode='simulate')
        status = self.finish(self.start(p, False))
        self.assertEqual(status['stage'], 'done')
        self.assertEqual(self.provider.checks, checks)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.adapter.store.get('synthetic')['title'], '')

    def test_confirmation_is_required_and_submission_ledger_commits_first(self):
        p = self.plan()
        with self.assertRaisesRegex(PrivateError, 'CONFIRMATION_REQUIRED'):
            self.start(p, False)
        self.assertEqual(self.calls, [])
        p = self.plan()
        status = self.finish(self.start(p))
        self.assertEqual(status['items'][0]['state'], 'registration-requested')
        self.assertTrue(status['items'][0]['retryLocked'])
        self.assertNotIn('verify-result', self.calls)
        self.assertNotIn('body', json.dumps(status))
        self.assertNotIn('log', status)

    def test_immutable_plan_rejects_content_attachment_or_account_change(self):
        p = self.plan()
        self.adapter.store.save({**self.adapter.store.get('synthetic'), 'body': '바뀐 합성 본문'})
        with self.assertRaises(ValueError): self.start(p)
        self.assertEqual(self.calls, [])
        path = Path(self.tmp.name) / 'synthetic.txt'
        path.write_text('one', encoding='utf-8')
        p = self.plan(self.message(id='attachment', attachmentPaths=[str(path)]))
        path.write_text('changed', encoding='utf-8')
        status = self.finish(self.start(p))
        self.assertEqual(status['stage'], 'failed')
        self.assertNotIn('submit', self.calls)
        p = self.plan(self.message(id='account'))
        self.provider.marker = {'label': '바뀐 합성 본인'}
        with self.assertRaisesRegex(PrivateError, 'CONNECTION_CHANGED'): self.start(p)

    def test_mismatched_binding_and_unsupported_result_mode_are_blocked(self):
        with self.assertRaisesRegex(PrivateError, 'CONNECTION_CHANGED'):
            self.start(self.plan(), binding='different')
        p = self.plan(resultConfirmation='automatic')
        self.assertEqual(p['issues'][0]['code'], 'unsupported-feature')
        with self.assertRaisesRegex(PrivateError, 'PLAN_BLOCKED'): self.start(p)

    def test_uncertain_submission_restart_and_new_id_keep_retry_lock(self):
        self.hook = lambda kind, job: (_ for _ in ()).throw(RuntimeError('synthetic secret')) if kind == 'submit' else None
        status = self.finish(self.start(self.plan()))
        self.assertEqual(status['items'][0]['state'], 'uncertain')
        self.assertTrue(status['items'][0]['retryLocked'])
        self.assertNotIn('synthetic secret', json.dumps(status))
        self.adapter.close()
        self.adapter = PrivateAdapter(self.tmp.name, provider=self.provider, driver_factory=self.factory,
                                      context_factory=contextlib.nullcontext)
        self.connect()
        p = self.plan(self.message(id='recreated'))
        self.assertTrue(p['items'][0]['retryLocked'])
        self.assertEqual(p['issues'][0]['code'], 'retry-locked')
        with self.assertRaisesRegex(PrivateError, 'PLAN_BLOCKED'): self.start(p)

    def test_crash_after_begin_submit_becomes_uncertain(self):
        self.plan()
        self.adapter.store.begin_submit(self.adapter.store.get('synthetic'))
        self.adapter.close()
        self.adapter = PrivateAdapter(self.tmp.name, provider=self.provider, driver_factory=self.factory,
                                      context_factory=contextlib.nullcontext)
        self.assertEqual(self.adapter.store.get('synthetic')['status'], 'uncertain')
        self.connect()
        self.assertTrue(self.plan()['items'][0]['retryLocked'])

    def test_status_and_cancel_continue_while_original_runner_is_active(self):
        entered, release = threading.Event(), threading.Event()
        def hook(kind, job):
            if kind == 'prepare':
                entered.set()
                if not release.wait(2): raise RuntimeError('fixture timeout')
        self.hook = hook
        run = self.start(self.plan())
        self.assertTrue(entered.wait(2))
        self.assertTrue(self.adapter.status(run)['active'])
        self.assertEqual(self.adapter.cancel(run), {'requested': True})
        release.set()
        self.assertEqual(self.finish(run)['stage'], 'stopped')
        self.assertNotIn('submit', self.calls)

    def test_original_f8_uses_the_same_cancel_event(self):
        stop = threading.Event()
        driver = WindowsDriver(profile(), stop)
        with patch.dict(sys.modules, {'win32api': type('FakeWin32', (), {'GetAsyncKeyState': staticmethod(lambda key: 0x8000)})}):
            with self.assertRaises(Stopped): driver.checkpoint()
        self.assertTrue(stop.is_set())

    def test_same_name_accounts_are_distinct_and_bad_inputs_never_truncate(self):
        recipients = self.adapter.recipients({'connectionId': self.connection['connectionId']})
        self.assertEqual(len(recipients), 2)
        self.assertNotEqual(recipients[0]['accountLabel'], recipients[1]['accountLabel'])
        for message in (self.message(recipientIds=['one', 'one']), self.message(title=3),
                        self.message(body='x'*30001), self.message(body=' changed ')):
            with self.assertRaises(PrivateError): self.plan(message)
        a = {'connectionId': self.connection['connectionId'], 'messages': [self.message()]*501, 'mode': 'simulate'}
        with self.assertRaises(PrivateError): self.adapter.plan(a)

    def test_json_framing_redacts_provider_diagnostics_and_rejects_duplicate_keys(self):
        secret = 'C:/SyntheticOnly/secret.txt private body'
        class Fail:
            def dispatch(self, method, args): raise RuntimeError(secret)
        response = handle_request(Fail(), b'{"id":"test","method":"connect","args":{}}\n')
        self.assertEqual(json.loads(response)['error']['code'], 'PROVIDER_FAILED')
        self.assertNotIn(secret.encode(), response)
        duplicate = handle_request(self.adapter, b'{"id":"one","id":"two","method":"connect","args":{}}\n')
        self.assertEqual(json.loads(duplicate)['error']['code'], 'INVALID_ARGS')
        out = io.BytesIO()
        serve(self.adapter, io.BytesIO(b'x'*(MAX_LINE+1)+b'\n'), out)
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(out.getvalue())['error']['code'], 'INVALID_ARGS')

    def test_valid_self_evidence_reuses_connection_and_missing_evidence_requires_one_setup(self):
        p = profile()
        p['self_binding']['login_marker'] = {'label': '합성'}
        with patch('app.teacherdeck_worker.initialize_profile', return_value={'profile': p}), \
             patch('app.teacherdeck_worker.com_session', contextlib.nullcontext), \
             patch('app.teacherdeck_worker.read_gentoo_login_marker', return_value={'label': '합성'}), \
             patch('app.teacherdeck_worker.gentoo_self_expected', return_value=p['contacts']['one']['expected']), \
             patch('app.teacherdeck_worker.build_profile', side_effect=AssertionError('Must not require another composer')):
            connected, marker = GentooProvider().connect(p)
            self.assertEqual(connected, p)
        p.pop('self_binding')
        with patch('app.teacherdeck_worker.initialize_profile', return_value={'profile': p}), \
             patch('app.teacherdeck_worker.com_session', contextlib.nullcontext), \
             patch('app.teacherdeck_worker.read_gentoo_login_marker', return_value={'label': '합성'}), \
             patch('app.teacherdeck_worker.build_profile', side_effect=RuntimeError('fixture-only')):
            with self.assertRaisesRegex(PrivateError, 'SELF_VERIFICATION_REQUIRED'):
                GentooProvider().connect(p)

    def test_default_original_profile_does_not_advertise_attachment_support(self):
        p = automatic_profile({'exe': 'C:/SyntheticOnly/CoolMessenger.exe'})
        p['contacts'] = profile()['contacts']
        self.assertNotIn('attachments', self.adapter._features(p))

if __name__ == '__main__':
    unittest.main()
