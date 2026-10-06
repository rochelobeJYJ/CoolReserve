"""COM lifetime regressions; real probes read only the desktop UIA root."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import windows
from app import gentoo


CHILD = r'''
import contextlib, json, sys, threading
sys.path.insert(0, sys.argv[1])
mode = sys.argv[2]
results = []

if mode == 'legacy':
    # Reproduce the previous startup: UIA is first imported from a short-lived
    # STA request thread, with no persistent apartment holding its singleton.
    sys.coinit_flags = 2
    runtime = contextlib.nullcontext
    @contextlib.contextmanager
    def session():
        import pythoncom
        pythoncom.CoInitialize()
        try:
            yield
        finally:
            pythoncom.CoUninitialize()
else:
    from app.windows import automation_runtime, com_session
    runtime = automation_runtime
    session = com_session

def readonly_request():
    try:
        with session():
            from pywinauto.uia_defines import IUIA
            client = IUIA()
            control_type = int(client.root.CurrentControlType)
            # TreeScope_Element reads only the root and exercises the shared
            # root, client and condition together without enumerating windows.
            count = int(client.root.FindAll(client.tree_scope['element'], client.true_condition).Length)
            results.append({'status': 'ok', 'control_type': control_type, 'root_count': count})
    except Exception as error:
        results.append({'status': 'error', 'kind': type(error).__name__, 'message': str(error)[:200]})

with runtime():
    if mode == 'incompatible':
        import pythoncom
        def incompatible_request():
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            try:
                try:
                    with session():
                        results.append({'status': 'incorrectly-entered'})
                except Exception as error:
                    results.append({'status': 'rejected', 'hresult': getattr(error, 'hresult', None)})
            finally:
                pythoncom.CoUninitialize()
        targets = [incompatible_request]
    else:
        targets = [readonly_request] * 4
    for target in targets:
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(8)
        if thread.is_alive():
            results.append({'status': 'timeout'})
            break
print(json.dumps(results), flush=True)
'''


class ComRuntimeTests(unittest.TestCase):
    def test_session_balances_initialization_on_success_and_exception(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                fake = SimpleNamespace(COINIT_MULTITHREADED=0, CoInitializeEx=Mock(), CoUninitialize=Mock())
                with patch.dict(sys.modules, {'pythoncom': fake}), \
                        patch.object(windows, 'require_windows'):
                    if fail:
                        with self.assertRaisesRegex(ValueError, 'body failure'):
                            with windows.com_session():
                                raise ValueError('body failure')
                    else:
                        with windows.com_session():
                            pass
                fake.CoInitializeEx.assert_called_once_with(0)
                fake.CoUninitialize.assert_called_once_with()

    def test_incompatible_initialization_never_enters_or_uninitializes(self):
        failure = RuntimeError('incompatible apartment')
        fake = SimpleNamespace(COINIT_MULTITHREADED=0,
                               CoInitializeEx=Mock(side_effect=failure), CoUninitialize=Mock())
        entered = False
        with patch.dict(sys.modules, {'pythoncom': fake}), patch.object(windows, 'require_windows'):
            with self.assertRaisesRegex(RuntimeError, 'incompatible apartment'):
                with windows.com_session():
                    entered = True
        self.assertFalse(entered)
        fake.CoUninitialize.assert_not_called()

    def test_runtime_has_no_com_dependency_on_non_windows(self):
        with patch.object(windows.sys, 'platform', 'linux'), \
                patch.object(windows, 'com_session') as session:
            with windows.automation_runtime():
                pass
        session.assert_not_called()

    def test_selection_query_failure_is_distinct_from_empty_selection(self):
        path = 'C:\\TestOnly\\CoolMessenger.exe'
        failure = RuntimeError('disconnected test COM object')
        for broken in (True, False):
            with self.subTest(broken=broken):
                tree = SimpleNamespace(get_selection=Mock(side_effect=failure) if broken else Mock(return_value=[]))
                root = SimpleNamespace(window_text=lambda: 'TestOnly', process_id=lambda: 42,
                                       descendants=lambda **kwargs: [tree])
                desktop = Mock(return_value=SimpleNamespace(windows=lambda: [root]))
                fake_module = SimpleNamespace(UIAWrapper=Mock())
                message = '선택 상태를 읽지 못했습니다' if broken else '본인 항목 하나를 선택'
                with patch.dict(sys.modules, {'pywinauto.controls.uiawrapper': fake_module}), \
                        patch.object(gentoo, 'executable', return_value=path):
                    with self.assertRaisesRegex(windows.AutomationError, message) as caught:
                        gentoo._selected_contact(desktop, path, '93001')
                self.assertIs(caught.exception.__cause__, failure if broken else None)

    def run_probe(self, mode):
        result = subprocess.run(
            [sys.executable, '-B', '-X', 'utf8', '-c', CHILD, str(ROOT), mode],
            capture_output=True, text=True, encoding='utf-8', timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @unittest.skipUnless(os.name == 'nt', 'Real Windows COM apartment lifecycle')
    def test_persistent_runtime_survives_consecutive_request_thread_exit(self):
        legacy = self.run_probe('legacy')
        self.assertEqual(legacy[0]['status'], 'ok', legacy)
        self.assertTrue(any(item['status'] == 'error' for item in legacy[1:]), legacy)
        current = self.run_probe('current')
        self.assertEqual(len(current), 4, current)
        self.assertEqual([item['status'] for item in current], ['ok'] * 4, current)
        self.assertEqual([item['root_count'] for item in current], [1] * 4, current)
        self.assertGreater(current[0]['control_type'], 0)
        self.assertEqual(len({item['control_type'] for item in current}), 1, current)

    @unittest.skipUnless(os.name == 'nt', 'Real Windows incompatible COM apartment')
    def test_real_sta_request_is_rejected_by_mta_session(self):
        result = self.run_probe('incompatible')
        self.assertEqual(result, [{'status': 'rejected', 'hresult': -2147417850}])


if __name__ == '__main__':
    unittest.main()
