"""跨平台支持测试（Windows / macOS 分支）

Windows 机器上也能完整跑：macOS 分支全部走参数注入，不依赖 darwin 运行时。
"""

import os
import sys
import unittest

import appcache
import pycheck


class PlatformCheckCase(unittest.TestCase):
    def test_windows_10_1809_x64_passes(self):
        self.assertEqual(pycheck.validate_platform(
            os_name='nt', version=(3, 12), bits=64, build=17763), '')

    def test_windows_old_build_rejected(self):
        self.assertTrue(pycheck.validate_platform(
            os_name='nt', version=(3, 12), bits=64, build=10240))

    def test_macos_intel_or_arm64_passes(self):
        for ver in ((3, 10), (3, 11), (3, 12), (3, 13)):
            self.assertEqual(pycheck.validate_platform(
                os_name='posix', platform='darwin', version=ver, bits=64),
                '', 'macOS + Python %s 应放行' % (ver,))

    def test_macos_rejects_old_python_and_32bit(self):
        self.assertTrue(pycheck.validate_platform(
            os_name='posix', platform='darwin', version=(3, 9), bits=64))
        self.assertTrue(pycheck.validate_platform(
            os_name='posix', platform='darwin', version=(3, 12), bits=32))

    def test_unsupported_platform_rejected(self):
        msg = pycheck.validate_platform(
            os_name='posix', platform='linux', version=(3, 12), bits=64)
        self.assertIn('不支持的系统', msg)

    def test_requirements_file_selected_by_platform(self):
        # macOS 用 requirements-macos.txt（默认参数不注入，仅断言文件存在）
        root = os.path.dirname(os.path.abspath(pycheck.__file__))
        for name in ('requirements-macos.txt', 'requirements-macos-legacy.txt'):
            self.assertTrue(os.path.isfile(os.path.join(root, name)), name)


class UserDataDirCase(unittest.TestCase):
    def test_macos_path(self):
        got = appcache.user_data_dir(platform='darwin', home='/Users/bob')
        # 断言与分隔符无关：Windows 上跑测试时 os.path.join 会用反斜杠
        self.assertTrue(got.startswith('/Users/bob'))
        self.assertTrue(got.endswith(os.path.join('Library', 'Application Support',
                                                  'MCAPViewer')))

    def test_windows_path_uses_localappdata(self):
        env = {'LOCALAPPDATA': r'C:\Users\bob\AppData\Local'}
        got = appcache.user_data_dir(platform='win32', home=r'C:\Users\bob',
                                     environ=env)
        self.assertEqual(got, os.path.join(env['LOCALAPPDATA'], 'MCAPViewer'))

    def test_windows_path_falls_back_to_home(self):
        got = appcache.user_data_dir(platform='win32', home=r'C:\Users\bob',
                                     environ={})
        self.assertTrue(got.endswith(os.path.join('AppData', 'Local', 'MCAPViewer')))

    def test_linux_path(self):
        got = appcache.user_data_dir(platform='linux', home='/home/bob',
                                     environ={})
        self.assertTrue(got.startswith('/home/bob'))
        self.assertTrue(got.endswith(os.path.join('.local', 'share', 'MCAPViewer')))

    def test_real_platform_matches_running_os(self):
        got = appcache.user_data_dir()
        if sys.platform == 'darwin':
            self.assertIn('Library/Application Support', got)
        elif os.name == 'nt':
            self.assertTrue(got.lower().endswith('mcapviewer'))
        self.assertTrue(got)


class AudioBackendCase(unittest.TestCase):
    def test_factory_returns_platform_backend(self):
        import desktop as D
        a = D.make_audio()
        if sys.platform == 'darwin':
            self.assertIsInstance(a, D.QtAudio)
        else:
            self.assertIsInstance(a, D.MciAudio)
        for attr in ('ok', 'speed_supported', 'load', 'play_from', 'pause',
                     'resume', 'stop', 'set_volume', 'set_speed', 'close'):
            self.assertTrue(hasattr(a, attr), '音频后端缺少接口：%s' % attr)

    def test_qt_audio_degrades_gracefully(self):
        # Windows 上 QtMultimedia 不可用时也必须不抛异常、ok=False（自动静音）
        import desktop as D
        a = D.QtAudio()
        self.assertIn(a.ok, (True, False))
        a.pause(); a.resume(); a.stop(); a.set_volume(0.5)
        self.assertFalse(a.play_from(0) and not a.ok)
        a.close()

    def test_mci_audio_degrades_gracefully(self):
        import desktop as D
        a = D.MciAudio()
        a.pause(); a.resume(); a.stop(); a.set_volume(0.5)
        a.close()
        self.assertIn(a.ok, (True, False))


class CacheLockCase(unittest.TestCase):
    def test_lock_file_created_and_reentrant_after_release(self):
        root = appcache.CACHE_ROOT
        with appcache.exclusive_cache_lock(timeout=1.0):
            self.assertTrue(os.path.isfile(os.path.join(root, '.cache.lock')))
        # 释放后可以再次获取
        with appcache.exclusive_cache_lock(timeout=1.0):
            pass


class MacScriptsCase(unittest.TestCase):
    """macOS 脚本必须能在默认 bash 3.2 下执行：LF 行尾、无 BOM、语法可解析"""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _scripts(self):
        return ['bootstrap.sh', '启动查看器.command', 'build_mac.sh']

    def _read(self, name, mode='r'):
        path = os.path.join(self.ROOT, name)
        if 'b' in mode:
            with open(path, mode) as fh:
                return fh.read()
        with open(path, mode, encoding='utf-8') as fh:
            return fh.read()

    def test_scripts_are_lf_without_bom(self):
        for name in self._scripts():
            raw = self._read(name, 'rb')
            self.assertFalse(raw.startswith(b'\xef\xbb\xbf'),
                             '%s 不能有 UTF-8 BOM（macOS bash 会报 bad interpreter）' % name)
            self.assertEqual(raw.count(b'\r\n'), 0,
                             '%s 必须用 LF 行尾（CRLF 在 macOS 上无法执行）' % name)

    def test_mac_requirements_exist_and_pin_versions(self):
        for name in ('requirements-macos.txt', 'requirements-macos-legacy.txt'):
            text = self._read(name)
            for pkg in ('PySide6', 'shiboken6', 'opencv-python', 'numpy',
                        'Pillow', 'mcap', 'zstandard', 'lz4'):
                self.assertIn('\n%s==' % pkg, '\n' + text,
                              '%s 缺少 %s 的锁定版本' % (name, pkg))
        # macOS 音频后端需要 QtMultimedia → 必须是完整 PySide6（不能锁 Essentials 包）
        main = self._read('requirements-macos.txt')
        for line in main.splitlines():
            self.assertFalse(line.strip().startswith('PySide6-Essentials=='),
                             'macOS 清单不能锁 PySide6-Essentials（缺 QtMultimedia）')

    def test_bootstrap_mentions_core_safety_behaviours(self):
        text = self._read('bootstrap.sh')
        for token in ('runtime.building-', 'cleanup_build_dir', '--check-only',
                      'sw_vers', 'requirements-macos'):
            self.assertIn(token, text, 'bootstrap.sh 缺少关键逻辑：%s' % token)

    def test_build_script_bundles_multimedia(self):
        text = self._read('build_mac.sh')
        self.assertIn('PySide6.QtMultimedia', text,
                      '打包必须显式带上 QtMultimedia（macOS 音频后端）')


if __name__ == '__main__':
    unittest.main()
