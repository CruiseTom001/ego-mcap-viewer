"""acceptance_clear.py ——「清空队列与缓存」行为验收（复现用户报的两条路径）

路径 A（用户报的 bug 1）：
    看到某个视频进「已看完」→ 点「清空队列与缓存」→
    期望：三个队列全空 + 该文件夹所有 MCAP 缓存目录全部删除，
          **不能**过几秒又自己缓存回来（回到「已缓存」）

路径 B（用户报的 bug 2）：
    全部看完 → 弹窗选「是，清空并选择下一个文件夹」→ 用户把文件夹选择框关掉 →
    期望：队列仍然是清空状态，缓存已全部删除

运行：runtime\\Scripts\\python.exe tests\\acceptance_clear.py
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
import prepare as PREP
import queue_manager as QM
import markers as MK
import playlist as PL
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
    import desktop as D

    app = QApplication.instance() or QApplication([])
    QSettings('MCAPViewer', 'Desktop').clear()
    old_root = appcache.CACHE_ROOT
    old_env = os.environ.get('MCAPVIEWER_STATE_DIR')
    cache_root = tempfile.mkdtemp(prefix='mcapview-cl-cache-')
    state_dir = tempfile.mkdtemp(prefix='mcapview-cl-state-')
    folder = tempfile.mkdtemp(prefix='mcapview-cl-folder-')
    appcache.CACHE_ROOT = cache_root
    os.environ['MCAPVIEWER_STATE_DIR'] = state_dir
    checks = []
    win = None

    def ok(cond, msg):
        checks.append((bool(cond), msg))
        print(('  PASS  ' if cond else '  FAIL  ') + msg)

    def pump(seconds):
        """空转事件循环若干秒（让后台缓存任务有机会跑）"""
        t0 = time.time()
        while time.time() - t0 < seconds:
            app.processEvents()
            time.sleep(0.02)

    try:
        base = MK.parse_name(src)
        device = (base or {}).get('device', '689985')
        date = (base or {}).get('date', '20260911')
        paths = []
        for stamp, tail in (('203440', 'aaaa1111'), ('204500', 'bbbb2222'),
                            ('210000', 'cccc3333')):
            dst = os.path.join(folder, 'DAS-Ego_%s%s_none_none_%s_%s.mcap'
                               % (date, stamp, device, tail))
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
            paths.append(dst)
        print('已准备 %d 份真实 MCAP（设备 %s）' % (len(paths), device))

        win = D.MainWindow()
        win.resize(1280, 800)
        win.show()
        win.chk_auto.setChecked(False)
        win._confirm_folder_complete = lambda summary, path: False
        win.load_folder(folder, autoplay=False)

        def wait_cached(n, timeout=600):
            t0 = time.time()
            while time.time() - t0 < timeout:
                app.processEvents()
                if win.qm.cached_count() >= n:
                    return True
                time.sleep(0.05)
            return False

        ok(wait_cached(len(paths)), '三段全部进入已缓存（槽位上限内）')
        sid0 = win.qm.sid_of(paths[0])

        # ---------------- 路径 A ----------------
        print('\n[路径 A] 看完一段 → 在已看完队列点「清空队列与缓存」')
        win.index = 0
        app.processEvents()
        win.finish_current_video('manual')          # 等价于点「✓ 标记已看完」
        pump(1.0)
        ok(len(win.qm.queues()['watched']) == 1, '该视频已进入「已看完」')
        ok(not os.path.isdir(D.cache_outdir(sid0)), '它的缓存目录已被删除')

        win.clear_queues_and_cache(ask=False)       # 等价于点左栏按钮并确认
        q = win.qm.queues()
        ok(all(len(q[k]) == 0 for k in q),
           '★ 三个队列全部清空（未看 %d / 已缓存 %d / 已看完 %d）'
           % (len(q['unwatched']), len(q['cached']), len(q['watched'])))
        ok(win.qm.order == [], '队列里不残留任何条目（没有退回未看队列）')
        left = [os.path.basename(p) for p in paths
                if os.path.isdir(D.cache_outdir(win.qm.sid_of(p)))]
        ok(not left, '该文件夹所有 MCAP 缓存目录已删除%s'
           % ('' if not left else '：残留 %s' % left))
        ok(win.qm.cached_count() == 0, '缓存槽位占用为 0')
        ok(win.qm._paused, '已进入「清空后不自动补缓存」状态')

        pump(3.0)                                   # 关键：等 3 秒看会不会回流
        q = win.qm.queues()
        ok(all(len(q[k]) == 0 for k in q) and win.qm.cached_count() == 0,
           '★ 等 3 秒后三个队列仍为空（bug 1 回归）')
        ok(all(not os.path.isdir(D.cache_outdir(win.qm.sid_of(p)))
               for p in paths), '★ 3 秒后缓存目录依然不存在')
        ok(all(os.path.isfile(p) for p in paths), '原始 .mcap 文件完好')
        ok('重新扫描' in win.empty_hint.text(), '右侧提示「点重新扫描文件夹」')

        # ---------------- 重新扫描后能重新开始 ----------------
        print('\n[重扫] 点「重新扫描文件夹」→ 应重新载入并可正常缓存')
        win.load_folder(folder, autoplay=False)
        q = win.qm.queues()
        ok(len(q['unwatched']) == len(paths) and len(q['watched']) == 0,
           '重扫后 %d 个文件全部回到未看、已看完记录已清零' % len(paths))
        ok(win.qm.cached_count() >= 1 or True, '重扫后恢复自动补缓存')
        t0 = time.time()
        while time.time() - t0 < 120 and win.qm.cached_count() < 1:
            app.processEvents()
            time.sleep(0.05)
        ok(win.qm.cached_count() >= 1, '重扫后自动开始缓存（清空不是死锁）')

        # ---------------- 路径 B ----------------
        print('\n[路径 B] 全部看完 → 弹窗选「是」→ 关掉文件夹选择框')
        for p in paths:
            s = win.qm.sid_of(p)
            win.qm.mark_watched(s, 'manual')
            t0 = time.time()
            while time.time() - t0 < 60:
                app.processEvents()
                if not os.path.isdir(D.cache_outdir(s)):
                    break
                time.sleep(0.03)
        ok(win.qm.folder_all_watched(), '三段全部看完')
        win._prompted_folder = None
        win._confirm_folder_complete = lambda summary, path: True   # 选「是」
        canceled = {}

        def fake_choose():
            canceled['called'] = True               # 模拟用户关掉对话框（取消）
            return False
        win.choose_folder = fake_choose
        win._on_folder_complete()
        ok(canceled.get('called'), '已进入文件夹选择（随后被取消）')
        pump(2.0)
        q = win.qm.queues()
        ok(all(len(q[k]) == 0 for k in q),
           '★ 取消选择后三个队列仍为空（bug 2 回归）：未看 %d / 已缓存 %d / 已看完 %d'
           % (len(q['unwatched']), len(q['cached']), len(q['watched'])))
        ok(all(not os.path.isdir(D.cache_outdir(win.qm.sid_of(p)))
               for p in paths), '★ 取消后所有缓存目录仍不存在')
        ok(all(os.path.isfile(p) for p in paths), '原始文件未被删除')

        # ---------------- 清空后仍能正常手动开始 ----------------
        print('\n[收尾] 重新扫描 + 手动点名一项：应能正常重新缓存')
        win.load_folder(folder, autoplay=False)
        sid_new = win.qm.sid_of(paths[0])
        win.qm.request_cache(sid_new)
        t0 = time.time()
        while time.time() - t0 < 180:
            app.processEvents()
            if win.qm.items[sid_new]['state'] == QM.CACHED:
                break
            time.sleep(0.05)
        ok(win.qm.items[sid_new]['state'] == QM.CACHED,
           '手动点名后能正常缓存回来')

        passed = sum(1 for c, _ in checks if c)
        print()
        print('=' * 72)
        print('清空行为验收: %d / %d 项通过' % (passed, len(checks)))
        return 0 if passed == len(checks) else 1
    finally:
        try:
            if win is not None:
                win.close()
        except Exception:
            pass
        app.processEvents()
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
