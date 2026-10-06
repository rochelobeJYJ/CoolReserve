import os
import sys
import unittest
from pathlib import Path
from app.windows import executable, profile_errors

@unittest.skipUnless(sys.platform == 'win32', 'Windows only')
class WindowsProcessTests(unittest.TestCase):
    def test_current_process_path(self):
        found = executable(os.getpid())
        self.assertTrue(Path(found).is_file())
        self.assertEqual(Path(found).name.casefold(), Path(sys.executable).name.casefold())

    def test_invalid_process_fails_closed(self):
        with self.assertRaises(Exception):
            executable(4294967295)

class IncompleteProfileTests(unittest.TestCase):
    def test_missing_profile_cannot_register(self):
        errors = profile_errors({'roles': {}, 'contacts': {}}, '본인 시험')
        self.assertGreaterEqual(len(errors), 7)

if __name__ == '__main__':
    unittest.main()
