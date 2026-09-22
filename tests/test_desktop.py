"""桌面端测试（Qt 离屏渲染，不需要真实显示器）

覆盖：按时间戳选帧（起点不同 / 变帧率 / 中间丢帧 / 早于首帧）/
      等比例缩放且不超目标宽 / 暂停时切换清晰度立即重解 /
      关闭窗口时所有线程安全结束
"""

import os
import shutil
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from tests import mcapfix as fx          # noqa: E402
import appcache                          # noqa: E402
import prepare as PREP                   # noqa: E402
import desktop as D                      # noqa: E402

from PySide6.QtWidgets import QApplication            # noqa: E402
from PySide6.QtCore import QSettings                   # noqa: E402
import numpy as np                                     # noqa: E402

NS = 1_000_000_000
BASE = 1_000_000_000_000


class ImuChartCase(unittest.TestCase):
    """IMU 曲线必须能在「有数据」时正常绘制。

    这里曾经因为 _build() 里解包变量名与返回字典不一致而崩溃，
    而只有真正含 IMU 的文件才会走到那条绘制分支，所以必须有这条用例。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _data(self, n=200):
        return dict(t=[i / 100.0 for i in range(n)],
                    av=[[0.013 * i - 1.0 for i in range(n)] for _ in range(3)],
                    la=[[0.0015 * i - 0.5 for i in range(n)] for _ in range(3)])

    def test_paints_with_data(self):
        chart = D.ImuChart()
        chart.resize(600, 120)
        chart.set_data(self._data(), 2.0)
        chart.set_time(1.0)
        pm = chart.grab()
        self.assertFalse(pm.isNull())
        self.assertEqual(pm.size().width(), 600)

    def test_paints_all_series_combinations(self):
        chart = D.ImuChart()
        chart.resize(600, 120)
        chart.set_data(self._data(), 2.0)
        for gyro in (True, False):
            for accel in (True, False):
                with self.subTest(gyro=gyro, accel=accel):
                    chart.set_visible_series(gyro, accel)
                    self.assertFalse(chart.grab().isNull())

    def test_paints_without_data(self):
        chart = D.ImuChart()
        chart.resize(600, 120)
        self.assertFalse(chart.grab().isNull())
        chart.clear()
        self.assertFalse(chart.grab().isNull())

    def test_paints_after_resize(self):
        chart = D.ImuChart()
        chart.resize(400, 100)
        chart.set_data(self._data(), 2.0)
        self.assertFalse(chart.grab().isNull())
        chart.resize(900, 160)
        self.assertFalse(chart.grab().isNull())


class PickFrameCase(unittest.TestCase):
    """选帧逻辑必须能处理起点偏移 / 变帧率 / 丢帧"""

    def test_before_first_frame_returns_minus_one(self):
        times = [0.033361, 0.066694, 0.100027]
        self.assertEqual(D.pick_frame(times, 0.0), -1)
        self.assertEqual(D.pick_frame(times, 0.033360), -1)
        self.assertEqual(D.pick_frame(times, 0.033361), 0)

    def test_desktop_keeps_only_camera2_and_camera3(self):
        self.assertTrue(D.is_primary_view(dict(key='camera2', topic='/camera2/compressed')))
        self.assertTrue(D.is_primary_view(dict(key='camera3', topic='/camera3/compressed')))
        for number in (0, 1, 4, 5, 20, 30):
            self.assertFalse(D.is_primary_view(
                dict(key='camera%d' % number, topic='/camera%d/compressed' % number)))

    def test_exact_and_between(self):
        times = [0.0, 0.1, 0.2, 0.3]
        self.assertEqual(D.pick_frame(times, 0.0), 0)
        self.assertEqual(D.pick_frame(times, 0.05), 0)
        self.assertEqual(D.pick_frame(times, 0.1), 1)
        self.assertEqual(D.pick_frame(times, 0.299999), 2)
        self.assertEqual(D.pick_frame(times, 0.3), 3)
        self.assertEqual(D.pick_frame(times, 99.0), 3)

    def test_empty(self):
        self.assertEqual(D.pick_frame([], 1.0), -1)
        self.assertEqual(D.pick_frame(None, 1.0), -1)

    def test_two_cameras_different_start_offsets(self):
        """相机 A 从 0.0334s 起、相机 B 从 0s 起：同一时刻应当选到各自的正确帧"""
        a = [0.033361 + i / 30.0 for i in range(60)]      # 起点晚
        b = [0.000043 + i / 30.0 for i in range(60)]      # 起点早
        t = 0.05
        self.assertEqual(D.pick_frame(b, t), int((t - 0.000043) * 30))
        self.assertEqual(D.pick_frame(a, t), 0)           # A 才刚出第一帧
        # A 在 0.03s 时还没出帧
        self.assertEqual(D.pick_frame(a, 0.03), -1)
        self.assertEqual(D.pick_frame(b, 0.03), 0)

    def test_variable_frame_rate(self):
        # 变帧率：第 3 帧之后卡了 0.5 秒
        times = [0.0, 0.1, 0.2, 0.7, 0.8, 0.9, 1.0]
        self.assertEqual(D.pick_frame(times, 0.15), 1)
        self.assertEqual(D.pick_frame(times, 0.35), 2)
        self.assertEqual(D.pick_frame(times, 0.60), 2)     # 卡顿期间保持上一帧
        self.assertEqual(D.pick_frame(times, 0.69), 2)
        self.assertEqual(D.pick_frame(times, 0.70), 3)
        self.assertEqual(D.pick_frame(times, 0.72), 3)
        self.assertEqual(D.pick_frame(times, 0.95), 5)

    def test_dropped_frames_gap(self):
        times = [0.0, 0.0333, 0.0667, 0.2000, 0.2333]      # 第 3~4 帧之间丢了 4 帧
        self.assertEqual(D.pick_frame(times, 0.10), 2)
        self.assertEqual(D.pick_frame(times, 0.19), 2)
        self.assertEqual(D.pick_frame(times, 0.20), 3)

    def test_first_frame_not_idr_timeshift(self):
        """首帧被丢弃后时间数组整体后移，选帧不能再用 t*fps 推"""
        fps = 30.0
        shift = 0.033361
        times = [shift + i / fps for i in range(20)]
        t = 0.05
        idx = D.pick_frame(times, t)
        self.assertEqual(idx, 0)                 # 不晚于 0.05 的只有第 0 帧
        self.assertEqual(int(t * fps), 1)        # 旧逻辑会算成第 1 帧 —— 错的
        self.assertNotEqual(idx, int(t * fps))
        # 每个时刻选出的帧都不晚于该时刻
        for tt in [shift + i / fps for i in range(20)]:
            i2 = D.pick_frame(times, tt)
            self.assertLessEqual(times[i2], tt + 1e-9)


class DownscaleCase(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np
        self.img = np.zeros((1200, 1600, 3), dtype=np.uint8)

    def test_never_exceeds_target(self):
        for target in (560, 800, 1100, 1600):
            out, resized = D.downscale(self.img, target)
            self.assertLessEqual(out.shape[1], target, target)
            self.assertLessEqual(out.shape[0], int(round(1200 * target / 1600)) + 1, target)

    def test_1600_to_560_really_560(self):
        out, resized = D.downscale(self.img, 560)
        self.assertTrue(resized)
        self.assertEqual(out.shape[1], 560)
        self.assertEqual(out.shape[0], int(round(1200 * 560 / 1600)))

    def test_aspect_ratio_preserved(self):
        for target in (560, 800, 1100):
            out, _ = D.downscale(self.img, target)
            self.assertAlmostEqual(out.shape[1] / out.shape[0], 1600 / 1200, places=2)

    def test_no_upscale(self):
        small = self.np.zeros((300, 400, 3), dtype=np.uint8)
        out, resized = D.downscale(small, 1600)
        self.assertFalse(resized)
        self.assertEqual(out.shape, (300, 400, 3))

    def test_zero_target_means_no_scale(self):
        out, resized = D.downscale(self.img, 0)
        self.assertFalse(resized)
        self.assertEqual(out.shape[1], 1600)


class CamStreamCase(unittest.TestCase):
    """解码线程自身的线程安全与「立即重解」行为"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.dir = fx.temp_dir('camstream')
        cls.frames = []
        for i in range(8):
            p = os.path.join(cls.dir, 'f%02d.png' % i)
            with open(p, 'wb') as fh:
                fh.write(fx.make_png(1600, 1200, (i * 20, 100, 200)))
            cls.frames.append(p)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _pump(self, pred, timeout=8.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.app.processEvents()
            if pred():
                return True
            time.sleep(0.01)
        return False

    def test_images_stream_and_quality_change_while_paused(self):
        """暂停状态下切换清晰度：当前帧必须立即按新尺寸重新解码"""
        s = D.CamStream(None, 800, list(self.frames))
        s.start()
        try:
            self.assertTrue(self._pump(lambda: s.latest()[0] is not None),
                            '图片序列没有解出帧')
            frame, idx = s.latest()
            self.assertEqual(idx, 0)
            self.assertEqual(frame.shape[1], 800)          # 1600 → 800

            # 模拟「暂停状态下切换清晰度」：不播放，只改尺寸 + 强制重解
            s.target_w = 560
            s.invalidate()
            self.assertTrue(self._pump(lambda: s.latest()[0] is not None
                                       and s.latest()[0].shape[1] == 560),
                            '切换清晰度后没有立即重解当前帧')
            frame2, idx2 = s.latest()
            self.assertEqual(idx2, 0, '重解不应该跳到别的帧')
            self.assertEqual(frame2.shape[0], int(round(1200 * 560 / 1600)))

            # 再切回大尺寸
            s.target_w = 1600
            s.invalidate()
            self.assertTrue(self._pump(lambda: s.latest()[0] is not None
                                       and s.latest()[0].shape[1] == 1600))
        finally:
            s.stop()
        self.assertFalse(s.isRunning())

    def test_latest_is_safe_while_running(self):
        s = D.CamStream(None, 400, list(self.frames))
        s.start()
        try:
            for _ in range(200):                            # 并发读，不应抛异常
                s.latest()
                s.request(3)
                self.app.processEvents()
        finally:
            s.stop()

    def test_stop_is_idempotent(self):
        s = D.CamStream(None, 400, list(self.frames))
        s.start()
        s.stop()
        s.stop()
        self.assertFalse(s.isRunning())


class MainWindowCase(unittest.TestCase):
    """整体窗口：加载缓存、切换清晰度、关闭时线程全部退出"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        QSettings('MCAPViewer', 'Desktop').clear()
        cls.dir = fx.temp_dir('mainwin')
        cls.work = []
        cls.wins = []
        # 缓存一律指向临时目录：窗口打开会触发 LRU 台账 / 滚动清理，
        # 绝不能让测试碰真实缓存（cache/ 下可能有真实用户的视频缓存）
        cls._old_cache_root = appcache.CACHE_ROOT
        cls._old_cache_external = appcache.CACHE_IS_EXTERNAL
        cls.cache_root = fx.temp_dir('mainwin-cache')
        appcache.CACHE_ROOT = cls.cache_root

    @classmethod
    def tearDownClass(cls):
        for w in cls.wins:
            try:
                w.deleteLater()
            except Exception:
                pass
        cls.app.processEvents()
        for mcap, outdir in cls.work:
            try:
                os.remove(mcap)
            except OSError:
                pass
            shutil.rmtree(outdir, ignore_errors=True)
            shutil.rmtree(outdir + '.staging', ignore_errors=True)
        shutil.rmtree(cls.dir, ignore_errors=True)
        appcache.CACHE_ROOT = cls._old_cache_root
        appcache.CACHE_IS_EXTERNAL = cls._old_cache_external
        shutil.rmtree(cls.cache_root, ignore_errors=True)

    def _pump(self, pred, timeout=10.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.app.processEvents()
            if pred():
                return True
            time.sleep(0.01)
        return False

    def _make(self, name, frames=8, width=1600, height=1200):
        mcap = os.path.join(self.dir, name + '.mcap')
        recs = [fx.header()]
        png = fx.make_png(width, height)
        recs.append(fx.schema(1, 'foxglove.CompressedImage'))
        recs.append(fx.channel(1, 1, '/robot0/sensor/camera2/compressed'))
        for i in range(frames):
            ts = BASE + i * 33_000_000
            recs.append(fx.message(1, i, ts, ts, fx.compressed_image(png, 'png', 'cam')))
        fx.assemble(mcap, recs)
        fid = appcache.file_id(mcap)
        outdir = D.cache_outdir(fid)
        man = PREP.prepare(mcap, outdir,
                           camera_pred=appcache.profile_keeps_topic,
                           profile=appcache.CACHE_PROFILE)
        self.work.append((mcap, outdir))
        return mcap, outdir, man

    def _window(self, mcap, man):
        win = D.MainWindow()
        self.wins.append(win)
        win.folder = os.path.dirname(mcap)
        win.index = 0
        win.fid = appcache.file_id(mcap)
        win.items = [dict(path=mcap, name=os.path.basename(mcap),
                          size=os.path.getsize(mcap), mtime=int(time.time()),
                          mtime_str='', stem=os.path.basename(mcap), dir=win.folder,
                          rel_dir='')]
        win._apply(man, cached=True)
        return win

    def test_window_loads_and_quality_refresh_while_paused(self):
        mcap, outdir, man = self._make('win_quality')
        win = self._window(mcap, man)
        self.assertEqual(len(win.panes), 1)
        key = man['cameras'][0]['key']
        pane = win.panes[key]
        self.assertTrue(self._pump(lambda: pane.stream.latest()[0] is not None),
                        '窗口没有解出画面')
        self.assertFalse(win.playing)
        self.assertEqual(pane.stream.latest()[0].shape[1], 800)

        # 暂停状态下切到「流畅 560px」
        idx = win.cmb_q.findData(560)
        win.cmb_q.setCurrentIndex(idx)
        self.assertTrue(self._pump(lambda: pane.stream.latest()[0] is not None
                                   and pane.stream.latest()[0].shape[1] == 560),
                        '暂停时切换清晰度没有立即生效')
        self.assertFalse(win.playing)

        # 单路放大（solo）也应当立即按 1600 重解
        win.toggle_solo(key)
        self.assertTrue(self._pump(lambda: pane.stream.latest()[0] is not None
                                   and pane.stream.latest()[0].shape[1] == 1600),
                        '单路放大后没有立即重解')

    def test_speed_selector_contains_requested_fast_rates(self):
        win = D.MainWindow()
        self.wins.append(win)
        values = [win.cmb_speed.itemData(i) for i in range(win.cmb_speed.count())]
        self.assertIn(2, values)
        self.assertIn(5, values)
        self.assertIn(8, values)
        self.assertEqual(win.cmb_speed.currentData(), 1)
        self.assertLessEqual(win.timer.interval(), 17)
        win.close()

    def test_slider_drag_coalesces_decode_requests(self):
        win = D.MainWindow()
        self.wins.append(win)
        win.duration = 10.0
        calls = []
        win._render_panes = lambda: calls.append(win.t)
        win.slider.setSliderDown(True)
        win._slider_down()
        for value in range(0, 1001, 10):
            win._slider_moved(value)
            self.app.processEvents()
        self.assertEqual(calls, [], '拖动事件不应逐次触发多路随机 seek')
        self.assertTrue(self._pump(lambda: len(calls) == 1, timeout=1.0))
        self.assertEqual(len(calls), 1, '预览解码应当被合并限频')
        win.slider.blockSignals(True)
        win.slider.setSliderDown(False)
        win.slider.blockSignals(False)
        win._slider_up()
        self.assertEqual(len(calls), 2, '松手时应只有一次最终精确定位')
        win.close()

    def test_close_stops_all_threads(self):
        mcap, outdir, man = self._make('win_close')
        win = self._window(mcap, man)
        self.assertTrue(self._pump(lambda: all(p.stream.latest()[0] is not None
                                              for p in win.panes.values())))
        streams = [p.stream for p in win.panes.values()]
        self.assertTrue(any(s.isRunning() for s in streams))

        win.close()
        self.app.processEvents()
        for s in streams:
            self.assertFalse(s.isRunning(), 'CamStream 关闭后仍在运行')
        self.assertIsNone(win.worker or None)
        if win.warm is not None:
            self.assertFalse(win.warm.isRunning(), '预热线程关闭后仍在运行')

    def test_switch_file_does_not_leak_threads(self):
        mcap1, outdir1, man1 = self._make('win_a')
        mcap2, outdir2, man2 = self._make('win_b')
        win = self._window(mcap1, man1)
        self.assertTrue(self._pump(lambda: all(p.stream.latest()[0] is not None
                                              for p in win.panes.values())))
        old = [p.stream for p in win.panes.values()]
        win.items.append(dict(path=mcap2, name=os.path.basename(mcap2),
                              size=os.path.getsize(mcap2), mtime=int(time.time()),
                              mtime_str='', stem=os.path.basename(mcap2),
                              dir=win.folder, rel_dir=''))
        win.open_index(1, autoplay=False)
        self.app.processEvents()
        for s in old:
            self.assertFalse(s.isRunning(), '切换文件后旧线程没有退出')
        self.assertTrue(self._pump(lambda: all(p.stream.latest()[0] is not None
                                              for p in win.panes.values())))
        win.close()
        self.app.processEvents()

    def test_window_renders_with_imu(self):
        """含 IMU 的文件：窗口整体绘制（会走到 IMU 曲线的绘制分支）"""
        mcap = os.path.join(self.dir, 'win_imu.mcap')
        recs = [fx.header()]
        png = fx.make_png(800, 600)
        recs.append(fx.schema(1, 'foxglove.CompressedImage'))
        recs.append(fx.channel(1, 1, '/robot0/sensor/camera2/compressed'))
        for i in range(6):
            ts = BASE + i * 33_000_000
            recs.append(fx.message(1, i, ts, ts, fx.compressed_image(png, 'png', 'cam')))
        recs.append(fx.schema(2, 'foxglove.IMUMeasurement'))
        recs.append(fx.channel(2, 2, '/robot0/sensor/imu'))
        for i in range(80):
            ts = BASE + i * 5_000_000
            recs.append(fx.message(2, i, ts, ts, fx.imu_message(ts)))
        fx.assemble(mcap, recs)
        fid = appcache.file_id(mcap)
        outdir = D.cache_outdir(fid)
        # P1.6D-R2B：Desktop 缓存已无 IMU → 用 full profile 造缓存，
        # 继续覆盖「IMU 曲线绘制分支」
        man = PREP.prepare(mcap, outdir,
                           camera_pred=appcache.profile_keeps_topic,
                           profile='full')
        self.work.append((mcap, outdir))

        win = self._window(mcap, man)
        self.assertIsNotNone(win.imu_data)
        self.assertFalse(win.imu_box.isHidden())
        key = man['cameras'][0]['key']
        self.assertTrue(self._pump(lambda: win.panes[key].stream.latest()[0] is not None))
        win.resize(1200, 800)
        win.seek(0.1)
        self.app.processEvents()
        self.assertFalse(win.grab().isNull())
        win.close()
        self.app.processEvents()

    def test_window_renders_without_imu(self):
        """P1.6D-R2B：Desktop videoonly 缓存无 imu.json —— 窗口正常渲染，不报错"""
        mcap = os.path.join(self.dir, 'win_noimu.mcap')
        recs = [fx.header()]
        png = fx.make_png(800, 600)
        recs.append(fx.schema(1, 'foxglove.CompressedImage'))
        recs.append(fx.channel(1, 1, '/robot0/sensor/camera2/compressed'))
        for i in range(6):
            ts = BASE + i * 33_000_000
            recs.append(fx.message(1, i, ts, ts, fx.compressed_image(png, 'png', 'cam')))
        recs.append(fx.schema(2, 'foxglove.IMUMeasurement'))
        recs.append(fx.channel(2, 2, '/robot0/sensor/imu'))
        for i in range(80):
            ts = BASE + i * 5_000_000
            recs.append(fx.message(2, i, ts, ts, fx.imu_message(ts)))
        fx.assemble(mcap, recs)
        fid = appcache.file_id(mcap)
        outdir = D.cache_outdir(fid)
        man = PREP.prepare(mcap, outdir,
                           camera_pred=appcache.profile_keeps_topic,
                           profile=appcache.CACHE_PROFILE)
        self.work.append((mcap, outdir))
        self.assertIsNone(man.get('imu'), 'Desktop 合同：无 IMU 产物')
        self.assertFalse(os.path.isfile(os.path.join(outdir, 'imu.json')))

        win = self._window(mcap, man)
        self.assertIsNone(win.imu_data, 'Desktop 缓存不应加载 IMU 数据')
        key = man['cameras'][0]['key']
        self.assertTrue(self._pump(lambda: win.panes[key].stream.latest()[0] is not None))
        win.resize(1200, 800)
        win.seek(0.1)
        self.app.processEvents()
        self.assertFalse(win.grab().isNull(), '无 IMU 时窗口仍必须可渲染')
        win.close()
        self.app.processEvents()

    def test_downscale_used_in_snapshot(self):
        """导出走同一套选帧逻辑：t=0.05s 应选到第 1 帧而不是第 0 帧"""
        mcap, outdir, man = self._make('win_snap')
        win = self._window(mcap, man)
        key = man['cameras'][0]['key']
        pane = win.panes[key]
        self.assertTrue(self._pump(lambda: pane.stream.latest()[0] is not None))
        win.t = 0.05
        self.assertEqual(D.pick_frame(pane.times, win.t), 1)
        img = win._grab(key)
        self.assertIsNotNone(img)
        self.assertEqual(img.shape[1], 1600)


if __name__ == '__main__':
    unittest.main()
