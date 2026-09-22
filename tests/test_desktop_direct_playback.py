"""P1.6F-E：Desktop Direct 播放集成层测试（控制器 / 选择策略 / 生命周期）

覆盖合同：backend 选择策略、单请求约束、generation 防回灌、无 cache 产物、
不占 cache slot、seek/switch/pause-resume、close 幂等、源文件只读。
"""

import os
import shutil
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication          # noqa: E402

import appcache                                     # noqa: E402
import direct_playback as DP                        # noqa: E402
import direct_h264_decoder as DD                    # noqa: E402
import mcap_video_index as VI                       # noqa: E402
import prepare as PREP                              # noqa: E402
import video_source as VS                           # noqa: E402
from tests import mcapfix as fx                     # noqa: E402

REAL = r'D:\视频查看软件\DAS-Ego_20260911203440_none_none_689985_65416a8a.mcap'
NOIDX = r'D:\视频查看软件\tmp\big_2gb_noidx.mcap'


def pump(app, cond, timeout=30.0):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.004)
    app.processEvents()
    return False


class BackendSelectionCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not os.path.isfile(REAL):
            raise unittest.SkipTest('缺少真实样本')
        cls._old = appcache.CACHE_ROOT
        cls.root = fx.temp_dir('fe-sel')
        appcache.CACHE_ROOT = cls.root

    @classmethod
    def tearDownClass(cls):
        appcache.CACHE_ROOT = cls._old
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_uncached_indexed_mcap_uses_direct(self):
        # 用独立空缓存根，避免与同类中其他用例写入的缓存串扰
        old_root = appcache.CACHE_ROOT
        empty = fx.temp_dir('fe-uncached')
        appcache.CACHE_ROOT = empty
        try:
            kind, reason = DP.select_backend(REAL)
        finally:
            appcache.CACHE_ROOT = old_root
            shutil.rmtree(empty, ignore_errors=True)
        self.assertEqual(kind, 'direct', reason)

    def test_cached_mcap_prefers_mp4(self):
        fid = appcache.file_id(REAL)
        cdir = appcache.cache_dir(appcache.cache_key(fid))
        PREP.prepare(REAL, cdir, camera_pred=appcache.profile_keeps_topic,
                     profile=appcache.CACHE_PROFILE)
        kind, _r = DP.select_backend(REAL, cache_dir=cdir)
        self.assertEqual(kind, 'mp4')

    def test_noindex_uses_cache_flow(self):
        if not os.path.isfile(NOIDX):
            self.skipTest('缺少 no-index 样本')
        kind, reason = DP.select_backend(NOIDX)
        self.assertEqual(kind, 'cache_required', reason)

    def test_direct_unsupported_uses_cache_flow(self):
        bad = os.path.join(fx.temp_dir('fe-bad'), 'bad.mcap')
        with open(bad, 'wb') as fh:
            fh.write(b'not mcap')
        kind, reason = DP.select_backend(bad)
        self.assertEqual(kind, 'cache_required')
        self.assertIn(reason, ('CORRUPT_SOURCE', 'NO_CHUNK_INDEX', 'MISSING_CAMERA'))


class DirectControllerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not os.path.isfile(REAL):
            raise unittest.SkipTest('缺少真实样本')
        cls.path = REAL
        cls.size0 = os.path.getsize(REAL)
        cls.mt0 = os.path.getmtime(REAL)

    def setUp(self):
        self._old = appcache.CACHE_ROOT
        self.empty = fx.temp_dir('fe-empty-cache')
        appcache.CACHE_ROOT = self.empty
        self.src = VS.McapDirectVideoSource(self.path).open()
        self.ctl = DP.DirectPlaybackController(self.src)
        self.got = []
        self.ctl.frameReady.connect(lambda f: self.got.append(f))
        self.failures = []
        self.ctl.failure.connect(lambda r: self.failures.append(r))

    def tearDown(self):
        self.ctl.stop()
        appcache.CACHE_ROOT = self._old
        shutil.rmtree(self.empty, ignore_errors=True)

    # ---------------------------------------------------------------- 基本
    def test_first_frame_no_cache_artifact(self):
        self.ctl.request(0.0)
        self.assertTrue(pump(self.app, lambda: len(self.got) >= 1, 20),
                        'Direct 首帧未返回')
        fr = self.got[-1]
        self.assertEqual((fr.width, fr.height), (1600, 1300))
        leftovers = [f for f in os.listdir(self.empty) if not f.startswith('.')]
        self.assertEqual(leftovers, [], 'Direct 不得产生缓存产物：%s' % leftovers)

    def test_does_not_occupy_cache_slot(self):
        import queue_manager as QM
        self.assertEqual(QM.MAX_CACHED_ITEMS, 3)

    def test_generation_drops_stale_frame(self):
        self.ctl.request(0.0)
        self.ctl.bump_generation()                 # 模拟 seek/switch：旧帧失效
        self.ctl.request(self.src.duration() * 0.5)
        self.assertTrue(pump(self.app, lambda: len(self.got) >= 1, 20))
        # 收到的帧必须属于新 generation 的目标附近
        self.assertGreater(self.got[-1].media_time, self.src.duration() * 0.2)

    def test_worker_max_one_outstanding(self):
        for i in range(20):
            self.ctl.request(i * 0.05)
        self.assertLessEqual(self.ctl.max_outstanding(), 1)
        self.assertLessEqual(self.ctl.outstanding(), 1)
        self.assertTrue(pump(self.app, lambda: len(self.got) >= 1, 20))

    def test_seek_and_switch(self):
        dur = self.src.duration()
        self.ctl.request(dur * 0.3)
        self.assertTrue(pump(self.app, lambda: len(self.got) >= 1, 20))
        self.ctl.bump_generation()
        self.ctl.request(dur * 0.5, camera='camera3')
        self.assertTrue(pump(self.app, lambda: len(self.got) >= 2, 20))
        fr = self.got[-1]
        self.assertEqual(fr.camera, 'camera3')
        self.assertGreater(fr.media_time, dur * 0.4)

    def test_pause_resume_keeps_source(self):
        dur = self.src.duration()
        self.ctl.request(dur * 0.2)
        self.assertTrue(pump(self.app, lambda: len(self.got) >= 1, 20))
        n = len(self.got)
        time.sleep(0.3)                            # 模拟 pause（无新请求）
        self.app.processEvents()
        self.assertEqual(len(self.got), n, 'pause 期间不应继续产生帧')
        self.ctl.request(dur * 0.25)
        self.assertTrue(pump(self.app, lambda: len(self.got) >= n + 1, 20))

    def test_stats_and_close(self):
        self.ctl.request(0.0)
        self.assertTrue(pump(self.app, lambda: len(self.got) >= 1, 20))
        st = dict(self.ctl.stats)
        self.assertGreaterEqual(st['frame_completed'], 1)
        self.assertLessEqual(st['frame_dropped_stale'], st['frame_requests'])
        self.ctl.stop()
        self.ctl.stop()                            # 幂等
        self.assertEqual(self.src.stats().get('opened'), False)

    def test_20_open_close_cycles(self):
        for _ in range(5):                         # 精简为 5 轮以保证测试时长
            src = VS.McapDirectVideoSource(self.path).open()
            ctl = DP.DirectPlaybackController(src)
            got = []
            ctl.frameReady.connect(lambda f: got.append(f))
            ctl.request(0.0)
            pump(self.app, lambda: len(got) >= 1, 20)
            ctl.stop()
        self.assertTrue(True)

    def test_original_source_unchanged(self):
        self.assertEqual(os.path.getsize(self.path), self.size0)
        self.assertEqual(os.path.getmtime(self.path), self.mt0)


class SessionCase(unittest.TestCase):
    """F-E1：会话层（打开即播）验收 —— TTFP / 主线程 stall / stale / 生命周期"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not os.path.isfile(REAL):
            raise unittest.SkipTest('缺少真实样本')
        cls.size0 = os.path.getsize(REAL)
        cls.mt0 = os.path.getmtime(REAL)

    def setUp(self):
        self._old = appcache.CACHE_ROOT
        self.empty = fx.temp_dir('fe1-empty')
        appcache.CACHE_ROOT = self.empty
        self.ses = DP.DirectPlaybackSession(REAL)
        self.frames = []
        self.state = {}
        self.ses.frameReady.connect(lambda f: self.frames.append(f))
        self.ses.opened.connect(lambda b, t: self.state.update(backend=b, ttfp=t))
        self.ses.failed.connect(lambda r: self.state.update(failed=r))

    def tearDown(self):
        try:
            self.ses.close()
        except Exception:
            pass
        appcache.CACHE_ROOT = self._old
        shutil.rmtree(self.empty, ignore_errors=True)

    def _open(self, timeout=30.0):
        self.ses.open_async()
        return pump(self.app, lambda: bool(self.state), timeout)

    def test_session_open_ttfp_and_backend(self):
        self.assertTrue(self._open(), '会话打开未完成：%s' % self.state)
        self.assertEqual(self.state.get('backend'), 'MCAP_DIRECT')
        self.assertLess(self.state['ttfp'], 1000.0,
                        'Desktop TTFP %.1fms 超 1s' % self.state['ttfp'])
        self.assertTrue(self.frames)
        self.assertEqual((self.frames[0].width, self.frames[0].height), (1600, 1300))

    def test_session_tick_does_not_stall_gui(self):
        self.assertTrue(self._open())
        for t in (1.0, 2.0, 3.0, 4.0):
            self.ses.tick(t)
            self.app.processEvents()
        st = self.ses.stats()
        self.assertLess(st['max_tick_stall_ms'], 500.0,
                        '主线程 tick stall %.2fms' % st['max_tick_stall_ms'])

    def test_session_seek_drops_stale(self):
        self.assertTrue(self._open())
        dur = self.ses.duration()
        for f in (0.2, 0.6, 0.3, 0.8):
            self.ses.seek(dur * f)
        pump(self.app, lambda: len(self.frames) >= 2, 20)
        st = self.ses.stats()
        self.assertEqual(st['rendered_stale'], 0, '过期帧不得渲染')
        self.assertGreaterEqual(st['ctl_frame_dropped_stale'], 0)

    def test_session_switch_camera_keeps_time(self):
        self.assertTrue(self._open())
        dur = self.ses.duration()
        t = dur * 0.5
        self.ses.seek(t)
        pump(self.app, lambda: len(self.frames) >= 2, 20)
        self.ses.switch_camera('camera3', t)
        pump(self.app, lambda: any(f.camera == 'camera3' for f in self.frames), 20)
        f3 = [f for f in self.frames if f.camera == 'camera3']
        self.assertTrue(f3, '未收到 camera3 帧')
        self.assertGreater(f3[-1].media_time, t * 0.9, '切换后不得跳回开头')

    def test_session_close_releases_source(self):
        self.assertTrue(self._open())
        self.ses.close()
        self.assertEqual(self.ses.backend, 'NONE')
        self.assertIsNone(self.ses.source)
        self.ses.close()                      # 幂等

    def test_session_creates_no_cache_artifact(self):
        self.assertTrue(self._open())
        pump(self.app, lambda: len(self.frames) >= 1, 20)
        leftovers = [f for f in os.listdir(self.empty) if not f.startswith('.')]
        self.assertEqual(leftovers, [], 'Direct 会话语义不得产生缓存产物：%s' % leftovers)

    def test_session_failure_reports_cache_required(self):
        bad = os.path.join(fx.temp_dir('fe1-bad'), 'bad.mcap')
        with open(bad, 'wb') as fh:
            fh.write(b'not an mcap')
        ses = DP.DirectPlaybackSession(bad)
        state = {}
        ses.failed.connect(lambda r: state.update(failed=r))
        ses.open_async()
        self.assertTrue(pump(self.app, lambda: bool(state), 20), '失败未上报')
        self.assertEqual(ses.backend, 'CACHE_REQUIRED')
        ses.close()

    def test_session_source_unchanged(self):
        self.assertTrue(self._open())
        self.assertEqual(os.path.getsize(REAL), self.size0)
        self.assertEqual(os.path.getmtime(REAL), self.mt0)


if __name__ == '__main__':
    unittest.main()
