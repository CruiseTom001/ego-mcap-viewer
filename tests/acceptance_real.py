"""acceptance_real.py —— 真实 MCAP 三队列验收（规范十二.10/12 的自动化部分）

用法：runtime\\Scripts\\python.exe tests\\acceptance_real.py [真实.mcap 路径]
不带参数时自动探测 D:\\视频查看软件\\*.mcap。

流程：装载（单文件文件夹）→ 等缓存 → 记录缓存大小 → 标记已看完 →
     验证缓存删除 + 原始文件哈希不变 → 重新缓存 → 等缓存 → 再次标记。
全程只读原始文件；结束后输出缓存释放字节。
"""

import os
import sys
import time
import shutil
import tempfile
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import appcache
import queue_manager as QM
from tests import mcapfix as fx

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings


def find_real_mcap():
    d = os.path.dirname(ROOT)
    cands = sorted(glob.glob(os.path.join(d, '*.mcap')),
                   key=os.path.getsize, reverse=True)
    return cands[0] if cands else None


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else find_real_mcap()
    if not src or not os.path.isfile(src):
        print('没有找到真实 MCAP 文件')
        return 2
    print('真实文件: %s (%.2f GB)' % (src, os.path.getsize(src) / 1e9))

    app = QApplication.instance() or QApplication([])
    QSettings('MCAPViewer', 'Desktop').clear()
    old_root = appcache.CACHE_ROOT
    old_env = os.environ.get('MCAPVIEWER_STATE_DIR')
    cache_root = tempfile.mkdtemp(prefix='mcapview-real-cache-')
    state_dir = tempfile.mkdtemp(prefix='mcapview-real-state-')
    folder = tempfile.mkdtemp(prefix='mcapview-real-folder-')
    appcache.CACHE_ROOT = cache_root
    os.environ['MCAPVIEWER_STATE_DIR'] = state_dir
    checks = []

    def ok(cond, msg):
        checks.append((bool(cond), msg))
        print(('  PASS  ' if cond else '  FAIL  ') + msg)

    try:
        link = os.path.join(folder, os.path.basename(src))
        # 复制太大（2.7GB）；用硬链接（同盘）保持"原始文件不被改动"的可校验性
        try:
            os.link(src, link)
            print('已用硬链接挂入测试文件夹')
        except OSError:
            shutil.copy2(src, link)
            print('已复制进测试文件夹')
        before_hash = fx.file_sha256(link)

        qm = QM.CacheQueueManager()
        qm.load_folder(folder, [link])

        def pump(pred, timeout=900, what=''):
            t0 = time.time()
            while time.time() - t0 < timeout:
                app.processEvents()
                if pred():
                    return True
                time.sleep(0.05)
            print('  !! 超时: %s' % what)
            return False

        sid = qm.sid_of(link)
        t0 = time.time()
        ok(pump(lambda: qm.items[sid]['state'] == QM.CACHED, 900, '真实文件缓存'),
           '真实文件完成桌面精简缓存（首次解析封装）')
        print('  首次缓存耗时 %.1f 秒' % (time.time() - t0))
        cache_bytes = appcache.cache_size(sid)
        print('  缓存大小: %.1f MB（源 %.2f GB，膨胀 %.2f×）'
              % (cache_bytes / 1048576.0, os.path.getsize(link) / 1e9,
                 cache_bytes / max(1, os.path.getsize(link))))

        qm.mark_watched(sid, 'natural_end')
        ok(pump(lambda: not os.path.isdir(
            appcache.cache_dir(appcache.cache_key(sid))), 120, '缓存删除'),
           '看完后桌面缓存已删除')
        freed = appcache.cache_size(sid)
        print('  删除后缓存目录残留字节: %d' % freed)
        ok(freed == 0, '缓存目录彻底消失（0 字节残留）')
        after_hash = fx.file_sha256(link)
        ok(after_hash == before_hash, '原始 MCAP SHA256 前后一致')
        qm.flush()

        # 重新缓存 → 再次可播
        qm.request_recache(sid)
        ok(pump(lambda: qm.items[sid]['state'] == QM.CACHED, 900, '重新缓存'),
           '重新缓存后回到已缓存')
        qm.mark_watched(sid, 'manual')
        ok(pump(lambda: not os.path.isdir(
            appcache.cache_dir(appcache.cache_key(sid))), 120, '再次删除'),
           '二次看完后缓存再次删除')
        qm.flush()
        qm.shutdown()
        if qm.worker is not None and qm.worker.isRunning():
            qm.worker.wait(15000)

        passed = sum(1 for c, _ in checks if c)
        print('=' * 60)
        print('真实 MCAP 验收: %d / %d 项通过；缓存释放 %.1f MB'
              % (passed, len(checks), cache_bytes / 1048576.0))
        return 0 if passed == len(checks) else 1
    finally:
        appcache.CACHE_ROOT = old_root
        if old_env is None:
            os.environ.pop('MCAPVIEWER_STATE_DIR', None)
        else:
            os.environ['MCAPVIEWER_STATE_DIR'] = old_env
        QSettings('MCAPViewer', 'Desktop').clear()
        shutil.rmtree(cache_root, ignore_errors=True)
        shutil.rmtree(state_dir, ignore_errors=True)
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
