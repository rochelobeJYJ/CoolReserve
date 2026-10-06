"""Synthetic archive and recipient-list contracts; no native UI or live API."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.core import Store, signature
from app.recipients import equivalent_attempt
from app.workflows import WorkflowService
from server import Service


def contact(name, number):
    return {'display_name': name, 'expected': f'{name}({number})', 'verified': True,
            'selector': {'exe': 'C:\\SyntheticOnly\\CoolMessenger.exe', 'element': {'name': name}}}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = patch('app.core.now', return_value=datetime(2026, 10, 6, 12))
        self.clock.start()
        self.service = Service(Path(self.tmp.name), offline=True)
        self.store = self.service.store

    def tearDown(self):
        self.store.close()
        self.clock.stop()
        self.tmp.cleanup()

    def job(self, name, status='draft', scheduled='2026-10-05 09:22'):
        job = self.store.save({'id': name, 'scheduled': scheduled, 'recipient': 'synthetic',
                               'title': name, 'body': '합성 본문', 'attachments': []})
        if status in {'confirmed', 'needs_review', 'uncertain', 'submitting'}:
            self.store.begin_submit(job)
            if status != 'submitting':
                self.store.finish_submit(job, status, '합성 상태')
        elif status != 'draft':
            self.store.status(job['id'], status, '합성 상태')
        return self.store.get(job['id'])

    def test_one_click_hides_only_eligible_past_states_and_preserves_ledgers(self):
        states = ['confirmed', 'needs_review', 'draft', 'failed', 'uncertain', 'submitting', 'preparing', 'prepared']
        for state in states:
            self.job(state, state)
        self.job('earlier-today', 'confirmed', '2026-10-06 09:22')
        self.job('tomorrow', 'needs_review', '2026-10-07 09:22')
        jobs_before = self.store.list_jobs()
        ledger_before = [tuple(row) for row in self.store.db.execute('SELECT * FROM attempts ORDER BY fingerprint')]
        events_before = self.store.events()
        self.assertEqual(self.service.state()['past_cleanup_count'], 4)
        result = self.service.dispatch('/api/archive-past', {})
        self.assertEqual(set(result['job_ids']), {'confirmed', 'needs_review', 'draft', 'failed'})
        state = self.service.state()
        self.assertEqual((state['archived_count'], state['past_cleanup_count']), (4, 0))
        self.assertEqual({job['id'] for job in state['jobs']}, set(states[4:] + ['earlier-today', 'tomorrow']))
        self.assertEqual(self.store.list_jobs(), jobs_before)
        self.assertEqual([tuple(row) for row in self.store.db.execute('SELECT * FROM attempts ORDER BY fingerprint')], ledger_before)
        self.assertEqual(self.store.events(), events_before)
        self.assertEqual(self.service.dispatch('/api/archive-past', {})['archived'], 0)

    def test_archive_and_restore_are_atomic_and_idempotent(self):
        old = self.job('old', 'confirmed')
        self.job('future', 'confirmed', '2026-10-07 09:22')
        for ids in (['old', 'future'], ['old', 'unknown'], ['old', 'old'], [], 'old', [True]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.store.archive_past(ids)
            self.assertEqual(self.store.list_archived(), [])
        self.assertEqual(self.store.archive_past(['old'])['archived'], 1)
        self.assertEqual(self.store.archive_past(['old'])['archived'], 0)
        archived = self.service.dispatch('/api/archives', {})['jobs']
        self.assertEqual(archived[0]['id'], 'old')
        self.assertIn('archived_at', archived[0])
        with self.assertRaises(ValueError):
            self.store.restore_archived(['old', 'unknown'])
        self.assertEqual(len(self.store.list_archived()), 1)
        self.assertEqual(self.service.dispatch('/api/restore-archived', {'ids': ['old']})['restored'], 1)
        self.assertEqual(self.store.restore_archived(['old'])['restored'], 0)
        self.assertEqual(self.store.get('old'), old)

    def test_hidden_submitted_job_stays_locked_and_blocks_equivalent_reimport(self):
        p = {'contacts': {'a': contact('합성가', 9001)}}
        self.store.set_setting('profile', p)
        old = self.store.save({'id': 'submitted', 'scheduled': '2026-10-05 09:22', 'recipient': 'a',
                              'title': '등록 이력', 'body': '보존 본문'})
        self.store.begin_submit(old)
        self.store.finish_submit(old, 'confirmed', '합성 확인')
        self.store.archive_past()
        replay = dict(old, id='another', recipient='합성가', recipient_ids=['a'],
                      recipient_bindings={'a': {'selector': p['contacts']['a']['selector'], 'expected': p['contacts']['a']['expected']}})
        self.assertTrue(equivalent_attempt(self.store, replay, p))
        self.assertTrue(self.store.has_attempt(signature(old)))
        with self.assertRaises(ValueError):
            self.store.save(dict(old, body='수정 본문'))
        with self.assertRaises(ValueError):
            self.store.remove(['submitted'])

    def test_archives_persist_when_store_reopens(self):
        self.job('past', 'needs_review')
        self.store.archive_past()
        self.store.close()
        self.store = Store(Path(self.tmp.name))
        self.service.store = self.store
        self.assertEqual(self.store.list_jobs(include_archived=False), [])
        self.assertEqual(self.store.list_archived()[0]['status'], 'needs_review')
        self.assertEqual(len(self.store.list_jobs()), 1)

    def test_archiving_keeps_workflow_provenance_and_reread_does_not_recreate_it(self):
        workflow = WorkflowService(self.store)
        self.store.set_setting('profile', {'contacts': {'a': contact('합성가', 9001)}})
        source = {'source': {'source_id': 'synthetic-archive-workflow'}, 'values': [
            ['업무일', '담당자', '제목', '내용'], ['2026-10-05', '합성가', '업무 안내', '합성 내용']]}
        recipe = {'preset': 'general', 'contact_mapping': {'합성가': 'a'}}
        with patch('app.core.now', return_value=datetime(2026, 10, 1, 12)):
            preview = workflow.preview(source, recipe, '2026-10-05', '2026-10-05')
            initial = workflow.commit(preview['preview_id'])
        before = [tuple(row) for row in self.store.db.execute('SELECT * FROM workflow_items')]
        self.store.archive_past()
        self.assertEqual([tuple(row) for row in self.store.db.execute('SELECT * FROM workflow_items')], before)
        with patch('app.core.now', return_value=datetime(2026, 10, 1, 12)):
            reread = workflow.preview(source, recipe, '2026-10-05', '2026-10-05')
            result = workflow.commit(reread['preview_id'])
        self.assertEqual((result['saved'], result['unchanged']), (0, 1))
        self.assertEqual(result['job_ids'], initial['job_ids'])
        self.assertEqual(self.store.list_jobs(include_archived=False), [])

    def test_editing_archived_draft_restores_it_without_losing_history(self):
        old = self.job('old-draft')
        self.store.archive_past()
        self.store.save(dict(old, scheduled='2026-10-08 09:22'))
        self.assertEqual(self.store.list_archived(), [])
        self.assertEqual(self.store.list_jobs(include_archived=False)[0]['scheduled'], '2026-10-08 09:22')

    def test_archived_history_does_not_consume_active_capacity_and_restore_is_bounded(self):
        self.job('old')
        self.store.archive_past()
        jobs = [{'id': f'new-{index}', 'scheduled': '2026-10-08 09:22', 'recipient': 'synthetic',
                 'title': f'새 메시지 {index}', 'body': '합성'} for index in range(500)]
        self.store.save_batch(jobs)
        self.assertEqual(len(self.store.list_jobs()), 501)
        with self.assertRaisesRegex(ValueError, '500'):
            self.store.restore_archived(['old'])
        with self.assertRaisesRegex(ValueError, '500'):
            self.store.save(dict(self.store.get('old'), scheduled='2026-10-09 09:22'))
        self.assertEqual(len(self.store.list_archived()), 1)
        self.store.remove(['new-0'])
        self.assertEqual(self.store.restore_archived(['old'])['restored'], 1)

    def test_mutation_is_blocked_while_runner_is_active(self):
        self.job('old')
        self.service.runner.state['active'] = True
        for path, body in (('/api/archive-past', {}), ('/api/restore-archived', {'ids': ['old']})):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, '실행 중'):
                self.service.dispatch(path, body)
        self.assertEqual(self.store.list_archived(), [])
        self.service.runner.state['active'] = False


class RecipientListTests(unittest.TestCase):
    def test_pasted_list_requires_all_names_to_resolve_before_any_ids_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), offline=True)
            try:
                p = {'contacts': {'a': contact('합성가', 9001), 'b': contact('동명이인', 9002),
                                  'c': contact('동명이인', 9003)}}
                service.store.set_setting('profile', p)
                result = service.dispatch('/api/recipients/resolve', {'names': '합성가\n동명이인'})
                self.assertFalse(result['can_apply'])
                self.assertEqual(result['ids'], [])
                self.assertEqual(result['bindings'], {})
                self.assertEqual(result['selections'][0]['selected_id'], 'a')
                resolved = service.dispatch('/api/recipients/resolve',
                    {'names': '합성가\n동명이인', 'choices': {'동명이인': 'c'}})
                self.assertTrue(resolved['can_apply'])
                self.assertEqual(resolved['ids'], ['a', 'c'])
                self.assertEqual(set(resolved['bindings']), {'a', 'c'})
                self.assertEqual(service.store.list_jobs(), [])
                self.assertEqual(service.store.setting('profile'), p)
                self.assertFalse(service.runner.busy())
            finally:
                service.store.close()

    def test_invalid_shapes_and_unmatched_names_do_not_grant_partial_recipients(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), offline=True)
            try:
                service.store.set_setting('profile', {'contacts': {'a': contact('합성가', 9001)}})
                for body in ({'names': None}, {'names': [1]}, {'names': '합성가', 'choices': []},
                             {'names': '합성가', 'choices': {'합성가': ['a']}}):
                    with self.subTest(body=body), self.assertRaises(ValueError):
                        service.dispatch('/api/recipients/resolve', body)
                result = service.dispatch('/api/recipients/resolve', {'names': ['합성가', '없는사람']})
                self.assertFalse(result['can_apply'])
                self.assertEqual(result['ids'], [])
            finally:
                service.store.close()


if __name__ == '__main__':
    unittest.main()
