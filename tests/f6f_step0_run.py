"""f6f_step0_run.py —— P1.6F-F Step0 FIX003：no-delete 基线（create-only）

原则（用户合同）：
  * **绝不删除任何东西**（无 rmtree / remove / unlink）；
  * 每次运行使用**唯一 run root**：tmp/f6f_step0_runs/<时间戳>_<uuid>/
  * 结果写到 run root 内，并打印 RESULT_PATH=<绝对路径>；含 run_id / completed
  * 不设置 MCAPVIEWER_STATE_DIR（用程序默认行为，且不清理 state）

用法：python tests/f6f_step0_run.py --sample 216MB
     --sample 可选 41MB / 216MB / 2GB / all
"""

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(os.path.dirname(ROOT), 'tmp')
sys.path.insert(0, ROOT)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

RUN_ID = '%s_%s' % (time.strftime('%Y%m%d_%H%M%S'), uuid.uuid4().hex[:8])
RUN_ROOT = os.path.join(TMP, 'f6f_step0_runs', RUN_ID)
os.makedirs(RUN_ROOT, exist_ok=True)          # create-only（不删旧 run）
os.environ['MCAPVIEWER_CACHE'] = os.path.join(RUN_ROOT, 'cache_main')

from PySide6.QtCore import QEventLoop, QTimer           # noqa: E402
from PySide6.QtWidgets import QApplication              # noqa: E402

import appcache                                         # noqa: E402
import desktop as D                                     # noqa: E402

SAMPLES = {
    '41MB': (r'D:\视频查看软件\DAS-Ego_20260911203440_none_none_689985_65416a8a.mcap',
             'Real 41.8MB', ['ttfp', 'rate1x', 'cycles']),
    '216MB': (r'D:\wendang\xwechat_files\wxid_oz7zj4zmnwgz12_a0ce\msg\file\2026-09'
              r'\DAS-Ego_20260911154513_none_none_689985_371aafac.mcap',
              'Real 216.4MB', ['ttfp', 'rate1x', 'rate2x', 'seek', 'camera', 'cycles']),
    '2GB': (os.path.join(TMP, 'big_2gb.mcap'), 'Synthetic 2GB',
            ['ttfp', 'rate1x', 'rate2x', 'seek', 'camera', 'cycles']),
}


def rss_mb():
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1048576.0
    except Exception:
        return -1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sample', default='216MB')
    a = ap.parse_args()
    keys = list(SAMPLES) if a.sample == 'all' else [a.sample]
    app = QApplication.instance() or QApplication([])

    # ---- 产品热点计时（诊断）：产品侧 GUI 阻塞的判定依据 ----
    hot = {'_tick': [], '_update_clock': [], '_on_direct_frame': [], 'set_image': []}

    def _wrap(cls, name, slot):
        orig = getattr(cls, name)

        def timed(self, *a, **k):
            t = time.perf_counter()
            try:
                return orig(self, *a, **k)
            finally:
                hot[slot].append((time.perf_counter() - t) * 1000.0)
        setattr(cls, name, timed)

    _wrap(D.MainWindow, '_tick', '_tick')
    _wrap(D.MainWindow, '_update_clock', '_update_clock')
    _wrap(D.MainWindow, '_on_direct_frame', '_on_direct_frame')

    calls = []
    real_set_image = D.FrameView.set_image
    def _si(s, img):
        t = time.perf_counter()
        try:
            return real_set_image(s, img)
        finally:
            hot['set_image'].append((time.perf_counter() - t) * 1000.0)
            calls.append(time.perf_counter())

    patcher = mock.patch.object(D.FrameView, 'set_image', _si)
    patcher.start()

    folder = os.path.join(RUN_ROOT, 'folder')
    os.makedirs(folder, exist_ok=True)
    out = {'run_id': RUN_ID, 'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
           'samples': [], 'completed': False}

    for key in keys:
        src, label, items = SAMPLES[key]
        if not os.path.isfile(src):
            out['samples'].append(dict(key=key, label=label, error='missing'))
            continue
        import shutil
        dst = os.path.join(folder, os.path.basename(src))
        if not os.path.isfile(dst):
            shutil.copy2(src, dst)              # 只创建，不删除
        sub = os.path.join(RUN_ROOT, 'cache_%s' % key)
        os.makedirs(sub, exist_ok=True)
        appcache.CACHE_ROOT = sub
        rec = dict(key=key, label=label, synthetic=(key == '2GB'),
                   size_mb=round(os.path.getsize(src) / 1048576, 1),
                   rss_start_mb=round(rss_mb(), 1))

        win = D.MainWindow()
        win.load_folder(folder)
        win.qm._paused = True                   # 仅 harness 隔离 prewarm

        # ---- 主线程 gap 采样（5ms 心跳）+ 阶段标签 ----
        stamps = []
        stage = {'cur': 'IDLE'}
        hb = QTimer()
        hb.setInterval(5)
        hb.timeout.connect(lambda: stamps.append((time.perf_counter(), stage['cur'])))
        hb.start()

        sid = None
        for s in win.sids:
            if win.qm.items[s]['path'] == dst:
                sid = s
                break
        sid = sid or win.sids[0]

        # ---- TTFP ----
        stage['cur'] = 'OPEN'
        calls.clear()
        t0 = time.perf_counter()
        win.play_sid(sid, autoplay=True)
        while time.perf_counter() - t0 < 90 and not calls:
            app.processEvents(QEventLoop.AllEvents, 5)
        rec['ttfp_ms'] = round(getattr(win, '_direct_ttpf_ms', None) or -1, 1)
        rec['backend'] = win._playback_backend
        rec['clock_valid'] = bool(win.clock.isValid())
        rec['anchor_after_open'] = getattr(win, '_direct_anchor_count', -1)
        rec['duration_s'] = round(win.duration or 0.0, 2)
        stage['cur'] = 'NORMAL_PLAYBACK'
        dur = win.duration or 0.0

        def measure(speed, seconds, tag):
            for i in range(win.cmb_speed.count()):
                if abs(float(win.cmb_speed.itemData(i) or 1.0) - speed) < 1e-9:
                    win.cmb_speed.setCurrentIndex(i)
                    break
            win._speed_changed()
            # 从 5% 处开始，保证远离末尾
            win.seek(min(dur * 0.05, max(0.0, dur - seconds * speed - 5)))
            win.play()
            time.sleep(0.05)
            app.processEvents()
            m0, w0 = win.t, time.perf_counter()
            while time.perf_counter() - w0 < seconds:
                app.processEvents(QEventLoop.AllEvents, 5)
            w1, m1 = time.perf_counter(), win.t
            win.pause()
            return (round((m1 - m0) / (w1 - w0), 3) if (w1 - w0) > 0 else None,
                    round(m1 - m0, 2), round(w1 - w0, 2), m0, m1)

        if 'rate1x' in items:
            stage['cur'] = 'NORMAL_PLAYBACK'
            r1 = measure(1.0, 10.0 if key != '41MB' else 5.0, '1x')
            rec['rate_1x'], rec['media_1x'], rec['wall_1x'] = r1[0], r1[1], r1[2]
        if 'rate2x' in items:
            r2 = measure(2.0, 10.0, '2x')
            rec['rate_2x'], rec['media_2x'], rec['wall_2x'] = r2[0], r2[1], r2[2]

        rec['anchor_after_playback'] = getattr(win, '_direct_anchor_count', -1)
        rec['clock_valid_after'] = bool(win.clock.isValid())
        rec['negative_t'] = bool(win.t < 0)

        # ---- Seek 10/50/90% ----
        if 'seek' in items:
            stage['cur'] = 'SEEK'
            lat = []
            for frac in (0.10, 0.50, 0.90):
                n0 = len(calls)
                t = time.perf_counter()
                win.seek(dur * frac)
                while time.perf_counter() - t < 15 and len(calls) <= n0:
                    app.processEvents(QEventLoop.AllEvents, 5)
                lat.append((time.perf_counter() - t) * 1000.0)
            rec['seek_p50_ms'] = round(statistics.median(lat), 1)
            rec['seek_p95_ms'] = round(sorted(lat)[-1], 1)
            rec['anchor_after_seek'] = getattr(win, '_direct_anchor_count', -1)

        # ---- Camera ×10 ----
        if 'camera' in items:
            stage['cur'] = 'CAMERA_SWITCH'
            sw = []
            for k in range(10):
                cam = 'camera3' if k % 2 == 0 else 'camera2'
                n0 = len(calls)
                t = time.perf_counter()
                if win._direct_session is not None:
                    win._direct_session.switch_camera(cam, win.t)
                while time.perf_counter() - t < 15 and len(calls) <= n0:
                    app.processEvents(QEventLoop.AllEvents, 5)
                sw.append((time.perf_counter() - t) * 1000.0)
            rec['camera_p50_ms'] = round(statistics.median(sw), 1)
            rec['camera_p95_ms'] = round(sorted(sw)[-1], 1)
            rec['anchor_after_camera'] = getattr(win, '_direct_anchor_count', -1)

        # ---- 20×（或 8×）open → first frame → close ----
        if 'cycles' in items:
            stage['cur'] = 'OPEN'
            n_cyc = 20 if key == '41MB' else 8
            ok = 0
            rss_marks = []
            for k in range(n_cyc):
                calls.clear()
                win._close_direct_session()
                win.play_sid(sid, autoplay=True)
                t = time.perf_counter()
                while time.perf_counter() - t < 40 and not calls:
                    app.processEvents(QEventLoop.AllEvents, 5)
                if calls:
                    ok += 1
                win._close_direct_session()
                if k in (0, min(9, n_cyc - 1), n_cyc - 1):
                    rss_marks.append(round(rss_mb(), 1))
            rec['open_close_ok'] = ok
            rec['open_close_cycles'] = n_cyc
            rec['rss_marks_mb'] = rss_marks
            stage['cur'] = 'CLOSE'

        # ---- gap 统计（含 max 事件近似：取该 gap 结束时的阶段标签）----
        hb.stop()
        gaps = []
        for i in range(len(stamps) - 1):
            gaps.append(((stamps[i + 1][0] - stamps[i][0]) * 1000.0, stamps[i + 1][1]))
        if gaps:
            ms = sorted(g[0] for g in gaps)
            rec['gap_p50_ms'] = round(ms[len(ms) // 2], 1)
            rec['gap_p95_ms'] = round(ms[int(len(ms) * 0.95) - 1], 1)
            rec['gap_p99_ms'] = round(ms[int(len(ms) * 0.99) - 1], 1)
            mx = max(gaps, key=lambda g: g[0])
            rec['gap_max_ms'] = round(mx[0], 1)
            rec['gap_max_event'] = mx[1]
        rec['slider_value'] = win.slider.value()
        rec['slider_max'] = win.slider.maximum()
        rec['rss_end_mb'] = round(rss_mb(), 1)
        win.close()
        out['samples'].append(rec)
        print('%-15s TTFP=%7.1f 1x=%-6s 2x=%-6s seekP95=%-7s camP95=%-7s '
              'gap P95=%-6s P99=%-6s MAX=%-8s(%s) cycles=%s/%s anchors=%s'
              % (label, rec['ttfp_ms'], rec.get('rate_1x'), rec.get('rate_2x'),
                 rec.get('seek_p95_ms'), rec.get('camera_p95_ms'),
                 rec.get('gap_p95_ms'), rec.get('gap_p99_ms'), rec.get('gap_max_ms'),
                 rec.get('gap_max_event'), rec.get('open_close_ok'),
                 rec.get('open_close_cycles'), rec.get('anchor_after_playback')))

    def _stat(xs):
        if not xs:
            return None
        s = sorted(xs)
        return dict(n=len(s), p50=round(s[len(s) // 2], 2),
                    p95=round(s[int(len(s) * 0.95) - 1], 2),
                    p99=round(s[int(len(s) * 0.99) - 1], 2),
                    max=round(s[-1], 2))

    out['hotspots'] = {k: _stat(v) for k, v in hot.items()}
    patcher.stop()
    out['completed'] = True
    out['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    out['run_root'] = RUN_ROOT
    rp = os.path.join(RUN_ROOT, 'f6f_step0_result.json')
    with open(rp, 'w', encoding='utf-8') as fh:          # create-only
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print('RESULT_PATH=%s' % rp)
    return 0


if __name__ == '__main__':
    sys.exit(main())
