"""Local, bounded Office previews. No document code, formulas or external links run."""
import json
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from file_store import FileError

MAX_OFFICE_BYTES = 20 * 1024 * 1024
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_TEXT = 100000
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def office_preview(path, suffix):
    if Path(path).stat().st_size > MAX_OFFICE_BYTES:
        raise ValueError('Office preview limited to 20 MiB')
    with ZipFile(path) as archive:
        if len(archive.infolist()) > 10000 or sum(info.file_size for info in archive.infolist()) > MAX_XML_BYTES:
            raise ValueError('Office archive too large')

        def xml(name):
            data = archive.read(name)
            if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
                raise ValueError('XML entities are not supported')
            return ET.fromstring(data)

        if suffix == '.docx':
            body = xml('word/document.xml').find(W + 'body')
            blocks, remaining, truncated = [], MAX_TEXT, False

            def paragraph(node):
                nonlocal remaining, truncated
                value = ''.join(child.text or '' if child.tag == W + 't' else '\t' if child.tag == W + 'tab' else '\n'
                                for child in node.iter() if child.tag in {W + 't', W + 'tab', W + 'br'})
                truncated = truncated or len(value) > remaining
                value = value[:remaining]; remaining -= len(value)
                return value

            if body is None:
                raise ValueError('Missing document body')
            for node in body:
                if len(blocks) >= 1000 or remaining <= 0:
                    truncated = True; break
                if node.tag == W + 'p':
                    blocks.append({'type': 'paragraph', 'text': paragraph(node)})
                elif node.tag == W + 'tbl':
                    all_rows = node.findall(W + 'tr')
                    rows = []
                    for row in all_rows[:200]:
                        cells = row.findall(W + 'tc')
                        rows.append(['\n'.join(paragraph(p) for p in cell.iter(W + 'p')) for cell in cells[:30]])
                        truncated = truncated or len(cells) > 30
                    blocks.append({'type': 'table', 'rows': rows})
                    truncated = truncated or len(all_rows) > 200
            return {'kind': 'word', 'blocks': blocks, 'truncated': truncated}

        if suffix != '.xlsx':
            raise ValueError('Unsupported Office format')
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(node.itertext()) for node in xml('xl/sharedStrings.xml').findall(S + 'si')]
        relationships = {node.get('Id'): node for node in xml('xl/_rels/workbook.xml.rels')}
        sheets = xml('xl/workbook.xml').findall(S + 'sheets/' + S + 'sheet')
        result, remaining, truncated = [], MAX_TEXT, len(sheets) > 10
        for sheet in sheets[:10]:
            relation = relationships.get(sheet.get(R + 'id'))
            if relation is None or relation.get('TargetMode') == 'External':
                continue
            target = relation.get('Target', '')
            if target.startswith('/xl/'):
                target = target[1:]
            elif not target.startswith('/'):
                target = 'xl/' + target
            if '\\' in target or '..' in target.split('/') or not target.startswith('xl/'):
                raise ValueError('Invalid sheet path')
            rows, max_column = [], 0
            sheet_rows = xml(target).findall(S + 'sheetData/' + S + 'row')
            truncated = truncated or len(sheet_rows) > 1000
            for row in sheet_rows[:1000]:
                values = {}
                for cell in row.findall(S + 'c'):
                    address = cell.get('r', '')
                    letters = ''.join(c for c in address if 'A' <= c <= 'Z')
                    column = 0
                    for letter in letters[:4]:
                        column = column * 26 + ord(letter) - 64
                    if not 1 <= column <= 50:
                        truncated = True; continue
                    kind = cell.get('t')
                    value = cell.findtext(S + 'v', '')
                    if kind == 's':
                        value = strings[int(value)]
                    elif kind == 'inlineStr':
                        node = cell.find(S + 'is')
                        value = ''.join(node.itertext()) if node is not None else ''
                    elif kind == 'b':
                        value = 'TRUE' if value == '1' else 'FALSE'
                    formula = cell.findtext(S + 'f')
                    if formula is not None:
                        value = '=' + formula + (' → ' + value if value else '')
                    truncated = truncated or len(value) > min(remaining, 4000)
                    value = value[:min(remaining, 4000)]; remaining -= len(value)
                    values[str(column)] = value; max_column = max(max_column, column)
                rows.append({'number': row.get('r', str(len(rows) + 1)), 'cells': values})
                if remaining <= 0:
                    truncated = True; break
            result.append({'name': sheet.get('name', 'Аркуш'), 'rows': rows, 'columns': max_column})
            if remaining <= 0:
                break
        return {'kind': 'excel', 'sheets': result, 'truncated': truncated}


def read_office(path, suffix):
    try:
        result = subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__)), str(path), suffix],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
            raise ValueError('Preview worker failed')
        return json.loads(result.stdout.decode('utf-8'))
    except (ValueError, OSError, subprocess.TimeoutExpired):
        raise FileError('Не вдалося відкрити документ. Перегляд DOCX/XLSX підтримує файли до 20 МіБ без пароля. Оригінал можна скачати.') from None


if __name__ == '__main__':
    try:
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        except ImportError:
            pass
        print(json.dumps(office_preview(sys.argv[1], sys.argv[2]), ensure_ascii=False))
    except Exception:
        raise SystemExit(1)
