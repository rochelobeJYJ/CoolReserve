"""Installer failures and environment probes, without messenger access."""
import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cool_install_probe', ROOT / 'tools/check_environment.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
VERSIONS = {'pywinauto': '0.6.9', 'comtypes': '1.4.17', 'pywin32': '312', 'six': '1.17.0'}


class EnvironmentProbeTests(unittest.TestCase):
    def check(self, *, platform='win32', version=(3,14,0), maxsize=2**63-1, versions=None):
        def distribution(name):
            if name not in (VERSIONS if versions is None else versions):
                raise importlib.metadata.PackageNotFoundError(name)
            return (VERSIONS if versions is None else versions)[name]
        with patch.object(probe.sys, 'platform', platform), patch.object(probe.sys, 'version_info', version), \
                patch.object(probe.sys, 'maxsize', maxsize), patch.object(probe.importlib.metadata, 'version', side_effect=distribution):
            return probe.check()

    def test_supported_64_bit_versions_are_ready_without_messenger_operation(self):
        for version in ((3,13,0), (3,14,0)):
            self.assertEqual(self.check(version=version), {'ready': True, 'problems': [], 'messenger_operated': False})

    def test_unsupported_python_and_32_bit_have_install_requirement(self):
        for config in ({'version': (3,12,9)}, {'version': (3,15,0)}, {'maxsize': 2**31-1}):
            result = self.check(**config)
            self.assertFalse(result['ready'])
            self.assertIn('64-bit Python 3.13 or 3.14 is required.', result['problems'])

    def test_missing_and_wrong_package_versions_request_install_again(self):
        result = self.check(versions={'six': '0.0'})
        self.assertFalse(result['ready'])
        self.assertEqual(len(result['problems']), 4)
        self.assertTrue(all('INSTALL.cmd' in item for item in result['problems']))
        self.assertFalse(result['messenger_operated'])

    def test_non_windows_environment_is_not_marked_ready(self):
        result = self.check(platform='linux')
        self.assertFalse(result['ready'])
        self.assertIn('Windows is required', result['problems'][0])


@unittest.skipUnless(os.name == 'nt', 'Windows installer entry points')
class InstallerFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='CoolReserve install fixture ')
        self.folder = Path(self.tmp.name) / 'package with spaces'
        (self.folder / 'tools').mkdir(parents=True)
        shutil.copy2(ROOT / 'tools/install.ps1', self.folder / 'tools/install.ps1')
        shutil.copy2(ROOT / 'INSTALL.cmd', self.folder / 'INSTALL.cmd')

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, *args):
        return subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                               str(self.folder / 'tools/install.ps1'), *args],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15,
                              creationflags=subprocess.CREATE_NO_WINDOW, cwd=self.folder)

    def test_check_only_missing_environment_fails_without_creating_it(self):
        result = self.run_install('-CheckOnly')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No local environment. Run INSTALL.cmd first.', result.stderr)
        self.assertFalse((self.folder / '.venv').exists())

    def test_missing_explicit_python_path_provides_supported_version_guidance(self):
        result = self.run_install('-PythonPath', str(self.folder / 'missing Python/python.exe'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Install 64-bit Python 3.13 or 3.14', result.stderr)
        self.assertFalse((self.folder / '.venv').exists())

    def test_rejected_python_probe_stops_before_creating_environment(self):
        rejected = self.folder / 'unsupported Python.cmd'
        rejected.write_text('@echo off\nexit /b 23\n', encoding='ascii')
        result = self.run_install('-PythonPath', str(rejected))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Install 64-bit Python 3.13 or 3.14', result.stderr)
        self.assertFalse((self.folder / '.venv').exists())

    def test_cmd_propagates_failed_install_and_does_not_report_success(self):
        # Replace only this fixture's installer; no venv or network is used.
        (self.folder / 'tools/install.ps1').write_text('Write-Output "synthetic install failure"\nexit 19\n', encoding='ascii')
        result = subprocess.run(['cmd.exe', '/d', '/c', str(self.folder / 'INSTALL.cmd')],
                                input='\n', capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=15, creationflags=subprocess.CREATE_NO_WINDOW, cwd=self.folder)
        self.assertEqual(result.returncode, 19)
        self.assertIn('설치하지 못했습니다', result.stdout)
        self.assertNotIn('설치가 끝났습니다', result.stdout)

    def test_start_cmd_preserves_launcher_failure_without_starting_a_server(self):
        shutil.copy2(ROOT / 'START.cmd', self.folder / 'START.cmd')
        (self.folder / '.venv/Scripts').mkdir(parents=True)
        (self.folder / '.venv/Scripts/python.exe').write_bytes(b'not executed')
        (self.folder / 'tools/launch.ps1').write_text('Write-Output "synthetic launcher failure"\nexit 17\n', encoding='ascii')
        result = subprocess.run(['cmd.exe', '/d', '/c', str(self.folder / 'START.cmd')],
                                input='\n', capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=15, creationflags=subprocess.CREATE_NO_WINDOW, cwd=self.folder)
        self.assertEqual(result.returncode, 17)
        self.assertIn('시작하지 못했습니다', result.stdout)

    def test_start_and_test_missing_environment_remain_failed_with_install_guidance(self):
        for filename in ('START.cmd', 'TEST.cmd'):
            shutil.copy2(ROOT / filename, self.folder / filename)
            result = subprocess.run(['cmd.exe', '/d', '/c', str(self.folder / filename)],
                                    input='\n', capture_output=True, text=True, encoding='utf-8', errors='replace',
                                    timeout=15, creationflags=subprocess.CREATE_NO_WINDOW, cwd=self.folder)
            with self.subTest(filename=filename):
                self.assertEqual(result.returncode, 1)
                self.assertIn('INSTALL.cmd' if filename == 'START.cmd' else '설치', result.stdout)


if __name__ == '__main__':
    unittest.main()
