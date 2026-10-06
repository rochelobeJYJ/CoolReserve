"""Exact picker button input guards; all native calls and controls are fake."""
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from app.windows import AutomationError, WindowsDriver


class PickerButtonTests(unittest.TestCase):
    def setUp(self):
        self.driver = WindowsDriver({}, threading.Event())
        self.driver.focus = Mock()
        self.driver.wait = Mock()
        self.driver.checkpoint = Mock()
        self.button = SimpleNamespace(handle=222, process_id=Mock(return_value=90000),
            class_name=Mock(return_value='Button'), control_id=Mock(return_value=1636),
            style=Mock(return_value=0x5001000B), press_mouse=Mock(), release_mouse=Mock())
        self.point = (-966, -292)
        self.selection = Mock(return_value=True)
        self.gui = SimpleNamespace(IsWindow=Mock(return_value=True), IsWindowVisible=Mock(return_value=True),
            IsWindowEnabled=Mock(return_value=True), IsIconic=Mock(return_value=False),
            GetAncestor=Mock(return_value=101), GetClassName=Mock(return_value='#32770'),
            GetWindowText=Mock(return_value='사용자 선택'), GetForegroundWindow=Mock(return_value=101),
            GetClientRect=Mock(return_value=(0, 0, 68, 16)),
            GetWindowRect=Mock(side_effect=lambda handle: (-1000, -300, -932, -284) if handle == 222
                              else (-1100, -500, -500, 200)),
            ClientToScreen=Mock(return_value=self.point), WindowFromPoint=Mock(return_value=222))
        self.api = SimpleNamespace(SetCursorPos=Mock(), GetCursorPos=Mock(return_value=self.point))
        self.process = SimpleNamespace(GetWindowThreadProcessId=Mock(return_value=(1, 90000)))
        self.profile_patch = patch('app.windows.is_gentoo_profile', return_value=True)
        self.profile_patch.start()
        self.addCleanup(self.profile_patch.stop)
        native_patch = patch.dict(sys.modules, {'win32gui': self.gui, 'win32api': self.api,
                                                'win32process': self.process})
        native_patch.start()
        self.addCleanup(native_patch.stop)

    def click(self):
        self.driver._click_gentoo_picker_button(self.button, self.selection)

    def assert_no_input(self):
        self.button.press_mouse.assert_not_called()
        self.button.release_mouse.assert_not_called()

    def test_exact_button_moves_cursor_then_dispatches_one_correct_flag_pair(self):
        actions = Mock()
        actions.attach_mock(self.driver.focus, 'focus')
        actions.attach_mock(self.api.SetCursorPos, 'cursor')
        actions.attach_mock(self.button.press_mouse, 'down')
        actions.attach_mock(self.button.release_mouse, 'up')
        self.click()
        self.assertEqual(actions.mock_calls, [call.focus(self.button), call.cursor(self.point),
            call.down(pressed='left', coords=(34, 8)), call.up(pressed='', coords=(34, 8))])
        self.assertEqual(self.selection.call_count, 2)

    def test_confirm_uses_same_guard_but_send_and_other_buttons_are_rejected(self):
        self.button.control_id.return_value = 3007
        self.click()
        self.button.press_mouse.assert_called_once()
        self.button.press_mouse.reset_mock()
        self.button.release_mouse.reset_mock()
        for control_id in (3249, 1656, 0):
            self.button.control_id.return_value = control_id
            with self.assertRaises(AutomationError):
                self.click()
        self.assert_no_input()

    def test_wrong_class_style_or_read_only_is_rejected_before_focus(self):
        self.button.class_name.return_value = 'Edit'
        with self.assertRaises(AutomationError):
            self.click()
        self.button.class_name.return_value = 'Button'
        self.button.style.return_value = 0
        with self.assertRaises(AutomationError):
            self.click()
        self.button.style.return_value = 0xB
        self.driver.read_only = True
        with self.assertRaises(AutomationError):
            self.click()
        self.driver.focus.assert_not_called()
        self.api.SetCursorPos.assert_not_called()
        self.assert_no_input()

    def test_selection_changed_by_focus_is_rejected_before_cursor_move(self):
        self.driver.focus.side_effect = lambda _: setattr(self.selection, 'return_value', False)
        with self.assertRaisesRegex(AutomationError, '활성화 후'):
            self.click()
        self.api.SetCursorPos.assert_not_called()
        self.assert_no_input()

    def test_selection_changed_after_cursor_move_is_rejected(self):
        self.api.SetCursorPos.side_effect = lambda _: setattr(self.selection, 'return_value', False)
        with self.assertRaisesRegex(AutomationError, '직전에 수신자'):
            self.click()
        self.assert_no_input()

    def test_same_process_sibling_or_foreign_overlay_blocks_before_cursor_move(self):
        for hit in (0, 101, 333):
            self.gui.WindowFromPoint.return_value = hit
            with self.assertRaisesRegex(AutomationError, '가려져'):
                self.click()
        self.api.SetCursorPos.assert_not_called()
        self.assert_no_input()

    def test_late_overlay_blocks_after_cursor_move(self):
        self.api.SetCursorPos.side_effect = lambda _: setattr(self.gui.WindowFromPoint, 'return_value', 333)
        with self.assertRaisesRegex(AutomationError, '가려져'):
            self.click()
        self.assert_no_input()

    def test_root_pid_and_geometry_changes_after_cursor_move_block(self):
        for issue in ('pid', 'root', 'rectangle', 'foreground', 'hidden', 'disabled'):
            with self.subTest(issue=issue):
                self.process.GetWindowThreadProcessId.return_value = (1, 90000)
                self.gui.GetAncestor.return_value = 101
                self.gui.GetForegroundWindow.return_value = 101
                self.gui.IsWindowVisible.return_value = True
                self.gui.IsWindowEnabled.return_value = True
                self.gui.GetWindowRect.side_effect = lambda handle: (-1000, -300, -932, -284) if handle == 222 else (-1100, -500, -500, 200)
                def change(_):
                    if issue == 'pid':
                        self.process.GetWindowThreadProcessId.return_value = (1, 90001)
                    elif issue == 'root':
                        self.gui.GetAncestor.return_value = 102
                    elif issue == 'rectangle':
                        self.gui.GetWindowRect.side_effect = lambda _: (0, 0, 1, 1)
                    elif issue == 'foreground':
                        self.gui.GetForegroundWindow.return_value = 102
                    elif issue == 'hidden':
                        self.gui.IsWindowVisible.return_value = False
                    else:
                        self.gui.IsWindowEnabled.return_value = False
                self.api.SetCursorPos.side_effect = change
                with self.assertRaises(AutomationError):
                    self.click()
        self.assert_no_input()

    def test_cursor_mismatch_or_later_user_movement_blocks(self):
        for readings in ((0, 0), [self.point, (0, 0)]):
            self.api.GetCursorPos.side_effect = readings if isinstance(readings, list) else None
            self.api.GetCursorPos.return_value = readings
            with self.assertRaisesRegex(AutomationError, '커서'):
                self.click()
        self.assert_no_input()

    def test_uncertain_down_or_up_is_not_retried(self):
        self.button.press_mouse.side_effect = TimeoutError('synthetic')
        with self.assertRaises(AutomationError):
            self.click()
        self.button.press_mouse.assert_called_once()
        self.button.release_mouse.assert_not_called()
        self.button.press_mouse.reset_mock()
        self.button.press_mouse.side_effect = None
        self.button.release_mouse.side_effect = TimeoutError('synthetic')
        with self.assertRaises(AutomationError):
            self.click()
        self.button.press_mouse.assert_called_once()
        self.button.release_mouse.assert_called_once()


if __name__ == '__main__':
    unittest.main()
