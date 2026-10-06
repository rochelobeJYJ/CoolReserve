"""Bounded, read-only snapshots of logical Win32 TreeView items.

Reading a snapshot never expands a branch or changes focus/selection. Input
methods on NativeContact are used only by the driver's guarded action path.
"""
from __future__ import annotations

import re
import time
from types import SimpleNamespace

from .core import normalize
from .windows import AutomationError


# common_controls._treeview_element.children() walks siblings in an unbounded
# loop. Read its first logical child directly, then bound next_item ourselves.
TVM_GETNEXTITEM = 0x110A
TVGN_CHILD = 4


def _read(label, operation):
    try:
        return operation()
    except AutomationError:
        raise
    except Exception as exc:
        raise AutomationError(f'조직도 {label}을 읽지 못했습니다.') from exc


def _item_key(item):
    value = getattr(item, 'elem', None)
    value = getattr(value, 'value', value)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AutomationError('조직도 항목의 고유 핸들을 확인할 수 없습니다.')
    return value


def _siblings(first, limit):
    current, seen = first, set()
    while current is not None:
        key = _item_key(current)
        if key in seen:
            raise AutomationError('조직도 형제 항목에 순환 참조가 있습니다.')
        if len(seen) >= limit:
            raise AutomationError('조직도 항목 수가 읽기 한도를 초과했습니다.')
        seen.add(key)
        yield current
        current = _read('다음 항목', current.next_item)


def _children(item, limit):
    tree = getattr(item, 'tree_ctrl', None)
    if callable(getattr(tree, 'send_message', None)) and callable(getattr(item, 'item', None)):
        details = _read('하위 항목 정보', item.item)
        count = getattr(details, 'cChildren', None)
        if count not in (0, 1):
            raise AutomationError('조직도 하위 항목의 존재 여부를 확인할 수 없습니다.')
        if not count:
            return
        handle = _read('첫 하위 항목', lambda: tree.send_message(TVM_GETNEXTITEM, TVGN_CHILD, item.elem))
        if not handle:
            raise AutomationError('조직도 하위 항목이 있지만 전체 목록을 읽지 못했습니다.')
        first = _read('하위 항목 연결', lambda: type(item)(handle, tree))
        yield from _siblings(first, limit)
        return
    # An adapter may already expose a bounded children iterator. Real bundled
    # Win32 items use the message path above, avoiding the library's inner loop.
    children = _read('하위 항목 목록', item.children)
    seen = set()
    try:
        for child in children:
            key = _item_key(child)
            if key in seen:
                raise AutomationError('조직도 하위 항목에 순환 참조가 있습니다.')
            if len(seen) >= limit:
                raise AutomationError('조직도 항목 수가 읽기 한도를 초과했습니다.')
            seen.add(key)
            yield child
    except AutomationError:
        raise
    except Exception as exc:
        raise AutomationError('조직도 하위 항목 목록을 끝까지 읽지 못했습니다.') from exc


def snapshot_tree(tree, *, max_depth=16, max_nodes=5000):
    """Return item/text/nearest-first parent records for every logical item.

    The TreeView item count must match before/after the traversal and the
    number of unique handles read. A partial or changing tree never succeeds.
    """
    if type(max_depth) is not int or not 0 <= max_depth <= 16:
        raise ValueError('조직도 깊이 한도는 0~16이어야 합니다.')
    if type(max_nodes) is not int or not 1 <= max_nodes <= 5000:
        raise ValueError('조직도 항목 한도는 1~5000이어야 합니다.')
    expected = _read('전체 항목 수', tree.item_count)
    if type(expected) is not int or expected < 0:
        raise AutomationError('조직도 전체 항목 수를 확인할 수 없습니다.')
    if expected > max_nodes:
        raise AutomationError('조직도 항목 수가 읽기 한도를 초과했습니다.')
    first = _read('최상위 항목', tree.tree_root)
    records, visited = [], set()

    def visit(item, parents):
        if len(parents) > max_depth:
            raise AutomationError('조직도 부모 깊이가 읽기 한도를 초과했습니다.')
        key = _item_key(item)
        if key in visited:
            raise AutomationError('조직도 항목에 순환 참조 또는 중복 핸들이 있습니다.')
        if len(visited) >= max_nodes:
            raise AutomationError('조직도 항목 수가 읽기 한도를 초과했습니다.')
        visited.add(key)
        text = _read('항목 표시', item.text)
        if not isinstance(text, str) or not normalize(text) or '\n' in text or '\r' in text or '\x00' in text:
            raise AutomationError('조직도 항목의 전체 표시를 확인할 수 없습니다.')
        records.append({'item': item, 'text': text, 'parents': list(parents)})
        for child in _children(item, max_nodes):
            visit(child, [text, *parents])

    for root in _siblings(first, max_nodes):
        visit(root, [])
    final = _read('마지막 전체 항목 수', tree.item_count)
    if type(final) is not int or final != expected:
        raise AutomationError('조직도 항목 수가 읽는 동안 바뀌었습니다. 다시 불러오세요.')
    if len(records) != expected:
        raise AutomationError(f'조직도 전체 항목 수({expected})와 읽은 항목 수({len(records)})가 다릅니다.')
    return records


read_tree_snapshot = snapshot_tree


def parent_selectors(parent_texts):
    """Make UIA-compatible nearest-first selectors, allowing count changes."""
    if not isinstance(parent_texts, list) or len(parent_texts) > 16:
        raise AutomationError('조직도 부모 경로를 확인할 수 없습니다.')
    selectors = []
    for text in parent_texts:
        if not isinstance(text, str) or not normalize(text) or '\n' in text or '\r' in text or '\x00' in text:
            raise AutomationError('조직도 부모의 전체 표시를 확인할 수 없습니다.')
        base = re.sub(r'\s*\(\d+(?:/\d+)?\)\s*$', '', text)
        if not normalize(base):
            raise AutomationError('조직도 부모 이름이 비어 있습니다.')
        selectors.append({'control_type': 'TreeItem',
                          'name_regex': '^' + re.escape(base) + r'(?:\s*\(\d+(?:/\d+)?\))?$'})
    return selectors


class NativeContact:
    """Minimal driver adapter for one logical Win32 TreeView item."""
    def __init__(self, item):
        self.item = item
        self.tree_ctrl = item.tree_ctrl
        self.item_key = _item_key(item)
        self.element_info = SimpleNamespace(control_type='TreeItem', name=self.window_text())

    def window_text(self):
        return _read('연락처 표시', self.item.text)

    def process_id(self):
        return self.tree_ctrl.process_id()

    def top_level_parent(self):
        return self.tree_ctrl.top_level_parent()

    def set_focus(self):
        return self.tree_ctrl.set_focus()

    def is_enabled(self):
        return self.tree_ctrl.is_enabled()

    def double_click_input(self):
        # A posted WM_LBUTTONDBLCLK does not move the real cursor. Native
        # NM_DBLCLK handlers may hit-test that cursor instead of the message's
        # coordinates, so keep logical selection and physical input aligned.
        import win32api
        import win32gui
        import win32process
        tree = self.tree_ctrl
        tree_handle, expected_pid = tree.handle, self.process_id()
        root = win32gui.GetAncestor(tree_handle, 2)

        def valid_target():
            if (not root or tree.handle != tree_handle
                    or not win32gui.IsWindow(tree_handle) or not win32gui.IsWindow(root)
                    or win32gui.GetAncestor(tree_handle, 2) != root
                    or win32process.GetWindowThreadProcessId(tree_handle)[1] != expected_pid
                    or win32process.GetWindowThreadProcessId(root)[1] != expected_pid
                    or not win32gui.IsWindowVisible(tree_handle) or not win32gui.IsWindowEnabled(tree_handle)
                    or not win32gui.IsWindowVisible(root) or not win32gui.IsWindowEnabled(root)
                    or win32gui.GetForegroundWindow() != root
                    or _item_key(self.item) != self.item_key or self.window_text() != self.element_info.name):
                raise AutomationError('조직도 연락처나 활성 창이 바뀌었습니다. 더블클릭하지 않고 멈춥니다.')

        def text_rect():
            rect = self.item.client_rect(text_area_rect=True)
            if rect is None:
                raise AutomationError('조직도 연락처의 글자 위치를 확인할 수 없습니다.')
            return (rect.left, rect.top, rect.right, rect.bottom)

        valid_target()
        # Select the exact logical account without item.select(), whose hidden
        # set_focus() can activate a different owner window.
        selected_ok = tree.send_message_timeout(0x110B, 9, self.item_key, timeout=.2)  # TVM_SELECTITEM / TVGN_CARET
        selected = tree.send_message_timeout(TVM_GETNEXTITEM, 9, 0, timeout=.2)
        if (type(selected_ok) is not int or selected_ok != 1
                or type(selected) is not int or selected != self.item_key):
            error = AutomationError('조직도 대상 계정을 정확히 선택하지 못했습니다. 클릭하지 않고 멈춥니다.')
            error.window_state = {'selected_item': selected if type(selected) is int else 0,
                                  'expected_item': self.item_key}
            raise error
        valid_target()
        self.item.ensure_visible()
        # Do not let an earlier activation click become the first half of this
        # double-click. The interval is bounded by Windows' documented maximum.
        interval_ms = win32gui.GetDoubleClickTime()
        if type(interval_ms) is not int or not 1 <= interval_ms <= 5000:
            raise AutomationError('Windows 더블클릭 간격을 확인할 수 없습니다.')
        time.sleep(interval_ms / 1000 + .02)
        valid_target()
        bounds = text_rect()
        left, top, right, bottom = bounds
        client_point = ((left + right) // 2, (top + bottom) // 2)
        client_bounds = win32gui.GetClientRect(tree_handle)
        if (left >= right or top >= bottom
                or not client_bounds[0] <= client_point[0] < client_bounds[2]
                or not client_bounds[1] <= client_point[1] < client_bounds[3]):
            raise AutomationError('조직도 연락처 글자가 보이는 영역 밖에 있습니다.')
        point = win32gui.ClientToScreen(tree_handle, client_point)

        def verify_point():
            valid_target()
            if (text_rect() != bounds or win32gui.GetClientRect(tree_handle) != client_bounds
                    or win32gui.ClientToScreen(tree_handle, client_point) != point
                    or win32gui.WindowFromPoint(point) != tree_handle):
                raise AutomationError('조직도 연락처 위치가 바뀌거나 가려졌습니다. 추가 클릭 없이 멈춥니다.')

        verify_point()
        win32api.SetCursorPos(point)
        verify_point()
        if win32api.GetCursorPos() != point:
            raise AutomationError('조직도 연락처와 실제 커서 위치가 다릅니다. 클릭하지 않고 멈춥니다.')
        started = time.monotonic()
        for index in range(2):
            if index:
                # A single physical click must select this exact HTREEITEM.
                # Never call item.select(), which also steals native focus.
                time.sleep(min(.03, interval_ms / 4000))
                selection_deadline = min(time.monotonic() + .2, started + interval_ms / 1000)
                selected = 0
                for attempt in range(11):
                    remaining = selection_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    selected = tree.send_message_timeout(TVM_GETNEXTITEM, 9, 0,
                                                         timeout=min(.02, remaining))  # TVGN_CARET
                    if type(selected) is not int or selected == self.item_key or attempt == 10:
                        break
                    time.sleep(min(.02, max(0, selection_deadline - time.monotonic())))
                if type(selected) is not int or selected != self.item_key:
                    error = AutomationError('실제로 선택된 조직도 연락처가 다릅니다. 추가 클릭 없이 멈춥니다.')
                    error.window_state = {'selected_item': selected if type(selected) is int else 0,
                                          'expected_item': self.item_key}
                    raise error
                verify_point()
                if (win32api.GetCursorPos() != point
                        or time.monotonic() - started >= interval_ms / 1000):
                    raise AutomationError('더블클릭 위치나 입력 간격이 바뀌었습니다. 추가 클릭 없이 멈춥니다.')
            try:
                win32api.mouse_event(0x0002, 0, 0, 0)
            finally:
                win32api.mouse_event(0x0004, 0, 0, 0)
        return self

    def select(self):
        return self.item.select()
