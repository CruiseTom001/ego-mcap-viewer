"""p16e_playback_acceptance.py —— P1.6E-R4 播放倍率 / Render FPS / 漂移自动测量

用真实 MCAP 的 v3 缓存跑 1x/2x/4x/8x 全程播放，记录：
  wall time、实际倍率、render FPS、dropped、max_drift、catch-up 次数
以及 pause/resume、seek、变速、播放结束只触发一次。

离屏运行（QT_QPA_PLATFORM=offscreen），结果写 tmp/p16e_playback.json。
"""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(os.path.dirname(ROOT), 'tmp')
sys.path.insert(0, ROOT)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('MCAPVIEWER_CACHE', os.path.join(TMP, 'p16e_pb_cache'))
os.environ.setdefault('MCAPVIEWER_STATE_DIR', os.path.join(TMP, 'p16e_pb_state'))

from PySide6.QtWidgets import QApplication                # noqa: E402
import appcache                                           # noqa: E402
import prepare as PREP                                    # noqa: E402
import desktop as D                                       # noqa: E402

REAL_MCAP = r'D:\视频查看软件\DAS-Ego_20260911203440_none_none_689985_65416a8a.mcap'
SPEEDS = (1.0, 2.0, 4.0, 8.0)


def pump(app, cond, timeout=180.0):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.004)
    app.processEvents()
    return False


def main():
    out = {'mcap': REAL_MCAP, 'speeds': {}, 'interactions': {}}
    if not os.path.isfile(REAL_MCAP):
        out['error'] = 'real small MCAP missing'
        print(json.dumps(out, ensure_ascii=False))
        return 1
    app = QApplication.instance() or QApplication([])
    fid = appcache.file_id(REAL_MCAP)
    outdir = D.cache_outdir(fid)
    man = PREP.prepare(REAL_MCAP, outdir,
                       camera_pred=appcache.profile_keeps_topic,
                       profile=appcache.CACHE_PROFILE)
    out['duration_s'] = round(man.get('duration_s') or 0.0, 3)
    out['profile'] = man.get('cache_profile')

    win = D.MainWindow()
    win.folder = os.path.dirname(REAL_MCAP)
    win.index = 0
    win.fid = fid
    win.items = [dict(path=REAL_MCAP, name=os.path.basename(REAL_MCAP),
                      size=os.path.getsize(REAL_MCAP), mtime=int(time.time()),
                      mtime_str='', stem=os.path.basename(REAL_MCAP),
                      dir=win.folder, rel_dir='')]
    win._apply(man, cached=True)
    pump(app, lambda: win.duration > 0, 30)

    # 找到各倍速在速度下拉框里的 index
    speed_idx = {}
    for i in range(win.cmb_speed.count()):
        speed_idx[round(float(win.cmb_speed.itemData(i) or 1.0), 2)] = i

    dur = float(win.duration)
    for sp in SPEEDS:
        if sp not in speed_idx:
            continue
        win.seek(0.0)
        win.cmb_speed.setCurrentIndex(speed_idx[sp])
        win._speed_changed()
        app.processEvents()
        pb0 = dict(win.pb)
        t0 = time.perf_counter()
        win.play()
        pump(app, lambda: not win.playing, timeout=dur / min(sp, 1.0) + 60)
        wall = time.perf_counter() - t0
        pb = win.pb
        out['speeds'][('%g' % sp)] = dict(
            wall_s=round(wall, 3),
            media_s=round(dur, 3),
            effective_rate=round(dur / wall, 2) if wall > 0 else None,
            wall_ratio_pct=round((wall / (dur / sp) - 1.0) * 100.0, 1)
            if dur > 0 and sp > 0 else None,
            render_fps=round((pb['rendered'] - pb0['rendered']) / wall, 1)
            if wall > 0 else None,
            rendered=pb['rendered'] - pb0['rendered'],
            dropped=pb['dropped'] - pb0['dropped'],
            max_drift_ms=round(pb['max_drift_ms'], 1),
            catchup_seeks=pb['catchup_seek_count'] - pb0['catchup_seek_count'],
            timer_interval_ms=win.timer.interval(),
            render_cap_fps=win._render_cap_fps(),
        )
        print('%-4s wall=%.2fs rate=%.2fx render=%.1ffps drop=%d drift=%.0fms catchup=%d'
              % ('%gx' % sp, wall, out['speeds']['%g' % sp]['effective_rate'],
                 out['speeds']['%g' % sp]['render_fps'],
                 out['speeds']['%g' % sp]['dropped'],
                 out['speeds']['%g' % sp]['max_drift_ms'],
                 out['speeds']['%g' % sp]['catchup_seeks']))

    # ---- 交互：pause/resume（8x）----
    inter = {}
    if 8.0 in speed_idx:
        win.seek(0.0)
        win.cmb_speed.setCurrentIndex(speed_idx[8.0])
        win._speed_changed()
        win.play()
        pump(app, lambda: win.t > 0.5, 20)
        win.pause()
        t_pause = float(win.t)
        time.sleep(3.0)
        app.processEvents()
        moved_while_paused = abs(float(win.t) - t_pause)
        win.play()
        pump(app, lambda: win.t > t_pause + 0.2, 20)
        inter['pause_resume'] = dict(
            paused_at_s=round(t_pause, 3),
            moved_during_pause_s=round(moved_while_paused, 3),
            resumed_ok=bool(win.t > t_pause),
            jump_s=round(float(win.t) - t_pause, 3))
        # ---- seek 后继续 8x ----
        win.seek(dur * 0.5)
        inter['seek'] = dict(target_s=round(dur * 0.5, 3),
                             actual_s=round(float(win.t), 3),
                             still_8x=abs(float(win.speed) - 8.0) < 1e-9)
        # ---- 变速不倒退 ----
        before = float(win.t)
        win.cmb_speed.setCurrentIndex(speed_idx[1.0])
        win._speed_changed()
        inter['speed_change'] = dict(before_s=round(before, 3),
                                     after_s=round(float(win.t), 3),
                                     no_rewind=bool(float(win.t) >= before - 0.01))
        win.pause()

    # ---- finish 只触发一次 ----
    win.seek(dur - 1.2)
    win.cmb_speed.setCurrentIndex(speed_idx.get(8.0, 0))
    win._speed_changed()
    emits = {'n': 0}
    orig = win.finish_current_video

    def spy(reason):
        emits['n'] += 1
        return orig(reason)
    win.finish_current_video = spy
    win.play()
    pump(app, lambda: not win.playing, 30)
    time.sleep(1.0)
    app.processEvents()
    inter['finish_emitted_once'] = (emits['n'] == 1, emits['n'])

    out['interactions'] = inter
    win.close()
    app.processEvents()

    path = os.path.join(TMP, 'p16e_playback.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print('汇总已写入', path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
