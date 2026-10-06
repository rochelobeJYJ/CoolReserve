"""Synthetic release boundaries; no private fixtures or live Windows operations."""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools.check_public import (CONFIG, RECEIPT, PublicAuditError, audit, inspect_bytes,
                                load_allowlist, load_deny_file, text_findings)
from tools.build_public_release import build


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'
        self.root.mkdir()
        (self.root / 'README.md').write_text('Synthetic release documentation.\n', encoding='utf-8')
        self.configure(['README.md', CONFIG])

    def configure(self, files):
        (self.root / CONFIG).write_text(json.dumps({'schema_version': 1, 'files': files}), encoding='utf-8')

    def workbook(self, entries):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            for name, text in entries:
                archive.writestr(name, text)
        return stream.getvalue()

    def test_export_contains_only_allowlisted_files_and_verified_hashes(self):
        private = self.root / '.qa-data'
        private.mkdir()
        (private / 'session.json').write_text('PRIVATE FIXTURE', encoding='utf-8')
        target = self.base / 'public'
        result = build(self.root, target)
        self.assertTrue(result['passed'])
        self.assertTrue(audit(target, staging=True)['passed'])
        self.assertEqual({p.name for p in target.iterdir()}, {'README.md', CONFIG, RECEIPT})
        with zipfile.ZipFile(target.with_suffix('.zip')) as archive:
            self.assertEqual(set(archive.namelist()), {'README.md', CONFIG, RECEIPT})
        self.assertEqual((private / 'session.json').read_text(), 'PRIVATE FIXTURE')

    def test_private_marker_blocks_export_without_printing_value(self):
        marker = 'PrivateFixture' + 'Person'
        (self.root / 'README.md').write_text(marker, encoding='utf-8')
        target = self.base / 'public'
        result = build(self.root, target, deny=[marker])
        self.assertFalse(result['passed'])
        self.assertNotIn(marker, json.dumps(result))
        self.assertFalse(target.exists())
        self.assertFalse(target.with_suffix('.zip').exists())

    def test_unlisted_file_and_empty_git_directory_fail_staging(self):
        target = self.base / 'public'
        build(self.root, target)
        (target / 'extra.txt').write_text('unlisted', encoding='utf-8')
        self.assertFalse(audit(target, staging=True)['passed'])
        (target / 'extra.txt').unlink()
        (target / '.git').mkdir()
        self.assertFalse(audit(target, staging=True)['passed'])

    def test_modified_file_fails_manifest(self):
        target = self.base / 'public'
        build(self.root, target)
        (target / 'README.md').write_text('edited', encoding='utf-8')
        result = audit(target, staging=True)
        self.assertIn('fingerprint_mismatch', [p['type'] for p in result['problems']])

    def test_existing_output_is_never_overwritten(self):
        target = self.base / 'public'
        build(self.root, target)
        with self.assertRaises(PublicAuditError):
            build(self.root, target)
        self.assertTrue(audit(target, staging=True)['passed'])

    def test_version_dots_preserved_and_existing_version_zip_is_not_overwritten(self):
        target = self.base / 'CoolReserve-1.4.0-public'
        expected_zip = self.base / 'CoolReserve-1.4.0-public.zip'
        truncated_zip = self.base / 'CoolReserve-1.4.zip'
        truncated_zip.write_bytes(b'unrelated artifact')
        self.assertTrue(build(self.root, target)['passed'])
        self.assertEqual(truncated_zip.read_bytes(), b'unrelated artifact')
        with zipfile.ZipFile(expected_zip) as archive:
            self.assertEqual(set(archive.namelist()), {'README.md', CONFIG, RECEIPT})
        other = self.base / 'CoolReserve-1.4.1-public'
        other_zip = other.with_name(other.name + '.zip')
        other_zip.write_bytes(b'keep existing release')
        with self.assertRaises(PublicAuditError):
            build(self.root, other)
        self.assertFalse(other.exists())
        self.assertEqual(other_zip.read_bytes(), b'keep existing release')

    def test_allowlist_rejects_traversal_runtime_case_collision_and_binary(self):
        for name in ('../escape.py', '.git/config', 'output/notes.md', 'app/windows_v1_original.txt',
                     'session.json', 'capture.png'):
            with self.subTest(kind=name.split('/')[-1]):
                self.configure([name])
                with self.assertRaises(PublicAuditError):
                    load_allowlist(self.root)
        self.configure(['README.md', 'readme.md'])
        with self.assertRaises(PublicAuditError):
            load_allowlist(self.root)

    def test_missing_file_fails(self):
        self.configure(['absent.py'])
        self.assertEqual(audit(self.root)['problems'][0]['type'], 'missing_file')

    def test_linked_file_is_not_read(self):
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda p: p.name == 'README.md' or original(p)):
            result = audit(self.root)
        self.assertIn('linked_path', [p['type'] for p in result['problems']])

    def test_xlsx_xml_hyperlink_sheet_id_and_split_rich_text_are_scanned(self):
        marker = 'PrivateFixture' + 'Person'
        url = 'https://docs.google.com/' + 'spreadsheets/d/' + ('x' * 36)
        data = self.workbook([('xl/sharedStrings.xml', '<sst><si><r><t>PrivateFixture</t></r><r><t>Person</t></r></si></sst>'),
                              ('xl/_rels/sheet1.xml.rels', '<Relationships><r Target="' + url + '"/></Relationships>')])
        result = inspect_bytes('blank.xlsx', data, [marker])
        self.assertIn('private_deny_match', [r['type'] for r in result])
        self.assertIn('private_sheet_literal', [r['type'] for r in result])
        self.assertNotIn(marker, json.dumps(result))

    def test_xlsx_nested_binary_traversal_duplicate_and_external_link_fail(self):
        for entry in ('../escape.xml', 'xl/vbaProject.bin', 'embedded.zip', 'xl/externalLinks/link.xml'):
            self.assertTrue(inspect_bytes('blank.xlsx', self.workbook([(entry, '<x/>')]), []))
        self.assertTrue(inspect_bytes('blank.xlsx', b'not a workbook', []))
        self.assertTrue(inspect_bytes('blank.xlsx', self.workbook([('x.xml', '<bad')]), []))

    def test_secrets_and_escaped_markers_never_appear_in_findings(self):
        cases = [('ghp_' + 'q' * 30, 'secret_literal'),
                 ('https://docs.google.com/' + 'spreadsheets/d/' + 'z' * 36, 'private_sheet_literal'),
                 ('C:' + '\\Users\\private-fixture\\a', 'private_absolute_path'),
                 ('담당이름' + '(12345)', 'personal_account_literal')]
        for value, category in cases:
            result = text_findings(value, [])
            self.assertIn(category, [r['type'] for r in result])
            self.assertNotIn(value, json.dumps(result))
        self.assertTrue(text_findings('\\u0050rivateFixture', ['PrivateFixture']))

    def test_explicit_synthetic_name_prefix_is_allowed_but_deny_always_wins(self):
        synthetic = '합성교사' + '(901)'
        self.assertFalse(text_findings(synthetic, []))
        self.assertTrue(text_findings(synthetic, ['합성교사']))

    def test_deny_file_only_json_string_array_and_no_values_in_errors(self):
        path = self.base / 'private-deny.json'
        path.write_text(json.dumps(['SyntheticMarker']), encoding='utf-8')
        self.assertEqual(load_deny_file(path), ['SyntheticMarker'])
        path.write_text('{ broken fixture', encoding='utf-8')
        with self.assertRaisesRegex(PublicAuditError, '^invalid_private_deny_file$'):
            load_deny_file(path)

    def test_normal_utf8_xml_template_passes(self):
        data = self.workbook([('[Content_Types].xml', '<Types/>'), ('_rels/.rels', '<Relationships/>'),
                              ('xl/worksheets/sheet1.xml', '<worksheet><t>받는사람</t></worksheet>')])
        self.assertEqual(inspect_bytes('blank.xlsx', data, []), [])

    def test_archive_comments_and_xml_entities_fail_closed(self):
        stream = io.BytesIO(self.workbook([('x.xml', '<x/>')]))
        with zipfile.ZipFile(stream, 'a') as archive:
            archive.comment = b'SyntheticHiddenValue'
        result = inspect_bytes('blank.xlsx', stream.getvalue(), [])
        self.assertIn('archive_metadata_not_allowed', [p['type'] for p in result])
        data = self.workbook([('x.xml', '<!DOCTYPE x [<!ENTITY e "Synthetic">]><x>&e;</x>')])
        self.assertIn('xml_dtd_not_allowed', [p['type'] for p in inspect_bytes('blank.xlsx', data, [])])


if __name__ == '__main__':
    unittest.main()
