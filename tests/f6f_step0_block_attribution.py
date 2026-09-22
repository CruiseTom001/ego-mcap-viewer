"""f6f_step0_block_attribution.py —— FIX005：GUI 阻塞归因（诊断专用，不改产品）

create-only（不删除任何文件）；唯一 run root；采集 gap>=50ms 样本：
每个样本记录 gap_ms / phase / operation / **阻塞结束时主线程栈** / thread；
同时包装 4 个热点函数计时（_tick / _update_clock / _on_direct_frame / set_image），
输出 P50/P95/P99/MAX 与 MAX 时的调用栈。

用法：python tests/f6f_step0_block_attribution.py --sample 216MB
"""

import argparse
import json
import os
import statistics
import sys
import threading
import time
import traceback
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(os.path.dirname(ROOT), 'tmp')
sys.path.insert(0, ROOT)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

RUN_ID = '%s_%s' % (time.strftime('%Y%m%d_%H%M%S'), uuid.uuid4().hex[:8])
RUN_ROOT = os.path.join(TMP, 'f6f_block_runs', RUN_ID)
os.makedirs(RUN_ROOT, exist_ok=True)
os.environ['MCAPVIEWER_CACHE'] = os.path.join(RUN_ROOT, 'cache_main')

from PySide6.QtCore import QTimer                       # noqa: E402
from PySide6.QtWidgets import QApplication              # noqa: E402

import appcache                                         # noqa: E402
import desktop as D                                     # noqa: E402

SAMPLES = {
    '216MB': (r'D:\wendang\xwechat_files\wxid_oz7zj4zmnwgz12_a0ce\msg\file\2026-09'
              r'\DAS-Ego_20260911154513_none_none_689985_371aafac.mcap', 'Real 216.4MB'),
    '2GB': (os.path.join(TMP, 'big_2gb.mcap'), 'Synthetic 2GB'),
}

HOT = ('_tick', '_update_clock', '_on_direct_frame')      # set_image 走 FrameView


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sample', default='216MB')
    ap.add_argument('--seconds', type=float, default=12.0)
    a = ap.parse_args()
    src, label = SAMPLES[a.sample]
    if not os.path.isfile(src):
        print('sample missing:', src)
        return 2

    app = QApplication.instance() or QApplication([])

    blocks = []
    phase = {'cur': 'IDLE'}
    op = {'cur': 'IDLE'}
    last = {'t': time.perf_counter()}
    T0 = time.perf_counter()

    # ---------------- 热点计时包装（仅诊断） ----------------
    hot = {k: [] for k in HOT}
    hot['set_image'] = []
    hot_max_stack = {}

    def wrap(cls, name, slot):
        orig = getattr(cls, name)

        def timed(self, *args, **kw):
            t0 = time.perf_counter()
            try:
                return orig(self, *args, **kw)
            finally:
                dt = (time.perf_counter() - t0) * 1000.0
                hot[slot].append(dt)
                if dt > hot_max_stack.get(slot, (0, ''))[0]:
                    hot_max_stack[slot] = (dt, traceback.format_stack(limit=8)[-3])
        setattr(cls, name, timed)
        return orig

    # ---------------- 心跳：gap>=50ms 抓栈 ----------------
    def on_tick():
        now = time.perf_counter()
        gap = (now - last['t']) * 1000.0
        last['t'] = now
        if gap >= 50.0:
            st = traceback.format_stack(limit=18)
            blocks.append(dict(
                t_rel=round(now - T0, 3), gap_ms=round(gap, 1),
                phase=phase['cur'], operation=op['cur'],
                thread=threading.current_thread().name,
                stack=[l.strip().replace('\n', ' ') for l in st[-14:]]))

    hb = QTimer()
    hb.setInterval(10)
    hb.timeout.connect(on_tick)
    hb.start()

    folder = os.path.join(RUN_ROOT, 'folder')
    os.makedirs(folder, exist_ok=True)
    import shutil
    dst = os.path.join(folder, os.path.basename(src))
    if not os.path.isfile(dst):
        shutil.copy2(src, dst)                  # 只创建
    sub = os.path.join(RUN_ROOT, 'cache_%s' % a.sample)
    os.makedirs(sub, exist_ok=True)
    appcache.CACHE_ROOT = sub

    # 热点计时（在窗口创建前包装类方法）
    orig_tick = wrap(D.MainWindow, '_tick', '_tick')
    orig_uc = wrap(D.MainWindow, '_update_clock', '_update_clock')
    orig_of = wrap(D.MainWindow, '_on_direct_frame', '_on_direct_frame')
    real_si = D.FrameView.set_image

    def si_spy(view_self, img):
        t0 = time.perf_counter()
        try:
            return real_si(view_self, img)
        finally:
            dt = (time.perf_counter() - t0) * 1000.0
            hot['set_image'].append(dt)

    D.FrameView.set_image = si_spy

    win = D.MainWindow()
    op['cur'] = 'LOAD_FOLDER'
    win.load_folder(folder)
    win.qm._paused = True
    sid = win.sids[0]
    for s in win.sids:
        if win.qm.items[s]['path'] == dst:
            sid = s
            break

    # ---------------- 复现序列：open → 播放 → seek → 切相机 ----------------
    phase['cur'] = 'OPEN'; op['cur'] = 'PLAY_SID'
    win.play_sid(sid, autoplay=True)
    t_end = time.perf_counter() + a.seconds
    while time.perf_counter() < t_end and not win.playing:
        app.processEvents(); time.sleep(0.003)
    phase['cur'] = 'NORMAL_PLAYBACK'; op['cur'] = 'PLAY'
    dur = win.duration or 0.0
    while time.perf_counter() < t_end:
        app.processEvents(); time.sleep(0.003)
    op['cur'] = 'SEEK'; phase['cur'] = 'SEEK'
    win.seek(dur * 0.5)
    t2 = time.perf_counter()
    while time.perf_counter() - t2 < 3:
        app.processEvents(); time.sleep(0.003)
    op['cur'] = 'CAMERA_SWITCH'; phase['cur'] = 'CAMERA_SWITCH'
    if win._direct_session is not None:
        win._direct_session.switch_camera('camera3', win.t)
    t3 = time.perf_counter()
    while time.perf_counter() - t3 < 3:
        app.processEvents(); time.sleep(0.003)
    op['cur'] = 'CLOSE'; phase['cur'] = 'CLOSE'
    win._close_direct_session()
    hb.stop()

    def stat(xs):
        if not xs:
            return None
        s = sorted(xs)
        return dict(n=len(s), p50=round(s[len(s) // 2], 2),
                    p95=round(s[int(len(s) * 0.95) - 1], 2),
                    p99=round(s[int(len(s) * 0.99) - 1], 2),
                    max=round(s[-1], 2))

    out = dict(run_id=RUN_ID, label=label, sample=a.sample,
               completed=True, blocks=blocks,
               hotspots={k: stat(v) for k, v in hot.items()},
               hotspot_max_stack={k: (round(v[0], 1), v[1])
                                  for k, v in hot_max_stack.items()})
    rp = os.path.join(RUN_ROOT, 'f6f_block_attribution.json')
    with open(rp, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)

    print('blocks(>=50ms):', len(blocks))
    for b in sorted(blocks, key=lambda x: -x['gap_ms'])[:5]:
        print('  %.1fms  phase=%s op=%s thread=%s' % (b['gap_ms'], b['phase'],
                                                      b['operation'], b['thread']))
        for l in b['stack'][-6:]:
            print('      |', l[:120])
    for k, v in out['hotspots'].items():
        print('  %-18s %s' % (k, v))
    print('RESULT_PATH=%s' % rp)
    return 0


if __name__ == '__main__':
    sys.exit(main())
