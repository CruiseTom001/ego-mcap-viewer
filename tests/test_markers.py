"""不合格标记与定位合格率报告测试（markers.py + 桌面端集成）

覆盖：
  * 采集文件名解析（日期/时间/设备号）与异常名容错
  * 两下 X 的标记状态机（起点→闭合、过短丢弃、越界截断、撤销）
  * 区间并集（重叠合并、倒置纠正）
  * 汇总口径：总时长 / 不合格 / 合格 / 合格率
  * 报告文件生成（文件名带设备号与日期、内容要素齐全、不可写时回退）
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import markers as MK
import appcache
import queue_manager as QM
import prepare as PREP
import watchstate as WS
from tests import mcapfix as fx

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings

BASE = 1_000_000_000_000


class ParseNameCase(unittest.TestCase):
    def test_real_name(self):
        info = MK.parse_name(
            'DAS-Ego_20260911203440_none_none_689985_65416a8a.mcap')
        self.assertEqual(info['date'], '20260911')
        self.assertEqual(info['date_text'], '2026-09-11')
        self.assertEqual(info['time'], '203440')
        self.assertEqual(info['time_text'], '20:34:40')
        self.assertEqual(info['device'], '689985')

    def test_real_genrobot_names_with_hex_device_id(self):
        """真实 Genrobot 文件名（《元数据填写指南》第 2 页截图）：
        设备号是 6 位十六进制，可能带字母——必须能解析出来"""
        cases = [
            ('DAS-Ego_20260815082408_none_none_9fb723_3839df9d.mcap',
             '20260815', '082408', '9fb723'),
            ('DAS-Ego_20260815083914_none_none_9fb723_7dd556.mcap',
             '20260815', '083914', '9fb723'),
            ('DAS-Ego_20260815161313_none_none_9fb723_9f702b98.mcap',
             '20260815', '161313', '9fb723'),
            # 设备号带字母的另一种（设备界面显示 EGO_3b6fb9）
            ('DAS-Ego_20260815085416_none_none_3b6fb9_74746b1.mcap',
             '20260815', '085416', '3b6fb9'),
        ]
        for name, date, tm, dev in cases:
            info = MK.parse_name(name)
            self.assertIsNotNone(info, '必须能解析真实文件名：%s' % name)
            self.assertEqual(info['date'], date, name)
            self.assertEqual(info['time'], tm, name)
            self.assertEqual(info['device'], dev, name)

    def test_report_device_text_for_hex_id(self):
        """报告里设备号要显示 9fb723 这种编号，而不是「未知」"""
        items = [dict(name='DAS-Ego_20260815082408_none_none_9fb723_3839df9d.mcap',
                      duration_s=600.0, segments=[[10.0, 20.0]])]
        s = MK.summarize(items, '/tmp/x')
        self.assertEqual(s['meta']['device_text'], '9fb723')
        body = MK.report_text(s)
        self.assertIn('9fb723', body)
        self.assertNotIn('未知', body.split('设备号')[1].split('\n')[0])

    def test_arbitrary_name_returns_none(self):
        for name in ('foo.mcap', 'video_20260911.mcap', '视频.mcap',
                     'DAS-Ego_20261399_none_none_1_a.mcap'):
            self.assertIsNone(MK.parse_name(name), name)

    def test_folder_device_info_single_and_multiple(self):
        one = MK.folder_device_info([
            'DAS-Ego_20260911203440_none_none_689985_aaaa.mcap',
            'DAS-Ego_20260911204500_none_none_689985_bbbb.mcap'])
        self.assertEqual(one['device_text'], '689985')
        self.assertEqual(one['date_text'], '2026-09-11')
        self.assertFalse(one['multiple_devices'])
        mixed = MK.folder_device_info([
            'DAS-Ego_20260911203440_none_none_689985_aaaa.mcap',
            'DAS-Ego_20260912203440_none_none_700001_bbbb.mcap'])
        self.assertTrue(mixed['multiple_devices'])
        self.assertTrue(mixed['multiple_dates'])


class SegmentCase(unittest.TestCase):
    def test_two_presses_make_one_segment(self):
        segs, pending, closed = MK.add_point([], None, 10.0, 70.0)
        self.assertEqual(segs, [])
        self.assertEqual(pending, 10.0)
        self.assertIsNone(closed)
        segs, pending, closed = MK.add_point(segs, pending, 12.5, 70.0)
        self.assertEqual(segs, [(10.0, 12.5)])
        self.assertIsNone(pending)
        self.assertEqual(closed, (10.0, 12.5))

    def test_reversed_presses_are_normalized(self):
        segs, pending, _ = MK.add_point([], None, 20.0, 70.0)
        segs, pending, closed = MK.add_point(segs, pending, 15.0, 70.0)
        self.assertEqual(closed, (15.0, 20.0))

    def test_too_short_press_is_discarded(self):
        segs, pending, _ = MK.add_point([], None, 10.0, 70.0)
        segs, pending, closed = MK.add_point(segs, pending, 10.0002, 70.0)
        self.assertEqual(segs, [])
        self.assertIsNone(pending)
        self.assertIsNone(closed)

    def test_clamped_to_duration(self):
        segs, pending, _ = MK.add_point([], None, 80.0, 70.0)
        self.assertEqual(pending, 70.0)
        segs, pending, closed = MK.add_point(segs, pending, -5.0, 70.0)
        self.assertEqual(closed, (0.0, 70.0))

    def test_overlapping_segments_merge(self):
        merged = MK.normalize_segments([[1, 5], [4, 9], [20, 18]], 70)
        self.assertEqual(merged, [(1.0, 9.0), (18.0, 20.0)])

    def test_bad_total_is_union_length(self):
        self.assertAlmostEqual(MK.bad_total([[0, 1], [0.5, 2]], 60), 2.0)

    def test_remove_segment_deletes_only_that_one(self):
        segs = [[1.0, 2.0], [5.0, 6.0], [9.0, 10.0]]
        self.assertEqual(MK.remove_segment(segs, 1),
                         [(1.0, 2.0), (9.0, 10.0)])
        self.assertEqual(MK.remove_segment(segs, 0), [(5.0, 6.0), (9.0, 10.0)])
        self.assertEqual(MK.remove_segment(segs, 2), [(1.0, 2.0), (5.0, 6.0)])

    def test_remove_segment_out_of_range_keeps_all(self):
        segs = [[1.0, 2.0]]
        for idx in (-1, 1, 99):
            self.assertEqual(MK.remove_segment(segs, idx), [(1.0, 2.0)])

    def test_remove_segment_normalizes_first(self):
        # 重叠段先合并，再按合并后的顺序删
        self.assertEqual(MK.remove_segment([[1, 5], [4, 9], [20, 25]], 1),
                         [(1.0, 9.0)])
        self.assertEqual(MK.remove_segment([[1, 5], [4, 9], [20, 25]], 0),
                         [(20.0, 25.0)])


class DayWindowCase(unittest.TestCase):
    """当地开始/结束时间（《元数据填写指南》三/四/五节口径 + 截图里的真实数字）"""

    def test_guide_example_rounding(self):
        # 指南第 6 页截图：首段 08:24:08 → 8:20；末段 09:07:40 + 2:10 → 9:10
        items = [
            dict(name='DAS-Ego_20260815082408_none_none_9fb723_3839df9d.mcap',
                 duration_s=600.0, segments=[]),
            dict(name='DAS-Ego_20260815083914_none_none_9fb723_7dd556.mcap',
                 duration_s=600.0, segments=[]),
            dict(name='DAS-Ego_20260815090740_none_none_9fb723_b62b252d.mcap',
                 duration_s=130.0, segments=[]),      # 2 分 10 秒
        ]
        s = MK.summarize(items, 'F')
        w = s['window']
        self.assertTrue(w['ok'])
        self.assertEqual(w['first_clock'], '08:24:08')
        self.assertEqual(w['start_text'], '8:20', '开始时间要向前取整到 10 分钟')
        self.assertEqual(w['last_clock'], '09:07:40')
        self.assertEqual(MK.fmt_clock(w['last_end']), '09:09:50')
        self.assertEqual(w['end_text'], '9:10', '结束时间要向后取整到 10 分钟')

    def test_end_time_on_exact_boundary_stays(self):
        self.assertEqual(MK.ceil_to_step(10 * 3600), 10 * 3600)
        self.assertEqual(MK.ceil_to_step(10 * 3600 + 1), 10 * 3600 + 600)
        self.assertEqual(MK.floor_to_step(8 * 3600 + 24 * 60 + 8),
                         8 * 3600 + 20 * 60)

    def test_window_uses_latest_start_plus_its_duration(self):
        items = [
            dict(name='DAS-Ego_20260815082408_none_none_9fb723_a.mcap',
                 duration_s=86400.0, segments=[]),      # 中间某段超长
            dict(name='DAS-Ego_20260815161313_none_none_9fb723_b.mcap',
                 duration_s=60.0, segments=[]),         # 最晚一段
        ]
        w = MK.summarize(items, 'F')['window']
        self.assertEqual(w['last_clock'], '16:13:13')
        self.assertEqual(MK.fmt_clock(w['last_end']), '16:14:13')
        self.assertEqual(w['end_text'], '16:20')

    def test_window_flags_missing_duration(self):
        items = [dict(name='DAS-Ego_20260815082408_none_none_9fb723_a.mcap',
                      duration_s=0.0, segments=[])]
        w = MK.summarize(items, 'F')['window']
        self.assertTrue(w['ok'])
        self.assertEqual(w['start_text'], '8:20')
        self.assertIsNone(w['end_rounded'])
        self.assertEqual(w['end_text'], '—')
        self.assertTrue(any('时长' in n for n in w['notes']))

    def test_window_without_parseable_names(self):
        items = [dict(name='other_device.mp4', duration_s=100.0, segments=[])]
        w = MK.summarize(items, 'F')['window']
        self.assertFalse(w['ok'])
        self.assertIn('无法推算', w['note'])

    def test_date_meta_text(self):
        # 指南截图里那批数据是 2024-08-15 → 与表内写法完全一致
        self.assertEqual(MK.date_meta_text('20240815'), '15-Aug-24')
        self.assertEqual(MK.date_meta_text('20260815'), '15-Aug-26')
        self.assertEqual(MK.date_meta_text('20260911'), '11-Sep-26')
        self.assertEqual(MK.date_meta_text('bad'), '—')

    def test_report_contains_metadata_block(self):
        items = [
            dict(name='DAS-Ego_20240815082408_none_none_9fb723_3839df9d.mcap',
                 duration_s=600.0, segments=[]),
            dict(name='DAS-Ego_20240815090740_none_none_9fb723_b62b252d.mcap',
                 duration_s=130.0, segments=[[10.0, 20.0]]),
        ]
        body = MK.report_text(MK.summarize(items, 'D:/x'))
        for token in ('【元数据（可直接填入元数据表）】', '设备号      ：9fb723',
                      '采集日期    ：2024-08-15（表内写法：15-Aug-24）',
                      '当地开始时间：8:20', '当地结束时间：9:10',
                      '向前取整到 10 分钟', '向后取整到 10 分钟'):
            self.assertIn(token, body, '报告缺少：%s' % token)


class SummaryCase(unittest.TestCase):
    def _items(self):
        return [
            dict(name='DAS-Ego_20260911203440_none_none_689985_aaaa.mcap',
                 path='a', duration_s=100.0, segments=[[10.0, 20.0]]),
            dict(name='DAS-Ego_20260911204500_none_none_689985_bbbb.mcap',
                 path='b', duration_s=50.0, segments=[]),
            dict(name='DAS-Ego_20260911210000_none_none_689985_cccc.mcap',
                 path='c', duration_s=50.0, segments=[[0.0, 5.0], [45.0, 50.0]]),
        ]

    def test_totals_and_rate(self):
        s = MK.summarize(self._items(), 'FOLDER')
        self.assertEqual(s['count'], 3)
        self.assertAlmostEqual(s['total'], 200.0)
        self.assertAlmostEqual(s['bad'], 20.0)
        self.assertAlmostEqual(s['good'], 180.0)
        self.assertAlmostEqual(s['rate'], 90.0)
        self.assertEqual(s['meta']['device_text'], '689985')
        self.assertEqual(s['marked_count'], 2)

    def test_zero_duration_is_safe(self):
        s = MK.summarize([dict(name='x.mcap', duration_s=0.0,
                               segments=[[1, 2]])], 'F')
        self.assertEqual(s['rate'], 0.0)
        self.assertAlmostEqual(s['good'], 0.0)

    def test_bad_cannot_exceed_duration(self):
        s = MK.summarize([dict(name='x.mcap', duration_s=10.0,
                               segments=[[-5, 30]])], 'F')
        self.assertAlmostEqual(s['bad'], 10.0)
        self.assertAlmostEqual(s['rate'], 0.0)

    def test_report_text_contains_required_fields(self):
        s = MK.summarize(self._items(), 'FOLDER')
        text = MK.report_text(s)
        for token in ('设备号', '689985', '采集日期', '2026-09-11',
                      '总定位时长', '不合格时长', '合格时长', '合格率',
                      '90.00%', '两下 X'):
            self.assertIn(token, text, '报告缺少要素：%s' % token)

    def test_report_written_to_ego_report_dir(self):
        """P1.6E：报告写入软件目录的 ego_report\，文件名 = 设备名 + 采集日期"""
        folder = tempfile.mkdtemp(prefix='mcapview-rep-')
        repdir = tempfile.mkdtemp(prefix='mcapview-rep-dir-')
        try:
            s = MK.summarize(self._items(), folder)
            ego = os.path.join(repdir, 'ego_report')
            with mock.patch.object(MK, 'report_dir', return_value=ego):
                path = MK.write_report(folder, s)
            self.assertTrue(os.path.isfile(path))
            self.assertIn('ego_report', path)
            self.assertIn('设备689985_20260911.txt', os.path.basename(path))
            with open(path, encoding='utf-8-sig') as fh:
                body = fh.read()
            self.assertIn('90.00%', body)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
            shutil.rmtree(repdir, ignore_errors=True)

    def test_report_falls_back_when_report_dir_not_writable(self):
        # 用「文件占位 report_dir 返回的路径」制造写入失败（跨平台确定）
        folder = tempfile.mkdtemp(prefix='mcapview-rep-bad-')
        fallback = tempfile.mkdtemp(prefix='mcapview-rep-fb-')
        try:
            s = MK.summarize(self._items(), folder)
            blocked = os.path.join(fallback, 'ego_report')
            os.makedirs(blocked, exist_ok=True)
            # 把 ego_report 目录本身占位成文件：mkdir/open 全部失败
            shutil.rmtree(blocked)
            with open(blocked, 'w') as fh:
                fh.write('x')
            with mock.patch.object(MK, 'report_dir', return_value=blocked):
                path = MK.write_report(folder, s, fallback_dir=fallback)
            self.assertTrue(os.path.isfile(path))
            self.assertIn('设备689985_20260911.txt', os.path.basename(path))
        finally:
            shutil.rmtree(folder, ignore_errors=True)
            shutil.rmtree(fallback, ignore_errors=True)


class ClearAllCase(unittest.TestCase):
    """QueueManager 层：标记持久化 / 全看完判定 / 清空三队列与缓存"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._old_root = appcache.CACHE_ROOT
        self._old_env = os.environ.get('MCAPVIEWER_STATE_DIR')
        self.root = tempfile.mkdtemp(prefix='mcapview-clr-cache-')
        self.state_dir = tempfile.mkdtemp(prefix='mcapview-clr-state-')
        appcache.CACHE_ROOT = self.root
        os.environ['MCAPVIEWER_STATE_DIR'] = self.state_dir
        self.folder = tempfile.mkdtemp(prefix='mcapview-clr-folder-')
        self.paths = []
        for i, tag in enumerate(('203440', '204500', '210000')):
            name = 'DAS-Ego_20260911%s_none_none_689985_%04x' % (tag, i)
            self.paths.append(self._build(name))
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

    def _build(self, stem):
        mcap = os.path.join(self.folder, stem + '.mcap')
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
        import time
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.app.processEvents()
            if pred():
                return True
            time.sleep(0.02)
        return False

    def test_markers_persist_across_restart(self):
        self.qm.load_folder(self.folder, self.paths)
        sid = self.qm.sid_of(self.paths[0])
        self.qm.update_markers(sid, [[1.0, 2.5], [5.0, 6.0]], None)
        self.qm.flush()
        qm2 = QM.CacheQueueManager()
        try:
            qm2.load_folder(self.folder, self.paths)
            segs, pending = qm2.markers_of(sid)
            self.assertEqual([list(s) for s in segs], [[1.0, 2.5], [5.0, 6.0]])
            self.assertIsNone(pending)
        finally:
            qm2.shutdown()
            if qm2.worker is not None and qm2.worker.isRunning():
                qm2.worker.wait(10000)

    def test_folder_all_watched_and_summary_duration(self):
        self.qm.load_folder(self.folder, self.paths)
        self.assertFalse(self.qm.folder_all_watched())
        for p in self.paths:
            sid = self.qm.sid_of(p)
            self.qm.mark_watched(sid, 'manual', 70.0)
            self._pump(lambda s=sid: not os.path.isdir(
                appcache.cache_dir(appcache.cache_key(s))), 20)
        self.assertTrue(self.qm.folder_all_watched())
        items = self.qm.summary_items()
        self.assertEqual(len(items), 3)
        self.assertTrue(all(i['duration_s'] == 70.0 for i in items),
                        'mark_watched 必须记录视频时长，供合格率汇总使用')

    def _empty_queues(self, q=None):
        """q 可传 queues() 的结果；不传则现取"""
        q = self.qm.queues() if q is None else q
        return all(len(q[k]) == 0 for k in q)

    def test_clear_all_wipes_all_three_queues(self):
        self.qm.load_folder(self.folder, self.paths)
        self._pump(lambda: self.qm.cached_count() >= 3, 60)
        sid0 = self.qm.sid_of(self.paths[0])
        self.qm.mark_watched(sid0, 'manual', 70.0)
        self._pump(lambda: not os.path.isdir(
            appcache.cache_dir(appcache.cache_key(sid0))), 20)
        res = self.qm.clear_all()
        q = self.qm.queues()
        self.assertTrue(self._empty_queues(q),
                        '清空后三个队列都必须为空，而不是退回未看：%s'
                        % {k: len(v) for k, v in q.items()})
        self.assertEqual(self.qm.order, [], '清空后队列里不应残留任何条目')
        self.assertEqual(self.qm.cached_count(), 0)
        for p in self.paths:
            s = self.qm.sid_of(p)
            self.assertFalse(os.path.isdir(appcache.cache_dir(appcache.cache_key(s))),
                             '该文件夹的 MCAP 缓存目录必须全部删除：%s' % p)
        self.assertGreaterEqual(len(res['deleted']), 1)

    def test_clear_all_does_not_refill_caches(self):
        """回归用户报的 bug：清空后不能立刻又把前几个缓存回来"""
        self.qm.load_folder(self.folder, self.paths)
        self._pump(lambda: self.qm.cached_count() >= 3, 60)
        self.qm.clear_all()
        self._pump(lambda: False, 2.0)          # 空转 2 秒
        self.assertTrue(self._empty_queues(), '清空后 2 秒内不得有项回流')
        self.assertEqual(self.qm.cached_count(), 0)
        self.assertIsNone(self.qm.worker, '清空后不应有缓存任务在跑')

    def test_clear_all_after_watched_stays_empty(self):
        """用户实际操作的复现：看完 → 在已看完队列点清空 → 三队列都应空"""
        self.qm.load_folder(self.folder, self.paths)
        self._pump(lambda: self.qm.cached_count() >= 1, 60)
        sid0 = self.qm.sid_of(self.paths[0])
        self.qm.mark_watched(sid0, 'manual', 70.0)
        self._pump(lambda: not os.path.isdir(
            appcache.cache_dir(appcache.cache_key(sid0))), 20)
        self.assertEqual(len(self.qm.queues()['watched']), 1)
        self.qm.clear_all()
        self._pump(lambda: False, 2.0)
        self.assertTrue(self._empty_queues(),
                        '清空后不得有项回到已缓存或未看队列')

    def test_rescan_after_clear_starts_fresh(self):
        """清空后点「重新扫描文件夹」：条目回到未看并恢复自动补缓存"""
        self.qm.load_folder(self.folder, self.paths)
        self._pump(lambda: self.qm.cached_count() >= 3, 60)
        sid0 = self.qm.sid_of(self.paths[0])
        self.qm.update_markers(sid0, [[1.0, 2.0]], None)   # 标注要先保住
        self.qm.mark_watched(sid0, 'manual', 70.0)
        self._pump(lambda: not os.path.isdir(
            appcache.cache_dir(appcache.cache_key(sid0))), 20)
        self.qm.clear_all()
        self.assertTrue(self._empty_queues())
        # 重新装载（等价于点「重新扫描文件夹」）
        self.qm.load_folder(self.folder, self.paths)
        q = self.qm.queues()
        self.assertEqual(len(q['unwatched']), len(self.paths), '重扫后全部回到未看')
        self.assertEqual(len(q['watched']), 0, '已看完记录不应残留')
        self.assertFalse(self.qm._paused, '重扫后应恢复自动补槽')
        self.assertTrue(self._pump(lambda: self.qm.cached_count() >= 1, 60),
                        '重扫后应自动开始缓存')
        segs, _p = self.qm.markers_of(sid0)
        self.assertEqual([list(s) for s in segs], [[1.0, 2.0]],
                         '人工标注不能因清空而丢失')

    def test_clear_all_also_wipes_currently_playing(self):
        """清空连正在播放的那一项一起清（先经 before_delete 停流）"""
        self.qm.load_folder(self.folder, self.paths)
        self._pump(lambda: self.qm.cached_count() >= 1, 60)
        sid0 = self.qm.sid_of(self.paths[0])
        self.qm.set_current(sid0)
        stopped = []
        self.qm.before_delete = lambda s: stopped.append(s)
        res = self.qm.clear_all()
        self.assertIn(sid0, stopped, '删当前播放项之前必须调用停流回调')
        self.assertIn(sid0, res['deleted'])
        self.assertFalse(os.path.isdir(
            appcache.cache_dir(appcache.cache_key(sid0))))
        self.assertEqual(self.qm.order, [])


if __name__ == '__main__':
    unittest.main()
