"""Click routing tests with fake Windows APIs; no real input or windows."""
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from app.windows import AutomationError, WindowsDriver


class FakeRectangle:
    left, top, right, bottom = -1200, 30, -1000, 70

    def mid_point(self):
        return SimpleNamespace(x=-1100, y=50)

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


def fake_control(class_name='FixtureCustom', control_type='Custom'):
    return SimpleNamespace(element_info=SimpleNamespace(control_type=control_type),
                           rectangle=Mock(return_value=FakeRectangle()),
                           class_name=Mock(return_value=class_name), is_enabled=Mock(return_value=True),
                           invoke=Mock(), click_input=Mock(), set_focus=Mock(),
                           process_id=Mock(return_value=777))


class PhysicalClickTests(unittest.TestCase):
    def setUp(self):
        self.driver = WindowsDriver({'roles': {}}, threading.Event())
        self.driver.checkpoint = Mock()
        self.driver.wait = Mock()
        self.control = fake_control()
        self.cursor = None
        def set_cursor(point):
            self.cursor = point
        self.api = SimpleNamespace(SetCursorPos=Mock(side_effect=set_cursor),
                                   GetCursorPos=Mock(side_effect=lambda: self.cursor), mouse_event=Mock())
        self.fake_api = patch.dict(sys.modules, {'win32api': self.api})
        self.fake_api.start()

    def tearDown(self):
        self.fake_api.stop()

    def test_negative_screen_center_is_preserved_and_only_button_flags_are_sent(self):
        self.driver.physical_click(self.control)
        self.api.SetCursorPos.assert_called_once_with((-1100, 50))
        self.api.GetCursorPos.assert_called_once()
        self.assertEqual(self.api.mouse_event.call_args_list,
                         [call(0x2, 0, 0, 0), call(0x4, 0, 0, 0)])
        self.control.click_input.assert_not_called()
        self.control.invoke.assert_not_called()

    def test_secondary_screen_centers_are_preserved_on_all_four_sides(self):
        placements = [
            ('left', (-2800, 330, -2600, 370), (-2700, 350)),
            ('right', (3000, 120, 3200, 180), (3100, 150)),
            ('above', (400, -1200, 600, -1000), (500, -1100)),
            ('below', (400, 1300, 600, 1500), (500, 1400)),
        ]
        for side, bounds, expected_center in placements:
            with self.subTest(side=side):
                for operation in (self.api.SetCursorPos, self.api.GetCursorPos, self.api.mouse_event):
                    operation.reset_mock()
                self.control.rectangle.return_value = SimpleNamespace(
                    left=bounds[0], top=bounds[1], right=bounds[2], bottom=bounds[3])
                self.driver.physical_click(self.control)
                self.api.SetCursorPos.assert_called_once_with(expected_center)
                self.api.GetCursorPos.assert_called_once()
                self.assertEqual(self.api.mouse_event.call_args_list,
                                 [call(0x2, 0, 0, 0), call(0x4, 0, 0, 0)])
                self.control.click_input.assert_not_called()
                self.control.invoke.assert_not_called()

    def test_double_click_sends_two_down_up_pairs_without_absolute_or_move_flags(self):
        self.driver.physical_click(self.control, double=True)
        self.api.SetCursorPos.assert_called_once_with((-1100, 50))
        self.assertEqual(self.api.mouse_event.call_args_list,
                         [call(0x2, 0, 0, 0), call(0x4, 0, 0, 0),
                          call(0x2, 0, 0, 0), call(0x4, 0, 0, 0)])

    def test_read_only_guard_runs_before_geometry_or_any_windows_api(self):
        self.driver.read_only = True
        with self.assertRaises(AutomationError):
            self.driver.physical_click(self.control)
        self.control.rectangle.assert_not_called()
        self.api.SetCursorPos.assert_not_called()
        self.api.GetCursorPos.assert_not_called()
        self.api.mouse_event.assert_not_called()

    def test_cursor_mismatch_aborts_before_button_input(self):
        self.api.GetCursorPos.side_effect = None
        self.api.GetCursorPos.return_value = (-1099, 50)
        with self.assertRaises(AutomationError):
            self.driver.physical_click(self.control)
        self.api.SetCursorPos.assert_called_once_with((-1100, 50))
        self.api.mouse_event.assert_not_called()


class ClickRoutingTests(unittest.TestCase):
    def setUp(self):
        self.driver = WindowsDriver({'roles': {}}, threading.Event())
        self.driver.focus = Mock()
        self.driver.wait = Mock()
        self.driver.physical_click = Mock()

    def native_button(self):
        button = fake_control('Button', 'Button')
        button.send_message_timeout = Mock()
        button.get_check_state = Mock(return_value=0)
        return button

    def ownerdraw_button(self):
        button = self.native_button()
        button.style = Mock(return_value=0x5001000B)
        button.client_rect = Mock(return_value=SimpleNamespace(left=0, top=0, right=68, bottom=15))
        button.click = Mock()
        button.press_mouse = Mock()
        button.release_mouse = Mock()
        return button

    def test_native_button_uses_one_bm_click_without_invoke_or_mouse(self):
        button = self.native_button()
        self.driver.click(button)
        self.driver.focus.assert_called_once_with(button)
        button.send_message_timeout.assert_called_once_with(0xF5, 0, 0)
        button.invoke.assert_not_called()
        button.click_input.assert_not_called()
        self.driver.physical_click.assert_not_called()

    def test_ownerdraw_button_uses_one_wm_down_up_pair_at_client_center(self):
        button = self.ownerdraw_button()
        input_calls = Mock()
        input_calls.attach_mock(button.press_mouse, 'down')
        input_calls.attach_mock(button.release_mouse, 'up')
        self.driver.click(button)
        self.driver.focus.assert_called_once_with(button)
        button.client_rect.assert_called_once_with()
        self.assertEqual(input_calls.mock_calls,
                         [call.down(pressed='left', coords=(34, 7)),
                          call.up(pressed='', coords=(34, 7))])
        button.click.assert_not_called()
        button.rectangle.assert_not_called()
        button.send_message_timeout.assert_not_called()
        button.invoke.assert_not_called()
        button.click_input.assert_not_called()
        self.driver.physical_click.assert_not_called()

    def test_ownerdraw_wm_pair_exceptions_never_retry_or_continue_after_failed_down(self):
        for failure_stage in ('press_mouse', 'release_mouse'):
            for error_type in (TimeoutError, AttributeError):
                with self.subTest(failure_stage=failure_stage, error_type=error_type.__name__):
                    button = self.ownerdraw_button()
                    getattr(button, failure_stage).side_effect = error_type('Fixture WM result unavailable')
                    self.driver.physical_click.reset_mock()
                    with self.assertRaises(AutomationError):
                        self.driver.click(button)
                    button.press_mouse.assert_called_once_with(pressed='left', coords=(34, 7))
                    if failure_stage == 'press_mouse':
                        button.release_mouse.assert_not_called()
                    else:
                        button.release_mouse.assert_called_once_with(pressed='', coords=(34, 7))
                    button.click.assert_not_called()
                    button.send_message_timeout.assert_not_called()
                    button.invoke.assert_not_called()
                    button.click_input.assert_not_called()
                    self.driver.physical_click.assert_not_called()

    def test_uncertain_native_timeout_never_retries_as_invoke_or_physical_click(self):
        button = self.native_button()
        button.send_message_timeout.side_effect = TimeoutError('Fixture-only native timeout')
        with self.assertRaises(AutomationError):
            self.driver.click(button)
        button.send_message_timeout.assert_called_once_with(0xF5, 0, 0)
        button.invoke.assert_not_called()
        button.click_input.assert_not_called()
        self.driver.physical_click.assert_not_called()

    def test_native_attribute_error_after_dispatch_never_retries(self):
        button = self.native_button()
        button.send_message_timeout.side_effect = AttributeError('Fixture dispatch result unavailable')
        with self.assertRaises(AutomationError):
            self.driver.click(button)
        button.send_message_timeout.assert_called_once_with(0xF5, 0, 0)
        button.invoke.assert_not_called()
        button.click_input.assert_not_called()
        self.driver.physical_click.assert_not_called()

    def test_read_only_guard_blocks_native_bm_click_before_focus(self):
        button = self.native_button()
        self.driver.read_only = True
        with self.assertRaises(AutomationError):
            self.driver.click(button)
        self.driver.focus.assert_not_called()
        button.send_message_timeout.assert_not_called()
        button.invoke.assert_not_called()
        self.driver.physical_click.assert_not_called()

    def test_generic_missing_invoke_uses_safe_physical_helper(self):
        control = fake_control()
        del control.invoke
        self.driver.click(control)
        self.driver.physical_click.assert_called_once_with(control)
        control.click_input.assert_not_called()

    def test_successful_generic_invoke_does_not_add_a_second_click(self):
        control = fake_control()
        self.driver.click(control)
        control.invoke.assert_called_once_with()
        self.driver.physical_click.assert_not_called()
        control.click_input.assert_not_called()

    def test_generic_invoke_attribute_error_after_dispatch_never_retries(self):
        control = fake_control()
        control.invoke.side_effect = AttributeError('Fixture Invoke result unavailable')
        with self.assertRaises(AutomationError):
            self.driver.click(control)
        control.invoke.assert_called_once_with()
        self.driver.physical_click.assert_not_called()
        control.click_input.assert_not_called()

    def test_document_focus_uses_safe_helper_and_preserves_its_existing_caret_strategy(self):
        driver = WindowsDriver({'roles': {}}, threading.Event())
        driver.checkpoint = Mock()
        driver.wait = Mock()
        driver.physical_click = Mock()
        document = fake_control('Chrome_WidgetWin', 'Document')
        top = SimpleNamespace(set_focus=Mock())
        document.top_level_parent = Mock(return_value=top)
        gui = SimpleNamespace(GetForegroundWindow=Mock(return_value=12345))
        processes = SimpleNamespace(GetWindowThreadProcessId=Mock(return_value=(55, 777)))
        with patch.dict(sys.modules, {'win32gui': gui, 'win32process': processes}):
            driver.focus(document)
        top.set_focus.assert_called_once_with()
        driver.physical_click.assert_called_once_with(document)
        document.set_focus.assert_not_called()
        document.click_input.assert_not_called()


if __name__ == '__main__':
    unittest.main()
