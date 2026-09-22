"""Windows 10 启动链路的可移植性回归测试。"""

import os
import sys
import unittest
from pathlib import Path


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pycheck


class PlatformCheckCase(unittest.TestCase):
    def test_windows_10_1809_x64_is_supported(self):
        self.assertEqual(pycheck.validate_platform('nt', (3, 10), 64, 17763), '')

    def test_old_windows_10_is_rejected(self):
        reason = pycheck.validate_platform('nt', (3, 12), 64, 17762)
        self.assertIn('17763', reason)

    def test_32_bit_python_is_rejected(self):
        self.assertIn('64 位', pycheck.validate_platform('nt', (3, 12), 32, 19045))

    def test_supported_python_range(self):
        for version in ((3, 10), (3, 11), (3, 12), (3, 13)):
            self.assertEqual(pycheck.validate_platform('nt', version, 64, 19045), '')
        self.assertTrue(pycheck.validate_platform('nt', (3, 9), 64, 19045))
        self.assertTrue(pycheck.validate_platform('nt', (3, 14), 64, 19045))

    def test_requirement_pins_are_exact(self):
        pins = pycheck._requirements(os.path.join(ROOT, 'requirements-win10.txt'))
        expected = {'PySide6-Essentials', 'shiboken6', 'opencv-python', 'numpy',
                    'Pillow', 'mcap', 'zstandard', 'lz4'}
        self.assertEqual(set(pins), expected)
        self.assertEqual(pins['numpy'], '2.2.6')
        self.assertEqual(pins['PySide6-Essentials'], pins['shiboken6'])


class LauncherTextCase(unittest.TestCase):
    def test_powershell_bootstrap_is_ascii_for_windows_powershell_51(self):
        raw = Path(ROOT, 'bootstrap.ps1').read_bytes()
        self.assertEqual(raw.decode('ascii').encode('ascii'), raw)

    def test_batch_keeps_delayed_expansion_disabled(self):
        path = os.path.join(ROOT, '启动查看器.bat')
        text = Path(path).read_text(encoding='utf-8').lower()
        self.assertIn('disabledelayedexpansion', text)
        self.assertNotIn('enabledelayedexpansion', text)

    def test_no_specific_username_is_hardcoded(self):
        files = ('启动查看器.bat', 'bootstrap.ps1', 'python.txt')
        for name in files:
            path = os.path.join(ROOT, name)
            if os.path.isfile(path):
                text = Path(path).read_text(encoding='utf-8-sig').lower()
                self.assertNotIn(r'c:\users\cruise', text, name)

    def test_runtime_is_ignored_as_generated_data(self):
        text = Path(ROOT, '.gitignore').read_text(encoding='utf-8')
        self.assertIn('runtime/', text)
        self.assertIn('cache/', text)


if __name__ == '__main__':
    unittest.main()
