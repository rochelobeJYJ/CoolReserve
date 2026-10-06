"""Exercise the hidden Windows launcher against an isolated offline server."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell launcher')
class LauncherTests(unittest.TestCase):
    def test_hidden_launcher_starts_and_reuses_offline_service_in_path_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='cool launcher ') as temporary:
            directory = Path(temporary)
            session_path = directory / 'session.json'
            opener = build_opener(ProxyHandler({}))

            def request(route, body=None, *, session=None):
                session = session or json.loads(session_path.read_text(encoding='utf-8'))
                url = urlsplit(session['url'])
                self.assertEqual(url.scheme, 'http')
                self.assertEqual(url.hostname, '127.0.0.1')
                self.assertEqual(url.path, '/')
                self.assertFalse(url.query or url.username or url.password)
                self.assertTrue(url.fragment and url.port)
                data = None if body is None else json.dumps(body).encode()
                req = Request(f'http://127.0.0.1:{url.port}/api/{route}', data=data,
                              headers={'X-Cool-Token': url.fragment, 'Content-Type': 'application/json'})
                with opener.open(req, timeout=3) as response:
                    return json.load(response)

            args = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                    '-File', str(ROOT / 'tools' / 'launch.ps1'), '-DataDirectory', str(directory),
                    '-PythonPath', sys.executable, '-Offline', '-NoBrowser']
            try:
                first = subprocess.run(args, capture_output=True, timeout=30,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(first.returncode, 0, first.stderr.decode(errors='replace'))
                original = session_path.read_bytes()
                state = request('state')
                self.assertTrue(state['offline'])
                self.assertEqual(state['jobs'], [])
                self.assertEqual(Path(state['data_dir']).resolve(), directory.resolve())
                # The ready server must be reused before any child is spawned.
                # Guarding Start-Process makes the former delayed-child race a
                # deterministic failure without leaving an extra process alive.
                def quoted(value):
                    return "'" + str(value).replace("'", "''") + "'"
                guard_script = directory / 'reuse without new process.ps1'
                guard_script.write_text(
                    "function Start-Process { throw 'Synthetic guard: reuse must not start a process.' }\n"
                    + '& ' + quoted(ROOT / 'tools' / 'launch.ps1')
                    + ' -DataDirectory ' + quoted(directory)
                    + ' -PythonPath ' + quoted(sys.executable)
                    + ' -Offline -NoBrowser\nexit $LASTEXITCODE\n', encoding='utf-8-sig')
                second = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                         '-File', str(guard_script)], capture_output=True, timeout=30,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(second.returncode, 0, second.stderr.decode(errors='replace'))
                self.assertEqual(session_path.read_bytes(), original)
                self.assertNotIn(b'Traceback', first.stderr + second.stderr)

                # Normal START still requests the authenticated browser URL,
                # but the test records that request without opening any UI.
                browser_marker = directory / 'synthetic browser request.txt'
                guard_script.write_text(
                    'function Start-Process { param([string]$FilePath)\n'
                    + '  $fixtureExpected = (Get-Content -LiteralPath ' + quoted(session_path)
                    + ' -Raw -Encoding UTF8 | ConvertFrom-Json).url\n'
                    + "  if ($FilePath -ne $fixtureExpected) { throw 'Synthetic guard: unexpected process.' }\n"
                    + '  Add-Content -LiteralPath ' + quoted(browser_marker) + " -Value 'requested'\n}\n"
                    + '& ' + quoted(ROOT / 'tools' / 'launch.ps1')
                    + ' -DataDirectory ' + quoted(directory)
                    + ' -PythonPath ' + quoted(sys.executable)
                    + ' -Offline\nexit $LASTEXITCODE\n', encoding='utf-8-sig')
                browser = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                          '-File', str(guard_script)], capture_output=True, timeout=30,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(browser.returncode, 0, browser.stderr.decode(errors='replace'))
                self.assertEqual(browser_marker.read_text().splitlines(), ['requested'])

                guard_script.write_text(
                    "function Start-Process { param([string]$FilePath) throw ('Synthetic browser failure: ' + $FilePath) }\n"
                    + '& ' + quoted(ROOT / 'tools' / 'launch.ps1')
                    + ' -DataDirectory ' + quoted(directory)
                    + ' -PythonPath ' + quoted(sys.executable)
                    + ' -Offline\nexit $LASTEXITCODE\n', encoding='utf-8-sig')
                browser_failure = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                                  '-File', str(guard_script)], capture_output=True, timeout=30,
                                                 creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertNotEqual(browser_failure.returncode, 0)
                secret = urlsplit(json.loads(original)['url']).fragment.encode()
                self.assertNotIn(secret, browser_failure.stdout + browser_failure.stderr)
                self.assertEqual(session_path.read_bytes(), original)

                foreign = directory / 'different fixture data'
                foreign.mkdir()
                (foreign / 'session.json').write_bytes(original)
                for candidate, offline in ((directory, ''), (foreign, '-Offline')):
                    guard_script.write_text(
                        "function Start-Process { throw 'Synthetic guard: mismatched service was not reused.' }\n"
                        + '& ' + quoted(ROOT / 'tools' / 'launch.ps1')
                        + ' -DataDirectory ' + quoted(candidate)
                        + ' -PythonPath ' + quoted(sys.executable)
                        + ' ' + offline + ' -NoBrowser\nexit $LASTEXITCODE\n', encoding='utf-8-sig')
                    mismatch = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                               '-File', str(guard_script)], capture_output=True, timeout=30,
                                              creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertNotEqual(mismatch.returncode, 0)
                    self.assertNotIn(b'CoolReserve is ready.', mismatch.stdout)
                    self.assertEqual(session_path.read_bytes(), original)
            finally:
                if session_path.exists():
                    closing_session = json.loads(session_path.read_text(encoding='utf-8'))
                    closing_state = request('state', session=closing_session)
                    self.assertIs(closing_state['offline'], True)
                    self.assertEqual(Path(closing_state['data_dir']).resolve(), directory.resolve())
                    self.assertEqual(closing_state['jobs'], [])
                    self.assertEqual(json.loads(session_path.read_text(encoding='utf-8')), closing_session)
                    # Session removal precedes InstanceLock.close() in main().
                    # Wait on this exact verified server PID before tempfile
                    # cleanup, without terminating or signaling the process.
                    from win32api import OpenProcess
                    from win32con import SYNCHRONIZE
                    from win32event import WaitForSingleObject, WAIT_OBJECT_0
                    self.assertIs(type(closing_session['pid']), int)
                    self.assertGreater(closing_session['pid'], 0)
                    process = OpenProcess(SYNCHRONIZE, False, closing_session['pid'])
                    try:
                        self.assertEqual(request('shutdown', {}, session=closing_session), {'ok': True})
                        self.assertEqual(WaitForSingleObject(process, 8000), WAIT_OBJECT_0)
                    finally:
                        process.Close()
                    deadline = time.monotonic() + 8
                    while session_path.exists() and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertFalse(session_path.exists())
