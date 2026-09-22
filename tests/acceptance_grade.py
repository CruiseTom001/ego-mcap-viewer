"""acceptance_grade.py —— 真实 MCAP 的「不合格标注 + 定位合格率报告」验收

用真实录像复制成同一设备同一天的三段（改名成标准采集名），走完整流程：

  装载 → 缓存 → 两下 X 标注两段不合格 → 标记已看完（记录真实时长）
  → 全部看完 → 生成定位合格率报告 → 校验报告内容与原始文件哈希

运行：runtime\\Scripts\\python.exe tests\\acceptance_grade.py
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
from tests import mcapfix as fx

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings

#: 三段的时间戳（同一天、同一设备）
SEGMENTS = [('203440', '65416a8a'), ('204500', 'a1b2c3d4'),
            ('210000', 'deadbeef')]
#: 每段要标注的不合格区间（秒）
MARKS = [[(2.0, 5.0), (10.0, 11.0)]] * len(SEGMENTS)


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
    cache_root = tempfile.mkdtemp(prefix='mcapview-gr-cache-')
    state_dir = tempfile.mkdtemp(prefix='mcapview-gr-state-')
    folder = tempfile.mkdtemp(prefix='mcapview-gr-folder-')
    appcache.CACHE_ROOT = cache_root
    os.environ['MCAPVIEWER_STATE_DIR'] = state_dir
    checks = []

    def ok(cond, msg):
        checks.append((bool(cond), msg))
        print(('  PASS  ' if cond else '  FAIL  ') + msg)

    try:
        base = MK.parse_name(src)
        device = (base or {}).get('device', '689985')
        date = (base or {}).get('date', '20260911')
        paths = []
        for stamp, tail in SEGMENTS:
            dst = os.path.join(
                folder, 'DAS-Ego_%s%s_none_none_%s_%s.mcap' % (date, stamp,
                                                               device, tail))
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
            paths.append(dst)
        hashes = {p: fx.file_sha256(p) for p in paths}
        print('已准备 %d 段（设备 %s，日期 %s）' % (len(paths), device, date))

        qm = QM.CacheQueueManager()
        qm.load_folder(folder, paths)

        def pump(pred, timeout=600, what=''):
            t0 = time.time()
            while time.time() - t0 < timeout:
                app.processEvents()
                if pred():
                    return True
                time.sleep(0.05)
            print('  !! 超时: %s' % what)
            return False

        durations = []
        for i, p in enumerate(paths):
            sid = qm.sid_of(p)
            ok(pump(lambda s=sid: qm.items[s]['state'] == QM.CACHED, 600,
                    '第 %d 段缓存' % (i + 1)),
               '第 %d 段完成桌面精简缓存' % (i + 1))
            man = PREP.load_manifest(appcache.cache_dir(appcache.cache_key(sid)),
                                     source_path=p)
            dur = float(man.get('duration_s') or 0.0)
            durations.append(dur)
            # 模拟「按两下 X」：走 markers 状态机（起点 → 终点）
            segs, pending = [], None
            for a, b in MARKS[i]:
                segs, pending, _closed = MK.add_point(segs, pending, a, dur)
                qm.update_markers(sid, segs, pending)
                segs, pending, _closed = MK.add_point(segs, pending, b, dur)
                qm.update_markers(sid, segs, pending)
            qm.mark_watched(sid, 'manual', dur)
            pump(lambda s=sid: not os.path.isdir(
                appcache.cache_dir(appcache.cache_key(s))), 20, '缓存删除')
        print('三段时长: %s 秒' % ['%.3f' % d for d in durations])

        ok(qm.folder_all_watched(), '三段全部看完 → 触发报告条件成立')
        summary = MK.summarize(qm.summary_items(), folder)
        MK.report_dir = lambda: os.path.join(folder, 'ego_report')
        report = MK.write_report(folder, summary,
                                 fallback_dir=appcache.user_data_dir())
        print('报告路径: %s' % report)
        body = open(report, encoding='utf-8-sig').read()

        expect_bad = sum(sum(b - a for a, b in MK.normalize_segments(
            MARKS[i], durations[i])) for i in range(len(paths)))
        ok(os.path.isfile(report), '报告文件已生成')
        ok(device in os.path.basename(report), '报告文件名含设备号 %s' % device)
        ok('ego_report' in report, '报告写入软件目录 ego_report（P1.6E）')
        ok('设备%s_' % device in os.path.basename(report),
           '报告文件名 = 设备名+采集日期（P1.6E）')
        ok('20260911' in os.path.basename(report), '报告文件名含采集日期(YYYYMMDD)')
        ok('设备号      ：%s' % device in body, '报告正文含设备号')
        ok('2026-09-11' in body, '报告正文含采集日期')
        ok(abs(summary['total'] - sum(durations)) < 0.01, '总定位时长 = 各段之和')
        ok(abs(summary['bad'] - expect_bad) < 0.01,
           '不合格时长 = 各段并集之和（%.3f 秒）' % expect_bad)
        ok(abs(summary['rate'] - summary['good'] / summary['total'] * 100) < 0.01,
           '合格率 = 合格 ÷ 总时长（%.2f%%）' % summary['rate'])
        ok('合格率     ：%.2f%%' % summary['rate'] in body, '报告含合格率一行')
        # 元数据块（《元数据填写指南》三/四/五节口径）
        win = summary['window']
        ok('【元数据（可直接填入元数据表）】' in body, '报告含元数据块')
        ok('设备号      ：%s' % device in body, '元数据块含设备号')
        ok('采集日期    ：' in body and date in body.replace('-', ''),
           '元数据块含采集日期')
        # 三段时间为 20:34:40 / 20:45:00 / 21:00:00 → 首段向前取整 20:30；
        # 末段 21:00:00 + 时长 → 向后取整 21:10
        ok(win['start_text'] == '20:30',
           '当地开始时间向前取整到 10 分钟：%s（首段 %s）'
           % (win['start_text'], win['first_clock']))
        ok(win['end_text'] == '21:10',
           '当地结束时间向后取整到 10 分钟：%s（末段 %s + 时长）'
           % (win['end_text'], win['last_clock']))
        ok('当地开始时间：%s' % win['start_text'] in body
           and '当地结束时间：%s' % win['end_text'] in body,
           '报告正文含取整后的开始/结束时间')
        for i in range(len(paths)):
            ok(os.path.isfile(paths[i]) and
               fx.file_sha256(paths[i]) == hashes[paths[i]],
               '第 %d 段原始文件哈希不变' % (i + 1))

        qm.flush()
        qm.shutdown()
        if qm.worker is not None and qm.worker.isRunning():
            qm.worker.wait(15000)

        print()
        print('=' * 72)
        print(body)
        print('=' * 72)
        passed = sum(1 for c, _ in checks if c)
        print('真实验收: %d / %d 项通过（设备 %s / %s）'
              % (passed, len(checks), device, date))
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
