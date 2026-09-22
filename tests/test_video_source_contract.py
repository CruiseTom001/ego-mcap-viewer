"""P1.6F-D1 VideoSource 双后端合同测试（Mp4VideoSource vs McapDirectVideoSource）

覆盖：统一语义（duration/seek/frame_at/camera switch/clamp/close 幂等）、
时间戳与像素等价、Direct 无需 cache 且不产生产物、首帧前解压 chunk 数 ≤2、
20 次 open/close 稳定、内存有界、源文件未改动。
"""

import math
import os
import random
import shutil
import time
import unittest

import appcache
import prepare as PREP
import video_source as VS
from tests import mcapfix as fx

REAL = r'D:\视频查看软件\DAS-Ego_20260911203440_none_none_689985_65416a8a.mcap'


def rss_mb():
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1048576.0
    except Exception:
        return -1.0


class VideoSourceContractCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(REAL):
            raise unittest.SkipTest('缺少真实样本 %s' % REAL)
        cls.path = REAL
        cls.size0 = os.path.getsize(REAL)
        cls.mt0 = os.path.getmtime(REAL)
        cls._old_root = appcache.CACHE_ROOT
        cls.root = fx.temp_dir('d1-cache')
        appcache.CACHE_ROOT = cls.root
        # 准备一份 MP4 缓存（供 Mp4VideoSource）
        fid = appcache.file_id(REAL)
        cls.cache_dir = appcache.cache_dir(appcache.cache_key(fid))
        cls.man = PREP.prepare(REAL, cls.cache_dir,
                               camera_pred=appcache.profile_keeps_topic,
                               profile=appcache.CACHE_PROFILE)

    @classmethod
    def tearDownClass(cls):
        appcache.CACHE_ROOT = cls._old_root
        shutil.rmtree(cls.root, ignore_errors=True)

    # ---------------------------------------------------------------- 工厂
    def _direct(self, camera='camera2'):
        return VS.McapDirectVideoSource(self.path, camera=camera).open()

    def _mp4(self, camera='camera2'):
        return VS.Mp4VideoSource(self.path, cache_dir=self.cache_dir,
                                 camera=camera).open()

    # ---------------------------------------------------------------- 合同
    def test_direct_open_close(self):
        src = self._direct()
        self.assertEqual(src.cameras(), ('camera2', 'camera3'))
        self.assertGreater(src.duration(), 1.0)
        src.close()

    def test_mp4_open_close(self):
        src = self._mp4()
        self.assertGreater(len(src.cameras()), 0)
        self.assertGreater(src.duration(), 1.0)
        src.close()

    def test_close_idempotent(self):
        src = self._direct()
        src.close()
        src.close()                     # 重复 close 不得崩
        src2 = self._mp4()
        src2.close()
        src2.close()

    def test_duration_equivalence(self):
        d1 = self._direct().duration()
        d2 = self._mp4().duration()
        # Direct 的 duration 目前是 chunk 级时间范围（±1 个 chunk 内最后帧的粒度，
        # 本样本约 2 帧 ≈0.06s）；精确到帧级留待 F-E。语义方向必须一致。
        self.assertLessEqual(abs(d1 - d2), 0.10,
                             '两后端 duration 差异 %.4fs' % abs(d1 - d2))

    def test_seek_semantics_and_clamp(self):
        for factory in (self._direct, self._mp4):
            src = factory()
            dur = src.duration()
            for frac in (0.10, 0.50, 0.90):
                fr = src.seek(dur * frac)
                self.assertIsNotNone(fr)
                self.assertEqual((fr.width, fr.height), (1600, 1300))
                self.assertGreaterEqual(fr.media_time + 0.05, dur * frac * 0.98)
            # clamp：负数 / 超时长
            self.assertGreaterEqual(src.seek(-5.0).media_time, 0.0)
            self.assertLess(src.seek(dur + 100.0).media_time, dur + 0.2)
            src.close()

    def test_frame_at_is_stateless(self):
        src = self._direct()
        dur = src.duration()
        a = src.seek(dur * 0.2)
        b = src.frame_at(dur * 0.8)
        c = src.frame_at(dur * 0.2)
        self.assertLess(abs(c.media_time - a.media_time), 0.05,
                        'frame_at 应无状态（不受之前 seek 影响）')
        self.assertGreater(b.media_time, a.media_time)
        src.close()

    def test_camera_switch_preserves_time(self):
        for factory in (self._direct, self._mp4):
            src = factory('camera2')
            dur = src.duration()
            t = dur * 0.5
            f2 = src.seek(t)
            f3 = src.switch_camera('camera3', t)
            self.assertEqual(f3.camera, 'camera3')
            self.assertLessEqual(abs(f3.media_time - f2.media_time), 0.05,
                                 '切换必须保持媒体时间')
            self.assertGreater(f3.media_time, t * 0.9, '切换不得跳回开头')
            src.close()

    def test_timestamp_equivalence(self):
        d = self._direct()
        m = self._mp4()
        dur = min(d.duration(), m.duration())
        worst = 0.0
        for frac in (0.10, 0.25, 0.50, 0.75, 0.90):
            fd = d.seek(dur * frac)
            fm = m.seek(dur * frac)
            worst = max(worst, abs(fd.media_time - fm.media_time))
        self.assertLessEqual(worst, 0.04,
                             '两后端时间戳最大差异 %.4fs' % worst)
        d.close()
        m.close()

    def test_pixel_and_dimension_equivalence(self):
        d = self._direct()
        m = self._mp4()
        dur = min(d.duration(), m.duration())
        exact = 0
        checked = 0
        max_frame_gap = 0.0
        min_psnr = 99.0
        for frac in (0.25, 0.50, 0.75):
            fd = d.seek(dur * frac)
            fm = m.seek(dur * frac)
            self.assertEqual((fd.width, fd.height), (fm.width, fm.height))
            checked += 1
            # 先看两后端是否落在同一帧时间（1 帧 = 1/30s）
            gap = abs(fd.media_time - fm.media_time)
            max_frame_gap = max(max_frame_gap, gap)
            if fd.image.tobytes() == fm.image.tobytes():
                exact += 1
            else:
                import numpy as np
                mse = float(((fd.image.astype('f4') - fm.image.astype('f4')) ** 2).mean())
                psnr = 99.0 if mse == 0 else 10 * math.log10(255.0 ** 2 / mse)
                min_psnr = min(min_psnr, psnr)
                if gap <= 1.2 / 30.0:
                    # 帧时间在 1 帧容差内 → 差异来自相邻帧（非解码路径），
                    # 按合同允许非 exact，但必须满足 PSNR >= 38 且原因可解释。
                    self.assertGreaterEqual(psnr, 38.0,
                                            '相邻帧差异过大（PSNR %.1f, Δ%.3fs）'
                                            % (psnr, gap))
                else:
                    self.assertGreaterEqual(psnr, 50.0,
                                            '帧时间差距 %.3fs 且像素不一致（PSNR %.1f）'
                                            % (gap, psnr))
        self.assertGreater(checked, 0)
        # 记录到测试输出，便于报告引述
        print('[pixel] checked=%d exact=%d max_frame_gap=%.4fs min_psnr=%.1f'
              % (checked, exact, max_frame_gap, min_psnr))
        d.close()
        m.close()

    # ---------------------------------------------------------------- Direct 专属
    def test_no_full_scan_and_chunk_bound(self):
        src = self._direct()
        src.seek(0.0)
        st = src.stats()
        self.assertIsNotNone(st['chunks_before_first_frame'])
        self.assertLessEqual(st['chunks_before_first_frame'], 2,
                             '首帧前解压 chunk 应 ≤2（实际 %s）'
                             % st['chunks_before_first_frame'])
        self.assertLessEqual(st['chunk_cache']['chunks'], 2)
        src.close()

    def test_direct_no_cache_required_and_no_artifacts(self):
        """隔离缓存根后 Direct 仍可用，且不产生任何缓存产物"""
        empty = fx.temp_dir('d1-empty-cache')
        old = appcache.CACHE_ROOT
        appcache.CACHE_ROOT = empty
        try:
            src = VS.McapDirectVideoSource(self.path).open()
            fr = src.seek(src.duration() * 0.3)
            self.assertIsNotNone(fr)
            src.close()
            leftovers = [f for f in os.listdir(empty)
                         if not f.startswith('.')]
            self.assertEqual(leftovers, [],
                             'Direct 不得产生缓存产物：%s' % leftovers)
        finally:
            appcache.CACHE_ROOT = old
            shutil.rmtree(empty, ignore_errors=True)

    def test_capability(self):
        cap = VS.direct_capability(self.path)
        self.assertTrue(cap.supported, cap.reason)
        self.assertEqual(cap.cameras, ('camera2', 'camera3'))
        bad = os.path.join(fx.temp_dir('d1-bad'), 'x.mcap')
        with open(bad, 'wb') as fh:
            fh.write(b'not an mcap')
        cap2 = VS.direct_capability(bad)
        self.assertFalse(cap2.supported)
        self.assertIn(cap2.reason, ('CORRUPT_SOURCE', 'NO_CHUNK_INDEX',
                                    'MISSING_CAMERA'))

    def test_open_close_20_cycles(self):
        r0 = rss_mb()
        for i in range(20):
            src = (self._direct() if i % 2 == 0 else self._mp4())
            src.seek(src.duration() * 0.2)
            src.close()
        r1 = rss_mb()
        if r0 > 0 and r1 > 0:
            self.assertLess(r1 - r0, 100.0,
                            '20 次 open/close 内存增量应 <100MB（%.1f→%.1f）' % (r0, r1))

    def test_direct_memory_bounded(self):
        src = self._direct()
        r0 = rss_mb()
        dur = src.duration()
        for _ in range(30):
            src.seek(random.uniform(0, dur))
        r1 = rss_mb()
        src.close()
        if r0 > 0 and r1 > 0:
            self.assertLess(r1 - r0, 100.0,
                            '30 次 seek 内存增量应 <100MB（%.1f→%.1f）' % (r0, r1))

    def test_source_not_modified(self):
        self.assertEqual(os.path.getsize(self.path), self.size0)
        self.assertEqual(os.path.getmtime(self.path), self.mt0)

    def test_e2e_ttfp_within_gate(self):
        """cold open → first frame（E2E-TTFP）必须 ≥ TTFI 且 <1s"""
        t0 = time.perf_counter()
        src = VS.McapDirectVideoSource(self.path).open()
        ttfi = (time.perf_counter() - t0) * 1000.0
        fr = src.seek(0.0)
        e2e = (time.perf_counter() - t0) * 1000.0
        src.close()
        self.assertIsNotNone(fr)
        self.assertGreaterEqual(e2e, ttfi)
        self.assertLess(e2e, 1000.0, 'E2E-TTFP %.1fms 超 1s' % e2e)


if __name__ == '__main__':
    unittest.main()
