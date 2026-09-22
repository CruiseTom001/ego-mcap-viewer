"""启动前的 Python / Windows / 运行时完整性检查。

本文件刻意只使用 Python 3.10 可用的语法。候选基础 Python 只做平台检查；
已经安装好的 ``runtime`` 还会检查锁定版本、Qt 平台插件和桌面模块导入。
"""

import ctypes
import importlib
import importlib.metadata
import os
import struct
import sys


MIN_VERSION = (3, 10)
MAX_VERSION = (3, 13)
MIN_WINDOWS_BUILD = 17763          # Windows 10 1809
STARTUP_LOG = 'startup-error.log'
CANDIDATE_LOG = 'python-candidates.log'


def _root():
    return os.path.dirname(os.path.abspath(__file__))


def _append_log(name, message):
    try:
        with open(os.path.join(_root(), name), 'a', encoding='utf-8') as fh:
            fh.write(message.rstrip() + '\n')
    except Exception:
        pass


def windows_build():
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        return 0


def validate_platform(os_name=None, version=None, bits=None, build=None,
                      platform=None):
    """返回错误文本；空字符串表示平台可用。参数可注入，便于单元测试。

    支持 Windows 10 1809+（x64）与 macOS（Intel / Apple Silicon，64 位 Python）。
    """
    os_name = os.name if os_name is None else os_name
    platform = sys.platform if platform is None else platform
    version = sys.version_info[:2] if version is None else tuple(version[:2])
    bits = struct.calcsize('P') * 8 if bits is None else int(bits)
    build = windows_build() if build is None else int(build)
    if os_name == 'nt' or platform.startswith('win'):
        if bits != 64:
            return '检测到 %d 位 Python，本程序需要 64 位 Python' % bits
        if version < MIN_VERSION:
            return 'Python %d.%d 过低，需要 3.10 ~ 3.13' % version
        if version > MAX_VERSION:
            return 'Python %d.%d 尚未验证，需要 3.10 ~ 3.13' % version
        if build and build < MIN_WINDOWS_BUILD:
            return ('Windows 内部版本 %d 过低，需要 Windows 10 1809 '
                    '（内部版本 %d）或更高' % (build, MIN_WINDOWS_BUILD))
        return ''
    if platform == 'darwin':
        if bits != 64:
            return '检测到 %d 位 Python，本程序需要 64 位 Python' % bits
        if version < MIN_VERSION:
            return 'Python %d.%d 过低，需要 3.10 ~ 3.13' % version
        if version > MAX_VERSION:
            return 'Python %d.%d 尚未验证，需要 3.10 ~ 3.13' % version
        return ''
    return '不支持的系统：%s（仅支持 Windows 10 1809+ 与 macOS）' % (platform or os_name)


def _requirements(path):
    """读取简单的 ``name==version`` 锁定清单，包含 -r 引用。"""
    result = {}
    path = os.path.abspath(path)
    with open(path, encoding='utf-8-sig') as fh:
        for raw in fh:
            line = raw.split('#', 1)[0].strip()
            if not line:
                continue
            if line.startswith('-r '):
                child = line[3:].strip()
                result.update(_requirements(os.path.join(os.path.dirname(path), child)))
            elif '==' in line:
                name, wanted = line.split('==', 1)
                result[name.strip()] = wanted.strip()
    return result


def validate_runtime(requirements_path=None):
    """返回运行时问题列表。这里的失败意味着 runtime 应被重新构建。"""
    errors = []
    if requirements_path is None:
        requirements_path = os.path.join(
            _root(), 'requirements-macos.txt' if sys.platform == 'darwin'
            else 'requirements-win10.txt')
    try:
        pins = _requirements(requirements_path)
    except Exception as exc:
        return ['无法读取依赖锁定文件：%s' % exc]

    for name, wanted in pins.items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            errors.append('缺少依赖 %s==%s' % (name, wanted))
            continue
        except Exception as exc:
            errors.append('无法读取依赖 %s 的版本：%s' % (name, exc))
            continue
        if actual != wanted:
            errors.append('%s 版本不匹配：当前 %s，需要 %s' % (name, actual, wanted))

    for module in ('zstandard', 'lz4', 'mcap', 'numpy', 'PIL', 'cv2',
                   'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets'):
        try:
            importlib.import_module(module)
        except Exception as exc:
            errors.append('导入 %s 失败：%s' % (module, exc))

    if not errors:
        try:
            from PySide6.QtCore import QLibraryInfo
            plugins = QLibraryInfo.path(QLibraryInfo.PluginsPath)
            if sys.platform == 'darwin':
                qplugin = os.path.join(plugins, 'platforms', 'libqcocoa.dylib')
                if not os.path.isfile(qplugin):
                    errors.append('Qt macOS 平台插件不存在：%s' % qplugin)
            elif os.name == 'nt':
                qwindows = os.path.join(plugins, 'platforms', 'qwindows.dll')
                if not os.path.isfile(qwindows):
                    errors.append('Qt Windows 平台插件不存在：%s' % qwindows)
        except Exception as exc:
            errors.append('检查 Qt 平台插件失败：%s' % exc)

    if not errors:
        try:
            import appcache
            cache = appcache.ensure_cache_root()
            probe = os.path.join(cache, '.startup-probe-%d' % os.getpid())
            with open(probe, 'wb') as fh:
                fh.write(b'ok')
            os.remove(probe)
        except Exception as exc:
            errors.append('缓存目录不可写：%s' % exc)

    # winmm/MCI 是 Windows 专用可选功能。加载失败只记录警告，不阻止启动。
    if os.name == 'nt':
        try:
            ctypes.WinDLL('winmm.dll')
        except Exception as exc:
            _append_log(STARTUP_LOG, '警告：音频接口 winmm.dll 不可用：%s' % exc)

    if not errors:
        try:
            importlib.import_module('desktop')
        except Exception as exc:
            errors.append('导入 desktop.py 失败：%s' % exc)
    return errors


def main(argv):
    want_runtime = '--runtime' in argv[1:]
    want_log = '--log' in argv[1:]
    requirements_path = None
    if '--requirements' in argv:
        try:
            requirements_path = argv[argv.index('--requirements') + 1]
        except IndexError:
            print('NG --requirements 后缺少文件路径')
            return 2

    reason = validate_platform()
    exe = sys.executable or '(未知)'
    if reason:
        line = 'NG %s -> %s' % (exe, reason)
        print(line)
        if want_log:
            _append_log(CANDIDATE_LOG, line)
        return 3

    if want_runtime:
        errors = validate_runtime(requirements_path)
        if errors:
            print('NG runtime')
            for error in errors:
                print('  - ' + error)
                if want_log:
                    _append_log(STARTUP_LOG, error)
            return 7

    version = sys.version_info
    plat = ('Windows-build-%d' % windows_build()) if os.name == 'nt' \
        else sys.platform
    print('OK %s Python %d.%d.%d 64bit %s%s' % (
        exe, version[0], version[1], version[2], plat,
        ' runtime' if want_runtime else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
