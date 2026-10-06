"""Real local-service startup tests; no browser or messenger UI is launched."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.processes = []
        self.creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

    def tearDown(self):
        for process, directory in self.processes:
            if process.poll() is None:
                try:
                    self.stop_server(process, directory)
                except Exception:
                    process.kill()
                    process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        self.tmp.cleanup()

    def cli(self, directory, *args):
        return subprocess.run(
            [sys.executable, '-B', '-X', 'utf8', str(ROOT / 'server.py'),
             '--data-dir', str(directory), *args],
            capture_output=True, text=True, encoding='utf-8', timeout=10,
            creationflags=self.creation_flags,
        )

    def request(self, session, path, body=None):
        parsed = urlsplit(session['url'])
        headers = {'X-Cool-Token': parsed.fragment}
        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(f'http://127.0.0.1:{parsed.port}{path}', data, headers)
        with urllib.request.urlopen(req, timeout=2) as response:
            return json.loads(response.read())

    def start_server(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [sys.executable, '-B', '-X', 'utf8', str(ROOT / 'server.py'),
             '--data-dir', str(directory), '--port', '0'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', creationflags=self.creation_flags,
        )
        self.processes.append((process, directory))
        deadline = time.monotonic() + 10
        session_path = directory / 'session.json'
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail('Service exited during startup: ' + process.stderr.read())
            try:
                session = json.loads(session_path.read_text(encoding='utf-8'))
                # On Windows the venv executable may launch the real Python
                # worker, so Popen.pid need not equal os.getpid() in session.
                if isinstance(session.get('pid'), int) and session['pid'] > 0:
                    state = self.request(session, '/api/state')
                    self.assertEqual(state['jobs'], [])
                    self.assertEqual(Path(state['data_dir']).resolve(), directory.resolve())
                    return process, session
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(.02)
        self.fail('Service did not publish a healthy authenticated session')

    def stop_server(self, process, directory):
        session = json.loads((directory / 'session.json').read_text(encoding='utf-8'))
        self.assertEqual(self.request(session, '/api/shutdown', {}), {'ok': True})
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0, process.stderr.read())
        self.assertFalse((directory / 'session.json').exists())

    def test_duplicate_cli_and_open_reuse_existing_session(self):
        directory = self.directory / 'existing'
        process, session = self.start_server(directory)
        session_path = directory / 'session.json'
        before = session_path.read_bytes()
        second = self.cli(directory)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn('Traceback', second.stderr)
        self.assertIn('already running', second.stdout)
        self.assertEqual(session_path.read_bytes(), before)

        code = '''
import json, runpy, sys
from unittest.mock import patch
script, directory = sys.argv[1:]
sys.path.insert(0, str(__import__('pathlib').Path(script).parent))
sys.argv = [script, '--data-dir', directory, '--open']
with patch('webbrowser.open', side_effect=lambda url: print('OPENED ' + json.dumps(url))):
    runpy.run_path(script, run_name='__main__')
'''
        opened = subprocess.run(
            [sys.executable, '-B', '-X', 'utf8', '-c', code,
             str(ROOT / 'server.py'), str(directory)],
            capture_output=True, text=True, encoding='utf-8', timeout=10,
            creationflags=self.creation_flags,
        )
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertNotIn('Traceback', opened.stderr)
        urls = [json.loads(line[len('OPENED '):]) for line in opened.stdout.splitlines()
                if line.startswith('OPENED ')]
        self.assertEqual(urls, [session['url']])
        self.assertEqual(session_path.read_bytes(), before)
        self.assertIsNone(process.poll())

    def test_shutdown_and_failed_start_release_resources_for_restart(self):
        directory = self.directory / 'restart'
        first, original = self.start_server(directory)
        self.stop_server(first, directory)
        # A bind error happens after taking the instance lock. It must release
        # that lock and remove the obsolete session before another startup.
        (directory / 'session.json').write_text(json.dumps(original), encoding='utf-8')
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            failed = self.cli(directory, '--port', str(occupied.getsockname()[1]))
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse((directory / 'session.json').exists())
        second, fresh = self.start_server(directory)
        self.assertGreater(fresh['pid'], 0)
        self.assertNotEqual(fresh['pid'], original['pid'])
        self.assertEqual(server.running_session_url(directory, wait_seconds=.2), fresh['url'])
        self.stop_server(second, directory)

    def test_session_lookup_retries_missing_and_partial_json(self):
        lookup = self.directory / 'healthy'
        process, session = self.start_server(lookup)
        path = lookup / 'session.json'
        path.unlink()
        errors = []

        def publish_later():
            try:
                time.sleep(.08)
                path.write_text('{"url":', encoding='utf-8')
                time.sleep(.08)
                server.publish_session(path, session)
            except Exception as error:
                errors.append(error)

        writer = threading.Thread(target=publish_later)
        writer.start()
        try:
            self.assertEqual(server.running_session_url(lookup, wait_seconds=.8), session['url'])
        finally:
            writer.join(2)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])

    def test_session_lookup_rejects_foreign_stale_and_unauthenticated_urls(self):
        process, session = self.start_server(self.directory / 'healthy')
        lookup = self.directory / 'invalid-session'
        lookup.mkdir()
        path = lookup / 'session.json'
        foreign_urls = ('https://example.invalid/#token', 'http://example.invalid/#token',
                        'http://user:password@127.0.0.1:1234/#token',
                        'http://127.0.0.1:1234/other#token')
        with patch('socket.create_connection', side_effect=AssertionError('foreign URL was probed')) as connect:
            for url in foreign_urls:
                with self.subTest(url=url):
                    path.write_text(json.dumps({'url': url, 'pid': process.pid}), encoding='utf-8')
                    self.assertIsNone(server.running_session_url(lookup, wait_seconds=.01))
            connect.assert_not_called()
        for malformed in ([], {'url': None}, {'url': 123}, {'url': 'http://127.0.0.1:invalid/#token'}):
            with self.subTest(malformed=malformed):
                path.write_text(json.dumps(malformed), encoding='utf-8')
                self.assertIsNone(server.running_session_url(lookup, wait_seconds=.02))
        path.write_text(json.dumps(session), encoding='utf-8')
        # A healthy service in another data directory is not this instance.
        self.assertIsNone(server.running_session_url(lookup, wait_seconds=.02))
        parsed = urlsplit(session['url'])
        path.write_text(json.dumps({'url': f'http://127.0.0.1:{parsed.port}/#wrong-token',
                                    'pid': process.pid}), encoding='utf-8')
        self.assertIsNone(server.running_session_url(lookup, wait_seconds=.02))
        self.stop_server(process, self.directory / 'healthy')
        path.write_text(json.dumps(session), encoding='utf-8')
        self.assertIsNone(server.running_session_url(lookup, wait_seconds=.02))

    def test_session_publication_is_atomic_and_cleans_failed_temporary_file(self):
        directory = self.directory / 'publication'
        directory.mkdir()
        path = directory / 'session.json'
        old = {'url': 'old', 'pid': 1}
        new = {'url': 'new', 'pid': 2}
        path.write_text(json.dumps(old), encoding='utf-8')
        replace = os.replace

        def replace_after_inspection(source, target):
            self.assertEqual(Path(source).parent, path.parent)
            self.assertEqual(Path(target), path)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), old)
            self.assertEqual(json.loads(Path(source).read_text(encoding='utf-8')), new)
            return replace(source, target)

        with patch.object(server.os, 'replace', side_effect=replace_after_inspection) as mocked:
            server.publish_session(path, new)
        mocked.assert_called_once()
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), new)
        self.assertEqual(list(directory.iterdir()), [path])
        with patch.object(server.os, 'replace', side_effect=OSError('simulated replace error')):
            with self.assertRaises(OSError):
                server.publish_session(path, old)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), new)
        self.assertEqual(list(directory.iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
