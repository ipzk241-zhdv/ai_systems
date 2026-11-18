from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from file_preview import office_preview, read_office
from file_store import FileError


class OfficePreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'fixture'

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, parts):
        with ZipFile(self.path, 'w') as archive:
            for name, text in parts.items():
                archive.writestr(name, text)

    def test_docx_paragraphs_tables_unicode_and_no_external_fetches(self):
        self.archive({'word/document.xml': '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
          <w:p><w:r><w:t>Привіт </w:t><w:t>&lt;script&gt;</w:t><w:tab/><w:t>світ</w:t></w:r></w:p>
          <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Клітинка</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
          </w:body></w:document>''',
          'word/_rels/document.xml.rels': '<Relationships><Relationship TargetMode="External" Target="https://example.invalid"/></Relationships>'})
        result = read_office(self.path, '.docx')
        self.assertEqual(result['kind'], 'word')
        self.assertEqual(result['blocks'][0]['text'], 'Привіт <script>\tсвіт')
        self.assertEqual(result['blocks'][1]['rows'], [['Клітинка']])
        self.assertFalse(result['truncated'])

    def workbook(self, sheet, relationships=None):
        self.archive({
            'xl/workbook.xml': '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Аркуш 1" r:id="r1"/></sheets></workbook>',
            'xl/_rels/workbook.xml.rels': relationships or '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>',
            'xl/sharedStrings.xml': '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>&lt;img src=x&gt;</t></si></sst>',
            'xl/worksheets/sheet1.xml': '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + sheet + '</sheetData></worksheet>'})

    def test_xlsx_sparse_cells_cached_formulas_and_inline_strings(self):
        self.workbook('<row r="3"><c r="B3" t="s"><v>0</v></c><c r="D3"><f>SUM(1,2)</f><v>3</v></c><c r="E3" t="inlineStr"><is><t>Текст</t></is></c></row>')
        result = read_office(self.path, '.xlsx')
        sheet = result['sheets'][0]
        self.assertEqual(sheet['name'], 'Аркуш 1')
        self.assertEqual(sheet['columns'], 5)
        self.assertEqual(sheet['rows'], [{'number':'3', 'cells':{'2':'<img src=x>', '4':'=SUM(1,2) → 3', '5':'Текст'}}])

    def test_xlsx_rows_and_columns_are_bounded(self):
        self.workbook(''.join('<row r="%s"><c r="A%s"><v>1</v></c><c r="XFD%s"><v>9</v></c></row>' % (i, i, i) for i in range(1, 1100)))
        result = office_preview(self.path, '.xlsx')
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['sheets'][0]['rows']), 1000)
        self.assertEqual(result['sheets'][0]['columns'], 1)

    def test_malformed_archives_entities_and_external_sheet_paths_rejected(self):
        self.path.write_bytes(b'not a zip')
        with self.assertRaises(FileError):
            read_office(self.path, '.docx')
        self.archive({'word/document.xml': '<!DOCTYPE x [<!ENTITY x "boom">]><x>&x;</x>'})
        with self.assertRaises(FileError):
            read_office(self.path, '.docx')
        self.workbook('', '<Relationships><Relationship Id="r1" Target="../../secret"/></Relationships>')
        with self.assertRaises(FileError):
            read_office(self.path, '.xlsx')
        self.workbook('', '<Relationships><Relationship Id="r1" Target="https://example.invalid/sheet" TargetMode="External"/></Relationships>')
        self.assertEqual(office_preview(self.path, '.xlsx')['sheets'], [])
