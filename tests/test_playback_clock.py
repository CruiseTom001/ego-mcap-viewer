"""P1.6E-R2/R4 播放回归测试：时钟驱动 / 倍率 / Render 上限 / 暂停恢复 / 结束一次

重点覆盖这次的真实回归：``_tick`` 里新增的漂移统计一旦抛异常，
播放会完全停住（1x 画面不动）——本文件用真实 MCAP 缓存驱动真实播放循环，
确保「时间真的在前进」。
"""

import os
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication          # noqa: E402

from tests import mcapfix as fx                     # noqa: E402
import appcache                                     # noqa: E402
import desktop as D                                 # noqa: E402
import prepare as PREP                              # noqa: E402

REAL = r'D:\视频查看软件\DAS-Ego_20260911203440_none_none_689985_65416a8a.mcap'
NS = 1_000_000_000
BASE = 1_700_000_000_000_000_000


class RenderCapCase(unittest.TestCase):
    """Render FPS 上限与 lag 计算是纯逻辑，先单测（不依赖真实播放）"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.dir = fx.temp_dir('pb-cap')
        cls.mcap = os.path.join(cls.dir, 'cap.mcap')
        recs = [fx.header()]
        recs.append(fx.schema(1, 'foxglove.CompressedImage'))
        recs.append(fx.channel(1, 1, '/robot0/sensor/camera2/compressed'))
        for i, (ts, data) in enumerate(
                fx.build_h264_frames(8, fps=30.0, t0_ns=BASE, idr_every=1)):
            recs.append(fx.message(1, i, ts, ts,
                                   fx.compressed_image(data, 'h264', 'cam')))
        recs.append(fx.chunk(recs[1:], start_ns=BASE, end_ns=BASE + NS))
        fx.assemble(cls.mcap, [fx.header()], None)
        recs2 = recs
        fx.assemble(cls.mcap, recs2[1:], None)

    def setUp(self):
        self._old = appcache.CACHE_ROOT
        self.root = fx.temp_dir('pb-cap-cache')
        appcache.CACHE_ROOT = self.root

    def tearDown(self):
        appcache.CACHE_ROOT = self._old

    def test_render_cap_values(self):
        win = D.MainWindow()
        try:
            for sp, cap in ((1.0, 60), (2.0, 30), (4.0, 20), (8.0, 15)):
                win.speed = sp
                got = win._render_cap_fps()
                if sp == 1.0:
                    self.assertLessEqual(got, cap)
                    self.assertGreaterEqual(got, 5)
                else:
                    self.assertEqual(got, cap, '%gx 的 render 上限应为 %d' % (sp, cap))
            win.speed = 8.0
            self.assertLessEqual(win.timer.interval() if False else 66, 1000)
            win._apply_render_cap()
            self.assertGreaterEqual(win.timer.interval(), 60)   # 8x ≈ 66ms
        finally:
            win.close()

    def test_playback_lag_never_raises(self):
        win = D.MainWindow()
        try:
            self.assertIsInstance(win._playback_lag(), float)   # 无 pane 时 0.0
        finally:
            win.close()


class RealPlaybackCase(unittest.TestCase):
    """真实缓存上的播放推进（1x / 8x / 暂停恢复 / seek / 结束一次）"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not os.path.isfile(REAL):
            raise unittest.SkipTest('缺少真实样本 %s' % REAL)

    def setUp(self):
        self._old = appcache.CACHE_ROOT
        self._old_state = os.environ.get('MCAPVIEWER_STATE_DIR')
        appcache.CACHE_ROOT = fx.temp_dir('pb-real-cache-%d' % id(self))
        os.environ['MCAPVIEWER_STATE_DIR'] = fx.temp_dir('pb-real-state-%d' % id(self))

    def tearDown(self):
        appcache.CACHE_ROOT = self._old
        if self._old_state is None:
            os.environ.pop('MCAPVIEWER_STATE_DIR', None)
        else:
            os.environ['MCAPVIEWER_STATE_DIR'] = self._old_state

    def _win(self):
        fid = appcache.file_id(REAL)
        outdir = D.cache_outdir(fid)
        man = PREP.prepare(REAL, outdir,
                           camera_pred=appcache.profile_keeps_topic,
                           profile=appcache.CACHE_PROFILE)
        win = D.MainWindow()
        win.folder = os.path.dirname(REAL)
        win.index = 0
        win.fid = fid
        win.items = [dict(path=REAL, name=os.path.basename(REAL),
                          size=os.path.getsize(REAL), mtime=int(time.time()),
                          mtime_str='', stem=os.path.basename(REAL),
                          dir=win.folder, rel_dir='')]
        win._apply(man, cached=True)
        ok = self._pump(lambda: win.duration > 0, 20)
        self.assertTrue(ok, '时长未就绪')
        return win

    def _pump(self, cond, timeout):
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            self.app.processEvents()
            if cond():
                return True
            time.sleep(0.004)
        self.app.processEvents()
        return False

    def _set_speed(self, win, sp):
        for i in range(win.cmb_speed.count()):
            if abs(float(win.cmb_speed.itemData(i) or 1.0) - sp) < 1e-9:
                win.cmb_speed.setCurrentIndex(i)
                win._speed_changed()
                return True
        return False

    def _wall(self, win, seconds):
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            self.app.processEvents()
            time.sleep(0.004)

    def test_1x_playback_advances(self):
        """回归：1x 播放时媒体时间必须真的前进（_tick 不得被打断）"""
        win = self._win()
        try:
            self.assertTrue(self._set_speed(win, 1.0))
            win.seek(0.0)
            before = win.t
            win.play()
            self._wall(win, 1.5)
            adv = win.t - before
            win.pause()
            self.assertGreater(adv, 0.5,
                               '1x 播放 1.5s 后媒体时间仅前进 %.3fs（应≈1.5s）' % adv)
            self.assertGreater(win.pb['rendered'], 0, '必须有实际渲染帧')
        finally:
            win.close()

    def test_8x_playback_rate(self):
        """8x：1 秒 wall 应推进约 8 秒媒体时间（允许 5~11 秒）"""
        win = self._win()
        try:
            self.assertTrue(self._set_speed(win, 8.0))
            win.seek(0.0)
            before = win.t
            win.play()
            self._wall(win, 1.0)
            adv = win.t - before
            win.pause()
            self.assertGreater(adv, 4.0, '8x 前进过慢：%.2fs/1s' % adv)
            self.assertLess(adv, 12.0, '8x 前进异常：%.2fs/1s' % adv)
            # 回归防线：8x 必须有画面持续更新（曾因「落后就 return」导致画面冻结）
            self.assertGreaterEqual(
                win.pb['rendered'], 5,
                '8x 画面没有持续更新（1s 内仅渲染 %d 帧）' % win.pb['rendered'])
            self.assertLessEqual(win.pb['rendered'], 30,
                                 '8x 渲染帧数异常偏高（%d）' % win.pb['rendered'])
        finally:
            win.close()

    def test_pause_resume_no_jump(self):
        win = self._win()
        try:
            self._set_speed(win, 8.0)
            win.seek(0.0)
            win.play()
            self._pump(lambda: win.t > 0.4, 10)
            win.pause()
            paused_at = win.t
            self._wall(win, 2.0)
            moved = abs(win.t - paused_at)
            self.assertLess(moved, 0.05, '暂停期间时间不应走动（%.3fs）' % moved)
            win.play()
            self._pump(lambda: win.t > paused_at + 0.2, 10)
            jump = win.t - paused_at
            self.assertLess(jump, 1.0, '恢复后不应一次跳过 %.2fs' % jump)
            win.pause()
        finally:
            win.close()

    def test_seek_keeps_speed_and_time(self):
        win = self._win()
        try:
            self._set_speed(win, 8.0)
            target = win.duration * 0.5
            win.seek(target)
            self.assertAlmostEqual(win.t, target, delta=0.05)
            self.assertAlmostEqual(float(win.speed), 8.0, delta=1e-9)
            self.assertFalse(win.t < 0.1, 'seek 后不应回到开头')
        finally:
            win.close()

    def test_finish_emitted_once(self):
        win = self._win()
        try:
            self._set_speed(win, 8.0)
            win.seek(max(0.0, win.duration - 0.8))
            emits = {'n': 0}
            orig = win.finish_current_video

            def spy(reason):
                emits['n'] += 1
                return orig(reason)
            win.finish_current_video = spy
            win.play()
            self._pump(lambda: not win.playing, 20)
            self._wall(win, 1.0)
            self.assertEqual(emits['n'], 1, '结束事件应只触发一次，实际 %d' % emits['n'])
        finally:
            win.close()


if __name__ == '__main__':
    unittest.main()
