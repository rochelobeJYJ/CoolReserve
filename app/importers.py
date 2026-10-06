"""Read-only CSV/XLSX importer using the standard library; never evaluates formulas."""
from __future__ import annotations
import csv
import io
import json
import posixpath
import re
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET
from .core import clean_job, HEADERS, MAX_JOBS

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
RNS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def _xml(z: zipfile.ZipFile, name: str):
    data = z.read(name)
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('XML 외부 개체를 포함한 파일은 지원하지 않습니다.')
    return ET.fromstring(data)


def read_xlsx(data: bytes) -> list[list]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if len(z.infolist()) > 3000 or sum(i.file_size for i in z.infolist()) > 40 * 1024 * 1024:
            raise ValueError('엑셀 압축 해제 크기가 너무 큽니다. 필요한 시트만 저장해주세요.')
        wb = _xml(z, 'xl/workbook.xml')
        props = wb.find('m:workbookPr', NS)
        epoch = datetime(1904, 1, 1) if props is not None and props.get('date1904') in ('1', 'true') else datetime(1899, 12, 30)
        rels = _xml(z, 'xl/_rels/workbook.xml.rels')
        mapping = {r.get('Id'): r.get('Target') for r in rels if r.get('TargetMode') != 'External'}
        sheets = wb.findall('m:sheets/m:sheet', NS)
        if not sheets:
            raise ValueError('시트가 없습니다.')
        sheet = next((s for s in sheets if s.get('name') == '예약목록'), sheets[0])
        target = mapping[sheet.get(RNS + 'id')]
        member = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
        if not member.startswith('xl/'):
            raise ValueError('잘못된 시트 경로입니다.')
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            strings = [''.join(t.text or '' for t in si.iterfind('.//m:t', NS)) for si in _xml(z, 'xl/sharedStrings.xml').findall('m:si', NS)]
        rows = []
        root = _xml(z, member)
        for row in root.findall('m:sheetData/m:row', NS):
            cells = {}
            for c in row.findall('m:c', NS):
                coord = c.get('r', '')
                col = 0
                for ch in re.sub(r'\d', '', coord):
                    col = col * 26 + ord(ch.upper()) - 64
                if col > 20:
                    continue
                if c.find('m:f', NS) is not None:
                    raise ValueError(f'{coord}: 수식 대신 확정된 값을 붙여 넣어주세요.')
                v = c.findtext('m:v', '', NS)
                typ = c.get('t')
                if typ == 'e':
                    raise ValueError(f'{coord}: 엑셀에 계산 오류가 있습니다. 원본의 오류를 먼저 확인하세요.')
                if typ == 's':
                    value = strings[int(v)] if v else ''
                elif typ == 'inlineStr':
                    value = ''.join(t.text or '' for t in c.iterfind('.//m:t', NS))
                elif typ == 'd':
                    value = v.replace('T', ' ')
                else:
                    value = v
                # Our first column is a scheduled datetime; accept Excel date serials.
                date_column = (col == 1 if not rows else col <= len(rows[0]) and str(rows[0][col - 1]).strip().replace(' ', '') in ('예약일시', '예약날짜'))
                if date_column and typ not in ('s', 'inlineStr', 'd') and v:
                    try:
                        number = float(v)
                        if 10000 < number < 200000:
                            value = (epoch + timedelta(seconds=round(number * 86400))).strftime('%Y-%m-%d %H:%M')
                    except ValueError:
                        pass
                cells[max(col - 1, 0)] = value
            if cells and any(str(v).strip() for v in cells.values()):
                rows.append([cells.get(i, '') for i in range(max(cells) + 1)])
            if len(rows) > MAX_JOBS + 1:
                raise ValueError(f'최대 {MAX_JOBS}개 메시지만 불러올 수 있습니다.')
        return rows


def import_file(name: str, data: bytes) -> tuple[list[dict], list[str]]:
    if len(data) > 10 * 1024 * 1024:
        raise ValueError('파일 크기는 10MB 이하여야 합니다.')
    if name.lower().endswith('.xlsx'):
        rows = read_xlsx(data)
    elif name.lower().endswith('.csv'):
        text = None
        for encoding in ('utf-8-sig', 'cp949'):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                pass
        if text is None:
            raise ValueError('CSV 인코딩은 UTF-8 또는 CP949를 사용하세요.')
        csv.field_size_limit(1024 * 1024)
        try:
            rows = list(csv.reader(io.StringIO(text), strict=True))
        except csv.Error:
            raise ValueError('CSV의 구분자나 따옴표 형식을 확인하세요.') from None
    else:
        raise ValueError('.xlsx 또는 .csv 파일만 지원합니다. .xls는 .xlsx로 저장하세요.')
    rows = [r for r in rows if any(str(x).strip() for x in r)]
    if not rows:
        raise ValueError('파일이 비어 있습니다.')
    headers = [str(x).strip().replace(' ', '') for x in rows[0]]
    if len(set(headers)) != len(headers):
        raise ValueError('중복된 열 이름이 있습니다.')
    if not {'받는사람', '내용'}.issubset(headers) or not ('예약일시' in headers or {'예약날짜', '예약시간'}.issubset(headers)):
        raise ValueError('첫 행에 받는사람, 내용과 예약일시 또는 예약날짜·예약시간 열이 필요합니다. 제목은 선택 사항입니다.')
    if len(rows) - 1 > MAX_JOBS:
        raise ValueError(f'한 파일은 {MAX_JOBS}건 이하여야 합니다.')
    jobs, errors = [], []
    for index, row in enumerate(rows[1:], 2):
        try:
            if any(str(value).strip() for value in row[len(headers):]):
                raise ValueError('헤더보다 많은 값이 있습니다. 열 개수와 본문의 따옴표를 확인하세요.')
            raw = dict(zip(headers, row))
            if '예약일시' not in raw:
                from .batch import scheduled
                raw['scheduled'] = scheduled(raw)
            if raw.get('수신자ID'):
                ids = json.loads(raw['수신자ID'])
                if ids:
                    raw['recipient_ids'] = ids
                    raw['recipient_bindings'] = json.loads(raw.get('수신자확인정보') or '{}')
            jobs.append(clean_job(raw))
        except (ValueError, TypeError) as e:
            errors.append(f'{index}행: {e}')
    return jobs, errors
