"""f6f_step0_desktop_baseline.py —— P1.6F-F Step 0：Desktop 真实路径验收基线

全部走真实 Desktop 路径（MainWindow → DirectPlaybackSession → worker → set_image）：
  A 216MB Desktop TTFP
  B GUI 主线程 stall（打开 / seek / 切相机）
  C Desktop 1x effective
  D Desktop 2x effective
  E 稳定性：20× open→首帧→close；camera2↔camera3 ×10
输出 JSON 到 tmp/f6f_step0.json。不启动打包 EXE，仅源码离屏。
"""

import json
import os
import shutil
import statistics
import sys
import time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(os.path.dirname(ROOT), 'tmp')
sys.path.insert(0, ROOT)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['MCAPVIEWER_CACHE'] = os.path.join(TMP, 'f6f_cache')
os.environ['MCAPVIEWER_STATE_DIR'] = os.path.join(TMP, 'f6f_state')

from PySide6.QtCore import QTimer                       # noqa: E402
from PySide6.QtWidgets import QApplication              # noqa: E402

import appcache                                         # noqa: E402
import desktop as D                                     # noqa: E402

SAMPLES = [
    ('Real 216MB',
     r'D:\wendang\xwechat_files\wxid_oz7zj4zmnwgz12_a0ce\msg\file\2026-09'
     r'\DAS-Ego_20260911154513_none_none_689985_371aafac.mcap', True),
    ('Synthetic 2GB', os.path.join(TMP, 'big_2gb.mcap'), True),
    ('Real 41.8MB',
     r'D:\视频查看软件\DAS-Ego_20260911203440_none_none_689985_65416a8a.mcap', False),
]


def rss_mb():
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1048576.0
    except Exception:
        return -1.0


def main():
    app = QApplication.instance() or QApplication([])
    real_set_image = D.FrameView.set_image
    calls = []
    patcher = mock.patch.object(
        D.FrameView, 'set_image',
        lambda s, img: (calls.append((time.perf_counter(), getattr(img, 'shape', None))), real_set_image(s, img))[1])
    patcher.start()

    folder = os.path.join(TMP, 'f6f_folder')
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder, exist_ok=True)
    out = {'samples': []}

    for label, src, full in SAMPLES:
        if not os.path.isfile(src):
            out['samples'].append(dict(label=label, error='missing'))
            continue
        dst = os.path.join(folder, os.path.basename(src))
        if not os.path.isfile(dst):
            shutil.copy2(src, dst)
        shutil.rmtree(appcache.CACHE_ROOT, ignore_errors=True)
        rec = dict(label=label, size_mb=round(os.path.getsize(src) / 1048576, 1))
        win = D.MainWindow()
        win.load_folder(folder)
        win.qm._paused = True
        sid = [s for s in win.sids
               if win.qm.items[s]['path'] == dst]
        sid = sid[0] if sid else win.sids[0]

        # ---- 主线程 stall 心跳 ----
        stamps = []
        hb = QTimer()
        hb.setInterval(5)
        hb.timeout.connect(lambda: stamps.append(time.perf_counter()))
        hb.start()

        # ---- A. TTFP（打开 → set_image）----
        calls.clear()
        t0 = time.perf_counter()
        win.play_sid(sid, autoplay=True)
        while time.perf_counter() - t0 < 90 and not calls:
            app.processEvents()
            time.sleep(0.003)
        rec['ttfp_ms'] = round(getattr(win, '_direct_ttpf_ms', None) or -1, 1)
        rec['backend'] = win._playback_backend
        rec['set_image_calls_at_ttfp'] = len(calls)

        # ---- B. seek 50% 的 stall ----
        dur = win.duration or 0.0
        rec['duration_s'] = round(dur, 2)
        marks = len(stamps)
        win.seek(dur * 0.5)
        n_before = len(calls)
        t1 = time.perf_counter()
        while time.perf_counter() - t1 < 20 and len(calls) <= n_before:
            app.processEvents()
            time.sleep(0.003)
        rec['seek50_ms'] = round((time.perf_counter() - t1) * 1000.0, 1)
        if dur > 1:
            del marks
        # ---- 稳定性 E1：camera 切换 ×10（经 session 通道）----
        sw_lat = []
        for k in range(10):
            cam = 'camera3' if k % 2 == 0 else 'camera2'
            n0 = len(calls)
            t2 = time.perf_counter()
            if win._direct_session is not None:
                win._direct_session.switch_camera(cam, win.t)
            while time.perf_counter() - t2 < 10 and len(calls) <= n0:
                app.processEvents()
                time.sleep(0.003)
            sw_lat.append((time.perf_counter() - t2) * 1000.0)
        rec['camera_switch_p50_ms'] = round(statistics.median(sw_lat), 1)
        rec['camera_switch_p95_ms'] = round(sorted(sw_lat)[-1], 1)

        # ---- C/D. 1x / 2x effective ----
        def measure(speed, seconds):
            for i in range(win.cmb_speed.count()):
                if abs(float(win.cmb_speed.itemData(i) or 1.0) - speed) < 1e-9:
                    win.cmb_speed.setCurrentIndex(i)
                    break
            win._speed_changed()
            win.seek(min(1.0, max(0.0, dur * 0.05)))
            win.play()
            t = time.perf_counter()
            m0 = win.t
            while time.perf_counter() - t < seconds:
                app.processEvents()
                time.sleep(0.003)
            wall = time.perf_counter() - t
            media = win.t - m0
            win.pause()
            return round(media / wall, 3) if wall > 0 else None

        rec['rate_1x'] = measure(1.0, 10.0)
        rec['rate_2x'] = measure(2.0, 10.0)

        # ---- B(续). stall 统计 ----
        hb.stop()
        gaps = [(stamps[i + 1] - stamps[i]) * 1000.0 for i in range(len(stamps) - 1)]
        rec['max_gui_stall_ms'] = round(max(gaps), 1) if gaps else None
        rec['p95_gui_gap_ms'] = round(sorted(gaps)[int(len(gaps) * 0.95) - 1], 1) if gaps else None
        rec['rss_mb'] = round(rss_mb(), 1)

        # ---- E2. 20× open → 首帧 → close（仅小样本做 20 轮）----
        if label != 'Real 41.8MB':
            cycles = 8 if full else 20
        else:
            cycles = 20
        t3 = time.perf_counter()
        ok = 0
        for k in range(cycles):
            calls.clear()
            win._close_direct_session()
            win.play_sid(sid, autoplay=True)
            tt = time.perf_counter()
            while time.perf_counter() - tt < 30 and not calls:
                app.processEvents()
                time.sleep(0.003)
            if calls:
                ok += 1
            win._close_direct_session()
        rec['open_close_cycles'] = cycles
        rec['open_close_ok'] = ok
        rec['open_close_total_s'] = round(time.perf_counter() - t3, 1)
        rec['rss_after_cycles_mb'] = round(rss_mb(), 1)
        win.close()
        out['samples'].append(rec)
        print('%s: TTFP=%s ms  backend=%s  stall_max=%s ms  1x=%s  2x=%s  cam_p50=%s ms  cycles %d/%d'
              % (label, rec['ttfp_ms'], rec['backend'], rec['max_gui_stall_ms'],
                 rec['rate_1x'], rec['rate_2x'], rec['camera_switch_p50_ms'],
                 rec['open_close_ok'], rec['open_close_cycles']))

    patcher.stop()
    p = os.path.join(TMP, 'f6f_step0.json')
    with open(p, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print('汇总已写入', p)
    return 0


if __name__ == '__main__':
    sys.exit(main())
