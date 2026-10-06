"""Malformed input must not silently become a sendable reservation body."""
import io
import unittest
import zipfile

from app.importers import import_file


def reservation_xlsx(body_cell):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('xl/workbook.xml',
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="예약목록" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        headers = ''.join(f'<c r="{column}1" t="inlineStr"><is><t>{name}</t></is></c>'
                          for column, name in zip('ABCD', ('예약일시', '받는사람', '제목', '내용')))
        values = ''.join(f'<c r="{column}2" t="inlineStr"><is><t>{value}</t></is></c>'
                         for column, value in zip('ABC', ('2026-10-20 15:00', '합성 수신자', '합성 안내')))
        archive.writestr('xl/worksheets/sheet1.xml',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData><row r="1">{headers}</row><row r="2">{values}{body_cell}</row>'
            '</sheetData></worksheet>')
    return stream.getvalue()


class ImporterTableErrorTests(unittest.TestCase):
    def test_excel_error_body_is_rejected_before_creating_jobs(self):
        raw = reservation_xlsx('<c r="D2" t="e"><v>#REF!</v></c>')
        with self.assertRaisesRegex(ValueError, 'D2:.*계산 오류'):
            import_file('합성 예약.xlsx', raw)

    def test_literal_error_marker_in_message_is_preserved(self):
        raw = reservation_xlsx('<c r="D2" t="inlineStr"><is><t>#REF! 오류를 확인하세요.</t></is></c>')
        jobs, errors = import_file('합성 예약.xlsx', raw)
        self.assertEqual(errors, [])
        self.assertEqual(jobs[0]['body'], '#REF! 오류를 확인하세요.')

    def test_unclosed_quote_cannot_merge_following_reservation_into_body(self):
        raw = ('예약일시,받는사람,제목,내용\n'
               '2026-10-20 15:00,합성A,안내A,"닫히지 않은 본문\n'
               '2026-10-20 16:00,합성B,안내B,두 번째 본문').encode('utf-8')
        with self.assertRaisesRegex(ValueError, '따옴표'):
            import_file('합성 예약.csv', raw)

    def test_valid_quoted_multiline_body_still_imports_two_reservations(self):
        raw = ('예약일시,받는사람,제목,내용\n'
               '2026-10-20 15:00,합성A,안내A,"첫째 줄\n둘째 줄"\n'
               '2026-10-20 16:00,합성B,안내B,두 번째 본문').encode('utf-8-sig')
        jobs, errors = import_file('합성 예약.csv', raw)
        self.assertEqual(errors, [])
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]['body'], '첫째 줄\n둘째 줄')
        self.assertEqual(jobs[1]['body'], '두 번째 본문')

    def test_nonempty_extra_cells_cannot_silently_truncate_message(self):
        cases = [
            ('합성 예약.csv', ('예약일시,받는사람,제목,내용\n'
                             '2026-10-20 15:00,합성A,안내A,첫째,둘째').encode('utf-8')),
            ('합성 예약.xlsx', reservation_xlsx(
                '<c r="D2" t="inlineStr"><is><t>첫째</t></is></c>'
                '<c r="E2" t="inlineStr"><is><t>둘째</t></is></c>')),
        ]
        for name, raw in cases:
            with self.subTest(name=name):
                jobs, errors = import_file(name, raw)
                self.assertEqual(jobs, [])
                self.assertEqual(len(errors), 1)
                self.assertIn('2행:', errors[0])
                self.assertIn('헤더보다 많은 값', errors[0])

    def test_empty_extra_cells_do_not_reject_intact_message(self):
        cases = [
            ('합성 예약.csv', ('예약일시,받는사람,제목,내용\n'
                             '2026-10-20 15:00,합성A,안내A,전체 본문,,  ').encode('utf-8')),
            ('합성 예약.xlsx', reservation_xlsx(
                '<c r="D2" t="inlineStr"><is><t>전체 본문</t></is></c>'
                '<c r="E2" t="inlineStr"><is><t>  </t></is></c><c r="F2"/>')),
        ]
        for name, raw in cases:
            with self.subTest(name=name):
                jobs, errors = import_file(name, raw)
                self.assertEqual(errors, [])
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]['body'], '전체 본문')


if __name__ == '__main__':
    unittest.main()
