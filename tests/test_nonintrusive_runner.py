"""Read-only runner inspections with recording drivers and temporary databases."""
import contextlib
import copy
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core import Store, clean_job, now
from app.engine import Runner


def fixture_profile(**changes):
    selector = {'exe': 'C:\\FixtureOnly\\CoolMessenger.exe'}
    profile = {'tested': False, 'tested_multi': False,
               'roles': {role: dict(selector) for role in
                         ('title', 'body', 'recipient_read', 'scheduled_check', 'send', 'datetime')},
               'contacts': {'fixture-person': {'expected': 'FixturePerson(999)(Fixture)', 'selector': dict(selector)}}}
    profile.update(changes)
    return profile


class LegacyReader:
    def __init__(self, calls, failure=None, entered=None, release=None):
        self.calls = calls
        self.failure = failure
        self.entered = entered
        self.release = release

    def _read(self, name, job):
        self.calls.append((name, job['id']))
        if self.entered:
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError('Fixture reader release timed out')
        if self.failure:
            raise self.failure

    def verify(self, job):
        self._read('verify', job)

    def prepare(self, job):
        raise AssertionError('Read-only inspection must not prepare a message')

    def submit(self, job):
        raise AssertionError('Read-only inspection must not submit a message')

    def verify_result(self, job):
        raise AssertionError('Read-only inspection must not query a registration result')


class ExistingReader(LegacyReader):
    def verify_existing(self, job):
        self._read('verify_existing', job)


class NonintrusiveRunnerTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch('app.core.now', return_value=datetime(2026, 10, 1, 12))
        self.clock.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.store.set_setting('profile', fixture_profile())
        self.calls = []
        self.context_calls = []
        self.runner = None

    def tearDown(self):
        if self.runner and self.runner.thread:
            self.runner.thread.join(4)
            self.assertFalse(self.runner.thread.is_alive(), 'Fixture runner must finish before the database closes')
        self.store.close()
        self.tmp.cleanup()
        self.clock.stop()

    def message(self, title='Fixture message'):
        return self.store.save(clean_job({'scheduled': (now() + timedelta(days=1)).strftime('%Y-%m-%d %H:%M'),
                                         'recipient': 'fixture-person', 'title': title,
                                         'body': '첫째 줄\n\n둘째 줄 🚀', 'attachments': []}))

    def persisted(self):
        return {table: [tuple(row) for row in self.store.db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('jobs', 'settings', 'events', 'attempts')}

    def create_runner(self, reader_type=ExistingReader, **reader_options):
        @contextlib.contextmanager
        def context():
            self.context_calls.append('enter')
            try:
                yield
            finally:
                self.context_calls.append('exit')

        def factory(profile, stop):
            self.calls.append(('factory', copy.deepcopy(profile)))
            return reader_type(self.calls, **reader_options)
        self.runner = Runner(self.store, factory, context)
        return self.runner

    def execute(self, job, mode='inspect'):
        plan = self.runner.plan([job['id']], mode)
        self.assertEqual(plan['errors'], [])
        self.runner.start(plan['id'], '')
        self.runner.thread.join(4)
        self.assertFalse(self.runner.thread.is_alive())
        self.assertFalse(self.runner.busy())
        state = self.runner.snapshot()
        self.assertIsInstance(state['elapsed_ms'], int)
        self.assertGreaterEqual(state['elapsed_ms'], 0)
        return state

    def test_existing_reader_success_preserves_every_persisted_field_and_readiness(self):
        for tested, tested_multi in ((False, False), (True, False), (True, True)):
            with self.subTest(tested=tested, tested_multi=tested_multi):
                self.store.set_setting('profile', fixture_profile(tested=tested, tested_multi=tested_multi))
                job = self.message(f'Fixture {tested}-{tested_multi}')
                self.store.status(job['id'], 'prepared', 'Existing fixture preparation')
                before = self.persisted()
                self.calls.clear()
                self.context_calls.clear()
                self.create_runner()
                state = self.execute(job)
                self.assertEqual(self.persisted(), before)
                self.assertEqual([call[0] for call in self.calls], ['factory', 'verify_existing'])
                self.assertEqual(self.context_calls, ['enter', 'exit'])
                self.assertEqual(state['progress'], 1)
                self.assertEqual(state['mode'], 'inspect')
                self.assertEqual(state['stage'], 'done')
                self.assertEqual(state['report']['passed'], True)
                self.assertEqual(state['report']['read_only'], True)
                self.assertEqual(state['report']['job_id'], job['id'])
                self.assertEqual(set(state['report']['checks']),
                                 {'recipient', 'title', 'body', 'scheduled'})

    def test_optional_readers_are_reported_only_when_they_are_connected(self):
        for optional_roles in (('cc_read',), ('attach_list',), ('cc_read', 'attach_list')):
            with self.subTest(readers=optional_roles):
                profile = fixture_profile()
                for role in optional_roles:
                    profile['roles'][role] = {'exe': 'C:\\FixtureOnly\\CoolMessenger.exe',
                                               'reader': 'fixture-read-only'}
                self.store.set_setting('profile', profile)
                job = self.message('Fixture optional ' + '-'.join(optional_roles))
                before = self.persisted()
                self.calls.clear()
                self.context_calls.clear()
                self.create_runner()
                state = self.execute(job)
                expected = {'recipient', 'title', 'body', 'scheduled'}
                if 'cc_read' in optional_roles:
                    expected.add('cc')
                if 'attach_list' in optional_roles:
                    expected.add('attachments')
                self.assertEqual(set(state['report']['checks']), expected)
                self.assertEqual(state['report']['passed'], True)
                self.assertEqual(self.persisted(), before)
                self.assertEqual([call[0] for call in self.calls], ['factory', 'verify_existing'])

    def test_read_failure_keeps_job_status_and_never_authorizes_input_testing(self):
        job = self.message()
        self.store.status(job['id'], 'prepared', 'Already prepared fixture')
        before = self.persisted()
        self.create_runner(failure=RuntimeError('Fixture read mismatch'))
        state = self.execute(job)
        self.assertEqual(self.persisted(), before)
        self.assertEqual([call[0] for call in self.calls], ['factory', 'verify_existing'])
        self.assertEqual(self.context_calls, ['enter', 'exit'])
        self.assertEqual(state['stage'], 'failed')
        self.assertEqual({k: v for k, v in state['report'].items() if k != 'diagnostic'}, {'passed': False, 'read_only': True,
                                              'job_id': job['id'], 'error': '처리를 중단했습니다. 입력값과 쿨메신저 상태를 확인하세요.'})
        self.assertEqual(state['report']['diagnostic']['exception'], 'RuntimeError')
        self.assertEqual(state['report']['diagnostic']['frames'][-1]['file'], 'app/engine.py')
        self.assertNotIn('Fixture read mismatch', '\n'.join(state['log']))
        self.assertIn('중단', '\n'.join(state['log']))

    def test_two_recipients_in_one_job_do_not_gain_multi_input_readiness(self):
        profile = fixture_profile()
        profile['contacts']['fixture-person-b'] = copy.deepcopy(profile['contacts']['fixture-person'])
        profile['contacts']['fixture-person-b']['expected'] = 'FixturePersonB(998)(Fixture)'
        profile['multi_select'] = {'kind': 'fixture-only'}
        self.store.set_setting('profile', profile)
        job = self.message()
        job['recipient_ids'] = ['fixture-person', 'fixture-person-b']
        job['recipient_bindings'] = {key: copy.deepcopy(profile['contacts'][key]) for key in job['recipient_ids']}
        job = self.store.save(job)
        before = self.persisted()
        self.create_runner()
        self.execute(job)
        self.assertEqual(self.persisted(), before)
        self.assertFalse(self.store.setting('profile')['tested_multi'])
        self.assertEqual([call[0] for call in self.calls], ['factory', 'verify_existing'])

    def test_inspection_does_not_require_write_controls_or_contact_picker(self):
        profile = fixture_profile()
        del profile['roles']['send']
        del profile['contacts']['fixture-person']['selector']
        profile['contacts']['fixture-person-b'] = {'expected': 'FixturePersonB(998)(Fixture)'}
        self.store.set_setting('profile', profile)
        job = self.message()
        job['recipient_ids'] = ['fixture-person', 'fixture-person-b']
        job = self.store.save(job)
        before = self.persisted()
        self.create_runner()
        self.execute(job)
        self.assertEqual(self.persisted(), before)
        self.assertEqual([call[0] for call in self.calls], ['factory', 'verify_existing'])

    def test_uncertain_registration_record_can_be_read_without_altering_its_attempt(self):
        job = self.message()
        self.store.begin_submit(job)
        self.store.finish_submit(job, 'uncertain', 'Fixture local attempt marker')
        before = self.persisted()
        self.create_runner()
        self.execute(job)
        self.assertEqual(self.persisted(), before)
        self.assertEqual(self.store.get(job['id'])['status'], 'uncertain')

    def test_legacy_driver_uses_verify_without_input_or_registration(self):
        job = self.message()
        before = self.persisted()
        self.create_runner(LegacyReader)
        state = self.execute(job)
        self.assertEqual(self.persisted(), before)
        self.assertEqual([call[0] for call in self.calls], ['factory', 'verify'])
        self.assertEqual(state['progress'], 1)

    def test_inspection_rejects_two_selected_jobs_before_constructing_driver(self):
        jobs = [self.message('Fixture A'), self.message('Fixture B')]
        self.create_runner()
        with self.assertRaises(ValueError):
            self.runner.plan([job['id'] for job in jobs], 'inspect')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.context_calls, [])

    def test_invalid_profile_is_reported_in_plan_and_prevents_start(self):
        job = self.message()
        self.store.set_setting('profile', {'roles': {}, 'contacts': {}, 'tested': False})
        before = self.persisted()
        self.create_runner()
        inspect = self.runner.plan([job['id']], 'inspect')
        self.assertTrue(inspect['errors'])
        self.assertTrue(any('연결 필요' in error for item in inspect['errors'] for error in item['errors']))
        with self.assertRaises(ValueError):
            self.runner.start(inspect['id'], '')
        self.assertEqual(self.persisted(), before)
        self.assertEqual(self.calls, [])

    def test_busy_inspection_rejects_second_start_without_mutating_job(self):
        job = self.message()
        before = self.persisted()
        entered, release = threading.Event(), threading.Event()
        self.create_runner(entered=entered, release=release)
        first = self.runner.plan([job['id']], 'inspect')
        second = self.runner.plan([job['id']], 'inspect')
        self.assertEqual(first['errors'], [])
        self.runner.start(first['id'], '')
        try:
            self.assertTrue(entered.wait(2), 'Fixture inspection did not start')
            self.assertTrue(self.runner.busy())
            self.assertEqual(self.runner.snapshot()['stage'], 'reading')
            with self.assertRaisesRegex(ValueError, '실행 중'):
                self.runner.start(second['id'], '')
        finally:
            release.set()
            self.runner.thread.join(4)
        self.assertEqual(self.persisted(), before)
        self.assertEqual([call[0] for call in self.calls], ['factory', 'verify_existing'])

    def test_simulation_never_constructs_driver_context_or_performs_artificial_wait(self):
        jobs = [self.message('Simulate fixture A'), self.message('Simulate fixture B')]
        self.store.set_setting('profile', {'roles': {}, 'contacts': {}, 'tested': False})
        before = self.persisted()
        self.create_runner()
        plan = self.runner.plan([job['id'] for job in jobs], 'simulate')
        self.assertEqual(plan['errors'], [])
        with patch.object(self.runner.stop, 'wait', side_effect=AssertionError('Simulation must not add an artificial delay')) as wait:
            self.runner.start(plan['id'], '')
            self.runner.thread.join(4)
        self.assertFalse(self.runner.thread.is_alive())
        self.assertEqual(self.runner.snapshot()['progress'], 2)
        self.assertEqual(self.persisted(), before)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.context_calls, [])
        wait.assert_not_called()

    def test_profile_change_after_review_rejects_inspection_before_native_read(self):
        job = self.message()
        self.create_runner()
        plan = self.runner.plan([job['id']], 'inspect')
        changed = fixture_profile()
        changed['contacts']['fixture-person']['expected'] = 'ChangedFixture(111)(Fixture)'
        self.store.set_setting('profile', changed)
        before = self.persisted()
        with self.assertRaisesRegex(ValueError, '화면 연결이 변경'):
            self.runner.start(plan['id'], '')
        self.assertEqual(self.persisted(), before)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
