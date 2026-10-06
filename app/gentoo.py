"""Read-only inspection of the observed CoolMessenger Gentoo compose window.

This module never focuses a window or invokes an input/action method. Capability
flags indicate exposed attributes only; they do not prove an action will work.
"""
from __future__ import annotations

import re
import hashlib
import json
from pathlib import Path
from typing import Any

from .core import normalize
from .windows import AutomationError, com_session, executable, metadata, read_text, recipient_identity, is_gentoo_profile


COMPOSE_TITLES = {'메시지 전송 (크롬에디터)', '메시지 전송'}
COOL_EXE_NAME = 'coolmessenger.exe'
TEXT_LIMIT = 200
CAPABILITY_NAMES = (
    'set_edit_text', 'get_value', 'iface_value', 'iface_text',
    'get_toggle_state', 'get_check_state', 'set_time', 'get_time',
    'select', 'invoke',
)


def decode_gentoo_paragraphs(parts: list[str]) -> str:
    """Decode the observed CEF paragraph separator without trimming user spaces."""
    lines = []
    for part in parts:
        # This editor appends one synthetic space to each paragraph range.
        # Empty paragraphs expose a newline followed by that same separator.
        if not part.endswith(' '):
            raise AutomationError('본문 단락의 구분자를 확인할 수 없습니다.')
        text = part[:-1]
        if text == '\n':
            text = ''
        elif '\n' in text or '\r' in text:
            raise AutomationError('본문의 단락과 줄바꿈을 정확히 구분할 수 없습니다.')
        lines.append(text)
    return normalize('\n'.join(lines))


def read_gentoo_body(w) -> str:
    """Read hard paragraphs, including blanks, independently of visual wrapping."""
    try:
        pattern = w.iface_text
        if not normalize(pattern.DocumentRange.GetText(-1)):
            return ''
        groups = w.descendants(control_type='Group')
        if len(groups) != 1:
            raise AutomationError('본문 편집영역을 유일하게 확인할 수 없습니다.')
        # The enclosing Document has trailing accessibility sentinel spaces;
        # restrict all ranges to the actual editable group, never that sentinel.
        document = pattern.RangeFromChild(groups[0].element_info.element)
        original = document.GetText(-1)
        current = document.Clone()
        current.MoveEndpointByRange(1, document, 0)
        parts = []
        while current.CompareEndpoints(0, document, 1) < 0:
            if len(parts) >= 30002:
                raise AutomationError('본문 단락 수가 읽기 한도를 초과했습니다.')
            previous = current.Clone()
            if current.MoveEndpointByUnit(1, 4, 1) != 1:  # TextUnit_Paragraph
                raise AutomationError('본문의 다음 단락 경계를 읽을 수 없습니다.')
            if current.CompareEndpoints(1, document, 1) > 0:
                current.MoveEndpointByRange(1, document, 1)
            if current.CompareEndpoints(1, previous, 1) <= 0:
                raise AutomationError('본문 단락 읽기가 진행되지 않습니다.')
            parts.append(current.GetText(-1))
            current.MoveEndpointByRange(0, current, 1)
        if ''.join(parts) != original or document.GetText(-1) != original:
            raise AutomationError('본문 전체를 빠짐없이 읽지 못했거나 읽는 동안 내용이 바뀌었습니다.')
        return decode_gentoo_paragraphs(parts)
    except AutomationError:
        raise
    except Exception as exc:
        raise AutomationError('본문 편집기의 단락 원문을 읽을 수 없습니다.') from exc


def stable_metadata(w) -> dict:
    """Return selector fields that do not change with draft text or window IDs."""
    return metadata(w, mutable=True)


def _get(obj, name: str, default=None):
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _call(obj, name: str, default=None):
    try:
        return getattr(obj, name)()
    except Exception:
        return default


def _short(value: Any) -> str:
    return str(value or '')[:TEXT_LIMIT]


def _identity(w) -> dict:
    if w is None:
        return {}
    e = _get(w, 'element_info')
    handle = _get(w, 'handle') or _get(e, 'handle')
    runtime_id = _get(e, 'runtime_id')
    try:
        handle = int(handle) if handle else None
    except (ValueError, TypeError):
        handle = None
    try:
        runtime_id = list(runtime_id) if runtime_id is not None else None
    except TypeError:
        runtime_id = None
    return {'handle': handle, 'runtime_id': runtime_id}


def _parent_metadata(w) -> dict:
    p = _call(w, 'parent')
    if p is None:
        return {}
    try:
        fields = stable_metadata(p)
    except Exception:
        fields = {}
    e = _get(p, 'element_info')
    return dict(fields, **_identity(p), name=_short(_get(e, 'name')),
                window_text=_short(_call(p, 'window_text', '')))


def _control_metadata(w, index: int) -> dict:
    e = _get(w, 'element_info')
    try:
        stable = stable_metadata(w)
    except Exception:
        stable = {}
    rect = _call(w, 'rectangle')
    rectangle = None
    if rect is not None:
        try:
            rectangle = {k: int(getattr(rect, source)) for k, source in
                         (('l', 'left'), ('t', 'top'), ('r', 'right'), ('b', 'bottom'))}
        except (AttributeError, TypeError, ValueError):
            pass
    try:
        text = read_text(w)
        text_error = ''
    except Exception as exc:
        text = ''
        text_error = _short(exc)
    # Inspect attributes, never call any setter, selection or invocation method.
    supports = {name: _get(w, name) is not None for name in CAPABILITY_NAMES}
    return {
        'index': index,
        **stable,
        **_identity(w),
        'name': _short(_get(e, 'name')),
        'window_text': _short(_call(w, 'window_text', '')),
        'read_text': _short(text),
        'read_text_error': text_error,
        'rectangle': rectangle,
        'visible': _call(w, 'is_visible'),
        'enabled': _call(w, 'is_enabled'),
        'parent': _parent_metadata(w),
        'supports_limit': supports,
        'native_style': _call(w, 'style') if _call(w, 'class_name') == 'Button' else None,
        'native_check_state': _call(w, 'get_check_state') if _call(w, 'class_name') == 'Button' else None,
        'toggle_state': _call(w, 'get_toggle_state') if _call(w, 'class_name') == 'Button' else None,
    }


def _find_compose(Desktop):
    roots = []
    for w in Desktop(backend='win32').windows():
        try:
            title = w.window_text()
            if title not in COMPOSE_TITLES:
                continue
            path = executable(w.process_id())
            if Path(path).name.casefold() == COOL_EXE_NAME:
                roots.append((w, path, title))
        except Exception:
            continue
    if len(roots) != 1:
        raise AutomationError(
            f'쿨메신저 메시지 전송창을 하나만 열어주세요. 일치하는 창: {len(roots)}개')
    return roots[0]


def inspect_compose() -> dict:
    """Inspect exactly one observed compose window without changing the UI."""
    with com_session():
        from pywinauto import Desktop

        root, path, title = _find_compose(Desktop)
        handle = int(root.handle)
        result = {
            'root': {'exe': path, 'title': title, 'class_name': root.class_name(), 'handle': handle},
            'text_limit': TEXT_LIMIT,
            'capability_note': '속성 노출 여부만 표시합니다. 입력·클릭 가능 여부를 시험하지 않았습니다.',
            'backends': {},
        }
        for backend in ('uia', 'win32'):
            try:
                wrapper = Desktop(backend=backend).window(handle=handle).wrapper_object()
                controls = [wrapper] + wrapper.descendants()
                result['backends'][backend] = {
                    'controls': [_control_metadata(w, i) for i, w in enumerate(controls)],
                    'error': '',
                }
            except Exception as exc:
                result['backends'][backend] = {'controls': [], 'error': _short(exc)}
        return result


def _cid(w):
    value = _get(_get(w, 'element_info'), 'control_id')
    return value if value is not None else _call(w, 'control_id')


def _rect(w):
    rect = _call(w, 'rectangle')
    if rect is None:
        raise AutomationError('수신자 표시영역의 위치를 읽을 수 없습니다.')
    return rect


def _direct_children(w):
    """Normalize Win32's flattened EnumChildWindows to actual direct children."""
    try:
        children = w.children()
        result = []
        for child in children:
            parent = child.parent()
            if parent is None:
                raise AutomationError('수신자 항목의 부모 구조를 확인할 수 없습니다.')
            if parent == w:
                result.append(child)
        return result
    except AutomationError:
        raise
    except Exception as exc:
        raise AutomationError('수신자 항목의 부모 구조를 읽을 수 없습니다.') from exc


def read_gentoo_schedule_state(button) -> int:
    """Read Gentoo's owner-drawn checkbox using its label and date visibility."""
    if (_cid(button) != 1660 or button.class_name() != 'Button'
            or button.style() & 0xF != 0xB):
        raise AutomationError('예약 버튼의 화면 구조가 예상과 다릅니다.')
    dates = [w for w in button.top_level_parent().descendants()
             if _cid(w) == 1711 and w.class_name() == 'SysDateTimePick32']
    if len(dates) != 1:
        raise AutomationError('예약일시 표시영역이 유일하지 않습니다.')
    text = normalize(button.window_text())
    labels = {'해제 예약전송': 0, '선택 예약전송': 1}
    if text not in labels:
        raise AutomationError('예약 버튼의 실제 선택 문구를 확인할 수 없습니다: '+text)
    checked = labels[text]
    date = dates[0]
    if bool(date.is_visible()) != bool(checked) or (checked and not date.is_enabled()):
        raise AutomationError('예약 버튼과 예약일시 표시 상태가 일치하지 않습니다.')
    return checked


def gentoo_self_button(root):
    buttons = [w for w in root.descendants() if _cid(w) == 1661]
    if (len(buttons) != 1 or buttons[0].class_name() != 'Button'
            or buttons[0].style() & 0xF != 0xB or not buttons[0].is_visible()
            or not buttons[0].is_enabled()):
        raise AutomationError('나에게 보내기 버튼을 유일하게 확인할 수 없습니다.')
    return buttons[0]


def read_gentoo_self_state(root) -> int:
    text = normalize(gentoo_self_button(root).window_text())
    states = {'해제 나에게 보내기': 0, '선택 나에게 보내기': 1}
    if text not in states:
        raise AutomationError('나에게 보내기의 실제 선택 상태를 확인할 수 없습니다.')
    return states[text]


def read_gentoo_login_marker(profile: dict) -> dict:
    """Read the two observed login labels; do not interpret nickname digits as IDs."""
    from pywinauto import Desktop
    root, _, base = _organization_root(Desktop, profile)
    values = []
    children = _direct_children(root)
    for cid, cls in ((1596, 'Edit'), (4369, 'Button')):
        found = [w for w in children if _cid(w) == cid and w.class_name() == cls]
        if len(found) != 1:
            raise AutomationError('로그인한 본인의 표시를 유일하게 확인할 수 없습니다.')
        values.append(normalize(found[0].window_text()))
    if not values[0] or values[0] != values[1]:
        raise AutomationError('로그인한 본인의 표시가 일치하지 않습니다.')
    return {'exe': base['exe'], 'root_title': base['root_title'],
            'root_class': base['root_class'], 'label': values[0]}


def bind_gentoo_self(profile: dict, contact_id: str) -> dict:
    """Bind the user's explicitly identified self recipient once, with live login evidence."""
    if not is_gentoo_profile(profile):
        raise AutomationError('본인 수신자 연결은 젠투 자동 인식에서 지원합니다.')
    if not isinstance(contact_id, str) or not contact_id:
        raise AutomationError('본인 수신자를 하나 선택하세요.')
    contact = profile.get('contacts', {}).get(contact_id, {})
    expected = normalize(contact.get('expected', ''))
    if recipient_identity(expected)[0] != 'gentoo_account':
        raise AutomationError('본인의 전체 이름과 계정번호를 확인할 수 없습니다.')
    with com_session():
        from pywinauto import Desktop
        root, path, _ = _find_compose(Desktop)
        if path.casefold() != profile['roles']['body']['exe'].casefold():
            raise AutomationError('본인 확인창과 저장한 쿨메신저 설치가 다릅니다.')
        if read_gentoo_self_state(root) != 1:
            raise AutomationError('나에게 보내기를 선택한 본인용 작성창이 필요합니다.')
        result = json.loads(json.dumps(profile, ensure_ascii=False))
        result['self_binding'] = {'contact_id': contact_id, 'expected': expected,
                                  'login_marker': read_gentoo_login_marker(profile)}
        return result


def gentoo_self_expected(profile: dict) -> str:
    binding = profile.get('self_binding')
    if not isinstance(binding, dict):
        raise AutomationError('나에게 보내기의 본인 수신자 연결이 필요합니다. 본인 계정을 한 번 확인해 주세요.')
    contact = profile.get('contacts', {}).get(binding.get('contact_id'), {})
    expected = normalize(binding.get('expected', ''))
    identity = recipient_identity(expected)
    if (identity[0] != 'gentoo_account'
            or recipient_identity(contact.get('expected', '')) != identity):
        raise AutomationError('저장한 본인 수신자 연결이 변경되었습니다.')
    if read_gentoo_login_marker(profile) != binding.get('login_marker'):
        raise AutomationError('쿨메신저의 로그인 사용자가 본인 연결 때와 다릅니다. 전송을 중단합니다.')
    return expected


def read_gentoo_recipients(root, profile: dict) -> str:
    explicit = read_gentoo_chips(root, 'gentoo_recipients')
    if not read_gentoo_self_state(root):
        return explicit
    own = gentoo_self_expected(profile)
    lines = explicit.splitlines()
    if not any(recipient_identity(line) == recipient_identity(own) for line in lines):
        lines.append(own)
    return '\n'.join(lines)


def read_gentoo_chips(root, role: str) -> str:
    """Read every native recipient chip, not its child 'delete' button text."""
    role = {'recipient': 'gentoo_recipients', 'cc': 'gentoo_cc'}.get(role, role)
    if role not in ('gentoo_recipients', 'gentoo_cc', 'gentoo_empty_attachments'):
        raise AutomationError('지원하지 않는 수신자 표시영역입니다.')
    label_id = 1657 if role == 'gentoo_cc' else 1656
    labels = [w for w in root.descendants() if _cid(w) == label_id]
    if len(labels) != 1:
        raise AutomationError('받는 사람·참조 버튼의 화면 구조가 예상과 다릅니다.')
    label = _rect(labels[0])
    center = (label.top + label.bottom) / 2
    regions, all_regions = [], []
    for w in _direct_children(root):
        if _call(w, 'class_name') != '#32770':
            continue
        if not any(_cid(c) == 8011 for c in _direct_children(w)):
            continue
        all_regions.append(w)
        descendants = w.descendants()
        if not w.is_visible() and any(
                (_cid(c) == 8009 and normalize(c.window_text()))
                or (any(_cid(marker) == 8010 for marker in _direct_children(c)) and normalize(c.window_text()))
                for c in descendants):
            raise AutomationError('숨겨진 수신자 표시영역에 항목이 남아 있습니다. 받는 사람·참조 영역을 펼쳐 확인하세요.')
        rect = _rect(w)
        if (w.is_visible() and rect.left >= label.right - 2
                and rect.top <= center <= rect.bottom):
            regions.append(w)
    if not regions and role == 'gentoo_cc':
        return ''
    if len(regions) != 1:
        raise AutomationError('수신자 표시영역이 유일하지 않거나 보이지 않습니다.')

    region = regions[0]
    if role == 'gentoo_empty_attachments':
        # Only the empty attachment state has been observed. Never invent a
        # filename inventory from the hidden attachment/CC dialogs. Any text
        # outside the known recipient region requires explicit manual binding.
        for other in all_regions:
            if other == region:
                continue
            if normalize(other.window_text()) or any(
                    _cid(c) not in (8010, 8011) and normalize(c.window_text())
                    for c in other.descendants()):
                raise AutomationError('참조 또는 첨부 영역에 항목이 있습니다. 무첨부 시험은 빈 작성창에서 진행하세요. 파일 첨부에는 첨부 목록을 별도로 연결하세요.')
        return ''
    bounds = _rect(region)
    names = []
    if any(_cid(c) == 8009 and normalize(c.window_text()) for c in region.descendants()):
        raise AutomationError('입력 중인 수신자 이름이 남아 있습니다. 선택을 완료한 뒤 다시 연결하세요.')
    for chip in _direct_children(region):
        markers = [c for c in _direct_children(chip) if _cid(c) == 8010]
        if not markers:
            if _cid(chip) not in (8009, 8011) and normalize(chip.window_text()):
                raise AutomationError('수신자 표시영역에 읽을 수 없는 항목이 있습니다.')
            continue
        rect = _rect(chip)
        if (len(markers) != 1 or not chip.is_visible()
                or rect.left < bounds.left - 2 or rect.right > bounds.right + 2
                or rect.top < bounds.top - 2 or rect.bottom > bounds.bottom + 2):
            raise AutomationError('수신자 항목 전체가 보이지 않습니다. 받는 사람 영역을 펼쳐서 확인하세요.')
        text = normalize(chip.window_text())
        if not text or '\n' in text:
            raise AutomationError('수신자 항목의 전체 표시를 읽을 수 없습니다.')
        names.append(text)
    return '\n'.join(names)


def _ancestor_selector(w) -> dict:
    spec = metadata(w)
    if spec.get('control_type') == 'TreeItem' and spec.get('name'):
        name = spec.pop('name')
        base = re.sub(r'\s*\(\d+(?:/\d+)?\)\s*$', '', name)
        spec['name_regex'] = '^' + re.escape(base) + r'(?:\s*\(\d+(?:/\d+)?\))?$'
    return spec


def _selected_contact(Desktop, path: str, account: str) -> dict:
    from pywinauto.controls.uiawrapper import UIAWrapper

    selected, seen = [], set()
    for root in Desktop(backend='uia').windows():
        try:
            if root.window_text() in COMPOSE_TITLES or executable(root.process_id()).casefold() != path.casefold():
                continue
            trees = root.descendants(control_type='Tree')
        except Exception:
            continue
        for tree in trees:
            try:
                selection = tree.get_selection()
            except Exception as exc:
                raise AutomationError('조직도의 선택 상태를 읽지 못했습니다. 쿨 예약실을 종료한 뒤 START.cmd로 다시 실행하세요.') from exc
            for item in selection or []:
                w = item if _get(item, 'element_info') is not None else UIAWrapper(item)
                if _get(w.element_info, 'control_type') != 'TreeItem':
                    continue
                identity = _identity(w)
                key = tuple(identity['runtime_id']) if identity['runtime_id'] else identity['handle']
                if key is not None and key in seen:
                    continue
                if key is not None:
                    seen.add(key)
                selected.append((root, w))
    if len(selected) != 1:
        raise AutomationError('조직도에서 본인 항목 하나를 선택하고 본인에게 보내는 작성창을 다시 열어주세요.')
    root, item = selected[0]
    spec = metadata(item)
    identity = recipient_identity(spec.get('name', ''))
    if identity[0] != 'gentoo_account' or identity[2] != account:
        raise AutomationError('선택한 조직도 항목과 작성창 수신자의 계정번호가 다릅니다. 본인 항목을 다시 선택하세요.')
    parents = []
    p = item.parent()
    while p is not None and p != root:
        if len(parents) >= 16:
            raise AutomationError('조직도의 부모 구조를 끝까지 확인할 수 없습니다.')
        parents.append(_ancestor_selector(p))
        p = p.parent()
    if p is None:
        raise AutomationError('선택한 조직도 항목의 메인 창을 확인할 수 없습니다.')
    return {'backend': 'uia', 'exe': path, 'root_title': root.window_text(),
            'root_class': root.class_name(), 'element': spec, 'parents': parents}


def _organization_root(Desktop, profile: dict):
    """Identify the logged-in native organization tree without opening a draft."""
    contacts = profile.get('contacts', {})
    bases = [c.get('selector', {}) for c in contacts.values() if c.get('selector')]
    base = bases[0] if bases else None
    found = []
    for root in Desktop(backend='win32').windows():
        try:
            title, cls = root.window_text(), root.class_name()
            if title in COMPOSE_TITLES or cls != '#32770':
                continue
            if base and (title != base.get('root_title') or cls != base.get('root_class')):
                continue
            path = executable(root.process_id())
            if path.replace('\\', '/').rsplit('/', 1)[-1].casefold() != COOL_EXE_NAME:
                continue
            if base and path.casefold() != base.get('exe', '').casefold():
                continue
            trees = [w for w in root.descendants()
                     if w.class_name() == 'SysTreeView32' and _cid(w) == 3013]
        except Exception:
            # Unrelated protected/system windows must not prevent discovery.
            continue
        if len(trees) == 1:
            found.append((root, trees[0], path))
    if len(found) != 1:
        raise AutomationError('쿨메신저에 로그인하고 조직도 창을 하나만 열어주세요. 앱이 자동으로 확인합니다.')
    root, tree, path = found[0]
    return root, tree, {'backend':'uia', 'exe':path, 'root_title':root.window_text(),
                        'root_class':root.class_name()}


def inspect_contacts(profile: dict) -> dict:
    """Read all loaded TreeView nodes, including collapsed and offscreen items."""
    from .gentoo_tree import snapshot_tree, parent_selectors
    with com_session():
        from pywinauto import Desktop
        root, tree, base = _organization_root(Desktop, profile)
        records = snapshot_tree(tree)
        tree_spec = {'class_name':'SysTreeView32','control_type':'Tree',
                     'automation_id':'3013','control_id':3013}
        found, skipped = {}, 0
        for record in records:
            name = normalize(record['text'])
            identity = recipient_identity(name)
            # A department headcount such as '교무부 (12)' is not an account.
            if not name.startswith('(') or identity[0] != 'gentoo_account':
                skipped += 1
                continue
            spec = dict(base, element={'control_type':'TreeItem','name':name},
                        parents=parent_selectors(record['parents']) + [tree_spec])
            cid = 'gentoo:' + hashlib.sha256(json.dumps(spec,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]
            found[cid] = {'display_name':name,'expected':name,'selector':spec,'account':identity[2]}
        occurrences = len(found)
        saved = {json.dumps(c.get('selector',{}),sort_keys=True,ensure_ascii=False)
                 for c in profile.get('contacts',{}).values()}
        unique = {}
        for cid,contact in found.items():
            identity = recipient_identity(contact['expected'])
            old = unique.get(identity)
            bound = json.dumps(contact['selector'],sort_keys=True,ensure_ascii=False) in saved
            if old is None or (bound and not old[2]):
                unique[identity] = (cid,contact,bound)
        # One numeric account may appear under several departments. Present it
        # once, preferring an already-bound path; never merge different names.
        found = {cid:contact for cid,contact,_ in unique.values()}
        if not found:
            raise AutomationError('현재 조직도에 읽을 수 있는 개별 계정이 없습니다. 쿨메신저의 조직도 표시 설정을 확인하세요.')
        return {'contacts':found,'count':len(found),'organization':base['root_title'],'base':base,
                'summary':f'접힌 부서와 스크롤 밖 항목까지 읽은 {len(found)}개 계정 연결입니다.',
                'coverage':{'method':'win32_tree','scope':'loaded_tree','complete':False,
                            'node_count':len(records),'account_count':len(found),
                            'account_occurrences':occurrences,'skipped_count':skipped},
                'warnings':['쿨메신저 자체에서 조직도를 필터링하면 명부가 제한될 수 있습니다. 전체 서버 명부와의 일치 여부는 아직 확인하지 않았습니다.']}


def automatic_profile(base: dict) -> dict:
    """Supported Gentoo adapter; live controls are revalidated before every send."""
    root = {'backend':'win32','exe':base['exe'],'root_title':'메시지 전송 (크롬에디터)',
            'root_class':'#32770','parents':[]}
    roles = {key:dict(root,element={'class_name':cls,'control_id':cid})
             for key,cid,cls in (('title',4362,'Edit'),('scheduled_check',1660,'Button'),
                                ('send',3249,'Button'),('attach',1449,'Button'),
                                ('datetime',1711,'SysDateTimePick32'))}
    roles['body'] = dict(root,backend='uia',element={'control_type':'Document'},
                         parents=[{'control_type':'Pane','automation_id':'editFrame'}],reader='gentoo_body')
    for key,reader in (('recipient_read','gentoo_recipients'),('cc_read','gentoo_cc'),
                        ('attach_list','gentoo_empty_attachments')):
        roles[key] = dict(root,element={'class_name':'#32770'},reader=reader)
    return {'roles':roles,'contacts':{},'tested':False,'multi_select':{'kind':'gentoo_picker_v1'}}


def initialize_profile(previous: dict) -> dict:
    """One automatic read, no user selection, focus, draft creation or sending."""
    inventory = inspect_contacts(previous)
    profile = json.loads(json.dumps(previous if is_gentoo_profile(previous)
                                    else automatic_profile(inventory['base']),ensure_ascii=False))
    if not is_gentoo_profile(previous):
        profile['contacts'].update(previous.get('contacts', {}))
    # Keep saved bindings unchanged; new records do not silently redirect old jobs.
    identities = {recipient_identity(c.get('expected','')) for c in profile['contacts'].values()}
    for cid,contact in inventory['contacts'].items():
        if recipient_identity(contact['expected']) not in identities:
            profile['contacts'][cid] = contact
            identities.add(recipient_identity(contact['expected']))
    profile['inventory_version'] = '1.2.0'
    return {'profile':profile,'count':len(profile['contacts']),'coverage':inventory['coverage'],
            'warnings':inventory['warnings'],'summary':'쿨메신저와 수신자 계정을 자동으로 확인했습니다.'}


def build_profile(alias: str = '본인 테스트') -> dict:
    """Build unsaved bindings from the current compose and selected tree item."""
    alias = normalize(alias)
    if not alias:
        raise AutomationError('수신자 프리셋 이름을 입력하세요.')
    with com_session():
        from pywinauto import Desktop

        root, path, title = _find_compose(Desktop)
        if root.class_name() != '#32770':
            raise AutomationError('이 쿨메신저 작성창의 구조는 자동 연결을 지원하지 않습니다.')
        base = {'backend': 'win32', 'exe': path, 'root_title': title,
                'root_class': '#32770', 'parents': []}
        roles = {}
        controls = root.descendants()
        for role, cid in (('title', 4362), ('scheduled_check', 1660),
                          ('send', 3249), ('attach', 1449), ('datetime', 1711)):
            candidates = [w for w in controls if _cid(w) == cid]
            if len(candidates) != 1:
                raise AutomationError(f'자동 연결할 {role} 요소가 유일하지 않습니다.')
            if role == 'datetime' and candidates[0].class_name() != 'SysDateTimePick32':
                raise AutomationError('예약일시 컨트롤이 예상과 다릅니다.')
            roles[role] = dict(base, element={'class_name': candidates[0].class_name(), 'control_id': cid})
        uia = Desktop(backend='uia').window(handle=int(root.handle)).wrapper_object()
        bodies = [w for w in uia.descendants(control_type='Document')
                  if _get(_get(_call(w, 'parent'), 'element_info'), 'control_type') == 'Pane'
                  and _get(_get(_call(w, 'parent'), 'element_info'), 'automation_id') == 'editFrame']
        if len(bodies) != 1:
            raise AutomationError('크롬 본문 편집영역이 유일하지 않거나 읽히지 않습니다.')
        roles['body'] = dict(base, backend='uia', element={'control_type': 'Document'},
                             parents=[{'control_type': 'Pane', 'automation_id': 'editFrame'}],
                             reader='gentoo_body')
        roles['recipient_read'] = dict(base, element={'class_name': '#32770'}, reader='gentoo_recipients')
        roles['cc_read'] = dict(base, element={'class_name': '#32770'}, reader='gentoo_cc')
        roles['attach_list'] = dict(base, element={'class_name': '#32770'}, reader='gentoo_empty_attachments')
        recipient = read_gentoo_chips(root, 'gentoo_recipients')
        if len(recipient.splitlines()) != 1:
            raise AutomationError('자동 연결은 본인 한 명에게 보내는 작성창에서 시작하세요.')
        accounts = re.findall(r'\((\d+)\)', recipient)
        if len(accounts) != 1:
            raise AutomationError('수신자의 계정번호를 유일하게 읽을 수 없습니다.')
        if read_gentoo_chips(root, 'gentoo_cc'):
            raise AutomationError('참조 수신자를 비운 뒤 자동 연결하세요.')
        read_gentoo_chips(root, 'gentoo_empty_attachments')
        contact = _selected_contact(Desktop, path, accounts[0])
        profile = {'roles': roles, 'contacts': {alias: {'selector': contact, 'expected': recipient}},
                   'tested': False}
        profile['multi_select'] = {'kind':'gentoo_picker_v1'}
        return {'profile': profile, 'recipient': recipient, 'alias': alias,
                'summary': '본문·제목·예약일시·수신자 연결을 준비했습니다. 저장 후 보내지 않는 입력 시험을 실행하세요.'}
