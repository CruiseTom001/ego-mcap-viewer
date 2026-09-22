"""MP4 直读支持测试（playlist 探测 + 队列语义 + 播放/标注链路）

MP4 是其他设备的录像：文件名可能不符合 MCAP 采集命名约定，也不需要缓存封装，
所以这里验证的重点是「不占缓存槽位、直接播放、照样能按 X 标注并进合格率报告」。
"""

import os
import time
import shutil
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import cv2
import numpy as np

import playlist as PL
import appcache
import queue_manager as QM
import markers as MK
import prepare as PREP
import watchstate as WS
from tests import mcapfix as fx

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings

BASE = 1_000_000_000_000
CLIP_SECONDS = 2
CLIP_FPS = 25


def make_mp4(path, seconds=CLIP_SECONDS, fps=CLIP_FPS, size=(320, 240)):
    """生成一个真实可解码的小 MP4（无音轨）"""
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*'mp4v'),
                        float(fps), size)
    n = int(seconds * fps)
    for i in range(n):
        frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        frame[:, :, 0] = (i * 5) % 255
        frame[:, :, 1] = (i * 3) % 255
        w.write(frame)
    w.release()
    return path


class ProbeCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='mcapview-mp4-')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_probe_returns_duration_and_fps(self):
        p = make_mp4(os.path.join(self.dir, 'a.mp4'))
        info = PL.probe_mp4(p)
        self.assertIsNotNone(info)
        self.assertAlmostEqual(info['duration_s'], CLIP_SECONDS, places=1)
        self.assertAlmostEqual(info['fps'], CLIP_FPS, places=0)
        self.assertEqual(info['frames'], CLIP_SECONDS * CLIP_FPS)
        self.assertEqual((info['width'], info['height']), (320, 240))

    def test_probe_bad_file_returns_none(self):
        bad = os.path.join(self.dir, 'bad.mp4')
        with open(bad, 'wb') as fh:
            fh.write(b'not a video at all')
        self.assertIsNone(PL.probe_mp4(bad))
        self.assertIsNone(PL.probe_mp4(os.path.join(self.dir, 'missing.mp4')))

    def test_format_helpers(self):
        self.assertTrue(PL.is_direct_format('a.mp4'))
        self.assertTrue(PL.is_direct_format('A.MP4'))
        self.assertFalse(PL.is_direct_format('a.mcap'))
        self.assertTrue(PL.is_supported('a.mcap'))
        self.assertTrue(PL.is_supported('a.mp4'))
        self.assertFalse(PL.is_supported('a.txt'))

    def test_scan_folder_picks_up_both_formats(self):
        make_mp4(os.path.join(self.dir, 'rec_001.mp4'))
        fx.assemble(os.path.join(self.dir, 'DAS-Ego_20260911203440_none_none_689985_aa.mcap'),
                    [fx.header(), fx.schema(1, 'foxglove.CompressedImage'),
                     fx.channel(1, 1, '/robot0/sensor/camera2/compressed')])
        with open(os.path.join(self.dir, 'notes.txt'), 'w') as fh:
            fh.write('x')
        names = [it['name'] for it in PL.scan_folder(self.dir)]
        self.assertIn('rec_001.mp4', names)
        self.assertTrue(any(n.endswith('.mcap') for n in names))
        self.assertNotIn('notes.txt', names)


class Mp4QueueCase(unittest.TestCase):
    """MP4 在三队列里的语义：装载即可播、不占槽位、不触发缓存任务"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._old_root = appcache.CACHE_ROOT
        self._old_env = os.environ.get('MCAPVIEWER_STATE_DIR')
        self.root = tempfile.mkdtemp(prefix='mcapview-mp4q-cache-')
        self.state_dir = tempfile.mkdtemp(prefix='mcapview-mp4q-state-')
        appcache.CACHE_ROOT = self.root
        os.environ['MCAPVIEWER_STATE_DIR'] = self.state_dir
        self.folder = tempfile.mkdtemp(prefix='mcapview-mp4q-folder-')
        self.qm = QM.CacheQueueManager()

    def tearDown(self):
        if self.qm.worker is not None and self.qm.worker.isRunning():
            self.qm.worker.cancel()
            self.qm.worker.wait(10000)
        self.qm.shutdown()
        appcache.CACHE_ROOT = self._old_root
        if self._old_env is None:
            os.environ.pop('MCAPVIEWER_STATE_DIR', None)
        else:
            os.environ['MCAPVIEWER_STATE_DIR'] = self._old_env
        for d in (self.root, self.state_dir, self.folder):
            shutil.rmtree(d, ignore_errors=True)

    def _mp4(self, name):
        return make_mp4(os.path.join(self.folder, name))

    def _mcap(self, name):
        mcap = os.path.join(self.folder, name + '.mcap')
        recs = [fx.header(), fx.schema(1, 'foxglove.CompressedImage')]
        recs.append(fx.channel(1, 1, '/robot0/sensor/camera2/compressed'))
        png = fx.make_png(48, 36)
        for i in range(3):
            ts = BASE + i * 33_000_000
            recs.append(fx.message(1, i, ts, ts,
                                   fx.compressed_image(png, 'png', 'cam')))
        fx.assemble(mcap, recs)
        return os.path.abspath(mcap)

    def _pump(self, pred, timeout=30.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.app.processEvents()
            if pred():
                return True
            time.sleep(0.02)
        return False

    def test_mp4_loads_as_playable_without_cache_worker(self):
        paths = [self._mp4('other_dev_001.mp4'), self._mp4('other_dev_002.mp4')]
        self.qm.load_folder(self.folder, paths)
        sid = self.qm.sid_of(paths[0])
        it = self.qm.items[sid]
        self.assertTrue(it.get('direct'))
        self.assertEqual(it['state'], QM.CACHED, 'MP4 装载后应立即可播')
        self.assertEqual(self.qm.cached_count(), 0,
                         'MP4 不占缓存槽位')
        self.assertIsNone(self.qm.worker, 'MP4 不应触发任何缓存任务')
        q = self.qm.queues()
        self.assertEqual(len(q['cached']), 2, 'MP4 应显示在已缓存页（可播）')
        self.assertEqual(len(q['unwatched']), 0)

    def test_mp4_does_not_consume_slots_with_mcap_mix(self):
        mcap = self._mcap('DAS-Ego_20260911203440_none_none_689985_aa')
        mp4 = self._mp4('other_dev_001.mp4')
        self.qm.load_folder(self.folder, [mcap, mp4])
        self.assertTrue(self._pump(lambda: self.qm.cached_count() >= 1, 60),
                        '混排时 MCAP 仍应正常缓存')
        self.assertEqual(self.qm.cached_count(), 1, '槽位只数 MCAP')
        sid_mp4 = self.qm.sid_of(mp4)
        self.assertEqual(self.qm.items[sid_mp4]['state'], QM.CACHED)
        self.assertNotIn(sid_mp4, self.qm.cached_sids(),
                         '腾槽位时绝不能挑中 MP4')

    def test_mp4_summary_gets_duration_and_report_rate(self):
        paths = [self._mp4('dev_a_001.mp4'), self._mp4('dev_a_002.mp4')]
        self.qm.load_folder(self.folder, paths)
        sid0 = self.qm.sid_of(paths[0])
        self.qm.update_markers(sid0, [[0.5, 1.5]], None)   # 2 秒里标 1 秒
        for p in paths:
            self.qm.mark_watched(self.qm.sid_of(p), 'manual', None)
        items = self.qm.summary_items()
        self.assertTrue(all(i['duration_s'] > 0 for i in items),
                        '直读文件的长应由 probe 补上')
        summary = MK.summarize(items, self.folder)
        self.assertAlmostEqual(summary['total'], CLIP_SECONDS * 2, places=1)
        self.assertAlmostEqual(summary['bad'], 1.0, places=3)
        self.assertGreater(summary['rate'], 70.0)
        self.assertLess(summary['rate'], 80.0)

    def test_mp4_watched_state_persists_and_needs_no_delete(self):
        p = self._mp4('dev_b_001.mp4')
        self.qm.load_folder(self.folder, [p])
        sid = self.qm.sid_of(p)
        self.qm.mark_watched(sid, 'manual', PL.probe_mp4(p)['duration_s'])
        self.assertEqual(self.qm.items[sid]['state'], QM.WATCHED)
        self.assertFalse(self.qm.items[sid].get('cleanup_pending'),
                         '没有缓存可删，不该出现待清理')
        self.qm.flush()
        qm2 = QM.CacheQueueManager()
        try:
            qm2.load_folder(self.folder, [p])
            self.assertEqual(qm2.items[sid]['state'], QM.WATCHED,
                             'MP4 的已看完状态要能跨重启保持')
        finally:
            qm2.shutdown()
            if qm2.worker is not None and qm2.worker.isRunning():
                qm2.worker.wait(10000)

    def test_clear_all_removes_direct_items_from_queues(self):
        paths = [self._mp4('dev_c_001.mp4'), self._mp4('dev_c_002.mp4')]
        self.qm.load_folder(self.folder, paths)
        self.qm.mark_watched(self.qm.sid_of(paths[0]), 'manual', 2.0)
        res = self.qm.clear_all()
        self.assertEqual(res['deleted'], [], 'MP4 没有缓存目录可删')
        self.assertEqual(self.qm.order, [], '清空后队列里不应残留条目')
        q = self.qm.queues()
        self.assertTrue(all(len(q[k]) == 0 for k in q))
        for p in paths:
            self.assertTrue(os.path.isfile(p), '原文件不能被清空操作碰到')
        # 重新扫描后 MP4 又回到「可播放」
        self.qm.load_folder(self.folder, paths)
        for p in paths:
            s = self.qm.sid_of(p)
            self.assertEqual(self.qm.items[s]['state'], QM.CACHED,
                             '重扫后 MP4 仍应立即可播')

    def test_request_cache_is_noop_for_mp4(self):
        p = self._mp4('dev_d_001.mp4')
        self.qm.load_folder(self.folder, [p])
        sid = self.qm.sid_of(p)
        self.qm.request_cache(sid)
        self.assertIsNone(self.qm.worker, 'MP4 不该进缓存流程')
        self.assertIsNone(self.qm.requested_sid or None)

    def test_next_playable_can_target_mp4(self):
        mcap = self._mcap('DAS-Ego_20260911203440_none_none_689985_bb')
        mp4 = self._mp4('other_001.mp4')
        self.qm.load_folder(self.folder, [mcap, mp4])
        sid_mcap = self.qm.sid_of(mcap)
        sid_mp4 = self.qm.sid_of(mp4)
        self._pump(lambda: self.qm.items[sid_mcap]['state'] == QM.CACHED, 60)
        self.assertEqual(self.qm.next_playable_sid(sid_mcap, 1), sid_mp4,
                         '下一个可播目标应包含直读的 MP4')

    def test_mark_watched_without_playing_fills_duration(self):
        """没播放就直接点「标记已看完」：时长必须由程序自己补上，否则报告会失真"""
        mcap = self._mcap('DAS-Ego_20260911203440_none_none_689985_cc')
        mp4 = self._mp4('other_002.mp4')
        self.qm.load_folder(self.folder, [mcap, mp4])
        sid_mcap = self.qm.sid_of(mcap)
        sid_mp4 = self.qm.sid_of(mp4)
        self._pump(lambda: self.qm.items[sid_mcap]['state'] == QM.CACHED, 60)
        # 不播放，直接看完，且不传时长
        self.qm.mark_watched(sid_mcap, 'manual')
        self.qm.mark_watched(sid_mp4, 'manual')
        self._pump(lambda: not os.path.isdir(
            appcache.cache_dir(appcache.cache_key(sid_mcap))), 30)
        self.assertGreater(self.qm.items[sid_mcap]['duration_s'], 0.05,
                           'MCAP 时长应在删缓存前从 manifest 补上')
        self.assertAlmostEqual(self.qm.items[sid_mp4]['duration_s'],
                               CLIP_SECONDS, places=1,
                               msg='MP4 时长应由 probe 补上')
        summary = MK.summarize(self.qm.summary_items(), self.folder)
        self.assertTrue(all(r['duration'] > 0 for r in summary['rows']),
                        '报告每一行都要有真实时长')


class Mp4UiCase(unittest.TestCase):
    """界面链路：直读播放、时长正确、照样能按两下 X 标注"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        QSettings('MCAPViewer', 'Desktop').clear()
        cls._old_root = appcache.CACHE_ROOT
        cls._old_env = os.environ.get('MCAPVIEWER_STATE_DIR')
        cls.root = tempfile.mkdtemp(prefix='mcapview-mp4ui-cache-')
        cls.state_dir = tempfile.mkdtemp(prefix='mcapview-mp4ui-state-')
        appcache.CACHE_ROOT = cls.root
        os.environ['MCAPVIEWER_STATE_DIR'] = cls.state_dir
        cls.wins = []

    @classmethod
    def tearDownClass(cls):
        for w in cls.wins:
            try:
                w.close()
                w.deleteLater()
            except Exception:
                pass
        cls.app.processEvents()
        appcache.CACHE_ROOT = cls._old_root
        if cls._old_env is None:
            os.environ.pop('MCAPVIEWER_STATE_DIR', None)
        else:
            os.environ['MCAPVIEWER_STATE_DIR'] = cls._old_env
        QSettings('MCAPViewer', 'Desktop').clear()
        shutil.rmtree(cls.root, ignore_errors=True)
        shutil.rmtree(cls.state_dir, ignore_errors=True)

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='mcapview-mp4ui-f-')

    def tearDown(self):
        for w in list(self.wins):
            try:
                w.close()
                w.deleteLater()
            except Exception:
                pass
        self.wins.clear()
        self.app.processEvents()
        shutil.rmtree(self.folder, ignore_errors=True)

    def _pump(self, pred, timeout=30.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.app.processEvents()
            if pred():
                return True
            time.sleep(0.02)
        return False

    def _window(self, names):
        import desktop as D
        paths = [make_mp4(os.path.join(self.folder, n)) for n in names]
        win = D.MainWindow()
        self.wins.append(win)
        win.chk_auto.setChecked(False)
        win._confirm_folder_complete = lambda summary, path: False
        win.load_folder(self.folder, autoplay=False)
        win.show()
        win.resize(1280, 820)
        self.app.processEvents()
        return win, paths

    def test_mp4_opens_directly_with_marks_and_report(self):
        import desktop as D
        win, paths = self._window(['other_dev_rec_0001.mp4',
                                   'other_dev_rec_0002.mp4'])
        self.assertTrue(self._pump(lambda: win.qm.cached_count() >= 0, 5))
        self.assertEqual(win.qm.items[win.sids[0]]['state'], QM.CACHED)
        win.index = 0
        win.play_sid(win.sids[0], autoplay=False)
        self.assertTrue(self._pump(lambda: len(win.panes) >= 1, 30),
                        'MP4 应能直接打开并出画面')
        self.assertAlmostEqual(win.duration, CLIP_SECONDS, places=1)
        self.assertEqual(win.qm.items[win.sids[0]]['state'], QM.PLAYING)
        # 按两下 X 标注
        win.t = 0.4
        win._mark_bad_point()
        win.t = 1.6
        win._mark_bad_point()
        segs, pending = win.qm.markers_of(win.sids[0])
        self.assertEqual([list(s) for s in segs], [[0.4, 1.6]])
        self.assertIn('不合格 1 段', win.lbl_bad.text())
        # 逐段删除
        self.assertTrue(win._delete_bad_segment(win.sids[0], 0))
        self.assertEqual(list(win.qm.markers_of(win.sids[0])[0]), [])
        # 报告（全部看完）：重新标一段 1.2 秒的不合格
        win.t = 0.4
        win._mark_bad_point()                      # 起点
        win.t = 1.6
        win._mark_bad_point()                      # 闭合 → 1.2 秒
        self.assertEqual([list(s) for s in win.qm.markers_of(win.sids[0])[0]],
                         [[0.4, 1.6]])
        for p in paths:
            win.qm.mark_watched(win.qm.sid_of(p), 'manual', CLIP_SECONDS)
        summary = MK.summarize(win.qm.summary_items(), self.folder)
        path = MK.write_report(self.folder, summary)
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding='utf-8-sig') as fh:
            body = fh.read()
        self.assertIn('合格率', body)
        self.assertAlmostEqual(summary['bad'], 1.2, places=3)
        win.close()

    def test_mp4_folder_uses_playable_wording_no_cache_terms(self):
        """纯 MP4 文件夹：不出现「缓存槽位 / 已缓存 n/3」这类字样"""
        import desktop as D
        win, paths = self._window(['dev_x_001.mp4', 'dev_x_002.mp4'])
        self.assertTrue(self._pump(lambda: win.qm.items[win.sids[0]]['state']
                                   == QM.CACHED, 20))
        self.assertEqual(win.folder_mode(), 'mp4')
        self.assertEqual(win.tabs.tabText(1), '可播放 (2)')
        self.assertEqual(win._badge_text(win.qm.items[win.sids[0]]), '可播放')
        bar = win.lbl_queues.text()
        self.assertNotIn('/3', bar, 'MP4 文件夹不该出现缓存槽位数字：%r' % bar)
        self.assertNotIn('缓存', bar, 'MP4 文件夹不该出现「缓存」字样：%r' % bar)
        self.assertIn('可播放', bar)
        self.assertIn('共 2 个', bar)
        self.assertEqual(win.btn_clear.text(), '清空队列')
        win.close()

    def test_mcap_folder_keeps_cache_wording(self):
        """MCAP 文件夹：保持既有的槽位语义不变"""
        import desktop as D
        mcap = os.path.join(self.folder,
                            'DAS-Ego_20260911203440_none_none_689985_dd.mcap')
        recs = [fx.header(), fx.schema(1, 'foxglove.CompressedImage')]
        recs.append(fx.channel(1, 1, '/robot0/sensor/camera2/compressed'))
        png = fx.make_png(48, 36)
        for i in range(3):
            ts = BASE + i * 33_000_000
            recs.append(fx.message(1, i, ts, ts,
                                   fx.compressed_image(png, 'png', 'cam')))
        fx.assemble(mcap, recs)
        win = D.MainWindow()
        self.wins.append(win)
        win.chk_auto.setChecked(False)
        win._confirm_folder_complete = lambda summary, path: False
        win.load_folder(self.folder, autoplay=False)
        win.show(); win.resize(1200, 800)
        self.app.processEvents()
        self.assertTrue(self._pump(lambda: win.qm.cached_count() >= 1, 60))
        self.assertEqual(win.folder_mode(), 'mcap')
        self.assertIn('已缓存 (1/', win.tabs.tabText(1))
        self.assertIn('缓存', win.lbl_queues.text())
        self.assertEqual(win.btn_clear.text(), '清空队列与缓存')
        win.close()


if __name__ == '__main__':
    unittest.main()
