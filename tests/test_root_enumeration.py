"""Transient native window enumeration races, using fake Desktop instances only."""
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from app.windows import PreparationTransient, Stopped, WindowsDriver


class FakeInvalidWindowHandle(RuntimeError):
    pass


class RootEnumerationTests(unittest.TestCase):
    spec = {'backend': 'win32', 'root_class': '#32770', 'root_title': '합성 선택창',
            'exe': r'C:\SyntheticOnly\CoolMessenger.exe'}

    def setUp(self):
        self.driver = WindowsDriver({}, threading.Event())
        self.driver.checkpoint = Mock()
        self.driver.wait = Mock()
        self.window = SimpleNamespace(class_name=Mock(return_value='#32770'),
            window_text=Mock(return_value='합성 선택창'), process_id=Mock(return_value=90000),
            is_enabled=Mock(return_value=False), is_visible=Mock(return_value=True))
        self.enumerate = Mock(return_value=[self.window])
        self.desktop = Mock(side_effect=lambda **_: SimpleNamespace(windows=self.enumerate))
        patches = [patch.dict(sys.modules, {
            'pywinauto': SimpleNamespace(Desktop=self.desktop),
            'pywinauto.controls': SimpleNamespace(InvalidWindowHandle=FakeInvalidWindowHandle)}),
            patch('app.windows.executable', return_value=self.spec['exe'])]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_success_has_one_enumeration_and_preserves_disabled_matching_roots(self):
        self.assertEqual(self.driver.roots(self.spec), [self.window])
        self.desktop.assert_called_once_with(backend='win32')
        self.driver.wait.assert_not_called()
        self.window.is_enabled.assert_not_called()
        self.window.is_visible.assert_not_called()

    def test_one_or_two_transient_failures_use_fresh_desktops_then_return_matches(self):
        for failures in (1, 2):
            with self.subTest(failures=failures):
                self.desktop.reset_mock()
                self.driver.wait.reset_mock()
                self.enumerate.side_effect = [FakeInvalidWindowHandle('synthetic') for _ in range(failures)] + [[self.window]]
                self.assertEqual(self.driver.roots(self.spec), [self.window])
                self.assertEqual(self.desktop.call_count, failures + 1)
                self.assertEqual(self.driver.wait.call_args_list, [call(.1)] * failures)

    def test_persistent_invalid_handle_raises_instead_of_appearing_closed(self):
        error = FakeInvalidWindowHandle('synthetic persistent race')
        self.enumerate.side_effect = error
        with self.assertRaises(PreparationTransient) as caught:
            self.driver.roots(self.spec)
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(self.desktop.call_count, 3)
        self.assertEqual(self.driver.wait.call_args_list, [call(.1), call(.1)])

    def test_other_enumeration_errors_are_not_retried_or_converted_to_empty(self):
        for error in (RuntimeError('synthetic'), PermissionError('synthetic')):
            with self.subTest(error=type(error).__name__):
                self.desktop.reset_mock()
                self.enumerate.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    self.driver.roots(self.spec)
                self.assertIs(caught.exception, error)
                self.assertEqual(self.desktop.call_count, 1)
        self.driver.wait.assert_not_called()

    def test_successful_empty_reenumeration_can_return_empty(self):
        self.enumerate.side_effect = [FakeInvalidWindowHandle('synthetic'), []]
        self.assertEqual(self.driver.roots(self.spec), [])
        self.assertEqual(self.desktop.call_count, 2)

    def test_stop_during_retry_never_reenumerates(self):
        self.enumerate.side_effect = FakeInvalidWindowHandle('synthetic')
        self.driver.wait.side_effect = Stopped('F8')
        with self.assertRaises(Stopped):
            self.driver.roots(self.spec)
        self.assertEqual(self.desktop.call_count, 1)

    def test_filtering_and_stale_individual_wrappers_keep_existing_behavior(self):
        stale = SimpleNamespace(class_name=Mock(side_effect=FakeInvalidWindowHandle('synthetic')))
        wrong_class = SimpleNamespace(class_name=lambda: 'Other')
        wrong_title = SimpleNamespace(class_name=lambda: '#32770', window_text=lambda: '다른 합성창')
        self.enumerate.return_value = [stale, wrong_class, wrong_title, self.window]
        self.assertEqual(self.driver.roots(self.spec), [self.window])
        self.assertEqual(self.desktop.call_count, 1)
        self.driver.wait.assert_not_called()


if __name__ == '__main__':
    unittest.main()
