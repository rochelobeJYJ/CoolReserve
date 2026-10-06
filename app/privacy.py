"""Small allowlists for default logs and shareable diagnostics."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import traceback


def diagnostic(exc):
    """Return code locations and allowed integer window facts, never payloads."""
    project = Path(__file__).resolve().parent.parent
    frames = []
    for frame, lineno in traceback.walk_tb(exc.__traceback__):
        try:
            relative = Path(frame.f_code.co_filename).resolve().relative_to(project)
        except (OSError, ValueError):
            continue
        if not (relative == Path('server.py') or
                (relative.parts and relative.parts[0] == 'app' and relative.suffix == '.py')):
            continue
        function = frame.f_code.co_name
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*|<lambda>|<module>', function):
            function = '<code>'
        frames.append({'file': relative.as_posix(), 'function': function, 'line': lineno})
    name = type(exc).__name__
    if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', name):
        name = 'Exception'
    result = {'exception': name, 'frames': frames[-20:]}
    window_state = getattr(exc, 'window_state', None)
    if type(window_state) is dict:
        allowed = ('target_hwnd', 'tree_hwnd', 'hit_hwnd', 'hit_root_hwnd', 'hit_pid',
                   'target_pid', 'point_x', 'point_y', 'client_x', 'client_y',
                   'focus_hwnd', 'foreground_hwnd', 'foreground_pid', 'target_topmost',
                   'selected_item', 'expected_item')
        values = {key: window_state[key] for key in allowed
                  if key in window_state and type(window_state[key]) is int}
        if values:
            result['window_state'] = values
    return result


def safe_error(exc):
    text = str(exc)
    if any(word in text for word in ('활성', '포커스', '제목표시줄', '다른 창에 가려져')):
        return '쿨메신저 창을 활성화하지 못해 작업을 멈췄습니다.'
    if '첨부' in text and ('바뀌' in text or '변경' in text):
        return '검토 이후 첨부파일이 바뀌었습니다. 다시 미리보기 해주세요.'
    if '예약' in text or '시간' in text:
        return '예약시각 또는 예약 상태를 확인하지 못했습니다. 입력값과 쿨메신저를 확인하세요.'
    if '수신자' in text or '계정' in text or '조직도' in text:
        return '수신자를 정확히 확인하지 못했습니다. 연결과 검색 결과를 확인하세요.'
    if '본문' in text or '제목' in text:
        return '입력한 내용이 대조 결과와 다릅니다. 작성창을 확인하세요.'
    if '첨부' in text:
        return '첨부파일을 확인하지 못했습니다. 파일과 작성창을 확인하세요.'
    if isinstance(exc, TimeoutError):
        return '화면 응답을 기다리다 중단했습니다. 등록 여부가 불확실하면 먼저 예약 목록을 확인하세요.'
    return '처리를 중단했습니다. 입력값과 쿨메신저 상태를 확인하세요.'


def diagnostics(store, version):
    profile = store.setting('profile', {})
    return {'schema_version': 1, 'application': 'CoolReserve', 'version': version,
            'status_counts': dict(Counter(job['status'] for job in store.list_jobs())),
            'contact_count': len(profile.get('contacts', {})),
            'connected_roles': sorted(profile.get('roles', {})),
            'input_tested': bool(profile.get('tested')),
            'multi_input_tested': bool(profile.get('tested_multi')),
            'included': '상태 개수와 연결 역할만 포함합니다.'}


def portable_config(profile):
    return {'schema_version': 1, 'application': 'CoolReserve',
            'multi_select_kind': profile.get('multi_select', {}).get('kind', ''),
            'roles': {role: {'backend': spec.get('backend', ''),
                             'element': {key: value for key, value in spec.get('element', {}).items()
                                         if key in ('control_type', 'class_name', 'control_id')},
                             'reader': spec.get('reader', '')}
                      for role, spec in profile.get('roles', {}).items()},
            'requires_local_binding': True}
