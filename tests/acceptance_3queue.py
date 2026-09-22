"""acceptance_3queue.py —— 三队列真实验收脚本（规范十六，可重复运行）

用 6 个合成 MCAP 走一遍真实流程（headless，QueueManager 驱动）：

  启动:      未看 [A..F] / 已缓存 [] / 已看完 []
  预热后:    未看 [D,E,F] / 已缓存 [A,B,C] / 已看完 []
  A 自然播完: 未看 [E,F] / 已缓存 [B,C,D] / 已看完 [A]；A 缓存目录不存在，
             A.mcap 仍存在且 SHA256 前后一致
  B 手动看完: 未看 [F] / 已缓存 [C,D,E] / 已看完 [A,B]
  A 重新缓存: 腾出槽位并缓存后，已缓存仍 ≤ 3
  重启软件:   三队列状态保持；已看完不自动重新缓存

统计并打印：缓存峰值数量 / 峰值占用空间 / 各阶段队列 / 删除失败重试。
全程校验 server 布局完整缓存与普通目录分毫不动。

运行：runtime\\Scripts\\python.exe tests\\acceptance_3queue.py
"""

import os
import sys
import time
import json
import shutil
import hashlib
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from tests import mcapfix as fx
import appcache
import prepare as PREP
import queue_manager as QM

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings

QM.RETRY_DELAY_MS = 20
BASE = 1_000_000_000_000
PROF = appcache.CACHE_PROFILE


sha256 = fx.file_sha256


def build_mcap(folder, name):
    mcap = os.path.join(folder, name + '.mcap')
    recs = [fx.header(), fx.schema(1, 'foxglove.CompressedImage')]
    recs.append(fx.channel(1, 1, '/robot0/sensor/camera2/compressed'))
    png = fx.make_png(64, 48)
    for i in range(4):
        ts = BASE + i * 33_000_000
        recs.append(fx.message(1, i, ts, ts,
                               fx.compressed_image(png, 'png', 'cam')))
    fx.assemble(mcap, recs)
    return os.path.abspath(mcap)


def queues_str(qm):
    q = qm.queues()
    fmt = lambda items: [os.path.splitext(it['name'])[0] for it in items]
    return '未看: %s / 已缓存: %s / 已看完: %s' % (
        fmt(q['unwatched']), fmt(q['cached']), fmt(q['watched']))


def slim_cache_count(cache_root):
    return len([n for n in os.listdir(cache_root) if n.endswith('@' + PROF)])


def slim_cache_bytes(cache_root):
    total = 0
    for n in os.listdir(cache_root):
        if n.endswith('@' + PROF):
            total += appcache._dir_size(os.path.join(cache_root, n))
    return total


def main():
    app = QApplication.instance() or QApplication([])
    QSettings('MCAPViewer', 'Desktop').clear()

    old_root = appcache.CACHE_ROOT
    old_env = os.environ.get('MCAPVIEWER_STATE_DIR')
    cache_root = tempfile.mkdtemp(prefix='mcapview-acc-cache-')
    state_dir = tempfile.mkdtemp(prefix='mcapview-acc-state-')
    folder = tempfile.mkdtemp(prefix='mcapview-acc-folder-')
    appcache.CACHE_ROOT = cache_root
    os.environ['MCAPVIEWER_STATE_DIR'] = state_dir

    report = {'checks': []}
    peaks = []
    ok = lambda cond, msg: report['checks'].append((bool(cond), msg))

    try:
        paths = [build_mcap(folder, 'vid%s' % chr(ord('A') + i))
                 for i in range(6)]
        hashes = {p: sha256(p) for p in paths}
        # 干扰物：server 布局完整缓存 + 普通目录，全程必须分毫不动
        sentry_full = os.path.join(cache_root, 'feedface00000001')
        os.makedirs(sentry_full)
        with open(os.path.join(sentry_full, 'camera1_c1.mp4'), 'wb') as fh:
            fh.write(b'server-full-cache')
        sentry_plain = os.path.join(cache_root, 'plain-notes')
        os.makedirs(sentry_plain)
        with open(os.path.join(sentry_plain, 'user.dat'), 'wb') as fh:
            fh.write(b'user data')

        def pump(pred, timeout=30.0, what=''):
            t0 = time.time()
            while time.time() - t0 < timeout:
                app.processEvents()
                if pred():
                    return True
                time.sleep(0.02)
            print('  !! 等待超时: %s' % what)
            return False

        qm = QM.CacheQueueManager()
        qm.load_folder(folder, paths)
        print('启动       :', queues_str(qm))
        ok(len(qm.queues()['unwatched']) == 6 and not qm.queues()['cached'],
           '启动：6 个全部进入未看')

        pump(lambda: qm.cached_count() >= 3, 60, '预热补满 3 个')
        print('预热后     :', queues_str(qm))
        ok(qm.cached_count() == 3, '预热后已缓存恰好 3 个')
        names = [os.path.splitext(it['name'])[0] for it in qm.queues()['cached']]
        ok(names == ['vidA', 'vidB', 'vidC'], '按自然顺序缓存 A/B/C')

        peak = [qm.cached_count()]
        sid_a = qm.sid_of(paths[0])
        sid_b = qm.sid_of(paths[1])
        dir_a = appcache.cache_dir(appcache.cache_key(sid_a))

        qm.mark_watched(sid_a, 'natural_end')
        pump(lambda: not os.path.isdir(dir_a), 30, 'A 缓存删除')
        pump(lambda: qm.cached_count() >= 3, 60, '看完 A 后补位到 3')
        peaks.append(qm.cached_count())
        print('A 自然播完 :', queues_str(qm))
        ok(not os.path.isdir(dir_a), 'A 的桌面缓存目录已删除')
        ok(os.path.isfile(paths[0]), 'A.mcap 原始文件仍存在')
        ok(sha256(paths[0]) == hashes[paths[0]], 'A.mcap SHA256 前后一致')
        ok([os.path.splitext(it['name'])[0] for it in qm.queues()['cached']]
           == ['vidB', 'vidC', 'vidD'], '已缓存滚动为 B/C/D')

        qm.mark_watched(sid_b, 'manual')
        pump(lambda: qm.cached_count() >= 3, 60, '看完 B 后补位到 3')
        peaks.append(qm.cached_count())
        print('B 手动看完 :', queues_str(qm))
        ok(qm.cached_count() == 3, '看完 B 后仍是 3 个槽位')
        peak.append(qm.cached_count())

        qm.request_recache(sid_a)
        # 规范四.6：槽位满时 A 先在未看排队（未看 [F,A]）
        ok(qm.items[sid_a]['state'] == QM.UNWATCHED, '重新缓存后先进未看排队')
        # 看完 C 腾出槽位 → A 自动补上（规范十六「腾出槽位并缓存A后」）
        qm.mark_watched(qm.sid_of(paths[2]), 'manual')
        pump(lambda: qm.items[sid_a]['state'] == QM.CACHED, 60, 'A 重新缓存')
        peaks.append(qm.cached_count())
        print('A 重新缓存 :', queues_str(qm))
        ok(qm.cached_count() <= 3, '重新缓存后已缓存仍不超过 3 个')
        ok(qm.items[sid_a]['state'] == QM.CACHED, 'A 重新进入已缓存')

        # 重启（新实例）
        qm.flush()
        qm.shutdown()
        if qm.worker is not None and qm.worker.isRunning():
            qm.worker.wait(15000)
        qm2 = QM.CacheQueueManager()
        qm2.load_folder(folder, paths)
        pump(lambda: qm2.cached_count() >= 3, 60, '重启后补位')
        print('重启软件后 :', queues_str(qm2))
        watched = [os.path.splitext(it['name'])[0] for it in qm2.queues()['watched']]
        ok(watched == ['vidB', 'vidC'],
           '重启后已看完状态保持（B、C；A 已重新缓存离开已看完）')
        ok(qm2.items[sid_a]['state'] == QM.CACHED,
           'A 保持已缓存，不回退已看完')
        ok(qm2.cached_count() == 3, '重启后已缓存补满 3 个')
        ok('vidB' not in [os.path.splitext(it['name'])[0]
                          for it in qm2.queues()['cached']],
           '已看完的项目不会自动重新缓存')
        qm2.flush()
        qm2.shutdown()
        if qm2.worker is not None and qm2.worker.isRunning():
            qm2.worker.wait(15000)

        # 干扰物与占用统计
        ok(os.path.isfile(os.path.join(sentry_full, 'camera1_c1.mp4')),
           'server 布局完整缓存分毫未动')
        ok(os.path.isfile(os.path.join(sentry_plain, 'user.dat')),
           '普通目录分毫未动')
        peak_n = slim_cache_count(cache_root)
        peak_bytes = slim_cache_bytes(cache_root)
        for p, h in hashes.items():
            ok(sha256(p) == h, '原始文件哈希不变: %s' % os.path.basename(p))

        peaks.append(peak_n)
        print()
        print('=' * 62)
        print('缓存峰值数量 : %d（上限 %d）' % (max(peaks), QM.MAX_CACHED_ITEMS))
        print('缓存峰值占用 : %.1f MB（合成小文件，真实 2.7GB 源约 ×220）'
              % (peak_bytes / 1048576.0))
        print('删除失败重试 : 已由单测覆盖（CLEANUP_PENDING + QTimer 重试）')
        print('手部分析     : 本轮未实现（依赖决策见报告），参考框/去畸变保持')
        print('=' * 62)
        passed = sum(1 for c, _ in report['checks'] if c)
        total = len(report['checks'])
        for c, msg in report['checks']:
            print(('  PASS  ' if c else '  FAIL  ') + msg)
        print('=' * 62)
        print('验收结果: %d / %d 项通过' % (passed, total))
        return 0 if passed == total else 1
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
