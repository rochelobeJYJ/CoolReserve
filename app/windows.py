"""Fail-closed Windows UI automation. Selectors are learned on the actual school PC.
No hidden protocol, credentials, database writes or fixed screen coordinates are used.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .core import normalize, parse_time

# UIA's process-wide COM objects must share the same apartment across HTTP
# request threads and the batch runner. Set this before importing COM modules.
sys.coinit_flags = 0  # COINIT_MULTITHREADED

ROLE_LABELS = {
    'title': '제목 입력칸', 'body': '본문 입력칸', 'recipient_read': '받는 사람 표시영역 전체',
    'cc_read': '참조 표시영역 전체 (보이는 경우)', 'scheduled_check': '예약전송 체크박스',
    'datetime': '예약일시 (날짜·시간 통합)', 'date': '예약 날짜 (분리형)', 'time': '예약 시간 (분리형)',
    'send': '보내기 버튼', 'attach': '파일첨부 버튼', 'attach_list': '첨부 목록 전체',
    'file_name': '파일 선택창의 파일 이름 입력칸', 'file_open': '파일 선택창의 열기 버튼',
    'sent_open': '메시지 관리함 열기', 'sent_tab': '보낸 메시지 탭', 'sent_list': '보낸 메시지 목록',
    'result_title': '선택한 보낸 메시지의 제목', 'result_body': '선택한 보낸 메시지의 본문',
    'result_recipient': '선택한 보낸 메시지의 수신자 전체',
    'result_datetime': '선택한 보낸 메시지의 예약시각', 'result_reserved': '선택한 보낸 메시지의 발송취소 버튼',
    'result_attachments': '선택한 보낸 메시지의 첨부 목록 전체',
}
MUTABLE = {'title', 'body', 'recipient_read', 'cc_read', 'datetime', 'date', 'time',
           'attach_list', 'file_name', 'sent_list', 'result_title', 'result_body', 'result_recipient', 'result_datetime', 'result_attachments'}


class AutomationError(RuntimeError):
    pass


class PreparationTransient(AutomationError):    """A specifically identified window failure, eligible only before Send."""    passclass CaptionUnavailable(PreparationTransient):    """A verified target has no caption, before any activation click."""
    pass


class Stopped(AutomationError):
    pass


def require_windows():
    if sys.platform != 'win32':
        raise AutomationError('실제 쿨메신저 연동은 Windows PC에서만 사용할 수 있습니다.')


@contextlib.contextmanager
def com_session():
    require_windows()
    import pythoncom
    # CoInitialize silently accepts RPC_E_CHANGED_MODE; explicit MTA setup
    # rejects an incompatible STA instead of reusing invalid UIA pointers.
    pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


@contextlib.contextmanager
def automation_runtime():
    """Keep UIA's initial apartment alive for the whole local-service lifetime."""
    if sys.platform != 'win32':
        yield
        return
    with com_session():
        # pywinauto creates its shared UIA client during import. Import on this
        # persistent MTA thread before any short-lived request thread uses it.
        import pywinauto  # noqa: F401
        yield


def executable(pid: int) -> str:
    import win32api
    import win32con
    import win32process
    handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    try:
        # QueryFullProcessImageName supports lower privileges than GetModuleFileNameEx.
        from ctypes import wintypes
        query = ctypes.WinDLL('kernel32', use_last_error=True).QueryFullProcessImageNameW
        query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        query.restype = wintypes.BOOL
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not query(int(handle), 0, buf, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buf.value
    finally:
        handle.Close()


def metadata(w, mutable=False) -> dict:
    e = w.element_info
    out = {'class_name': str(getattr(e, 'class_name', '') or ''),
           'control_type': str(getattr(e, 'control_type', '') or ''),
           'automation_id': str(getattr(e, 'automation_id', '') or '')}
    cid = getattr(e, 'control_id', None)
    if cid not in (None, 0, -1):
        out['control_id'] = int(cid)
    if not mutable:
        name = str(getattr(e, 'name', '') or '')
        if name:
            out['name'] = name
    return {k: v for k, v in out.items() if v != ''}


def matches(w, spec: dict) -> bool:
    data = metadata(w)
    return all(bool(re.fullmatch(v, str(data.get('name', '')))) if k == 'name_regex'
               else data.get(k) == v for k, v in spec.items())


def read_text(w) -> str:
    # Chromium documents expose their URL through ValuePattern. TextPattern is
    # the actual editable message, including the valid empty-document case.
    if getattr(getattr(w, 'element_info', None), 'control_type', '') == 'Document':
        try:
            return normalize(w.iface_text.DocumentRange.GetText(-1))
        except Exception as e:
            raise AutomationError('본문 편집기의 실제 텍스트를 읽을 수 없습니다.') from e
    for method in ('get_value', 'text_block', 'text'):
        try:
            value = getattr(w, method)()
            if value is not None and str(value).strip():
                return normalize(value)
        except Exception:
            pass
    try:
        text = w.iface_text.DocumentRange.GetText(-1)
        if text is not None and text.strip():
            return normalize(text)
    except Exception:
        pass
    # Container chips expose their full recipient text as leaf names. Never just
    # read the first child: the entire captured region is compared exactly.
    leaves = []
    try:
        for c in w.descendants():
            if c.is_visible() and not c.children():
                s = normalize(c.window_text())
                if s:
                    leaves.append(s)
    except Exception:
        pass
    if leaves:
        return '\n'.join(leaves)
    try:
        values = w.texts()
        if values:
            return '\n'.join(normalize(v) for v in values if normalize(v))
    except Exception:
        pass
    try:
        return normalize(w.window_text())
    except Exception as e:
        raise AutomationError('선택한 화면 요소의 내용을 읽을 수 없습니다.') from e


def title_matches(actual: str, expected: str, *, gentoo_placeholder=False) -> bool:    """Only the known compose hint may represent an intentionally empty title."""    if not isinstance(actual, str) or not isinstance(expected, str):        return False    actual, expected = normalize(actual), normalize(expected)    return actual == expected or (gentoo_placeholder and not expected and actual == '제목을 입력하세요.')def body_matches(actual: str, expected: str) -> bool:
    actual, expected = normalize(actual), normalize(expected)
    # CEF uses NBSP to preserve typed consecutive/leading spaces in HTML.
    # Permit only that directional substitution, keeping every character,
    # newline and space count. An explicitly requested NBSP must stay NBSP.
    return len(actual) == len(expected) and all(
        a == e or (a == '\u00a0' and e == ' ')
        for a, e in zip(actual, expected)
    )


def enabled_state(w) -> bool:
    try:
        return bool(w.is_enabled())
    except Exception:
        return False


def literal_keys(value: str) -> str:
    """Escape keyboard syntax and encode non-BMP text for Win32 Unicode input."""
    if '\t' in value:
        raise AutomationError('탭 문자는 화면 입력에서 지원하지 않습니다. 공백으로 바꿔주세요.')
    mapping = {'{': '{{}', '}': '{}}', '+': '{+}', '^': '{^}', '%': '{%}',
               '~': '{~}', '(': '{(}', ')': '{)}', '\n': '{ENTER}'}
    keys = []
    for char in value:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise AutomationError('잘못된 유니코드 문자가 있습니다. 본문을 다시 입력하세요.')
        if code > 0xFFFF:
            # pywinauto puts ord(char) into a 16-bit KEYBDINPUT.wScan. Emit
            # the UTF-16 pair explicitly instead of truncating the code point.
            code -= 0x10000
            keys.extend((chr(0xD800 + (code >> 10)), chr(0xDC00 + (code & 0x3FF))))
        else:
            keys.append(mapping.get(char, char))
    return ''.join(keys)


def attachment_names(w) -> list[str]:
    """Read complete attachment rows without treating columns/headers as files."""
    count_method = getattr(w, 'item_count', None)
    get_item = getattr(w, 'get_item', None)
    backend = getattr(getattr(w, 'backend', None), 'name', '')
    count = None
    if callable(count_method):
        try:
            count = count_method()
        except Exception as e:
            raise AutomationError('첨부 목록의 전체 행 수를 읽을 수 없습니다.') from e
        if type(count) is not int or not 0 <= count <= 10000:
            raise AutomationError('첨부 목록의 행 수가 올바르지 않습니다.')

    if backend == 'win32' and count is not None and callable(get_item):
        # Native ListView.items() returns every cell, while get_item(row, 0)
        # reads only the filename column. Its items have .text(), no element_info.
        try:
            texts = [get_item(row, 0).text() for row in range(count)]
        except Exception as e:
            raise AutomationError('첨부 목록의 파일명 전체를 읽을 수 없습니다.') from e
    else:
        try:
            rows = [item for item in w.descendants()
                    if getattr(getattr(item, 'element_info', None), 'control_type', '')
                    in ('ListItem', 'DataItem')]
        except Exception as e:
            raise AutomationError('첨부 목록의 항목 전체를 읽을 수 없습니다.') from e
        if count is None and not rows:
            raise AutomationError('첨부 목록의 빈 상태를 확인할 수 없습니다. 목록 영역을 다시 연결하세요.')
        if count is not None and len(rows) != count:
            raise AutomationError('첨부 목록의 전체 행과 읽힌 항목 수가 다릅니다.')
        try:
            texts = [read_text(item) for item in rows]
        except Exception as e:
            raise AutomationError('첨부 목록의 파일명 전체를 읽을 수 없습니다.') from e

    names = []
    for text in texts:
        first_line = normalize(text).splitlines()
        if not first_line or not first_line[0].strip():
            raise AutomationError('파일명이 비어 있는 첨부 항목이 있습니다.')
        names.append(first_line[0].strip().casefold())
    # An authoritative zero-row inventory remains empty even with visible headers.
    return names


def capture(backend: str, role: str, delay: float = 4.0) -> dict:
    if backend not in ('uia', 'win32'):
        raise ValueError('지원하지 않는 인식 방식입니다.')
    with com_session():
        from pywinauto import Desktop
        import win32api
        import win32gui
        time.sleep(max(1, min(delay, 8)))
        x, y = win32api.GetCursorPos()
        w = Desktop(backend=backend).from_point(x, y)
        if role == 'contact':
            for _ in range(4):
                if getattr(w.element_info, 'control_type', '') in ('TreeItem', 'ListItem', 'DataItem'):
                    break
                w = w.parent()
            if getattr(w.element_info, 'control_type', '') not in ('TreeItem', 'ListItem', 'DataItem'):
                raise AutomationError('조직도나 마이리스트의 사용자/그룹 항목을 가리켜 주세요. UIA 방식으로 다시 시도하세요.')
        top = w.top_level_parent()
        pid = top.process_id()
        path = executable(pid)
        if not re.search(r'cool|쿨', Path(path).name, re.I):
            raise AutomationError(f'쿨메신저가 아닌 프로그램입니다: {Path(path).name}. 쿨메신저 화면을 가리켜 주세요.')
        top_title = normalize(top.window_text())
        spec = metadata(w, mutable=role in MUTABLE)
        parents = []
        p = w.parent()
        for _ in range(3):
            if p is None or p == top:
                break
            parent_spec = metadata(p, mutable=True)
            if role == 'contact' and getattr(p.element_info, 'control_type', '') == 'TreeItem':
                parent_name = re.sub(r'\s*\(\d+(?:/\d+)?\)\s*$', '', normalize(p.window_text()))
                parent_spec['name_regex'] = re.escape(parent_name) + r'(?:\s*\(\d+(?:/\d+)?\))?'
            parents.append(parent_spec)
            p = p.parent()
        info = {
            'backend': backend, 'exe': path, 'root_title': top_title,
            'root_class': top.class_name(), 'element': spec, 'parents': parents,
        }
        # A stable unique match is mandatory; no found_index or coordinate fallback.
        eng = WindowsDriver({'roles': {}}, threading.Event())
        eng.resolve(info)
        return {'selector': info, 'text': read_text(w), 'label': spec.get('name') or spec.get('automation_id') or spec.get('class_name'),
                'window': top_title, 'executable': path, 'captured_at': datetime.now().isoformat(timespec='seconds')}


def diagnose() -> dict:
    if sys.platform != 'win32':
        return {'windows': False, 'available': False, 'message': '목록 편집·모의 실행은 가능합니다. 실제 연동은 Windows가 필요합니다.', 'windows_found': []}
    try:
        with com_session():
            from pywinauto import Desktop
            import win32gui
            found = []
            for w in Desktop(backend='win32').windows():
                try:
                    if not w.is_visible():
                        continue
                    pid = w.process_id()
                    path = executable(pid)
                    if re.search('cool|쿨', Path(path).name, re.I):
                        handle = int(w.handle)
                        info = {'title': w.window_text(), 'exe': path, 'class_name': w.class_name(),
                                'hwnd': handle, 'pid': pid, 'enabled': bool(w.is_enabled()),
                                'has_caption': (w.style() & 0x00C00000) == 0x00C00000,
                                'owner_hwnd': int(win32gui.GetWindow(handle, 4) or 0)}
                        found.append(info)
                        button_window = False
                        if Path(path).name.casefold() == 'coolmessenger.exe' and info['class_name'] == '#32770':
                            button_window = info['title'] == '메시지 관리함'
                            if not button_window:
                                # Identify the organization main window by its
                                # native tree control only; never read TreeItems.
                                trees = [tree for tree in w.descendants(class_name='SysTreeView32', control_id=3013)
                                         if tree.class_name() == 'SysTreeView32' and tree.control_id() == 3013]
                                button_window = len(trees) == 1
                        if button_window:
                            # Read native button labels only; never message lists,
                            # rich-edit contents, or browser document descendants.
                            info['buttons'] = []
                            for button in w.descendants(class_name='Button'):
                                if button.class_name() != 'Button':
                                    continue
                                info['buttons'].append({
                                    'control_id': int(button.control_id()), 'name': button.window_text(),
                                    'enabled': bool(button.is_enabled()), 'visible': bool(button.is_visible()),
                                    'class_name': 'Button',
                                })
                except Exception:
                    continue
            return {'windows': True, 'available': bool(found), 'message': '쿨메신저 화면을 찾았습니다.' if found else '쿨메신저를 실행하고 로그인하세요.',
                    'windows_found': found}
    except Exception as e:
        return {'windows': True, 'available': False, 'message': f'연동 모듈 확인 필요: {e}', 'windows_found': []}


def recipient_identity(value: str):
    raw_value = str(value or '')
    value = normalize(value)
    organization = re.match(r'^\(([^()\n]+)\((\d+)\)', value)
    if organization:
        depth = 1
        for character in value[organization.end():]:
            if character == '(':
                depth += 1
            elif character == ')':
                depth -= 1
                if depth == 0:
                    break
        if depth != 0:
            organization = None
    if organization and any(account != organization[2]
                            for account in re.findall(r'\((\d+)\)', value)):
        return ('exact', value)
    chip = re.fullmatch(r'([^()\n]+)\((\d+)\)(?:\([^()\n]*\))?', value)
    described_chip = extra_close_chip = None    if not any(character in raw_value for character in '\r\n\v\f\x85\u2028\u2029'):
        described_chip = re.fullmatch(
            r'(?P<name>[^()\x00-\x1f]+)\((?P<account>\d+)\)[ \t]+'
            r'(?P<description>[^()\x00-\x1f]+)\((?P=name)\)', value)
        if described_chip and not described_chip['description'].strip():
            described_chip = None
        extra_close_chip = re.fullmatch(            r'(?P<name>[^()\x00-\x1f]+)\((?P<account>\d+)\)\)\((?P=name)\)', value)    match = organization or chip or described_chip or extra_close_chip    return ('gentoo_account', match[1], match[2]) if match else ('exact', value)


def is_gentoo_profile(profile: dict) -> bool:
    """Recognize the supported adapter, never trust a client readiness flag."""
    roles = profile.get('roles', {})
    for key, cid, cls in (('title', 4362, 'Edit'), ('scheduled_check', 1660, 'Button'),
                          ('send', 3249, 'Button'), ('datetime', 1711, 'SysDateTimePick32')):
        spec = roles.get(key, {})
        if (spec.get('backend') != 'win32' or spec.get('root_class') != '#32770'
                or spec.get('element', {}).get('control_id') != cid
                or spec.get('element', {}).get('class_name') != cls):
            return False
    for key, reader in (('body', 'gentoo_body'), ('recipient_read', 'gentoo_recipients'),
                         ('cc_read', 'gentoo_cc'), ('attach_list', 'gentoo_empty_attachments')):
        if roles.get(key, {}).get('reader') != reader:
            return False
        if key != 'body' and (roles[key].get('backend') != 'win32'
                              or roles[key].get('element') != {'class_name':'#32770'}
                              or roles[key].get('parents', []) != []):
            return False
    body = roles['body']
    if (body.get('backend') != 'uia' or body.get('element', {}).get('control_type') != 'Document'
            or body.get('parents', []) != [{'control_type': 'Pane', 'automation_id': 'editFrame'}]):
        return False
    required = ('title', 'scheduled_check', 'send', 'datetime', 'body', 'recipient_read', 'cc_read', 'attach_list')
    specs = [roles[k] for k in required]
    exes = {str(s.get('exe', '')).casefold() for s in specs}
    titles = {s.get('root_title') for s in specs}
    return (len(exes) == 1 and next(iter(exes)).replace('\\', '/').rsplit('/', 1)[-1] == 'coolmessenger.exe'
            and len(titles) == 1 and next(iter(titles)) in ('메시지 전송 (크롬에디터)', '메시지 전송')
            and all(s.get('root_class') == '#32770' for s in specs)
            and profile.get('multi_select', {}).get('kind') == 'gentoo_picker_v1')


def recipient_list_matches(actual: str, expected: list[str]) -> bool:
    actual_items = normalize(actual).splitlines()
    expected_items = [line for item in expected for line in normalize(item).splitlines()]
    a = [recipient_identity(v) for v in actual_items]
    e = [recipient_identity(v) for v in expected_items]
    return len(a) == len(set(a)) and sorted(a) == sorted(e)


def profile_errors(profile: dict, recipient: str, attachments: bool = False, read_only: bool = False) -> list[str]:
    roles = profile.get('roles', {})
    errors = []
    for k in (('title', 'body', 'recipient_read', 'scheduled_check') if read_only else
              ('title', 'body', 'recipient_read', 'scheduled_check', 'send')):
        if k not in roles:
            errors.append(ROLE_LABELS[k] + ' 연결 필요')
    if 'datetime' not in roles and not {'date', 'time'}.issubset(roles):
        errors.append('예약일시 입력칸 연결 필요')
    contact = profile.get('contacts', {}).get(recipient)
    if not contact:
        errors.append(f'수신자 프리셋 연결 필요: {recipient}')
    elif not contact.get('expected') or (not read_only and not contact.get('selector')):
        errors.append(f'수신자 프리셋의 열기 항목·표시 내용 연결 필요: {recipient}')
    if attachments:
        for k in (('attach_list',) if read_only else ('attach', 'attach_list', 'file_name', 'file_open')):
            if k not in roles:
                errors.append(ROLE_LABELS[k] + ' 연결 필요')
        if roles.get('attach_list', {}).get('reader') == 'gentoo_empty_attachments':
            errors.append('파일 첨부를 사용하려면 첨부 목록 전체를 별도로 연결하세요. 자동 연결은 무첨부 상태만 확인합니다.')
    # All operations must be tied to exactly one actual installation.
    exes = {str(v.get('exe', '')).casefold() for v in roles.values()}
    if not read_only and contact and contact.get('selector'):
        exes.add(contact['selector'].get('exe', '').casefold())
    if len(exes) > 1:
        errors.append('서로 다른 쿨메신저 실행파일의 화면 연결이 섞였습니다.')
    return errors


class WindowsDriver:
    def __init__(self, profile: dict, stop: threading.Event):
        self.profile = profile
        self.roles = profile.get('roles', {})
        self.stop = stop
        self.read_only = False

    def require_input(self):
        if self.read_only:
            raise AutomationError('작성창 대조에서는 화면 입력·클릭·전송을 실행할 수 없습니다.')

    def checkpoint(self):
        if self.stop.is_set():
            raise Stopped('사용자가 중지했습니다.')
        if sys.platform == 'win32':
            import win32api
            if win32api.GetAsyncKeyState(0x77) & 0x8000:  # F8
                self.stop.set()
                raise Stopped('F8 긴급 중지')
            import win32gui
            if not win32gui.GetForegroundWindow():
                raise Stopped('Windows 화면이 잠겨 있거나 활성 데스크톱을 확인할 수 없습니다.')

    def wait(self, seconds: float):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.checkpoint()
            time.sleep(.1)

    def roots(self, spec: dict):
        self.checkpoint()
        from pywinauto import Desktop        from pywinauto.controls import InvalidWindowHandle        # A different transient window may disappear while Desktop creates its        # wrappers. Refresh that enumeration only; failure is never an empty list.        for attempt in range(3):            self.checkpoint()            try:                windows = Desktop(backend=spec['backend']).windows()                break            except InvalidWindowHandle as exc:                if attempt == 2:                    raise PreparationTransient('창 목록을 읽는 동안 창이 변경되었습니다. 입력을 중단합니다.') from exc                self.wait(.1)
        roots = []
        for w in windows:
            try:
                if w.class_name() != spec['root_class'] or normalize(w.window_text()) != spec['root_title']:
                    continue
                if executable(w.process_id()).casefold() == spec['exe'].casefold():
                    roots.append(w)
            except Exception:
                continue
        return roots

    def resolve(self, spec: dict):
        parents = spec.get('parents', [])
        native_contact = (spec.get('backend') == 'uia'
                          and spec.get('element', {}).get('control_type') == 'TreeItem'
                          and recipient_identity(spec.get('element', {}).get('name', ''))[0] == 'gentoo_account'
                          and bool(parents) and parents[-1].get('class_name') == 'SysTreeView32'
                          and parents[-1].get('control_id') == 3013
                          and all(p.get('control_type') == 'TreeItem' for p in parents[:-1]))
        roots = self.roots(dict(spec, backend='win32') if native_contact else spec)
        if len(roots) != 1:
            raise AutomationError(f'“{spec["root_title"]}” 창을 하나만 열어주세요. 일치하는 창: {len(roots)}개')
        candidates = []
        root = roots[0]
        if native_contact:
            from .gentoo_tree import snapshot_tree, NativeContact
            trees = [w for w in root.descendants()
                     if w.class_name() == 'SysTreeView32' and w.control_id() == 3013]
            if len(trees) != 1:
                raise AutomationError('쿨메신저 조직도 전체를 유일하게 확인할 수 없습니다.')
            wanted = recipient_identity(spec['element']['name'])
            for record in snapshot_tree(trees[0]):
                if recipient_identity(record['text']) != wanted or len(record['parents']) != len(parents)-1:
                    continue
                ok = True
                for actual, expected in zip(record['parents'], parents[:-1]):
                    if expected.get('name_regex'):
                        same = re.fullmatch(expected['name_regex'], normalize(actual)) is not None
                    else:
                        same = normalize(actual) == normalize(expected.get('name'))
                    if not same:
                        ok = False
                        break
                if ok:
                    candidates.append(NativeContact(record['item']))
            if len(candidates) != 1:
                raise AutomationError(f'수신자 계정과 부서 경로가 유일하지 않습니다({len(candidates)}개). 조직도 계정을 다시 확인하세요.')
            return candidates[0]
        if spec.get('reader') in ('gentoo_recipients', 'gentoo_cc', 'gentoo_empty_attachments'):
            return root
        for w in [root] + root.descendants():
            try:
                if not w.is_visible() or not matches(w, spec['element']):
                    continue
                p = w.parent()
                ok = True
                for parent_spec in spec.get('parents', []):
                    if p is None or not matches(p, parent_spec):
                        ok = False
                        break
                    p = p.parent()
                if ok:
                    candidates.append(w)
            except Exception:
                continue
        if len(candidates) != 1:
            raise AutomationError(f'화면 요소가 유일하지 않습니다({len(candidates)}개). “화면 연결”에서 다시 지정하세요.')
        return candidates[0]

    def role(self, key: str):
        if key not in self.roles:
            raise AutomationError(ROLE_LABELS.get(key, key) + ' 연결이 필요합니다.')
        return self.resolve(self.roles[key])

    def _activate_top_level(self, handle, expected_pid):
        """Activate once per supported route, then require the exact target HWND."""
        import win32gui
        import win32process

        def valid_target():
            if (not win32gui.IsWindow(handle)
                    or win32process.GetWindowThreadProcessId(handle)[1] != expected_pid):
                raise AutomationError('활성화할 쿨메신저 창이 변경되었습니다. 입력하지 않고 멈춥니다.')

        valid_target()
        if win32gui.IsIconic(handle):
            win32gui.ShowWindow(handle, 9)  # SW_RESTORE
        elif win32gui.GetForegroundWindow() != handle and win32gui.IsWindowVisible(handle):            # Request activation without changing a visible window's size or            # position. Only minimized windows use the restoring show state.            win32gui.ShowWindow(handle, 5)  # SW_SHOW, once for this exact HWND        valid_target()        if win32gui.GetForegroundWindow() == handle:
            return
        try:
            win32gui.SetForegroundWindow(handle)
        except Exception:
            # Win32 may report failure even if activation just completed.
            pass
        valid_target()
        if win32gui.GetForegroundWindow() == handle:
            return

        # Try UI Automation before using the window's verified title bar.        # Never attach input queues around SetForegroundWindow.        try:
            from pywinauto import Desktop
            target = Desktop(backend='uia').window(handle=handle).wrapper_object()
            if target.handle != handle or target.process_id() != expected_pid:
                raise AutomationError('UI Automation의 활성화 대상 창이 일치하지 않습니다.')
            valid_target()
            target.element_info.element.SetFocus()
        except AutomationError:            raise        except Exception:            valid_target()
        for attempt in range(7):
            self.checkpoint()
            valid_target()
            if win32gui.GetForegroundWindow() == handle:
                return
            if attempt < 6:
                self.wait(.1)
        self._activate_by_caption(handle, expected_pid)        valid_target()        if win32gui.GetForegroundWindow() != handle:            raise AutomationError('쿨메신저 창 활성화를 확인하지 못했습니다. 입력하지 않고 멈춥니다.')    def _activate_by_caption(self, handle, expected_pid):
        """Click one verified native title-bar point, never client controls."""
        self.require_input()
        self.checkpoint()
        import win32api
        import win32gui
        import win32process

        def valid_window(initial=False):
            if (not win32gui.IsWindow(handle)
                    or win32process.GetWindowThreadProcessId(handle)[1] != expected_pid
                    or not win32gui.IsWindowVisible(handle)
                    or not win32gui.IsWindowEnabled(handle)):
                raise AutomationError('활성화할 창의 제목표시줄을 확인할 수 없습니다. 클릭하지 않고 멈춥니다.')
            if win32gui.GetWindowLong(handle, -16) & 0x00C00000 != 0x00C00000:
                error = CaptionUnavailable if initial else AutomationError
                raise error('활성화할 창에 표준 제목표시줄이 없습니다. 제목표시줄을 클릭하지 않습니다.')

        valid_window(initial=True)
        if win32gui.IsIconic(handle):
            win32gui.ShowWindow(handle, 9)
        if win32gui.IsIconic(handle):
            raise AutomationError('최소화된 창은 클릭하지 않습니다. 창 복원을 확인하지 못했습니다.')
        if win32gui.GetForegroundWindow() == handle:
            return
        # HWND_TOP without activation, movement, resizing, or topmost status.
        win32gui.SetWindowPos(handle, 0, 0, 0, 0, 0, 0x0013)
        valid_window()
        bounds = win32gui.GetWindowRect(handle)
        client_origin = win32gui.ClientToScreen(handle, (0, 0))
        left, top, right, bottom = bounds
        point = (left + (right - left) // 3, top + (client_origin[1] - top) // 2)
        if (not left < point[0] < right or not top < point[1] < min(bottom, client_origin[1])
                or any(value < -32768 or value > 32767 for value in point)):
            raise AutomationError('제목표시줄 좌표를 확인할 수 없습니다. 클릭하지 않고 멈춥니다.')

        def verify_point():
            valid_window()
            if (win32gui.IsIconic(handle) or win32gui.GetWindowRect(handle) != bounds
                    or win32gui.ClientToScreen(handle, (0, 0)) != client_origin):
                raise AutomationError('클릭 전에 창 위치가 바뀌었습니다. 클릭하지 않고 멈춥니다.')
            hit = win32gui.WindowFromPoint(point)
            if (not hit or win32gui.GetAncestor(hit, 2) != handle
                    or win32process.GetWindowThreadProcessId(hit)[1] != expected_pid):
                raise AutomationError('제목표시줄이 다른 창에 가려져 있습니다. 클릭하지 않고 멈춥니다.')
            packed = ((point[1] & 0xFFFF) << 16) | (point[0] & 0xFFFF)
            success, area = win32gui.SendMessageTimeout(handle, 0x0084, 0, packed, 0x0003, 200)
            if not success or area != 2:  # WM_NCHITTEST must report HTCAPTION.
                raise AutomationError('안전한 제목표시줄 영역이 아닙니다. 클릭하지 않고 멈춥니다.')

        verify_point()
        self.checkpoint()
        win32api.SetCursorPos(point)
        if win32api.GetCursorPos() != point:
            raise AutomationError('실제 커서 위치가 제목표시줄과 다릅니다. 클릭하지 않고 멈춥니다.')
        verify_point()
        self.checkpoint()
        try:
            win32api.mouse_event(0x0002, 0, 0, 0)
        finally:
            win32api.mouse_event(0x0004, 0, 0, 0)
        for attempt in range(4):
            valid_window()
            if win32gui.GetForegroundWindow() == handle:
                return
            if attempt < 3:
                self.wait(.1)
        raise AutomationError('제목표시줄 클릭 후 창 활성화를 확인하지 못했습니다. 추가 클릭 없이 멈춥니다.')
    def _activate_native_contact(self, contact, handle, expected_pid):
        """Activate a captionless main window using only its verified contact text."""
        self.require_input()
        self.checkpoint()
        from .gentoo_tree import NativeContact
        import win32api
        import win32gui
        import win32process
        if not isinstance(contact, NativeContact):
            raise AutomationError('조직도 연락처만 이 방식으로 활성화할 수 있습니다.')
        tree_handle = contact.tree_ctrl.handle
        expected_name = contact.element_info.name

        def valid_target():
            if (not isinstance(tree_handle, int) or not tree_handle
                    or not win32gui.IsWindow(tree_handle)
                    or not win32gui.IsWindow(handle)
                    or win32gui.GetAncestor(tree_handle, 2) != handle
                    or win32process.GetWindowThreadProcessId(tree_handle)[1] != expected_pid
                    or win32process.GetWindowThreadProcessId(handle)[1] != expected_pid
                    or not win32gui.IsWindowVisible(handle)
                    or not win32gui.IsWindowEnabled(handle)
                    or not win32gui.IsWindowVisible(tree_handle)
                    or not win32gui.IsWindowEnabled(tree_handle)
                    or win32gui.IsIconic(handle)):
                raise AutomationError('활성화할 조직도 창이 변경되거나 가려졌습니다. 클릭하지 않고 멈춥니다.')
            if not expected_name or contact.window_text() != expected_name:
                raise AutomationError('활성화할 조직도 연락처가 변경되었습니다. 클릭하지 않고 멈춥니다.')

        def text_rect():
            rect = contact.item.client_rect(text_area_rect=True)
            if rect is None:
                raise AutomationError('조직도 연락처 글자 위치를 확인할 수 없습니다.')
            return (rect.left, rect.top, rect.right, rect.bottom)

        valid_target()
        if win32gui.GetForegroundWindow() == handle:
            return
        was_topmost = bool(win32gui.GetWindowLong(handle, -20) & 0x00000008)
        valid_target()
        try:
            # Temporary topmost is restricted to this verified main HWND. It does
            # not activate, resize, move, minimize, or close any window.
            win32gui.SetWindowPos(handle, 0 if was_topmost else -1, 0, 0, 0, 0, 0x0013)
            contact.item.ensure_visible()
            valid_target()
            bounds = text_rect()  # TVM_GETITEMRECT is relative to the TreeView client.
            left, top, right, bottom = bounds
            point_client = ((left + right) // 2, (top + bottom) // 2)
            client_bounds = win32gui.GetClientRect(tree_handle)
            if (not left < right or not top < bottom
                    or not client_bounds[0] <= point_client[0] < client_bounds[2]
                    or not client_bounds[1] <= point_client[1] < client_bounds[3]):
                raise AutomationError('조직도 연락처 글자가 화면 안에 없습니다. 클릭하지 않고 멈춥니다.')
            point = win32gui.ClientToScreen(tree_handle, point_client)

            def observe_point():                valid_target()
                if (text_rect() != bounds or win32gui.GetClientRect(tree_handle) != client_bounds
                        or win32gui.ClientToScreen(tree_handle, point_client) != point):
                    raise AutomationError('클릭 전에 조직도 연락처 위치가 바뀌었습니다. 클릭하지 않고 멈춥니다.')
                # Strict HWND equality also excludes a different child in this app.
                return win32gui.WindowFromPoint(point)            def blocked_point(hit):                error = PreparationTransient('조직도 연락처가 다른 창에 가려져 있습니다. 클릭하지 않고 멈춥니다.')                hit_root = hit_pid = target_pid = foreground = foreground_pid = 0                target_topmost = -1                if hit:                    try:
                        hit_root = win32gui.GetAncestor(hit, 2)                        hit_pid = win32process.GetWindowThreadProcessId(hit)[1]                    except Exception:
                        pass
                try:                    target_pid = win32process.GetWindowThreadProcessId(handle)[1]                    target_topmost = int(bool(win32gui.GetWindowLong(handle, -20) & 0x00000008))                except Exception:                    pass                try:                    foreground = win32gui.GetForegroundWindow()                    if foreground:                        foreground_pid = win32process.GetWindowThreadProcessId(foreground)[1]                except Exception:                    pass                error.window_state = {                    'target_hwnd': handle, 'tree_hwnd': tree_handle,                    'hit_hwnd': hit, 'hit_root_hwnd': hit_root, 'hit_pid': hit_pid,                    'target_pid': target_pid, 'point_x': point[0], 'point_y': point[1],                    'client_x': point_client[0], 'client_y': point_client[1],                    'target_topmost': target_topmost, 'foreground_hwnd': foreground,                    'foreground_pid': foreground_pid,                }                return error
            def verify_point():                hit = observe_point()                if hit != tree_handle:                    raise blocked_point(hit)            # Promotion/scrolling can settle asynchronously. Observe only before            # moving the cursor; never repeat promotion or retry a physical click.            from .waiting import stable_observation            last_hit = 0            def initial_point():                nonlocal last_hit                last_hit = observe_point()                return last_hit            try:                stable_observation(initial_point, lambda hit: hit == tree_handle,                                   timeout=.5, interval=.1, samples=2,                                   checkpoint=self.checkpoint, wait=self.wait,                                   clock=time.monotonic)            except TimeoutError:                raise blocked_point(last_hit) from None            self.checkpoint()
            win32api.SetCursorPos(point)
            if win32api.GetCursorPos() != point:
                raise AutomationError('실제 커서 위치가 조직도 연락처와 다릅니다. 클릭하지 않고 멈춥니다.')
            verify_point()
            self.checkpoint()
            try:
                win32api.mouse_event(0x0002, 0, 0, 0)
            finally:
                win32api.mouse_event(0x0004, 0, 0, 0)
            for attempt in range(4):
                valid_target()
                if win32gui.GetForegroundWindow() == handle:
                    break
                if attempt < 3:
                    self.wait(.1)
            else:
                raise AutomationError('조직도 클릭 후 창 활성화를 확인하지 못했습니다. 추가 클릭 없이 멈춥니다.')
        finally:
            # Even a failed/uncertain promotion may have changed the style.
            # A vanished or reused HWND must never be manipulated during cleanup.
            if (not was_topmost and win32gui.IsWindow(handle)
                    and win32process.GetWindowThreadProcessId(handle)[1] == expected_pid):
                win32gui.SetWindowPos(handle, -2, 0, 0, 0, 0, 0x0013)
                if win32gui.GetWindowLong(handle, -20) & 0x00000008:
                    raise AutomationError('쿨메신저 창의 원래 표시 상태를 복원하지 못했습니다.')
        valid_target()
        if win32gui.GetForegroundWindow() != handle:
            raise AutomationError('창 표시 상태 복원 후 활성화가 바뀌었습니다. 추가 클릭 없이 멈춥니다.')

    def focus(self, w):
        self.require_input()
        self.checkpoint()
        import win32gui
        import win32process
        from .gentoo_tree import NativeContact
        # GA_ROOT follows parent windows only; pywinauto's top_level_parent
        # can walk an owned dialog up to its hidden single-instance owner.
        native = getattr(w, 'tree_ctrl', w)
        handle = None
        for _ in range(16):
            native_handle = getattr(native, 'handle', None)
            if isinstance(native_handle, int) and native_handle:
                handle = win32gui.GetAncestor(native_handle, 2)  # GA_ROOT, not GA_ROOTOWNER
                if not handle:
                    raise AutomationError('입력 컨트롤의 실제 최상위 창을 확인할 수 없습니다.')
                break
            parent = getattr(native, 'parent', None)
            if not callable(parent):
                break
            native = parent()
            if native is None:
                break
        if handle is None:
            top = w.top_level_parent()
            handle = getattr(top, 'handle', None)
            if isinstance(handle, int) and handle:
                handle = win32gui.GetAncestor(handle, 2)
                if not handle:
                    raise AutomationError('입력 컨트롤의 실제 최상위 창을 확인할 수 없습니다.')
        if isinstance(handle, int) and handle:
            # Activate the actual top-level HWND; native wrapper set_focus moves
            # the cursor and can try to activate a child window instead.
            try:
                self._activate_top_level(handle, w.process_id())
            except CaptionUnavailable:
                if not isinstance(w, NativeContact):
                    raise
                self._activate_native_contact(w, handle, w.process_id())
        else:
            top.set_focus()
        if getattr(w.element_info, 'control_type', '') == 'Document':
            # CEF SetFocus can leave the caret outside its contenteditable frame.
            self.physical_click(w)
        elif not isinstance(w, NativeContact):
            # NativeContact sends TreeView mouse messages directly. Giving its
            # child keyboard focus first can undo the confirmed activation.
            keyboard_target = getattr(w, 'tree_ctrl', w)
            keyboard_focus = getattr(keyboard_target, 'set_keyboard_focus', None)
            if callable(keyboard_focus):
                keyboard_focus()
            else:
                w.set_focus()
        self.wait(.15)
        foreground = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(foreground)
        if pid != w.process_id() or (isinstance(handle, int) and handle and foreground != handle):
            raise AutomationError('입력 대상 프로그램이 활성화되지 않았습니다. 다른 창을 닫고 다시 확인하세요.')

    def physical_click(self, w, double=False):
        """Click at desktop coordinates without primary-monitor normalization."""
        self.require_input()
        self.checkpoint()
        import win32api
        rect = w.rectangle()
        if rect.right <= rect.left or rect.bottom <= rect.top:
            raise AutomationError('클릭 대상의 실제 위치를 확인할 수 없습니다.')
        point = ((rect.left+rect.right)//2, (rect.top+rect.bottom)//2)
        win32api.SetCursorPos(point)
        if win32api.GetCursorPos() != point:
            raise AutomationError('클릭 위치가 대상과 다릅니다. 클릭하지 않고 멈춥니다.')
        for _ in range(2 if double else 1):
            # SetCursorPos accepts the entire virtual desktop, including negative
            # coordinates. Relative button events leave that position unchanged.
            win32api.mouse_event(0x0002, 0, 0, 0)
            win32api.mouse_event(0x0004, 0, 0, 0)

    def click(self, w, double=False):
        self.require_input()
        self.focus(w)
        if not enabled_state(w):
            raise AutomationError('비활성화된 버튼은 누르지 않습니다.')
        if double:
            if hasattr(w, 'tree_ctrl'):
                w.double_click_input()
            else:
                self.physical_click(w, double=True)
        else:
            native_button = (callable(getattr(w, 'get_check_state', None))
                             and callable(getattr(w, 'send_message_timeout', None))
                             and callable(getattr(w, 'class_name', None))
                             and w.class_name() == 'Button')
            ctype = getattr(getattr(w, 'element_info', None), 'control_type', '')
            select = getattr(w, 'select', None)
            invoke = getattr(w, 'invoke', None)
            try:
                if native_button:
                    # Gentoo subclasses BS_OWNERDRAW buttons and ignores BM_CLICK.
                    # Send their mouse messages in HWND client coordinates.
                    # Never retry an action with an uncertain result.
                    style = getattr(w, 'style', None)
                    if callable(style) and style() & 0xF == 0xB:
                        rect = w.client_rect()
                        point = ((rect.left+rect.right)//2, (rect.top+rect.bottom)//2)
                        # Gentoo's mouse handler needs MK_LBUTTON on DOWN and
                        # no held-button flag on UP; click() uses one flag for both.
                        w.press_mouse(pressed='left', coords=point)
                        w.release_mouse(pressed='', coords=point)
                    else:
                        w.send_message_timeout(0x00F5, 0, 0)
                elif ctype in ('TabItem', 'ListItem', 'DataItem', 'TreeItem') and callable(select):
                    select()
                elif callable(invoke):
                    invoke()
                else:
                    self.physical_click(w)
            except Exception as e:
                # Do not retry after a possibly successful Invoke operation.
                raise AutomationError(f'버튼 실행 결과를 확인할 수 없습니다: {e}') from e
        self.wait(.3)

    def text(self, key: str) -> str:
        reader = self.roles.get(key, {}).get('reader')
        if reader in ('gentoo_recipients', 'gentoo_cc', 'gentoo_empty_attachments'):
            from .gentoo import read_gentoo_chips
            if reader == 'gentoo_recipients':
                from .gentoo import read_gentoo_recipients
                return read_gentoo_recipients(self.role(key), self.profile)
            return read_gentoo_chips(self.role(key), reader)
        w = self.role(key)
        gentoo_body = reader == 'gentoo_body'
        if key in ('body', 'result_body') and not gentoo_body:
            # Upgrade already-saved Gentoo bindings without forcing users to
            # capture the same editor again. Other document readers stay intact.
            try:
                gentoo_body = (w.element_info.control_type == 'Document'
                               and w.parent().element_info.automation_id == 'editFrame')
            except Exception:
                pass
        if gentoo_body:
            from .gentoo import read_gentoo_body
            return read_gentoo_body(w)
        return read_text(w)

    def put(self, key: str, value: str):
        self.require_input()
        w = self.role(key)
        self.focus(w)
        try:
            if getattr(w.element_info, 'control_type', '') == 'Document':
                raise AttributeError('Use literal keystrokes for the Chromium editor')
            w.set_edit_text(value)
        except Exception:
            # For rich HTML editors: real Unicode keystrokes, not clipboard data.
            # Modifiers are escaped so message text cannot act as hotkeys.
            escaped = literal_keys(value)
            # focus() has already placed the caret inside CEF. Calling UIA
            # SetFocus again here moves it back out of the editable iframe.
            w.type_keys('^a', set_foreground=False)
            w.type_keys(escaped or '{BACKSPACE}', with_spaces=True, pause=.001, vk_packet=True, set_foreground=False)
        self.wait(.2)
        actual = self.text(key)
        same = (body_matches(actual, value) if key == 'body' else                title_matches(actual, value, gentoo_placeholder=is_gentoo_profile(self.profile))                if key == 'title' else actual == normalize(value))
        if not same:
            raise AutomationError(ROLE_LABELS[key] + '의 입력 결과가 원문과 다릅니다. 보내지 않고 멈춥니다.')

    def set_schedule(self, scheduled: str):
        self.require_input()
        c = self.role('scheduled_check')
        state = self.check_state(c)
        if state != 1:
            self.focus(c)
            if hasattr(c, 'toggle'):
                c.toggle()
            else:
                # BM_SETCHECK changes the check mark without notifying the app.
                # Use the actual button action so reservation controls activate.
                self.click(c)
        self.wait(.2)
        if self.check_state(self.role('scheduled_check')) != 1:
            raise AutomationError('예약전송 체크 상태를 확인할 수 없습니다.')
        dt = parse_time(scheduled)
        for role in (['datetime'] if 'datetime' in self.roles else ['date', 'time']):
            self.set_datetime(role, dt)
        self.verify_schedule(scheduled)

    def check_state(self, w):
        style = getattr(w, 'style', None)
        if (is_gentoo_profile(self.profile) and callable(style)
                and w.class_name() == 'Button' and style() & 0xF == 0xB):
            from .gentoo import read_gentoo_schedule_state
            return read_gentoo_schedule_state(w)
        try:
            return int(w.get_toggle_state())
        except Exception:
            try:
                if (callable(style) and w.class_name() == 'Button'
                        and style() & 0xF not in (2, 3, 4, 5, 6, 9)):
                    raise AutomationError('이 버튼 형식은 일반 체크 상태 조회를 지원하지 않습니다.')
                return int(w.get_check_state())
            except Exception as e:
                raise AutomationError('예약 체크박스의 실제 상태를 읽을 수 없습니다.') from e

    def native_date(self, role: str):
        from pywinauto import Desktop
        w = self.role(role)
        h = getattr(w, 'handle', None) or getattr(w.element_info, 'handle', None)
        if h and ('SysDateTimePick32' in w.class_name()):
            return Desktop(backend='win32').window(handle=h).wrapper_object()
        return w

    def set_datetime(self, role: str, dt: datetime):
        self.require_input()
        w = self.native_date(role)
        self.focus(w)
        if hasattr(w, 'set_time'):
            w.set_time(year=dt.year, month=dt.month, day=dt.day, hour=dt.hour, minute=dt.minute, second=0)
        else:
            fmt = {'datetime': '%Y-%m-%d %H:%M', 'date': '%Y-%m-%d', 'time': '%H:%M'}[role]
            self.put(role, dt.strftime(fmt))
        self.wait(.2)

    def get_datetime(self, role: str, target: datetime) -> bool:
        w = self.native_date(role)
        if hasattr(w, 'get_time'):
            t = w.get_time()
            date_ok = (t.wYear, t.wMonth, t.wDay) == (target.year, target.month, target.day)
            time_ok = (t.wHour, t.wMinute) == (target.hour, target.minute)
            return (date_ok and time_ok) if role == 'datetime' else date_ok if role == 'date' else time_ok
        fmt = {'datetime': '%Y-%m-%d %H:%M', 'date': '%Y-%m-%d', 'time': '%H:%M'}[role]
        return normalize(read_text(w)) == target.strftime(fmt)

    def verify_schedule(self, scheduled: str):
        if self.check_state(self.role('scheduled_check')) != 1:
            raise AutomationError('예약전송 체크가 해제되어 전송을 차단했습니다.')
        for role in (['datetime'] if 'datetime' in self.roles else ['date', 'time']):
            if not self.get_datetime(role, parse_time(scheduled)):
                raise AutomationError('화면의 예약일시가 목록과 다릅니다. 전송을 차단했습니다.')

    def _can_resume_empty_failed_compose(self, job: dict, opened) -> bool:        """Resume only a failed, exact-recipient, still-empty Gentoo draft."""        if (job.get('status') != 'failed' or job.get('attachments') != []                or not is_gentoo_profile(self.profile)):            return False        hwnd, pid = int(opened.handle), opened.process_id()        for _ in range(2):            self.checkpoint()            roots = self.roots(self.roles['body'])            if (len(roots) != 1 or int(roots[0].handle) != hwnd                    or roots[0].process_id() != pid):                raise AutomationError('작성창이 변경되어 빈 초안을 이어 쓰지 않습니다.')            self.verify_recipient(job)            if (self.text('title') not in ('', '제목을 입력하세요.')                    or self.text('body') or self.text('cc_read')):                return False            self.verify_attachments([])            native = self.role('recipient_read')            if int(native.handle) != hwnd or native.process_id() != pid:                raise AutomationError('네이티브 작성창이 변경되어 빈 초안을 이어 쓰지 않습니다.')        return True    def open_compose(self, job: dict):        self.require_input()
        opened = self.roots(self.roles['body'])
        if opened:
            # A failed preparation may leave this exact message open. Reuse
            # only after all content/account/attachment checks; other drafts
            # are never closed or overwritten automatically.
            try:
                if len(opened) != 1:
                    raise AutomationError('작성창이 여러 개입니다.')
                self.verify_recipient(job)
                if self._can_resume_empty_failed_compose(job, opened[0]):                    from .core import recipient_ids                    from .draft_recovery import discard_empty_draft                    actual = self.text('recipient_read')                    expected = [self.profile['contacts'][cid]['expected'] for cid in recipient_ids(job)]                    if not recipient_list_matches(actual, expected):                        raise AutomationError('수신자가 변경되어 빈 초안을 닫지 않습니다.')                    discard_empty_draft(self, hwnd=int(opened[0].handle),                                        pid=opened[0].process_id(), recipients=actual.splitlines())                    # A fresh snapshot disables a second automatic close in this                    # attempt; use the proven main-window/new-composer route.                    return self.open_compose({**job, 'status': 'draft'})                if (not title_matches(self.text('title'), job['title'], gentoo_placeholder=is_gentoo_profile(self.profile))                        or not body_matches(self.text('body'), job['body'])):                    raise AutomationError('제목 또는 본문이 이번 메시지와 다릅니다.')
                if 'cc_read' in self.roles and self.text('cc_read'):
                    raise AutomationError('참조 수신자가 있습니다.')
                self.verify_attachments(job['attachments'])
            except PreparationTransient:                raise            except AutomationError as exc:                raise AutomationError('이미 메시지 작성창이 열려 있습니다. '+str(exc)+' 해당 초안을 확인하고 닫은 뒤 다시 실행하세요.') from exc
            return True
        from .core import recipient_ids
        ids = recipient_ids(job)
        own = self.profile.get('self_binding', {}).get('expected') if is_gentoo_profile(self.profile) else None
        own_ids = [cid for cid in ids if own and recipient_identity(self.profile['contacts'][cid]['expected'])
                   == recipient_identity(own)]
        explicit_ids = [cid for cid in ids if cid not in own_ids]
        if explicit_ids and is_gentoo_profile(self.profile):            self.open_multi_compose(explicit_ids)
        else:
            contact = self.profile['contacts'][(explicit_ids or ids)[0]]
            target = self.resolve(contact['selector'])
            if own_ids and not explicit_ids:
                self._open_gentoo_self_compose(target)
            else:
                self.click(target, double=True)
        deadline = time.monotonic() + 6
        while True:
            try:
                self.role('body')
                break
            except AutomationError:
                if time.monotonic() > deadline:
                    raise AutomationError('메시지 전송창이 열리지 않았습니다. 더블클릭 동작이 “메시지 보내기”인지 확인하세요.')
                self.wait(.2)
        self.sync_self_recipient(bool(own_ids))
        self.verify_recipient(job)
        if 'cc_read' in self.roles and self.text('cc_read'):
            raise AutomationError('참조 수신자가 있습니다. 이 버전은 참조가 없는 메시지만 처리합니다.')
        if 'attach_list' in self.roles:
            self.verify_attachments([])

    def _open_gentoo_self_compose(self, contact):
        """Use the observed main-window New Message shortcut for self only."""
        self.require_input()        from .gentoo import gentoo_self_expected        from .gentoo_tree import NativeContact        if (not is_gentoo_profile(self.profile) or not isinstance(contact, NativeContact)                or recipient_identity(contact.window_text()) != recipient_identity(gentoo_self_expected(self.profile))):            raise AutomationError('본인 조직도 항목을 정확히 확인하지 못했습니다.')        return self._open_gentoo_blank_compose(contact)    def _open_gentoo_blank_compose(self, contact):        """Open New Message without a coordinate-dependent contact double-click."""        self.require_input()
        self.checkpoint()
        from .gentoo_tree import NativeContact
        import win32gui
        import win32process
        if not is_gentoo_profile(self.profile) or not isinstance(contact, NativeContact):
            raise AutomationError('본인 새 작성창은 확인된 쿨메신저 조직도에서만 열 수 있습니다.')
        expected = contact.element_info.name        tree = contact.tree_ctrl
        tree_handle = tree.handle
        expected_pid = contact.process_id()

        def valid_main(require_foreground=False):
            if (not isinstance(tree_handle, int) or not tree_handle
                    or not win32gui.IsWindow(tree_handle)
                    or tree.class_name() != 'SysTreeView32' or tree.control_id() != 3013
                    or not tree.is_visible() or not tree.is_enabled()
                    or win32process.GetWindowThreadProcessId(tree_handle)[1] != expected_pid
                    or recipient_identity(contact.window_text()) != recipient_identity(expected)):
                raise AutomationError('본인 조직도 항목이 변경되었습니다. 작성 단축키를 보내지 않습니다.')
            root = win32gui.GetAncestor(tree_handle, 2)
            if (not root or not win32gui.IsWindow(root) or win32gui.GetClassName(root) != '#32770'
                    or win32process.GetWindowThreadProcessId(root)[1] != expected_pid
                    or not win32gui.IsWindowVisible(root) or not win32gui.IsWindowEnabled(root)
                    or (require_foreground and win32gui.GetForegroundWindow() != root)):
                raise AutomationError('쿨메신저 조직도 메인창 활성화를 확인하지 못했습니다. 작성 단축키를 보내지 않습니다.')
            return root

        initial_root = valid_main()
        self.focus(contact)
        self.checkpoint()
        if valid_main(require_foreground=True) != initial_root:
            raise AutomationError('조직도 메인창이 변경되었습니다. 작성 단축키를 보내지 않습니다.')
        selected_ok = tree.send_message_timeout(0x110B, 9, contact.item_key, timeout=.2)        selected = tree.send_message_timeout(0x110A, 9, 0, timeout=.2)        if type(selected_ok) is not int or selected_ok != 1 or selected != contact.item_key:            raise AutomationError('조직도 대상 계정을 선택하지 못했습니다. 작성 단축키를 보내지 않습니다.')        # Top-level activation and keyboard focus are separate. This is only
        # needed for the self-only shortcut, not native contact mouse messages.
        tree.set_keyboard_focus()
        self.checkpoint()
        focused = tree.get_focus()
        focus_handle = getattr(focused, 'handle', 0)
        foreground = win32gui.GetForegroundWindow()
        if focus_handle != tree_handle or foreground != initial_root:
            error = AutomationError('조직도 키보드 포커스를 확인하지 못했습니다. 작성 단축키를 보내지 않습니다.')
            error.window_state = {'focus_hwnd': focus_handle, 'tree_hwnd': tree_handle,
                                  'foreground_hwnd': foreground, 'target_hwnd': initial_root}
            raise error
        if valid_main(require_foreground=True) != initial_root:
            raise AutomationError('조직도 메인창이 변경되었습니다. 작성 단축키를 보내지 않습니다.')
        from pywinauto.keyboard import send_keys
        send_keys('^n')

    def sync_self_recipient(self, include_self: bool):
        self.require_input()
        if not is_gentoo_profile(self.profile):
            return
        from .gentoo import gentoo_self_button, read_gentoo_self_state, gentoo_self_expected
        root = self.role('recipient_read')
        if include_self:
            gentoo_self_expected(self.profile)
        if read_gentoo_self_state(root) != int(include_self):
            self.click(gentoo_self_button(root))
            self.wait(.2)
        if read_gentoo_self_state(root) != int(include_self):
            raise AutomationError('나에게 보내기의 선택 상태가 요청한 수신자와 다릅니다.')

    def verify_recipient(self, job: dict):
        from .core import recipient_ids
        expected = [normalize(self.profile['contacts'][cid]['expected']) for cid in recipient_ids(job)]
        actual = self.text('recipient_read')
        if not recipient_list_matches(actual, expected):
            raise AutomationError('수신자 표시가 저장한 프리셋과 다릅니다. 동명이인·추가 수신자·그룹 변경을 확인하세요.')

    def _click_gentoo_picker_button(self, button, selection_matches):        """Dispatch one owner-draw picker action with the real cursor aligned."""        self.require_input()        self.checkpoint()        import win32api        import win32gui        import win32process        if (not is_gentoo_profile(self.profile) or not callable(selection_matches)                or button.class_name() != 'Button' or button.control_id() not in (1636, 3007)                or button.style() & 0xF != 0xB):            raise AutomationError('확인된 수신자 선택창의 추가·확인 버튼만 실행할 수 있습니다.')        handle, expected_pid = button.handle, button.process_id()        root = win32gui.GetAncestor(handle, 2)        expected_id = button.control_id()        def valid_target(require_foreground=True):            if (not isinstance(handle, int) or not handle or not root                    or not win32gui.IsWindow(handle) or not win32gui.IsWindow(root)                    or button.handle != handle or button.control_id() != expected_id                    or button.class_name() != 'Button' or button.style() & 0xF != 0xB                    or win32gui.GetAncestor(handle, 2) != root                    or win32gui.GetClassName(root) != '#32770'                    or win32gui.GetWindowText(root) != '사용자 선택'                    or win32process.GetWindowThreadProcessId(handle)[1] != expected_pid                    or win32process.GetWindowThreadProcessId(root)[1] != expected_pid                    or not win32gui.IsWindowVisible(handle) or not win32gui.IsWindowEnabled(handle)                    or not win32gui.IsWindowVisible(root) or not win32gui.IsWindowEnabled(root)                    or win32gui.IsIconic(root)                    or (require_foreground and win32gui.GetForegroundWindow() != root)):                raise AutomationError('수신자 선택창의 버튼이나 활성 창이 바뀌었습니다. 클릭하지 않고 멈춥니다.')        valid_target(require_foreground=False)        self.focus(button)        valid_target()        client = win32gui.GetClientRect(handle)        bounds = win32gui.GetWindowRect(handle)        root_bounds = win32gui.GetWindowRect(root)        left, top, right, bottom = client        if not left < right or not top < bottom:            raise AutomationError('수신자 선택 버튼의 크기를 확인할 수 없습니다.')        point_client = ((left + right) // 2, (top + bottom) // 2)        point = win32gui.ClientToScreen(handle, point_client)        if not bounds[0] <= point[0] < bounds[2] or not bounds[1] <= point[1] < bounds[3]:            raise AutomationError('수신자 선택 버튼의 화면 좌표가 일치하지 않습니다.')        def verify_point():            self.checkpoint()            valid_target()            if (win32gui.GetClientRect(handle) != client                    or win32gui.GetWindowRect(handle) != bounds                    or win32gui.GetWindowRect(root) != root_bounds                    or win32gui.ClientToScreen(handle, point_client) != point):                raise AutomationError('수신자 선택 버튼의 위치가 바뀌었습니다. 클릭하지 않고 멈춥니다.')            if win32gui.WindowFromPoint(point) != handle:                raise AutomationError('수신자 선택 버튼이 다른 창에 가려져 있습니다. 클릭하지 않고 멈춥니다.')        verify_point()        if selection_matches() is not True:            raise AutomationError('수신자 선택이 버튼 활성화 후 바뀌었습니다. 클릭하지 않고 멈춥니다.')        verify_point()        win32api.SetCursorPos(point)        if win32api.GetCursorPos() != point:            raise AutomationError('실제 커서 위치가 수신자 선택 버튼과 다릅니다. 클릭하지 않고 멈춥니다.')        verify_point()        if selection_matches() is not True:            raise AutomationError('클릭 직전에 수신자 선택이 바뀌었습니다. 클릭하지 않고 멈춥니다.')        verify_point()        if win32api.GetCursorPos() != point:            raise AutomationError('클릭 직전에 실제 커서 위치가 바뀌었습니다. 클릭하지 않고 멈춥니다.')        try:            # Keep the proven DOWN/UP flags, but align GetCursorPos-dependent            # Gentoo handlers with their client coordinates. Never retry either.            button.press_mouse(pressed='left', coords=point_client)            button.release_mouse(pressed='', coords=point_client)        except Exception as exc:            raise AutomationError('수신자 선택 버튼의 실행 결과를 확인할 수 없습니다. 다시 누르지 않고 멈춥니다.') from exc        self.wait(.3)    def open_multi_compose(self, ids: list[str]):        self.require_input()
        config = self.profile.get('multi_select', {})
        if config.get('kind') != 'gentoo_picker_v1':
            raise AutomationError('여러 수신자의 작성창 연결이 필요합니다.')
        # Resolve every account before opening the composer; never choose the
        # first name match returned by a search.
        names = []
        for cid in ids:
            contact = self.profile['contacts'][cid]
            resolved = self.resolve(contact['selector'])
            name = normalize(resolved.window_text())
            if recipient_identity(name) != recipient_identity(contact['expected']):
                raise AutomationError('조직도 계정과 저장한 수신자 정보가 다릅니다.')
            names.append(name)
        self._open_gentoo_blank_compose(self.resolve(self.profile['contacts'][ids[0]]['selector']))        deadline = time.monotonic() + 6
        while not self.roots(self.roles['body']):
            if time.monotonic() > deadline:
                raise AutomationError('작성창이 열리지 않았습니다.')
            self.wait(.2)
        self.sync_self_recipient(False)
        existing = self.text('recipient_read')        if existing:            # Some versions prefill the currently selected account. Accept only            # the intended first account; an unrelated draft is never rewritten.            self.verify_recipient({'recipient_ids':[ids[0]]})            names_to_add = names[1:]        else:            names_to_add = names        if not names_to_add:            return        base = self.roles['body']
        picker_button = dict(base, backend='win32', element={'class_name':'Button','control_id':1656}, parents=[])
        picker_button.pop('reader',None)
        self.click(self.resolve(picker_button))
        dialog_spec = {'backend':'win32','exe':base['exe'],'root_title':'사용자 선택','root_class':'#32770',
                       'element':{'class_name':'#32770'},'parents':[]}
        deadline = time.monotonic() + 5
        while len(self.roots(dialog_spec)) != 1:
            if time.monotonic() > deadline:
                raise AutomationError('수신자 선택창을 유일하게 열지 못했습니다.')
            self.wait(.2)
        def control(cid, cls):
            return self.resolve(dict(dialog_spec,element={'class_name':cls,'control_id':cid}))
        from pywinauto import Desktop
        from .waiting import stable_observation        def selected_accounts():            dialogs = self.roots(dialog_spec)            if len(dialogs) != 1:                return None            uia = Desktop(backend='uia').window(handle=int(dialogs[0].handle)).wrapper_object()            trees = [w for w in uia.descendants(control_type='Tree')                     if str(w.element_info.automation_id) == '1401']            if len(trees) != 1:                return None            return tuple(sorted(recipient_identity(w.window_text())                                for w in trees[0].descendants(control_type='TreeItem')                                if recipient_identity(w.window_text())[0] == 'gentoo_account'))        added_names = names[:len(names) - len(names_to_add)]        for name in names_to_add:            identity = recipient_identity(name)
            if identity[0] != 'gentoo_account':
                raise AutomationError('개별 계정번호가 있는 수신자만 함께 보낼 수 있습니다.')
            search = control(3064,'Edit')
            self.focus(search)
            search.set_edit_text(identity[2])
            search.type_keys('{ENTER}', set_foreground=False)
            from .waiting import stable_observation
            def search_result():
                dialogs = self.roots(dialog_spec)
                if len(dialogs) != 1:
                    return ()
                uia = Desktop(backend='uia').window(handle=int(dialogs[0].handle)).wrapper_object()
                trees = [w for w in uia.descendants(control_type='Tree') if str(w.element_info.automation_id) == '1400']
                if len(trees) != 1:
                    return ()
                return tuple(sorted((recipient_identity(w.window_text()), bool(getattr(w, 'is_selected', lambda:False)()))
                                    for w in trees[0].descendants(control_type='TreeItem')))
            def selected_account(value):
                selected = [entry for entry in value if entry[1]]
                return len(selected) == 1 and selected[0][0] == identity
            try:
                stable_observation(search_result, selected_account, checkpoint=self.checkpoint, wait=self.wait)
            except TimeoutError as exc:
                raise AutomationError('검색 결과가 선택한 계정과 안정적으로 일치하지 않습니다. 수신자를 추가하지 않고 멈춥니다.') from exc
            expected_before = tuple(sorted(recipient_identity(value) for value in added_names))            self._click_gentoo_picker_button(control(1636, 'Button'),                lambda: selected_account(search_result()) and selected_accounts() == expected_before)            added_names.append(name)            expected_after = tuple(sorted(recipient_identity(value) for value in added_names))            try:                stable_observation(selected_accounts, lambda value: value == expected_after,                                   checkpoint=self.checkpoint, wait=self.wait)            except TimeoutError as exc:                raise AutomationError('수신자 추가 결과의 전체 계정이 원래 목록과 다릅니다. 다시 누르지 않고 멈춥니다.') from exc        expected_all = tuple(sorted(recipient_identity(value) for value in names))        def all_selected():            try:                stable_observation(selected_accounts, lambda value: value == expected_all,                                   checkpoint=self.checkpoint, wait=self.wait)            except TimeoutError as exc:                raise AutomationError('선택창의 수신자 전체가 원래 목록과 다릅니다.') from exc            return True        self._click_gentoo_picker_button(control(3007, 'Button'), all_selected)        try:            stable_observation(lambda: len(self.roots(dialog_spec)), lambda count: count == 0,                               checkpoint=self.checkpoint, wait=self.wait)        except TimeoutError as exc:            raise AutomationError('수신자 선택창이 닫히지 않았습니다. 확인을 다시 누르지 않고 멈춥니다.') from exc
    def attach(self, paths: list[str]):
        self.require_input()
        for path in paths:
            self.click(self.role('attach'))
            self.wait(.3)
            self.put('file_name', path)
            self.click(self.role('file_open'))
            self.wait(.6)
        if paths:
            self.verify_attachments(paths)

    def verify_attachments(self, paths: list[str]):
        """Accept only a readable, exact filename inventory; never substring-match names."""
        if 'attach_list' not in self.roles:
            if paths:
                raise AutomationError('첨부 목록 연결이 필요합니다.')
            return
        if self.roles['attach_list'].get('reader') == 'gentoo_empty_attachments':
            if paths:
                raise AutomationError('첨부파일을 사용하려면 실제 첨부 목록 영역을 별도로 연결하세요.')
            if self.text('attach_list'):
                raise AutomationError('새 작성창에 기존 첨부가 있습니다. 직접 확인하세요.')
            return
        w = self.role('attach_list')
        expected = sorted(Path(p).name.casefold() for p in paths)
        names = attachment_names(w)
        if sorted(names) != expected:
            raise AutomationError('첨부 목록의 파일명·개수가 원본과 정확히 일치하지 않습니다. 등록하지 않고 멈춥니다.')

    def prepare(self, job: dict):
        self.require_input()
        if datetime.now().astimezone().utcoffset() != timedelta(hours=9):
            raise AutomationError('Windows 표준 시간대를 대한민국(UTC+09:00)으로 맞춘 뒤 실행하세요.')
        reused = self.open_compose(job)
        if not reused or (not job['title'] and self.text('title') != ''):
            self.put('title', job['title'])
        if not reused:
            self.put('body', job['body'])
        self.set_schedule(job['scheduled'])
        if is_gentoo_profile(self.profile):
            from .core import recipient_ids
            own = self.profile.get('self_binding', {}).get('expected')
            include_self = bool(own and any(
                recipient_identity(self.profile['contacts'][cid]['expected']) == recipient_identity(own)
                for cid in recipient_ids(job)))
            self.sync_self_recipient(include_self)
        if not reused:
            self.attach(job['attachments'])
        self.verify(job)

    def verify(self, job: dict):
        self.checkpoint()
        self.verify_recipient(job)
        self.verify_schedule(job['scheduled'])
        if (not title_matches(self.text('title'), job['title'], gentoo_placeholder=is_gentoo_profile(self.profile))                or not body_matches(self.text('body'), job['body'])):
            raise AutomationError('전송 직전 제목·본문이 목록과 달라져 전송을 차단했습니다.')
        if 'cc_read' in self.roles and self.text('cc_read'):
            raise AutomationError('참조 수신자가 추가되어 전송을 차단했습니다.')
        self.verify_attachments(job['attachments'])

    def verify_existing(self, job: dict):
        """Read a user-filled composer without activating it or sending input."""
        previous = self.read_only
        self.read_only = True
        try:
            self.verify(job)
        finally:
            self.read_only = previous

    def submit(self, job: dict):
        self.require_input()
        # Called only after the engine durably records the submit attempt.
        roots = self.roots(self.roles['body'])
        if len(roots) != 1:
            raise AutomationError('등록할 작성창이 유일하지 않습니다.')
        compose_handle = int(roots[0].handle)
        self.click(self.role('send'))
        deadline = time.monotonic() + 10
        import win32gui
        while time.monotonic() < deadline:
            if not win32gui.IsWindow(compose_handle):
                return
            self.wait(.2)
        raise AutomationError('보내기 이후 작성창이 닫히지 않았습니다. 확인창이나 서버 오류를 직접 확인하세요. 자동 재시도하지 않습니다.')

    def verify_result(self, job: dict) -> tuple[bool, str]:
        self.require_input()
        from .verification import available, match_receipts, parse_receipt_time        from .waiting import stable_observation
        if not available(self.profile, bool(job.get('attachments'))):
            return False, '작성창 종료를 확인했습니다. 보낸 메시지함에서 수신자·예약일시·내용을 확인하세요.'
        for role in ('sent_open', 'sent_tab'):
            if role in self.roles:
                self.click(self.role(role))
        self.wait(.4)
        listing = self.role('sent_list')
        items = []
        for method in ('get_items', 'items'):
            try:
                items = getattr(listing, method)()
                break
            except Exception:
                continue
        # An empty title cannot identify a list row. Read the complete inventory        # and retain exact full-message matching instead of inventing a title.        candidates = list(items) if not normalize(job['title']) else [            item for item in items if job['title'] in read_text(item).splitlines()]        from .core import recipient_ids
        expected = [normalize(self.profile['contacts'][cid].get('expected_result') or self.profile['contacts'][cid]['expected'])
                    for cid in recipient_ids(job)]
        count = getattr(listing, 'item_count', None)
        before = count() if callable(count) else None
        records = []
        for item in candidates:
            self.focus(listing)
            if hasattr(item, 'select'):
                item.select()
            else:
                self.physical_click(item)
            def read_record():
                scheduled = parse_receipt_time(self.text('result_datetime'))                record = {'title': self.text('result_title'), 'body': self.text('result_body'),
                          'recipient': self.text('result_recipient'),
                          'scheduled': scheduled.strftime('%Y-%m-%d %H:%M'),                          'reserved': enabled_state(self.role('result_reserved')),                          'attachments': attachment_names(self.role('result_attachments'))}                return record
            try:
                records.append(stable_observation(read_record, lambda value: bool(value)                                                  and isinstance(value['title'], str)                                                  and (not normalize(job['title']) or value['title'] == normalize(job['title'])),                                                  checkpoint=self.checkpoint, wait=self.wait))
            except (AutomationError, TimeoutError, ValueError):                return False, '예약 상세를 안정적으로 읽지 못했습니다. 재등록하지 말고 목록을 확인하세요.'
        after = count() if callable(count) else None
        result = match_receipts(records, job, expected, complete=type(before) is int and before == after == len(items))
        return result['confirmed'], result['message']
