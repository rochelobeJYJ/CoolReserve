"""Cross-process instance-lock regressions using only isolated temporary files."""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from server import InstanceLock

def report(status, **detail):
    print(json.dumps(dict(status=status, **detail)), flush=True)

if sys.argv[3] == 'wait':
    report('READY')
    sys.stdin.readline()
try:
    lock = InstanceLock(Path(sys.argv[2]))
except RuntimeError as error:
    report('ALREADY_RUNNING', message=str(error))
except Exception as error:
    report('ERROR', kind=type(error).__name__, message=str(error))
    sys.exit(2)
else:
    report('ACQUIRED')
    try:
        sys.stdin.readline()
    finally:
        lock.close()
'''


@unittest.skipUnless(os.name == 'nt', 'Windows byte-range locking regression')
class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.helpers = []

    def tearDown(self):
        for process, messages in self.helpers:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        self.tmp.cleanup()

    def start_helper(self, path, wait=False):
        process = subprocess.Popen(
            [sys.executable, '-B', '-X', 'utf8', '-c', CHILD, str(ROOT), str(path),
             'wait' if wait else 'immediate'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW,
        )
        messages = queue.Queue()

        def read_messages():
            for line in process.stdout:
                messages.put(json.loads(line))

        threading.Thread(target=read_messages, daemon=True).start()
        helper = (process, messages)
        self.helpers.append(helper)
        return helper

    def status(self, helper):
        process, messages = helper
        try:
            return messages.get(timeout=15)
        except queue.Empty:
            error = process.stderr.read() if process.poll() is not None else 'helper did not respond'
            self.fail(error)

    def release(self, helper):
        process, messages = helper
        process.stdin.close()
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0, process.stderr.read())

    def test_second_process_reports_already_running(self):
        path = self.directory / 'instance.lock'
        path.write_bytes(b'0')  # Existing installations already have this byte.
        first = self.start_helper(path)
        self.assertEqual(self.status(first)['status'], 'ACQUIRED')
        second = self.start_helper(path)
        result = self.status(second)
        self.assertEqual(result['status'], 'ALREADY_RUNNING', result)
        self.release(second)
        self.release(first)

    def test_lock_can_be_reacquired_after_first_process_closes(self):
        path = self.directory / 'instance.lock'
        first = self.start_helper(path)
        self.assertEqual(self.status(first)['status'], 'ACQUIRED')
        self.release(first)
        second = self.start_helper(path)
        self.assertEqual(self.status(second)['status'], 'ACQUIRED')
        self.release(second)

    def test_concurrent_processes_compete_for_an_empty_file(self):
        path = self.directory / 'instance.lock'
        path.touch()
        helpers = [self.start_helper(path, wait=True), self.start_helper(path, wait=True)]
        for helper in helpers:
            self.assertEqual(self.status(helper)['status'], 'READY')
        for process, messages in helpers:
            process.stdin.write('go\n')
            process.stdin.flush()
        results = [self.status(helper) for helper in helpers]
        self.assertCountEqual([result['status'] for result in results],
                              ['ACQUIRED', 'ALREADY_RUNNING'], results)
        for helper in helpers:
            self.release(helper)

    def test_separate_data_directories_have_independent_locks(self):
        paths = []
        for name in ('first-data', 'second-data'):
            directory = self.directory / name
            directory.mkdir()
            paths.append(directory / 'instance.lock')
        first, second = [self.start_helper(path) for path in paths]
        self.assertEqual(self.status(first)['status'], 'ACQUIRED')
        self.assertEqual(self.status(second)['status'], 'ACQUIRED')
        self.release(first)
        self.release(second)

    def test_new_lock_file_can_remain_empty(self):
        path = self.directory / 'instance.lock'
        helper = self.start_helper(path)
        self.assertEqual(self.status(helper)['status'], 'ACQUIRED')
        self.assertEqual(path.stat().st_size, 0)
        self.release(helper)


if __name__ == '__main__':
    unittest.main()
