"""Read-only diagnosis can expose a disabled owner and its modal child."""
import contextlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from app.windows import diagnose


def window(handle, *, enabled=True, visible=True, caption=True, pid=90000):
    return SimpleNamespace(handle=handle, process_id=Mock(return_value=pid),
                           is_visible=Mock(return_value=visible), is_enabled=Mock(return_value=enabled),
                           style=Mock(return_value=0x00C00000 if caption else 0),
                           class_name=Mock(return_value='#32770'), window_text=Mock(return_value='합성 창'),
                           set_focus=Mock(), click=Mock(), close=Mock(), descendants=Mock(return_value=[]))


class WindowDiagnosisTests(unittest.TestCase):
    def test_disabled_main_and_enabled_owned_dialog_are_visible_without_input(self):
        main = window(101, enabled=False)
        detail = window(202)
        hidden = window(303, visible=False)
        other = window(404, pid=90001)
        desktop = Mock()
        desktop.return_value.windows.return_value = [main, detail, hidden, other]
        gui = SimpleNamespace(GetWindow=Mock(side_effect=lambda handle, kind: 101 if handle == 202 else 0))
        with patch('app.windows.sys.platform', 'win32'), patch('app.windows.com_session', return_value=contextlib.nullcontext()), \
             patch('app.windows.executable', side_effect=lambda pid: 'C:/Synthetic/CoolMessenger.exe' if pid == 90000 else 'C:/Synthetic/Other.exe'), \
             patch.dict(sys.modules, {'pywinauto': SimpleNamespace(Desktop=desktop), 'win32gui': gui}):
            result = diagnose()
        self.assertTrue(result['available'])
        self.assertEqual([{key: row[key] for key in ('hwnd', 'pid', 'enabled', 'has_caption', 'owner_hwnd')}
                          for row in result['windows_found']],
                         [{'hwnd': 101, 'pid': 90000, 'enabled': False, 'has_caption': True, 'owner_hwnd': 0},
                          {'hwnd': 202, 'pid': 90000, 'enabled': True, 'has_caption': True, 'owner_hwnd': 101}])
        gui.GetWindow.assert_any_call(101, 4)
        gui.GetWindow.assert_any_call(202, 4)
        other.window_text.assert_not_called()
        hidden.process_id.assert_not_called()
        for control in (main, detail, hidden, other):
            control.set_focus.assert_not_called()
            control.click.assert_not_called()
            control.close.assert_not_called()
        main.descendants.assert_called_once_with(class_name='SysTreeView32', control_id=3013)
        detail.descendants.assert_called_once_with(class_name='SysTreeView32', control_id=3013)
        hidden.descendants.assert_not_called()
        other.descendants.assert_not_called()

    def test_captionless_window_is_reported_as_such(self):
        target = window(101, caption=False)
        desktop = Mock()
        desktop.return_value.windows.return_value = [target]
        with patch('app.windows.sys.platform', 'win32'), patch('app.windows.com_session', return_value=contextlib.nullcontext()), \
             patch('app.windows.executable', return_value='C:/Synthetic/CoolMessenger.exe'), \
             patch.dict(sys.modules, {'pywinauto': SimpleNamespace(Desktop=desktop),
                                      'win32gui': SimpleNamespace(GetWindow=Mock(return_value=0))}):
            result = diagnose()
        self.assertFalse(result['windows_found'][0]['has_caption'])
        self.assertTrue(result['windows_found'][0]['enabled'])

    def test_only_exact_message_manager_reads_native_button_labels(self):
        target = window(101)
        target.window_text.return_value = '메시지 관리함'
        button = window(111)
        button.class_name.return_value = 'Button'
        button.control_id = Mock(return_value=9001)
        button.window_text.return_value = '합성 새 메시지'
        message_list = window(112)
        message_list.class_name.return_value = 'SysListView32'
        target.descendants.return_value = [button, message_list]
        wrong_title = window(202)
        wrong_class = window(303)
        wrong_class.window_text.return_value = '메시지 관리함'
        wrong_class.class_name.return_value = 'OtherWindow'
        wrong_exe = window(404, pid=90001)
        wrong_exe.window_text.return_value = '메시지 관리함'
        desktop = Mock()
        desktop.return_value.windows.return_value = [target, wrong_title, wrong_class, wrong_exe]
        with patch('app.windows.sys.platform', 'win32'), patch('app.windows.com_session', return_value=contextlib.nullcontext()), \
             patch('app.windows.executable', side_effect=lambda pid: 'C:/Synthetic/CoolMessenger.exe' if pid == 90000 else 'C:/Synthetic/OtherCool.exe'), \
             patch.dict(sys.modules, {'pywinauto': SimpleNamespace(Desktop=desktop),
                                      'win32gui': SimpleNamespace(GetWindow=Mock(return_value=0))}):
            result = diagnose()
        target.descendants.assert_called_once_with(class_name='Button')
        self.assertEqual(result['windows_found'][0]['buttons'], [{
            'control_id': 9001, 'name': '합성 새 메시지', 'enabled': True,
            'visible': True, 'class_name': 'Button',
        }])
        message_list.window_text.assert_not_called()
        wrong_title.descendants.assert_called_once_with(class_name='SysTreeView32', control_id=3013)
        for other in (wrong_class, wrong_exe):
            other.descendants.assert_not_called()
        for control in (target, button, message_list, wrong_title, wrong_class, wrong_exe):
            control.set_focus.assert_not_called()
            control.click.assert_not_called()
            control.close.assert_not_called()

    def test_main_needs_exactly_one_native_organization_tree_without_reading_items(self):
        for count in (0, 1, 2):
            with self.subTest(tree_count=count):
                target = window(101)
                trees = [window(120 + index) for index in range(count)]
                for tree in trees:
                    tree.class_name.return_value = 'SysTreeView32'
                    tree.control_id = Mock(return_value=3013)
                    tree.window_text.side_effect = AssertionError('Tree contents must not be read')
                button = window(111)
                button.class_name.return_value = 'Button'
                button.control_id = Mock(return_value=9001)
                button.window_text.return_value = '합성 새 메시지'
                target.descendants.side_effect = lambda **kwargs: trees if kwargs['class_name'] == 'SysTreeView32' else [button]
                desktop = Mock()
                desktop.return_value.windows.return_value = [target]
                with patch('app.windows.sys.platform', 'win32'), patch('app.windows.com_session', return_value=contextlib.nullcontext()), \
                     patch('app.windows.executable', return_value='C:/Synthetic/CoolMessenger.exe'), \
                     patch.dict(sys.modules, {'pywinauto': SimpleNamespace(Desktop=desktop),
                                              'win32gui': SimpleNamespace(GetWindow=Mock(return_value=0))}):
                    result = diagnose()
                info = result['windows_found'][0]
                if count == 1:
                    self.assertEqual(info['buttons'][0]['control_id'], 9001)
                    target.descendants.assert_any_call(class_name='Button')
                else:
                    self.assertNotIn('buttons', info)
                    target.descendants.assert_called_once_with(class_name='SysTreeView32', control_id=3013)
                    button.window_text.assert_not_called()
                for tree in trees:
                    tree.window_text.assert_not_called()
                    tree.descendants.assert_not_called()
                target.click.assert_not_called()
                target.set_focus.assert_not_called()
