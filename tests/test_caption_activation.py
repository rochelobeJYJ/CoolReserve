"""Caption activation with fake Win32 calls: no native UI or physical input."""
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from app.windows import AutomationError, WindowsDriver


class CaptionActivationTests(unittest.TestCase):
    def setUp(self):
        self.driver = WindowsDriver({}, threading.Event())
        self.driver.checkpoint = Mock()
        self.driver.wait = Mock()
        self.point = (-1800, -484)
        self.gui = SimpleNamespace(
            IsWindow=Mock(return_value=True), IsWindowVisible=Mock(return_value=True),
            IsWindowEnabled=Mock(return_value=True), GetWindowLong=Mock(return_value=0x00C00000),
            IsIconic=Mock(return_value=False), ShowWindow=Mock(),
            GetForegroundWindow=Mock(return_value=202), SetWindowPos=Mock(),
            GetWindowRect=Mock(return_value=(-2000, -500, -1400, 200)),
            ClientToScreen=Mock(return_value=(-1992, -468)), WindowFromPoint=Mock(return_value=101),
            GetAncestor=Mock(return_value=101), SendMessageTimeout=Mock(return_value=(1, 2)))
        self.api = SimpleNamespace(SetCursorPos=Mock(), GetCursorPos=Mock(return_value=self.point), mouse_event=Mock())
        self.process = SimpleNamespace(GetWindowThreadProcessId=Mock(return_value=(1, 90000)))
        self.api.mouse_event.side_effect = lambda flags, *args: setattr(self.gui.GetForegroundWindow, 'return_value', 101) if flags == 4 else None
        self.modules = patch.dict(sys.modules, {'win32gui': self.gui, 'win32api': self.api, 'win32process': self.process})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def activate(self):
        self.driver._activate_by_caption(101, 90000)

    def test_negative_desktop_coordinates_use_caption_hit_test_and_one_button_pair(self):
        self.activate()
        self.gui.SetWindowPos.assert_called_once_with(101, 0, 0, 0, 0, 0, 0x13)
        self.api.SetCursorPos.assert_called_once_with(self.point)
        packed = ((self.point[1] & 0xFFFF) << 16) | (self.point[0] & 0xFFFF)
        self.assertEqual(self.gui.SendMessageTimeout.call_args_list,
                         [call(101, 0x84, 0, packed, 3, 200)] * 2)
        self.assertEqual(self.api.mouse_event.call_args_list, [call(2, 0, 0, 0), call(4, 0, 0, 0)])

    def test_already_foreground_has_no_cursor_or_z_order_changes(self):
        self.gui.GetForegroundWindow.return_value = 101
        self.activate()
        self.gui.SetWindowPos.assert_not_called()
        self.api.SetCursorPos.assert_not_called()
        self.api.mouse_event.assert_not_called()

    def test_overlay_or_foreign_point_is_never_clicked(self):
        for issue in ('overlay', 'pid', 'nothing'):
            with self.subTest(issue=issue):
                self.gui.WindowFromPoint.return_value = 0 if issue == 'nothing' else 303
                self.gui.GetAncestor.return_value = 404 if issue == 'overlay' else 101
                self.process.GetWindowThreadProcessId.side_effect = lambda handle: (1, 90001 if handle == 303 else 90000)
                with self.assertRaises(AutomationError):
                    self.activate()
        self.api.SetCursorPos.assert_not_called()
        self.api.mouse_event.assert_not_called()

    def test_close_button_client_area_and_hit_test_timeout_never_click(self):
        for area in (20, 1, 3, 0, 8):
            with self.subTest(area=area):
                self.gui.SendMessageTimeout.return_value = (1, area)
                with self.assertRaises(AutomationError):
                    self.activate()
        self.gui.SendMessageTimeout.side_effect = TimeoutError('Synthetic hung target')
        with self.assertRaises(TimeoutError):
            self.activate()
        self.api.mouse_event.assert_not_called()

    def test_cursor_mismatch_and_late_overlay_do_not_click(self):
        self.api.GetCursorPos.return_value = (100, 100)
        with self.assertRaisesRegex(AutomationError, '커서 위치'):
            self.activate()
        self.api.GetCursorPos.return_value = self.point
        self.gui.WindowFromPoint.side_effect = [101, 404]
        self.gui.GetAncestor.side_effect = lambda handle, flag: handle
        with self.assertRaisesRegex(AutomationError, '가려져'):
            self.activate()
        self.api.mouse_event.assert_not_called()

    def test_minimized_hidden_disabled_and_captionless_targets_do_not_click(self):
        for method, value in (('IsIconic', True), ('IsWindowVisible', False),
                              ('IsWindowEnabled', False), ('GetWindowLong', 0)):
            with self.subTest(method=method):
                original = getattr(self.gui, method).return_value
                getattr(self.gui, method).return_value = value
                with self.assertRaises(AutomationError):
                    self.activate()
                getattr(self.gui, method).return_value = original
        self.api.mouse_event.assert_not_called()

    def test_target_moves_or_changes_pid_before_click_blocks(self):
        self.gui.GetWindowRect.side_effect = [(-2000, -500, -1400, 200), (-1900, -500, -1300, 200)]
        with self.assertRaises(AutomationError):
            self.activate()
        self.gui.GetWindowRect.side_effect = None
        self.process.GetWindowThreadProcessId.return_value = (1, 90001)
        with self.assertRaises(AutomationError):
            self.activate()
        self.api.mouse_event.assert_not_called()

    def test_click_without_activation_stops_and_is_never_repeated(self):
        self.api.mouse_event.side_effect = None
        with self.assertRaisesRegex(AutomationError, '추가 클릭 없이'):
            self.activate()
        self.assertEqual(self.api.mouse_event.call_args_list, [call(2, 0, 0, 0), call(4, 0, 0, 0)])
        self.assertEqual(self.driver.wait.call_count, 3)

    def test_mouse_release_is_attempted_even_when_press_result_is_uncertain(self):
        self.api.mouse_event.side_effect = [RuntimeError('Synthetic input failure'), None]
        with self.assertRaises(RuntimeError):
            self.activate()
        self.assertEqual(self.api.mouse_event.call_args_list, [call(2, 0, 0, 0), call(4, 0, 0, 0)])

    def test_read_only_cannot_raise_or_click_a_window(self):
        self.driver.read_only = True
        with self.assertRaises(AutomationError):
            self.activate()
        self.gui.SetWindowPos.assert_not_called()
        self.api.mouse_event.assert_not_called()
