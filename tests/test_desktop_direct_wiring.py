"""F-E2：Desktop UI 接线验收（真正跑 desktop.py → session → pane.view.set_image）

覆盖：未缓存 Indexed → MCAP_DIRECT、第一帧真的 set_image、TTFP 度量、
close 释放会话、失败回落旧缓存流程。
"""

import os
import shutil
import time
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication          # noqa: E402

import appcache                                     # noqa: E402
import desktop as D                                 # noqa: E402
from tests import mcapfix as fx                     # noqa: E402

REAL = r'D:\视频查看软件\DAS-Ego_20260911203440_none_none_689985_65416a8a.mcap'


def pump(app, cond, timeout=40.0):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    app.processEvents()
    return False


class DesktopDirectWiringCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if not os.path.isfile(REAL):
            raise unittest.SkipTest('缺少真实样本')
        cls.folder = fx.temp_dir('fe2-folder')

    def setUp(self):
        self._old_root = appcache.CACHE_ROOT
        self._old_state = os.environ.get('MCAPVIEWER_STATE_DIR')
        self.root = fx.temp_dir('fe2-cache')
        self.state = fx.temp_dir('fe2-state')
        appcache.CACHE_ROOT = self.root
        os.environ['MCAPVIEWER_STATE_DIR'] = self.state
        self.path = os.path.join(self.folder, os.path.basename(REAL))
        if not os.path.isfile(self.path):
            shutil.copy2(REAL, self.path)
        self.win = D.MainWindow()
        self.win.settings = None if False else self.win.settings
        self.win.load_folder(self.folder)          # 建 items/sids/队列
        self.win.qm._paused = True                 # 暂停自动预热，避免与 Direct 抢文件
        self.sid = self.win.sids[0]
        # 类级 spy：_apply() 会重建 panes，逐个实例装 spy 会失效
        self.calls = []
        _real_set_image = D.FrameView.set_image

        def _spy(view_self, img):
            self.calls.append(getattr(img, 'shape', None))
            return _real_set_image(view_self, img)

        self._patch = mock.patch.object(D.FrameView, 'set_image', _spy)
        self._patch.start()

    def tearDown(self):
        try:
            self._patch.stop()
        except Exception:
            pass
        try:
            self.win._close_direct_session()
            self.win.close()
        except Exception:
            pass
        appcache.CACHE_ROOT = self._old_root
        if self._old_state is None:
            os.environ.pop('MCAPVIEWER_STATE_DIR', None)
        else:
            os.environ['MCAPVIEWER_STATE_DIR'] = self._old_state
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    # ---------------------------------------------------------------- 用例
    def test_desktop_direct_frame_reaches_video_pane(self):
        self.assertIn(self.win.qm.items[self.sid]['state'],
                      (D.QM.UNWATCHED, D.QM.CACHING, D.QM.ERROR),
                      '状态应为未缓存类（当前 %s）' % self.win.qm.items[self.sid]['state'])
        self.win.play_sid(self.sid, autoplay=True)
        ok = pump(self.app, lambda: self.win._playback_backend == 'MCAP_DIRECT'
                  and len(self.calls) >= 1, 60)
        self.assertTrue(ok, '未走到 MCAP_DIRECT 或未调用 set_image（backend=%s, calls=%d）'
                        % (self.win._playback_backend, len(self.calls)))
        self.assertGreaterEqual(len(self.calls), 1, 'VideoPane.view.set_image 未被调用')
        self.assertIsNotNone(self.win._direct_ttpf_ms, 'TTFP 未被记录')
        self.assertLess(self.win._direct_ttpf_ms, 1500.0,
                        'Desktop TTFP %.0fms 超 1.5s' % self.win._direct_ttpf_ms)
        self.assertIsNotNone(self.win.panes.get('camera2'))

    def test_desktop_direct_seek_and_stale(self):
        self.win.play_sid(self.sid, autoplay=True)
        self.assertTrue(pump(self.app, lambda: self.win._playback_backend == 'MCAP_DIRECT'
                             and len(self.calls) >= 1, 60))
        dur = self.win.duration or 0.0
        self.assertGreater(dur, 1.0)
        n0 = len(self.calls)
        self.win.seek(dur * 0.5)
        self.assertTrue(pump(self.app, lambda: len(self.calls) > n0, 30),
                        'seek 后未刷新画面')
        st = self.win._direct_session.stats()
        self.assertEqual(st['rendered_stale'], 0, '过期帧不得渲染')

    def test_desktop_direct_close_releases_session(self):
        self.win.play_sid(self.sid, autoplay=True)
        self.assertTrue(pump(self.app, lambda: self.win._playback_backend == 'MCAP_DIRECT', 60))
        ses = self.win._direct_session
        self.assertIsNotNone(ses)
        self.win._close_direct_session()
        self.assertIsNone(self.win._direct_session)
        self.assertEqual(self.win._playback_backend, 'NONE')
        self.assertIsNone(ses.source)
        n = len(self.calls)
        self.win._on_direct_frame(None)            # 迟到帧/空帧不得渲染
        self.assertEqual(len(self.calls), n)

    def test_desktop_direct_failure_falls_back(self):
        calls = {}
        with mock.patch.object(D.QM.CacheQueueManager, 'request_play',
                               lambda self, sid: calls.setdefault('sid', sid) or True):
            self.win.play_sid(self.sid, autoplay=True)
            self.assertTrue(pump(self.app,
                                 lambda: self.win._playback_backend in ('MCAP_DIRECT',
                                                                        'MCAP_DIRECT_OPENING'), 30))
            self.win._on_direct_failed('synthetic failure')
            self.assertEqual(self.win._playback_backend, 'CACHE_REQUIRED')
            self.assertEqual(calls.get('sid'), self.sid,
                             '失败后必须调用既有缓存流程入口')

    def test_desktop_direct_creates_no_cache_artifact(self):
        self.win.play_sid(self.sid, autoplay=True)
        self.assertTrue(pump(self.app, lambda: len(self.calls) >= 1, 60))
        self.win._close_direct_session()
        # 只统计"已完成发布"的缓存产物；预热产生的 ...v3.staging 不属 Direct 产出
        leftovers = []
        for root, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if not d.endswith(('.staging', '.old', '.tmp'))]
            for f in files:
                if f.endswith(('.raw', '.times', '.mp4', 'manifest.json')):
                    leftovers.append(os.path.join(root, f))
        self.assertEqual(leftovers, [], 'Direct 不得产生缓存产物：%s' % leftovers[:5])


if __name__ == '__main__':
    unittest.main()
