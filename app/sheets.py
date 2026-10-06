"""Bounded, read-only source tables and app-owned Google Sheets authorization.

No connector tokens, browser cookies, macros, or spreadsheet writes are used.
Google dates remain raw serial numbers in the API path (1899-12-30 epoch).
The server owns the OAuth callback; this module never opens a browser.
"""
from __future__ import annotations

import base64
import csv
import ctypes
import hashlib
import io
import json
import math
import os
import posixpath
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 1000
MAX_COLS = 50
MAX_CELLS = 30000
TIMEOUT = 15
SCOPE = 'https://www.googleapis.com/auth/spreadsheets.readonly'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
SHEETS_URL = 'https://sheets.googleapis.com/v4/spreadsheets/'
ID_RE = re.compile(r'[A-Za-z0-9_-]{20,120}')
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
RNS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


class SourceError(ValueError):
    pass


class SourceAccessRequired(SourceError):
    pass


def parse_google_url(url: str) -> dict:
    """Accept only a real Google Sheets resource URL, never an arbitrary host."""
    try:
        parsed = urllib.parse.urlsplit(str(url))
        if (parsed.scheme != 'https' or parsed.hostname != 'docs.google.com'
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise SourceError('docs.google.com의 HTTPS Google 시트 주소를 입력하세요.')
        match = re.fullmatch(r'/spreadsheets/d/([A-Za-z0-9_-]{20,120})(?:/(?:edit|view|preview))?/?', parsed.path)
        if not match:
            raise SourceError('Google 시트 주소에서 파일 ID를 확인할 수 없습니다.')
        query = urllib.parse.parse_qs(parsed.query)
        fragment = urllib.parse.parse_qs(parsed.fragment)
        gids = query.get('gid', []) + fragment.get('gid', [])
        if any(not re.fullmatch(r'\d{1,12}', value) for value in gids) or len(set(gids)) > 1:
            raise SourceError('시트 탭 ID가 잘못되었거나 서로 다릅니다.')
        return {'id': match.group(1), 'tab_id': gids[0] if gids else None}
    except (TypeError, ValueError) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError('Google 시트 주소 형식이 잘못되었습니다.') from None


def _col_number(letters: str) -> int:
    result = 0
    for char in letters:
        result = result * 26 + ord(char) - 64
    return result


def _col_name(number: int) -> str:
    result = ''
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


def parse_range(value: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r'([A-Z]{1,3})([1-9]\d{0,5}):([A-Z]{1,3})([1-9]\d{0,5})', str(value).upper())
    if not match:
        raise SourceError('읽을 범위는 A1:I1000처럼 시작과 끝을 지정하세요.')
    c1, r1, c2, r2 = _col_number(match[1]), int(match[2]), _col_number(match[3]), int(match[4])
    rows, cols = r2 - r1 + 1, c2 - c1 + 1
    if (rows < 1 or cols < 1 or rows > MAX_ROWS or cols > MAX_COLS
            or rows * cols > MAX_CELLS or c2 > 16384 or r2 > 1048576):
        raise SourceError(f'읽기 범위는 최대 {MAX_ROWS}행·{MAX_COLS}열·{MAX_CELLS}칸입니다.')
    return r1, c1, r2, c2


def _values(values) -> list[list]:
    if not isinstance(values, list) or len(values) > MAX_ROWS:
        raise SourceError('자료의 행 수 또는 형식이 허용 범위를 벗어났습니다.')
    count = 0
    for row in values:
        if not isinstance(row, list) or len(row) > MAX_COLS:
            raise SourceError('자료의 열 수 또는 형식이 허용 범위를 벗어났습니다.')
        count += len(row)
        for value in row:
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise SourceError('자료에 지원하지 않는 값이 있습니다.')
            if isinstance(value, float) and not math.isfinite(value):
                raise SourceError('자료에 유효하지 않은 숫자가 있습니다.')
            if isinstance(value, str) and len(value) > 30000:
                raise SourceError('한 셀의 내용은 30,000자 이하여야 합니다.')
    if count > MAX_CELLS:
        raise SourceError('자료의 전체 셀 수가 허용 범위를 벗어났습니다.')
    return values


def _snapshot(source: dict, values: list[list], header_row: int | None = None) -> dict:
    values = _values(values)
    canonical = json.dumps({'source': source, 'values': values}, ensure_ascii=False,
                           sort_keys=True, separators=(',', ':'), allow_nan=False)
    result = {'source': source, 'values': values, 'rows': values,
              'read_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'hash': hashlib.sha256(canonical.encode('utf-8')).hexdigest()}
    if header_row is not None:
        if type(header_row) is not int or not 1 <= header_row <= MAX_ROWS:
            raise SourceError('헤더 행 번호를 확인하세요.')
        result['header_row'] = header_row
        result['headers'] = values[header_row - 1] if header_row <= len(values) else []
    return result


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.hostname == 'accounts.google.com':
            raise SourceAccessRequired('이 시트는 Google 로그인과 앱의 읽기 전용 연결이 필요합니다.')
        if (req.has_header('Authorization') or target.scheme != 'https'
                or target.hostname != 'docs.google.com'):
            raise SourceError('예상하지 않은 외부 주소로 이동해 자료 읽기를 중단했습니다.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request(url: str, *, token: str | None = None, form: dict | None = None,
             limit: int = MAX_BYTES) -> tuple[bytes, str]:
    headers = {'User-Agent': 'CoolReserve/1.0', 'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    payload = None
    if form is not None:
        payload = urllib.parse.urlencode(form).encode('ascii')
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    req = urllib.request.Request(url, data=payload, headers=headers)
    try:
        with urllib.request.build_opener(_SafeRedirects()).open(req, timeout=TIMEOUT) as response:
            data = response.read(limit + 1)
            content_type = response.headers.get('Content-Type', '')
            if len(data) > limit:
                raise SourceError('응답이 너무 큽니다. 읽을 범위를 줄여주세요.')
            if 'text/html' in content_type or data.lstrip().lower().startswith((b'<html', b'<!doctype')):
                raise SourceAccessRequired('로그인 또는 오류 페이지를 받았습니다. 앱의 Google 읽기 전용 연결을 확인하세요.')
            return data, content_type
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise SourceAccessRequired('현재 공유 설정으로는 읽을 수 없습니다. 앱의 Google 읽기 전용 연결과 해당 시트 접근 권한을 확인하세요.') from None
        if exc.code == 404:
            raise SourceError('시트 또는 탭을 찾을 수 없습니다. 주소와 접근 권한을 확인하세요.') from None
        raise SourceError(f'Google 자료 요청이 실패했습니다(HTTP {exc.code}).') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SourceError('Google에 연결하지 못했습니다. 네트워크 상태를 확인한 뒤 다시 시도하세요.') from None


def _json_request(url: str, **kwargs) -> dict:
    raw, _ = _request(url, **kwargs)
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SourceError('Google 응답의 JSON 형식을 확인할 수 없습니다.') from None
    if not isinstance(value, dict) or value.get('error'):
        raise SourceError('Google이 유효한 자료를 반환하지 않았습니다.')
    return value


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != 'nt':
        raise SourceError('Google 연결 저장은 Windows 사용자 보호 기능이 있는 PC에서 사용하세요.')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypto = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    name = 'CryptUnprotectData' if decrypt else 'CryptProtectData'
    call = getattr(crypto, name)
    call.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                     ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    call.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not call(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise SourceError('Windows 사용자 계정으로 Google 연결 정보를 보호하거나 읽지 못했습니다.')
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


class SourceReader:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.auth_path = self.data_dir / 'google-sheets-auth.dpapi.json'
        self._pending = {}
        self._lock = threading.RLock()

    def _load_auth(self) -> dict:
        if not self.auth_path.is_file():
            return {}
        try:
            if self.auth_path.stat().st_size > 100000:
                raise SourceError('Google 연결 파일의 크기가 잘못되었습니다.')
            wrapped = json.loads(self.auth_path.read_text(encoding='utf-8'))
            if not isinstance(wrapped, dict) or wrapped.get('scheme') != 'windows-dpapi':
                raise SourceError('Google 연결 파일의 보호 형식을 확인할 수 없습니다.')
            value = json.loads(_dpapi(base64.b64decode(wrapped['data'], validate=True), True))
            if not isinstance(value, dict):
                raise SourceError('Google 연결 파일의 형식이 잘못되었습니다.')
            return value
        except SourceError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise SourceError('Google 연결 정보를 읽지 못했습니다. 다시 연결하세요.') from None

    def _save_auth(self, auth: dict):
        encrypted = _dpapi(json.dumps(auth, separators=(',', ':')).encode('utf-8'))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        raw = json.dumps({'scheme': 'windows-dpapi', 'data': base64.b64encode(encrypted).decode('ascii')})
        temporary = self.auth_path.with_suffix('.tmp')
        temporary.write_text(raw, encoding='utf-8')
        os.replace(temporary, self.auth_path)

    def connection_status(self) -> dict:
        with self._lock:
            try:
                auth = self._load_auth()
                connected = bool(auth.get('refresh_token') or auth.get('access_token'))
                return {'connected': connected, 'configured': bool(auth.get('client_id'))}
            except SourceError:
                return {'connected': False, 'configured': False}

    def begin_oauth(self, client_json_path: str | None, redirect_uri: str) -> dict:
        """Build an app-owned Desktop OAuth URL; only the user grants consent."""
        redirect = urllib.parse.urlsplit(redirect_uri)
        try:
            valid_redirect = (redirect.scheme == 'http' and redirect.hostname == '127.0.0.1'
                              and 1 <= redirect.port <= 65535 and not redirect.query
                              and not redirect.fragment and not redirect.username)
        except (ValueError, TypeError):
            valid_redirect = False
        if not valid_redirect:
            raise SourceError('Google 인증 반환 주소는 이 앱의 127.0.0.1 로컬 서버여야 합니다.')
        try:
            if client_json_path:
                path = Path(client_json_path)
                if not path.is_file() or path.stat().st_size > 100000:
                    raise SourceError('Google Desktop OAuth 클라이언트 JSON 파일을 선택하세요.')
                client = json.loads(path.read_text(encoding='utf-8-sig')).get('installed', {})
            else:
                client = self._load_auth()
            if (not isinstance(client, dict) or not isinstance(client.get('client_id'), str)
                    or not client['client_id'].endswith('.apps.googleusercontent.com')
                    or not isinstance(client.get('client_secret', ''), str)):
                raise SourceError('데스크톱 앱 유형의 Google OAuth 클라이언트 JSON이 필요합니다.')
        except SourceError:
            raise
        except (OSError, ValueError, TypeError, AttributeError):
            raise SourceError('Google Desktop OAuth 클라이언트 JSON을 읽지 못했습니다.') from None
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
        with self._lock:
            previous = self._load_auth()
            config = previous if previous.get('client_id') == client['client_id'] else {}
            config.update({'client_id':client['client_id'], 'client_secret':client.get('client_secret', '')})
            self._save_auth(config)
            self._pending = {state: {'client_id': client['client_id'], 'client_secret': client.get('client_secret', ''),
                                    'refresh_token': config.get('refresh_token', ''),
                                    'redirect_uri': redirect_uri, 'verifier': verifier, 'expires_at': time.time() + 600}}
        query = urllib.parse.urlencode({'client_id': client['client_id'], 'redirect_uri': redirect_uri,
                                       'response_type': 'code', 'scope': SCOPE, 'state': state,
                                       'code_challenge': challenge, 'code_challenge_method': 'S256',
                                       'access_type': 'offline', 'prompt': 'consent'})
        return {'authorization_url': AUTH_URL + '?' + query, 'state': state, 'expires_in': 600}

    def finish_oauth(self, code: str, state: str) -> dict:
        with self._lock:
            pending = self._pending.pop(str(state), None)
            if not pending or pending['expires_at'] < time.time():
                raise SourceError('Google 연결 요청이 만료되었거나 일치하지 않습니다. 다시 연결하세요.')
            if not isinstance(code, str) or not code or len(code) > 4096:
                raise SourceError('Google 인증 결과가 없습니다. 연결을 다시 시작하세요.')
            form = {'client_id': pending['client_id'], 'code': code,
                    'code_verifier': pending['verifier'], 'redirect_uri': pending['redirect_uri'],
                    'grant_type': 'authorization_code'}
            if pending['client_secret']:
                form['client_secret'] = pending['client_secret']
            response = _json_request(TOKEN_URL, form=form, limit=100000)
            auth = self._validated_token(response, pending)
            self._save_auth(auth)
            return {'connected': True}

    @staticmethod
    def _validated_token(response: dict, previous: dict) -> dict:
        if (not isinstance(response.get('access_token'), str) or not response['access_token']
                or not isinstance(response.get('token_type'), str)
                or response['token_type'].casefold() != 'bearer'
                or not isinstance(response.get('refresh_token', ''), str)):
            raise SourceError('Google이 유효한 읽기 전용 연결 정보를 반환하지 않았습니다.')
        granted = set(str(response.get('scope', SCOPE)).split())
        if granted != {SCOPE}:
            raise SourceError('요청한 시트 읽기 전용 권한과 승인 결과가 다릅니다. 다시 연결하세요.')
        expires = response.get('expires_in', 3600)
        if type(expires) not in (int, float) or not math.isfinite(expires) or not 1 <= expires <= 86400:
            raise SourceError('Google 연결 유효기간을 확인할 수 없습니다.')
        return {'client_id': previous['client_id'], 'client_secret': previous.get('client_secret', ''),
                'access_token': response['access_token'],
                'refresh_token': response.get('refresh_token') or previous.get('refresh_token', ''),
                'expires_at': time.time() + expires, 'scope': SCOPE}

    def _access_token(self) -> str:
        with self._lock:
            auth = self._load_auth()
            if not auth.get('client_id'):
                raise SourceAccessRequired('앱의 Google 읽기 전용 연결을 먼저 완료하세요. Desktop OAuth 클라이언트 JSON과 사용자 승인이 필요합니다.')
            expiry = auth.get('expires_at', 0)
            if type(expiry) not in (int, float) or not math.isfinite(expiry):
                raise SourceAccessRequired('Google 연결 유효기간을 읽지 못했습니다. 앱에서 다시 승인해 주세요.')
            if isinstance(auth.get('access_token'), str) and auth['access_token'] and expiry > time.time() + 60:
                return auth['access_token']
            if not auth.get('refresh_token'):
                raise SourceAccessRequired('Google 연결이 만료되었습니다. 앱에서 다시 승인해 주세요.')
            form = {'client_id': auth['client_id'], 'refresh_token': auth['refresh_token'], 'grant_type': 'refresh_token'}
            if auth.get('client_secret'):
                form['client_secret'] = auth['client_secret']
            auth = self._validated_token(_json_request(TOKEN_URL, form=form, limit=100000), auth)
            self._save_auth(auth)
            return auth['access_token']

    def disconnect(self) -> dict:
        """Remove only this app's local authorization; no other account is touched."""
        with self._lock:
            self._pending.clear()
            self.auth_path.unlink(missing_ok=True)
        return {'connected': False}

    def read(self, body: dict) -> dict:
        if not isinstance(body, dict):
            raise SourceError('자료 연결 형식이 잘못되었습니다.')
        if body.get('kind', body.get('type', 'google_sheet')) not in ('google_sheet', 'google', 'google_sheets'):
            raise SourceError('Google 시트 자료 연결을 선택하세요.')
        linked = parse_google_url(body['url']) if body.get('url') else {}
        sid = body.get('id', body.get('sheet_id', linked.get('id', '')))
        if not isinstance(sid, str) or not ID_RE.fullmatch(sid) or (linked.get('id') and sid != linked['id']):
            raise SourceError('Google 시트 파일 ID를 확인하세요.')
        tab_id = body.get('tab_id', body.get('gid', linked.get('tab_id')))
        if tab_id is not None and not re.fullmatch(r'\d{1,12}', str(tab_id)):
            raise SourceError('Google 시트 탭 ID를 확인하세요.')
        area = body.get('range', 'A1:I1000')
        parse_range(area)
        access = body.get('access', 'auto')
        if access not in ('auto', 'public', 'oauth'):
            raise SourceError('Google 자료 연결 방식을 확인하세요.')
        if access == 'oauth' or (access == 'auto' and self.connection_status()['connected']):
            return self._read_api(sid, tab_id, body, area)
        return self._read_public(sid, tab_id, body, area)

    def _read_api(self, sid, tab_id, body, area):
        token = self._access_token()
        fields = 'spreadsheetId,properties(title,timeZone),sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))'
        meta = _json_request(SHEETS_URL + sid + '?' + urllib.parse.urlencode({'fields': fields}), token=token, limit=300000)
        if meta.get('spreadsheetId') != sid or not isinstance(meta.get('sheets'), list):
            raise SourceError('요청한 Google 시트의 메타데이터를 확인하지 못했습니다.')
        if any(not isinstance(sheet, dict) or not isinstance(sheet.get('properties'), dict)
               for sheet in meta['sheets']):
            raise SourceError('Google 시트 탭 목록을 확인하지 못했습니다.')
        tabs = [sheet['properties'] for sheet in meta['sheets']]
        selected = [tab for tab in tabs if str(tab.get('sheetId')) == str(tab_id)] if tab_id is not None else []
        if tab_id is None and body.get('tab_name'):
            selected = [tab for tab in tabs if tab.get('title') == body['tab_name']]
        elif tab_id is None and len(tabs) == 1:
            selected = tabs
        if len(selected) != 1:
            raise SourceError('읽을 시트 탭을 정확히 하나 선택하세요.')
        tab = selected[0]
        r1, c1, r2, c2 = parse_range(area)
        grid = tab.get('gridProperties', {})
        if type(grid.get('rowCount')) is not int or type(grid.get('columnCount')) is not int:
            raise SourceError('시트의 행·열 범위를 확인하지 못했습니다.')
        r2, c2 = min(r2, grid['rowCount']), min(c2, grid['columnCount'])
        if r2 < r1 or c2 < c1:
            raise SourceError('선택한 범위가 시트 밖에 있습니다.')
        actual = f'{_col_name(c1)}{r1}:{_col_name(c2)}{r2}'
        title = tab.get('title')
        if not isinstance(title, str):
            raise SourceError('시트 탭 이름을 확인하지 못했습니다.')
        qualified = "'" + title.replace("'", "''") + "'!" + actual
        params = {'valueRenderOption': 'UNFORMATTED_VALUE', 'dateTimeRenderOption': 'SERIAL_NUMBER', 'majorDimension': 'ROWS'}
        response = _json_request(SHEETS_URL + sid + '/values/' + urllib.parse.quote(qualified, safe='') + '?' + urllib.parse.urlencode(params), token=token)
        values = _values(response.get('values', []))
        if len(values) > r2-r1+1 or any(len(row) > c2-c1+1 for row in values):
            raise SourceError('Google 응답이 지정한 읽기 범위를 넘었습니다.')
        source = {'id': sid, 'title': meta.get('properties', {}).get('title', ''),
                  'tab_id': str(tab['sheetId']), 'tab_name': title, 'range': actual,
                  'time_zone': meta.get('properties', {}).get('timeZone'), 'date_render': 'SERIAL_NUMBER',
                  'access': 'oauth', 'url': 'https://docs.google.com/spreadsheets/d/' + sid + '/edit#gid=' + str(tab['sheetId'])}
        result = _snapshot(source, values, body.get('header_row'))
        result['tabs'] = [{'id':str(tab.get('sheetId')), 'title':tab.get('title')} for tab in tabs]
        return result

    def _read_public(self, sid, tab_id, body, area):
        if tab_id is None:
            raise SourceError('로그인 없는 연결은 주소의 gid 또는 탭 ID가 필요합니다.')
        query = urllib.parse.urlencode({'tqx': 'out:json', 'gid': str(tab_id), 'range': area, 'headers': 0})
        raw, _ = _request('https://docs.google.com/spreadsheets/d/' + sid + '/gviz/tq?' + query)
        try:
            text = raw.decode('utf-8')
            match = re.fullmatch(r'\s*(?:/\*O_o\*/\s*)?google\.visualization\.Query\.setResponse\((.*)\);?\s*', text, re.S)
            if not match:
                raise SourceError('공개 시트 응답 형식을 확인할 수 없습니다.')
            response = json.loads(match[1])
            if response.get('status') != 'ok':
                raise SourceError('공개 시트가 해당 범위를 반환하지 않았습니다. 탭과 공유 설정을 확인하세요.')
            table = response['table']
            columns = table['cols']
            r1, c1, r2, c2 = parse_range(area)
            if len(columns) > c2-c1+1 or len(table['rows']) > r2-r1+1:
                raise SourceError('공개 시트 응답이 지정한 범위를 넘었습니다.')
            values = []
            for row in table['rows']:
                cells = row['c']
                if len(cells) > len(columns):
                    raise SourceError('공개 시트의 열 구조를 확인할 수 없습니다.')
                values.append([_gviz_value(cell.get('v'), columns[i].get('type')) if cell else None
                               for i, cell in enumerate(cells)])
        except SourceError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
            raise SourceError('공개 시트 응답을 안전하게 해석하지 못했습니다.') from None
        # gviz exposes values, not authoritative workbook metadata. Never invent
        # the workbook title, tab name, grid extent or source time zone.
        source = {'id': sid, 'title': None, 'tab_id': str(tab_id), 'tab_name': None,
                  'range': area, 'time_zone': None, 'date_render': 'CALENDAR_DATE',
                  'access': 'public', 'metadata_available': False,
                  'url': 'https://docs.google.com/spreadsheets/d/' + sid + '/edit#gid=' + str(tab_id)}
        result = _snapshot(source, values, body.get('header_row'))
        result['tabs'] = [{'id':str(tab_id), 'title':None}]
        return result


def _gviz_value(value, kind):
    if value is None:
        return None
    if kind in ('date', 'datetime'):
        match = re.fullmatch(r'Date\((\d{4}),(\d{1,2}),(\d{1,2})(?:,(\d{1,2}),(\d{1,2}),(\d{1,2}))?\)', str(value))
        if not match:
            raise SourceError('공개 시트 날짜 형식을 확인할 수 없습니다.')
        nums = [int(value) if value is not None else 0 for value in match.groups()]
        try:
            dt = datetime(nums[0], nums[1] + 1, nums[2], *nums[3:])
        except ValueError:
            raise SourceError('공개 시트에 잘못된 날짜가 있습니다.') from None
        return dt.date().isoformat() if kind == 'date' else dt.isoformat(timespec='seconds')
    return value


def read_table_csv(raw: bytes) -> list[list]:
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise SourceError('CSV 파일 크기는 5MB 이하여야 합니다.')
    decoded = None
    for encoding in ('utf-8-sig', 'cp949'):
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        raise SourceError('CSV 인코딩은 UTF-8 또는 CP949를 사용하세요.')
    try:
        rows = []
        for row in csv.reader(io.StringIO(decoded), strict=True):
            rows.append(row)
            if len(rows) > MAX_ROWS:
                raise SourceError('CSV 행 수가 허용 범위를 넘습니다.')
        return _values(rows)
    except csv.Error:
        raise SourceError('CSV의 구분자나 따옴표 형식을 확인하세요.') from None


def _xml(archive, name):
    raw = archive.read(name)
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise SourceError('외부 개체가 있는 엑셀 XML은 읽지 않습니다.')
    return ET.fromstring(raw)


def read_table_xlsx(raw: bytes, sheet_name: str) -> list[list]:
    """Read one explicitly selected table, preserving blank rows and booleans.

    Cached formula results may be read, but never calculated or executed. Missing
    caches fail closed. Macros are neither loaded nor run.
    """
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise SourceError('엑셀 파일 크기는 5MB 이하여야 합니다.')
    if not isinstance(sheet_name, str) or not sheet_name:
        raise SourceError('읽을 엑셀 시트 이름을 지정하세요.')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist()) > 3000 or sum(item.file_size for item in archive.infolist()) > 40 * 1024 * 1024:
                raise SourceError('엑셀 압축 해제 크기가 허용 범위를 넘습니다.')
            workbook = _xml(archive, 'xl/workbook.xml')
            properties = workbook.find('m:workbookPr', NS)
            if properties is not None and properties.get('date1904', '').casefold() in ('1', 'true'):
                raise SourceError('1904 날짜 체계의 엑셀은 지원하지 않습니다. 날짜를 연도가 포함된 텍스트로 내보낸 CSV를 사용하세요.')
            selected = [sheet for sheet in workbook.findall('m:sheets/m:sheet', NS) if sheet.get('name') == sheet_name]
            if len(selected) != 1:
                raise SourceError('지정한 엑셀 시트를 찾을 수 없습니다.')
            rels = _xml(archive, 'xl/_rels/workbook.xml.rels')
            targets = {r.get('Id'):r.get('Target') for r in rels if r.get('TargetMode') != 'External'}
            target = targets[selected[0].get(RNS + 'id')]
            member = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
            if not member.startswith('xl/'):
                raise SourceError('잘못된 엑셀 시트 경로입니다.')
            strings = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                strings = [''.join(t.text or '' for t in item.iterfind('.//m:t', NS))
                           for item in _xml(archive, 'xl/sharedStrings.xml').findall('m:si', NS)]
            cells = {}
            for row in _xml(archive, member).findall('m:sheetData/m:row', NS):
                for cell in row.findall('m:c', NS):
                    v = cell.find('m:v', NS)
                    inline = cell.find('m:is', NS)
                    formula = cell.find('m:f', NS)
                    kind = cell.get('t')
                    if (formula is not None and kind in (None, 'n', 'b')
                            and (v is None or not (v.text or '').strip())):
                        raise SourceError('수식의 저장된 계산값이 없습니다. Excel에서 계산·저장한 자료를 사용하세요.')
                    if v is None and inline is None:
                        if formula is not None:
                            raise SourceError('수식의 저장된 계산값이 없습니다. Excel에서 계산·저장한 자료를 사용하세요.')
                        continue  # Ignore formatting-only rows, even far below the table.
                    match = re.fullmatch(r'([A-Z]+)([1-9]\d*)', cell.get('r', ''))
                    if not match:
                        raise SourceError('엑셀 셀 위치를 확인할 수 없습니다.')
                    r, c = int(match[2]), _col_number(match[1])
                    if r > MAX_ROWS or c > MAX_COLS or r*c > MAX_CELLS:
                        raise SourceError('엑셀의 실제 데이터 범위가 허용 크기를 넘습니다.')
                    value = v.text if v is not None else ''
                    if kind == 's':
                        value = strings[int(value)]
                    elif kind == 'inlineStr':
                        if inline is None:
                            raise SourceError('엑셀 문자열의 저장 형식을 확인할 수 없습니다.')
                        value = ''.join(t.text or '' for t in inline.iterfind('.//m:t', NS))
                    elif kind == 'b':
                        if value not in ('0', '1'):
                            raise SourceError('엑셀 체크박스 값이 잘못되었습니다.')
                        value = value == '1'
                    elif kind == 'e':
                        raise SourceError('엑셀에 계산 오류가 있습니다. 원본의 오류를 먼저 확인하세요.')
                    elif kind in ('str', 'd'):
                        value = value or ''
                    elif value not in ('', None):
                        number = float(value)
                        value = int(number) if number.is_integer() else number
                    else:
                        value = None
                    if (r, c) in cells:
                        raise SourceError('엑셀에 중복된 셀 위치가 있습니다.')
                    cells[r, c] = value
            if not cells:
                return []
            max_r, max_c = max(r for r,c in cells), max(c for r,c in cells)
            return _values([[cells.get((r,c)) for c in range(1,max_c+1)] for r in range(1,max_r+1)])
    except SourceError:
        raise
    except (zipfile.BadZipFile, KeyError, ValueError, TypeError, IndexError, AttributeError, ET.ParseError, RuntimeError):
        raise SourceError('엑셀 업무표를 읽지 못했습니다. 시트와 파일 형식을 확인하세요.') from None
