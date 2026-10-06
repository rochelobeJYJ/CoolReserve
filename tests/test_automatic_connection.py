"""Automatic Gentoo connection and batch regressions without native UI calls."""
import contextlib
import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from app.core import Store, clean_job, signature
from app.engine import Runner
from app.gentoo import automatic_profile, initialize_profile
from app.windows import is_gentoo_profile
from server import Service


BASE = {'backend': 'uia', 'exe': 'C:\\TestOnly\\CoolMessenger.exe',
        'root_title': '학교', 'root_class': '#32770'}


def contact(name, account, department='테스트부서'):
    label = f'({name}({account}))학교/{department}/{account}'
    return {'display_name': label, 'expected': label, 'account': str(account),
            'selector': {**BASE, 'element': {'control_type': 'TreeItem', 'name': label},
                         'parents': [{'control_type': 'TreeItem', 'name_regex': '^' + department + r'(?:\s*\(\d+(?:/\d+)?\))?$',},
                                     {'class_name': 'SysTreeView32', 'control_type': 'Tree',
                                      'control_id': 3013, 'automation_id': '3013'}]}}


def fixture_profile():
    profile = automatic_profile(BASE)
    profile['tested'] = False
    profile['tested_multi'] = False
    profile['contacts'] = {'fixture-a': contact('FixtureA', 100), 'fixture-b': contact('FixtureB', 101)}
    return profile


def inventory():
    existing = contact('FixtureA', 100, '이동한부서')
    new = contact('FixtureC', 102)
    return {'base': dict(BASE), 'contacts': {'fresh-copy-a': existing, 'fresh-c': new}, 'count': 2,
            'coverage': {'method': 'win32_tree', 'scope': 'loaded_tree', 'complete': False,
                         'node_count': 7, 'account_count': 2, 'skipped_count': 5},
            'warnings': ['Fixture loaded tree is not a complete server directory.']}


class AutomaticTemplateTests(unittest.TestCase):
    def test_supported_automatic_template_is_recognized_without_readiness_flags(self):
        profile = fixture_profile()
        self.assertTrue(is_gentoo_profile(profile))
        self.assertFalse(profile['tested'])
        self.assertFalse(profile['tested_multi'])

    def test_mutated_controls_executable_editor_frame_and_picker_are_not_automatic(self):
        mutations = {
            'wrong title ID': lambda p: p['roles']['title']['element'].update(control_id=9999),
            'wrong button class': lambda p: p['roles']['send']['element'].update(class_name='Edit'),
            'wrong executable': lambda p: p['roles']['title'].update(exe='C:\\TestOnly\\Other.exe'),
            'all wrong executables': lambda p: [role.update(exe='C:\\TestOnly\\Other.exe') for role in p['roles'].values()],
            'wrong root class': lambda p: p['roles']['datetime'].update(root_class='OtherRoot'),
            'wrong body frame': lambda p: p['roles']['body']['parents'][0].update(automation_id='OtherFrame'),
            'wrong document type': lambda p: p['roles']['body']['element'].update(control_type='Pane'),
            'wrong body backend': lambda p: p['roles']['body'].update(backend='win32'),
            'wrong picker': lambda p: p['multi_select'].update(kind='unsupported-picker'),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                profile = fixture_profile()
                mutate(profile)
                self.assertFalse(is_gentoo_profile(profile))

    def test_initialization_preserves_saved_bindings_and_adds_only_new_accounts(self):
        for automatic in (True, False):
            with self.subTest(previous_automatic=automatic):
                previous = fixture_profile() if automatic else {'roles': {}, 'contacts': {'saved-alias': contact('FixtureA', 100)}}
                before = copy.deepcopy(previous)
                read = inventory()
                original_inventory = copy.deepcopy(read)
                with patch('app.gentoo.inspect_contacts', return_value=read) as inspect, \
                     patch('app.gentoo._organization_root', side_effect=AssertionError('No native organization lookup')) as native:
                    initialized = initialize_profile(previous)
                inspect.assert_called_once_with(previous)
                native.assert_not_called()
                self.assertEqual(previous, before)
                self.assertEqual(read, original_inventory)
                self.assertTrue(is_gentoo_profile(initialized['profile']))
                for key, value in before['contacts'].items():
                    self.assertEqual(initialized['profile']['contacts'][key], value)
                self.assertNotIn('fresh-copy-a', initialized['profile']['contacts'])
                self.assertEqual(initialized['profile']['contacts']['fresh-c'], read['contacts']['fresh-c'])
                self.assertEqual(initialized['count'], len(before['contacts']) + 1)
                self.assertEqual(initialized['warnings'], read['warnings'])
                self.assertEqual(initialized['coverage'], read['coverage'])
                self.assertFalse(initialized['coverage']['complete'])


class BatchDriver:
    """Records fake operations, checking the persisted attempt boundary."""
    def __init__(self, store, expected, calls):
        self.store, self.expected, self.calls = store, expected, calls
        self.current = None

    def record(self, operation, job):
        if clean_job(job) != self.expected[job['id']]:
            raise AssertionError('Fixture message content, recipient set, or time changed')
        self.calls.append((operation, job['id']))

    def prepare(self, job):
        if self.current is not None:
            raise AssertionError('Previous fake message has not completed')
        self.current = job
        if self.store.get(job['id'])['status'] != 'preparing':
            raise AssertionError('Fake preparation must be recorded before driver input')
        self.record('prepare', job)

    def verify(self, job):
        self.record('verify', job)

    def checkpoint(self):
        self.calls.append(('checkpoint', self.current['id']))

    def submit(self, job):
        if self.store.get(job['id'])['status'] != 'submitting' or not self.store.has_attempt(signature(job)):
            raise AssertionError('Attempt must persist before the fake submit operation')
        self.record('submit', job)

    def verify_result(self, job):
        self.record('result', job)
        self.current = None
        return True, 'Fixture-only confirmed result; no real UI or transmission'


class AutomaticBatchTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch('app.core.now', return_value=datetime(2026, 10, 2, 12))
        self.clock.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.profile = fixture_profile()
        from app.verification import RESULT_ROLES
        for role in RESULT_ROLES:
            self.profile['roles'][role] = copy.deepcopy(self.profile['roles']['title'])
        self.store.set_setting('profile', self.profile)
        self.runner = None

    def tearDown(self):
        if self.runner and self.runner.thread:
            self.runner.thread.join(5)
            self.assertFalse(self.runner.thread.is_alive())
        self.store.close()
        self.tmp.cleanup()
        self.clock.stop()

    def message(self, index=0, extra_evidence=False):
        ids = ['fixture-a'] if index % 2 == 0 else ['fixture-a', 'fixture-b']
        bindings = {key: copy.deepcopy(self.profile['contacts'][key]) for key in ids}
        if not extra_evidence:
            bindings = {key: {field: value[field] for field in ('selector', 'expected')} for key, value in bindings.items()}
        return self.store.save({'id': f'fixture-job-{index:02}',
                                'scheduled': (datetime(2026, 10, 3, 9, 5) + timedelta(minutes=index)).strftime('%Y-%m-%d %H:%M'),
                                'recipient': ' · '.join(ids), 'recipient_ids': ids, 'recipient_bindings': bindings,
                                'title': f'Fixture 안내 {index:02}', 'body': f'첫째 줄 {index}\n\n이모지 🚀\n마지막 줄',
                                'attachments': []})

    def test_forty_mixed_single_and_multi_jobs_register_in_order_with_fake_driver(self):
        jobs = [self.message(index) for index in range(40)]
        expected = {job['id']: copy.deepcopy(job) for job in jobs}
        calls = []
        driver = BatchDriver(self.store, expected, calls)
        factory = Mock(return_value=driver)
        self.runner = Runner(self.store, factory, contextlib.nullcontext)
        plan = self.runner.plan([job['id'] for job in jobs], 'register', 'automatic')
        self.assertEqual(plan['errors'], [])
        self.runner.start(plan['id'], '예약 등록')
        self.runner.thread.join(5)
        self.assertFalse(self.runner.thread.is_alive())
        self.assertFalse(self.runner.busy())
        self.assertEqual(self.runner.snapshot()['stage'], 'done')
        self.assertEqual(self.runner.snapshot()['progress'], 40)
        factory.assert_called_once()
        expected_calls = [(operation, job['id']) for job in jobs
                          for operation in ('prepare', 'verify', 'checkpoint', 'submit', 'result')]
        self.assertEqual(calls, expected_calls)
        for job in jobs:
            stored = self.store.get(job['id'])
            self.assertEqual(clean_job(stored), job)
            self.assertEqual(stored['status'], 'confirmed')
            self.assertTrue(self.store.has_attempt(signature(job)))
        rows = self.store.db.execute('SELECT job_id,status FROM attempts').fetchall()
        self.assertEqual(len(rows), 40)
        self.assertTrue(all(row['status'] == 'confirmed' for row in rows))
        self.assertEqual(self.store.setting('profile'), self.profile)
        repeated = self.runner.plan([job['id'] for job in jobs], 'register')
        self.assertEqual(len(repeated['errors']), 40)

    def test_frozen_binding_metadata_is_allowed_but_changed_selector_is_blocked(self):
        job = self.message(index=1, extra_evidence=True)
        self.assertIn('display_name', job['recipient_bindings']['fixture-a'])
        self.assertIn('account', job['recipient_bindings']['fixture-a'])
        factory = Mock(side_effect=AssertionError('Planning must not construct a native driver'))
        self.runner = Runner(self.store, factory, contextlib.nullcontext)
        self.assertEqual(self.runner.plan([job['id']], 'register')['errors'], [])
        changed = copy.deepcopy(self.profile)
        changed['contacts']['fixture-a']['selector']['element']['name'] = '(FixtureOther(999))학교/부서/999'
        self.store.set_setting('profile', changed)
        plan = self.runner.plan([job['id']], 'register')
        self.assertTrue(any('초안 저장 후 변경' in error for item in plan['errors'] for error in item['errors']))
        factory.assert_not_called()


class AutomaticInitializationApiTests(unittest.TestCase):
    def test_initialize_endpoint_saves_merged_profile_and_preserves_coverage_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory))
            try:
                original = fixture_profile()
                service.store.set_setting('profile', original)
                service.runner.driver_factory = Mock(side_effect=AssertionError('No native UI'))
                read = inventory()
                with patch('app.gentoo.inspect_contacts', return_value=read):
                    result = service.dispatch('/api/gentoo/initialize', {})
                self.assertTrue(result['ok'])
                self.assertEqual(result['count'], 3)
                self.assertEqual(result['warnings'], read['warnings'])
                self.assertEqual(result['coverage'], read['coverage'])
                self.assertFalse(result['coverage']['complete'])
                saved = service.store.setting('profile')
                self.assertTrue(is_gentoo_profile(saved))
                self.assertEqual(saved['contacts']['fixture-a'], original['contacts']['fixture-a'])
                self.assertEqual(saved['contacts']['fresh-c'], read['contacts']['fresh-c'])
                self.assertEqual(service.store.list_jobs(), [])
                self.assertEqual(service.store.events(), [])
                service.runner.driver_factory.assert_not_called()
            finally:
                service.store.close()


if __name__ == '__main__':
    unittest.main()
