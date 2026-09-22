"""acceptance_mp4.py —— MP4 直读 + X 标注 + 混合文件夹报告 的真实验收

用真实 MCAP 的桌面缓存里的 H.264 流复制成「其他设备」的 MP4 文件，
和真实 MCAP 放在同一个文件夹，走一遍：

  扫描（mcap + mp4）→ 装载 → MCAP 走缓存、MP4 直读
  → 播放 MP4（直读原文件，不产生缓存目录）→ 按两下 X 标注
  → 全部看完 → 生成合格率报告（两种格式混在同一份报告里）
  → 校验：MP4 不占缓存槽位、原文件哈希不变、报告时长/区间正确

运行：runtime\\Scripts\\python.exe tests\\acceptance_mp4.py
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
    print('真实 MCAP: %s' % os.path.basename(src))

    app = QApplication.instance() or QApplication([])
    QSettings('MCAPViewer', 'Desktop').clear()
    old_root = appcache.CACHE_ROOT
    old_env = os.environ.get('MCAPVIEWER_STATE_DIR')
    cache_root = tempfile.mkdtemp(prefix='mcapview-mp4a-cache-')
    state_dir = tempfile.mkdtemp(prefix='mcapview-mp4a-state-')
    folder = tempfile.mkdtemp(prefix='mcapview-mp4a-folder-')
    appcache.CACHE_ROOT = cache_root
    os.environ['MCAPVIEWER_STATE_DIR'] = state_dir
    checks = []

    def ok(cond, msg):
        checks.append((bool(cond), msg))
        print(('  PASS  ' if cond else '  FAIL  ') + msg)

    try:
        # 1) 真实 MCAP 入文件夹
        mcap = os.path.join(folder, os.path.basename(src))
        try:
            os.link(src, mcap)
        except OSError:
            shutil.copy2(src, mcap)

        def pump(pred, timeout=600, what=''):
            t0 = time.time()
            while time.time() - t0 < timeout:
                app.processEvents()
                if pred():
                    return True
                time.sleep(0.05)
            print('  !! 超时: %s' % what)
            return False

        # 2) 先用 MCAP 生成一次缓存，把里面的 camera2 H.264 流导出成「其他设备」的 MP4
        qm = QM.CacheQueueManager()
        qm.load_folder(folder, [mcap])
        sid_mcap = qm.sid_of(mcap)
        ok(pump(lambda: qm.items[sid_mcap]['state'] == QM.CACHED, 600,
                'MCAP 缓存'), '真实 MCAP 完成缓存（用于取真实 H.264 流）')
        outdir = appcache.cache_dir(appcache.cache_key(sid_mcap))
        man = PREP.load_manifest(outdir, source_path=mcap)
        cams = [c for c in (man.get('cameras') or [])
                if c.get('kind') == 'mp4' and c.get('playable')
                and 'camera2' in (c.get('key') or '')]
        ok(bool(cams), '缓存里取出 camera2 的 H.264 流')
        src_mp4 = os.path.join(outdir, cams[0]['file'])
        mp4_names = ['OTHER_DEV_20260919_120000_a.mp4',
                     'OTHER_DEV_20260919_120500_b.mp4']
        mp4_paths = []
        for n in mp4_names:
            dst = os.path.join(folder, n)
            shutil.copy2(src_mp4, dst)
            mp4_paths.append(dst)
        hashes = {p: fx.file_sha256(p) for p in mp4_paths + [mcap]}
        print('已准备 %d 个「其他设备」MP4 + 1 个真实 MCAP' % len(mp4_paths))

        # 3) 重新装载整个文件夹（mcap + mp4 混排）
        paths = [it['path'] for it in PL.scan_folder(folder)]
        ok(len(paths) == 3, '扫描同时收下 .mcap 与 .mp4（共 %d 个）' % len(paths))
        qm2 = QM.CacheQueueManager()
        qm2.load_folder(folder, paths)
        sid_mp4 = [qm2.sid_of(p) for p in mp4_paths]
        ok(all(qm2.items[s]['state'] == QM.CACHED for s in sid_mp4),
           '两个 MP4 装载后立即可播（无需缓存）')
        ok(all(qm2.is_direct(s) for s in sid_mp4), 'MP4 标记为直读')
        ok(qm2.cached_count() <= QM.MAX_CACHED_ITEMS,
           'MP4 不占缓存槽位（当前槽位 %d/%d）'
           % (qm2.cached_count(), QM.MAX_CACHED_ITEMS))
        for s in sid_mp4:
            ok(not os.path.isdir(appcache.cache_dir(appcache.cache_key(s))),
               'MP4 没有产生缓存目录：%s' % qm2.items[s]['name'])
        info = PL.probe_mp4(mp4_paths[0])
        print('  探测到的 MP4：%.2f 秒 / %.1f fps / %dx%d'
              % (info['duration_s'], info['fps'], info['width'], info['height']))

        # 4) 在 MP4 上按两下 X 标注一段
        sid0 = sid_mp4[0]
        dur = info['duration_s']
        a, b = max(0.0, dur * 0.2), min(dur, dur * 0.5)
        segs, pending = [], None
        for point in (a, b):
            segs, pending, _closed = MK.add_point(segs, pending, point, dur)
            qm2.update_markers(sid0, segs, pending)
        got = [list(s) for s in qm2.markers_of(sid0)[0]]
        ok(len(got) == 1 and abs(got[0][0] - a) < 0.01 and abs(got[0][1] - b) < 0.01,
           'MP4 上的不合格片段标注成功：%.3f ~ %.3f 秒' % (a, b))
        ok(qm2.markers_of(sid0)[1] is None, '标注已闭合（无待闭合起点）')

        # 5) 全部看完 → 报告（不传时长，验证程序自己补真实时长）
        for p in paths:
            s = qm2.sid_of(p)
            qm2.mark_watched(s, 'manual')
            pump(lambda x=s: not os.path.isdir(
                appcache.cache_dir(appcache.cache_key(x))), 60, '缓存删除')
        ok(qm2.folder_all_watched(), '整文件夹（含 MP4）全部看完')
        summary = MK.summarize(qm2.summary_items(), folder)
        report = MK.write_report(folder, summary,
                                 fallback_dir=appcache.user_data_dir())
        body = open(report, encoding='utf-8-sig').read()
        ok(os.path.isfile(report), '报告已生成：%s' % os.path.basename(report))
        ok(all(r['duration'] > 0 for r in summary['rows']),
           '报告里每个视频都有真实时长（含 MP4 的探测时长）')
        ok(abs(summary['bad'] - (b - a)) < 0.01,
           '不合格时长 = MP4 上标注的 %.3f 秒' % (b - a))
        ok('合格率' in body and '说明' in body, '报告含合格率与口径说明')
        ok('OTHER_DEV_20260919_120000_a.mp4' in body, '报告明细含 MP4 文件名')
        print('  汇总：总 %.3f 秒 / 不合格 %.3f 秒 / 合格率 %.2f%%'
              % (summary['total'], summary['bad'], summary['rate']))

        # 6) 原文件不动
        for p, h in hashes.items():
            ok(os.path.isfile(p) and fx.file_sha256(p) == h,
               '原文件哈希不变：%s' % os.path.basename(p))

        qm.flush(); qm.shutdown()
        qm2.flush(); qm2.shutdown()
        for q in (qm, qm2):
            if q.worker is not None and q.worker.isRunning():
                q.worker.wait(15000)

        print()
        print('=' * 72)
        print(body[:1500])
        print('=' * 72)
        passed = sum(1 for c, _ in checks if c)
        print('MP4 真实验收: %d / %d 项通过' % (passed, len(checks)))
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
