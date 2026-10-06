"""Optional titles across input/storage/compose; all controls are synthetic."""
import copy
import io
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from xml.sax.saxutils import escape

from app.batch import BatchService, table_file
from app.core import KST, Store, clean_job, export_csv, signature, validate_job
from app.gentoo import automatic_profile
from app.importers import import_file
from app.windows import AutomationError, WindowsDriver, title_matches
from app.workflows import WorkflowService


def message(**changes):
    raw = {'recipient': '합성교사', 'scheduled': '2026-10-20 09:22', 'body': '합성 본문', 'attachments': []}
    raw.update(changes)
    return clean_job(raw)


def titleless_xlsx():
    rows = [['받는사람', '예약날짜', '예약시간', '내용'],
            ['합성교사', '2026-10-20', '09:22', '합성 본문']]
    xml_rows = []
    for index, values in enumerate(rows, 1):
        cells = ''.join(f'<c r="{chr(65+column)}{index}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
                        for column, value in enumerate(values))
        xml_rows.append(f'<row r="{index}">{cells}</row>')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="예약목록" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(xml_rows)+'</sheetData></worksheet>')
    return output.getvalue()


class OptionalTitleValidationTests(unittest.TestCase):
    def test_missing_empty_and_whitespace_title_stay_empty(self):
        for raw in ({}, {'title': ''}, {'title': '   '}):
            job = message(**raw)
            self.assertEqual(job['title'], '')
            self.assertEqual(validate_job(job, current=datetime(2026,10,6,9)), [])
            self.assertEqual(job['body'], '합성 본문')

    def test_body_recipient_time_limits_and_control_characters_remain_validated(self):
        for changes in ({'body': ''}, {'recipient': ''}, {'title': 'x'*201},
                        {'body': 'x'*30001}, {'title': 'a\tb'}, {'title': 'a\x00b'}):
            with self.subTest(changes=list(changes)):
                self.assertTrue(validate_job(message(**changes), current=datetime(2026,10,6,9)))
        self.assertTrue(validate_job(message(scheduled='2026-10-06 09:01'), current=datetime(2026,10,6,9)))

    def test_missing_and_blank_title_have_same_fingerprint_but_body_changes_do_not(self):
        self.assertEqual(signature(message()), signature(message(title='')))
        self.assertNotEqual(signature(message()), signature(message(body='다른 합성 본문')))
        self.assertNotEqual(signature(message()), signature(message(title='명시한 제목')))

    def test_csv_without_title_column_is_supported_by_both_import_paths(self):
        for header, values in (
            ('받는사람,예약날짜,예약시간,내용', '합성교사,2026-10-20,09:22,합성 본문'),
            ('예약일시,받는사람,내용', '2026-10-20 09:22,합성교사,합성 본문')):
            data = (header+'\n'+values).encode()
            rows = table_file('fixture.csv', data)
            self.assertEqual(rows[0]['내용'], '합성 본문')
            jobs, errors = import_file('fixture.csv', data)
            self.assertEqual(errors, [])
            self.assertEqual(jobs[0]['title'], '')
            self.assertEqual(jobs[0]['body'], '합성 본문')

    def test_xlsx_without_title_column_is_supported_by_both_import_paths(self):
        data = titleless_xlsx()
        self.assertEqual(table_file('fixture.xlsx', data)[0]['내용'], '합성 본문')
        jobs, errors = import_file('fixture.xlsx', data)
        self.assertEqual(errors, [])
        self.assertEqual((jobs[0]['title'], jobs[0]['body']), ('', '합성 본문'))

    def test_export_and_reimport_keep_title_empty(self):
        jobs, errors = import_file('fixture.csv', export_csv([message()]))
        self.assertEqual(errors, [])
        self.assertEqual(jobs[0]['title'], '')
        self.assertEqual(signature(jobs[0]), signature(message()))

    def test_missing_body_recipient_or_time_header_still_blocks(self):
        for data in ('받는사람,예약일시\n합성교사,2026-10-20 09:22',
                     '내용,예약일시\n본문,2026-10-20 09:22', '받는사람,내용\n합성교사,본문'):
            for importer in (table_file, import_file):
                with self.subTest(importer=importer.__name__), self.assertRaises(ValueError):
                    importer('fixture.csv', data.encode())


class OptionalTitleStorageTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch('app.core.now', return_value=datetime(2026,10,6,9))
        self.clock.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.profile = {'contacts': {'fixture': {'expected': '합성교사(901)',
                         'display_name': '합성교사', 'selector': {'exe': 'C:/SyntheticOnly/CoolMessenger.exe'}}}}
        self.store.set_setting('profile', self.profile)
        self.batch = BatchService(self.store)
        self.workflow = WorkflowService(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        self.clock.stop()

    def test_titleless_batch_commits_once_and_duplicate_remains_blocked(self):
        rows = table_file('fixture.xlsx', titleless_xlsx())
        review = self.batch.preview(rows)
        self.assertTrue(review['can_commit'], review['rows'])
        saved = self.batch.commit(review['review_id'])
        job = self.store.get(saved['job_ids'][0])
        self.assertEqual(job['title'], '')
        self.assertFalse(self.batch.preview(rows)['can_commit'])
        self.store.begin_submit(job)
        self.store.finish_submit(job, 'needs_review', '합성 등록 요청')
        self.assertFalse(self.batch.preview(rows)['can_commit'])

    def test_general_default_template_without_title_column_commits_empty_title(self):
        source = {'source': {'source_id': 'titleless-fixture'}, 'values': [
            ['업무일', '담당자', '내용'], ['2026-10-20', '합성교사', '합성 본문']]}
        recipe = {'preset': 'general', 'contact_mapping': {'합성교사': 'fixture'}}
        result = self.workflow.preview(source, recipe, '2026-10-20', '2026-10-20')
        self.assertTrue(result['can_commit'], result['blocked'])
        self.assertEqual(result['candidates'][0]['job']['title'], '')
        self.assertEqual(self.workflow.commit(result['preview_id'])['saved'], 1)

    def test_explicit_blank_template_overrides_nonempty_source_title(self):
        source = {'source': {'source_id': 'titleless-fixture'}, 'values': [
            ['업무일', '담당자', '제목', '내용'], ['2026-10-20', '합성교사', '원본 제목', '합성 본문']]}
        recipe = {'preset': 'general', 'contact_mapping': {'합성교사': 'fixture'}, 'title_template': ''}
        saved_recipe = self.workflow.save_recipe(recipe)
        self.assertEqual(saved_recipe['title_template'], '')
        result = self.workflow.preview(source, saved_recipe, '2026-10-20', '2026-10-20')
        self.assertTrue(result['can_commit'], result['blocked'])
        self.assertEqual(result['candidates'][0]['job']['title'], '')

    def test_lunch_two_recipient_message_can_keep_an_empty_title(self):
        profile = copy.deepcopy(self.profile)
        profile['contacts']['second'] = {'expected': '합성다른교사(902)',
                                         'selector': {'exe': 'C:/SyntheticOnly/CoolMessenger.exe'}}
        self.store.set_setting('profile', profile)
        source = {'source': {'source_id': 'titleless-lunch'}, 'values': [
            ['날짜', '최종_중식1', '최종_중식2', '제외일', '고사(1차만)'],
            ['2026-10-20', '합성교사', '합성다른교사', False, False]]}
        recipe = {'preset': 'lunch', 'header_rows': 1, 'title_template': '',
                  'contact_mapping': {'합성교사': 'fixture', '합성다른교사': 'second'}}
        result = self.workflow.preview(source, recipe, '2026-10-20', '2026-10-20')
        self.assertTrue(result['can_commit'], result['blocked'])
        job = result['candidates'][0]['job']
        self.assertEqual(job['title'], '')
        self.assertEqual(set(job['recipient_ids']), {'fixture', 'second'})
        self.assertIn('1차:', job['body'])
        self.assertIn('2차:', job['body'])

    def test_empty_body_template_and_non_string_title_template_remain_invalid(self):
        for changes in ({'body_template': ''}, {'body_template': '  '}, {'title_template': None}, {'title_template': []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.workflow.save_recipe({'preset': 'general', **changes})


class OptionalTitleNativeTests(unittest.TestCase):
    def driver(self):
        profile = automatic_profile({'exe': 'C:/SyntheticOnly/CoolMessenger.exe'})
        driver = WindowsDriver(profile, threading.Event())
        driver.checkpoint = Mock()
        driver.focus = Mock()
        driver.wait = Mock()
        return driver

    def test_placeholder_alias_is_only_enabled_for_known_gentoo_composer(self):
        self.assertTrue(title_matches('', ''))
        self.assertTrue(title_matches('제목을 입력하세요.', '', gentoo_placeholder=True))
        self.assertFalse(title_matches('제목을 입력하세요.', ''))
        for actual in (None, '다른 제목', '제목 없음', '제목을 입력하세요', '제목을 입력하세요. 추가'):
            self.assertFalse(title_matches(actual, '', gentoo_placeholder=True))

    def test_empty_native_input_is_written_and_only_exact_placeholder_is_accepted(self):
        driver = self.driver()
        editor = SimpleNamespace(element_info=SimpleNamespace(control_type='Edit'), set_edit_text=Mock())
        driver.role = Mock(return_value=editor)
        for actual in ('', '제목을 입력하세요.'):
            driver.text = Mock(return_value=actual)
            driver.put('title', '')
        self.assertEqual(editor.set_edit_text.call_args_list, [call(''), call('')])
        driver.text = Mock(return_value='기존 제목')
        with self.assertRaises(AutomationError):
            driver.put('title', '')

    def test_fallback_clear_deletes_selection_instead_of_typing_empty_string(self):
        driver = self.driver()
        editor = SimpleNamespace(element_info=SimpleNamespace(control_type='Edit'),
                                 set_edit_text=Mock(side_effect=RuntimeError('synthetic')), type_keys=Mock())
        driver.role = Mock(return_value=editor)
        driver.text = Mock(return_value='')
        driver.put('title', '')
        self.assertEqual([entry.args[0] for entry in editor.type_keys.call_args_list], ['^a', '{BACKSPACE}'])

    def test_new_titleless_composer_writes_empty_title_and_original_body(self):
        driver = self.driver()
        driver.open_compose = Mock(return_value=False)
        driver.put = Mock()
        driver.set_schedule = Mock()
        driver.sync_self_recipient = Mock()
        driver.attach = Mock()
        driver.verify = Mock()
        job = message()
        with patch('app.windows.datetime') as clock:
            clock.now.return_value = datetime(2026,10,6,9,tzinfo=KST)
            driver.prepare(job)
        self.assertEqual(driver.put.call_args_list, [call('title', ''), call('body', job['body'])])
        driver.verify.assert_called_once_with(job)

    def test_reused_placeholder_is_explicitly_cleared_without_overwriting_body(self):
        driver = self.driver()
        driver.open_compose = Mock(return_value=True)
        driver.text = Mock(return_value='제목을 입력하세요.')
        driver.put = Mock()
        driver.set_schedule = Mock()
        driver.sync_self_recipient = Mock()
        driver.attach = Mock()
        driver.verify = Mock()
        with patch('app.windows.datetime') as clock:
            clock.now.return_value = datetime(2026,10,6,9,tzinfo=KST)
            driver.prepare(message())
        driver.put.assert_called_once_with('title', '')
        driver.attach.assert_not_called()

    def test_existing_titleless_composer_reuse_requires_exact_body_and_account(self):
        driver = self.driver()
        driver.roots = Mock(return_value=[object()])
        driver.verify_recipient = Mock()
        driver.verify_attachments = Mock()
        values = {'title': '제목을 입력하세요.', 'body': '합성 본문', 'cc_read': ''}
        driver.text = Mock(side_effect=values.get)
        self.assertTrue(driver.open_compose(message()))
        values['body'] = '다른 본문'
        with self.assertRaises(AutomationError):
            driver.open_compose(message())
        values['body'] = '합성 본문'
        driver.verify_recipient.side_effect = AutomationError('수신자 불일치')
        with self.assertRaises(AutomationError):
            driver.open_compose(message())

    def test_titleless_final_verification_keeps_body_and_account_checks(self):
        driver = self.driver()
        driver.verify_recipient = Mock()
        driver.verify_schedule = Mock()
        driver.verify_attachments = Mock()
        values = {'title': '제목을 입력하세요.', 'body': '합성 본문', 'cc_read': ''}
        driver.text = Mock(side_effect=values.get)
        driver.verify(message())
        values['title'] = '다른 제목'
        with self.assertRaises(AutomationError):
            driver.verify(message())
        values.update(title='', body='다른 본문')
        with self.assertRaises(AutomationError):
            driver.verify(message())
        self.assertEqual(driver.verify_recipient.call_count, 3)


if __name__ == '__main__':
    unittest.main()
