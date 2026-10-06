"""Empty numeric formula caches must never become false exclusion flags."""
import io
import unittest
import zipfile

from app.sheets import SourceError, read_table_xlsx


def workbook(cell):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('xl/workbook.xml',
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="합성 업무표" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr('xl/worksheets/sheet1.xml',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>제외일</t></is></c></row>'
            '<row r="2">' + cell + '</row></sheetData></worksheet>')
    return stream.getvalue()


class FormulaCacheTests(unittest.TestCase):
    def read(self, cell):
        return read_table_xlsx(workbook(cell), '합성 업무표')

    def test_uncalculated_numeric_and_boolean_exclusion_formulas_fail_closed(self):
        for kind in ('', ' t="n"', ' t="b"'):
            for cached in ('', '<v/>', '<v></v>', '<v>  </v>'):
                with self.subTest(kind=kind, cached=cached):
                    with self.assertRaisesRegex(SourceError, '저장된 계산값'):
                        self.read(f'<c r="A2"{kind}><f>1=1</f>{cached}</c>')

    def test_calculated_zero_and_false_remain_readable(self):
        for kind, value in (('', 0), (' t="n"', 0), (' t="b"', False)):
            with self.subTest(kind=kind):
                result = self.read(f'<c r="A2"{kind}><f>1=0</f><v>0</v></c>')
                self.assertEqual(result, [['제외일'], [value]])
                self.assertIs(type(result[1][0]), type(value))

    def test_calculated_empty_string_keeps_its_intended_value(self):
        result = self.read('<c r="A2" t="str"><f>IF(1=1,"","unused")</f><v/></c>')
        self.assertEqual(result, [['제외일'], ['']])

    def test_empty_nonformula_cell_keeps_existing_blank_semantics(self):
        self.assertEqual(self.read('<c r="A2"><v/></c>'), [['제외일'], [None]])


if __name__ == '__main__':
    unittest.main()
