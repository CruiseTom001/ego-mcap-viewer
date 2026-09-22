"""样例验收测试：直接对真实的 Genrobot 样例文件核对验收标准。

文件不存在时整个用例会 skip，不影响其它测试。
"""

import os
import shutil
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import appcache                       # noqa: E402
import h264mp4 as H                   # noqa: E402
import mcap_reader as MR              # noqa: E402
import prepare as PREP                # noqa: E402
import desktop as D                   # noqa: E402

SAMPLE = (r'D:\视频查看软件'
          r'\DAS-Ego_20260911203440_none_none_689985_65416a8a.mcap')

NS = 1_000_000_000

#: 验收标准里的期望值
EXPECT_MESSAGES = 5451
EXPECT_DURATION = 13.759962
EXPECT_CAMERAS = 6
EXPECT_IMU = 2745
EXPECT_AUDIO_CHUNKS = 214
EXPECT_AUDIO_OFFSET_S = 0.052275
#: 原始 412 帧、首帧非 IDR 的那一路，最终必须按 411 帧对外
EXPECT_DROPPED_CHANNEL = '/robot0/sensor/camera2/compressed'


@unittest.skipUnless(os.path.isfile(SAMPLE), '样例文件不存在，跳过')
class SampleCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fid = appcache.file_id(SAMPLE)
        cls.outdir = appcache.cache_dir(cls.fid)
        cls.man = PREP.prepare(SAMPLE, cls.outdir)

    def test_summary_numbers(self):
        s = MR.scan(SAMPLE)
        self.assertEqual(s['message_count'], EXPECT_MESSAGES)
        self.assertAlmostEqual(s['duration_s'], EXPECT_DURATION, places=6)
        self.assertTrue(s['has_statistics'])
        cams = [c for c in s['channels'] if c['kind'] == 'video']
        self.assertEqual(len(cams), EXPECT_CAMERAS)

    def test_cameras_count_and_audio_offset(self):
        cams = [c for c in self.man['cameras'] if c['playable']]
        self.assertEqual(len(cams), EXPECT_CAMERAS)
        a = self.man['audio']
        self.assertEqual(a['chunks'], EXPECT_AUDIO_CHUNKS)
        self.assertEqual(a['start_offset_ns'], 52_275_000)
        self.assertAlmostEqual(a['start_offset_s'], EXPECT_AUDIO_OFFSET_S, places=9)
        self.assertEqual(self.man['imu']['count'], EXPECT_IMU)

    def test_first_middle_last_frames_readable(self):
        """6 路的首帧 / 中间帧 / 末帧都必须能真的解出来"""
        try:
            import cv2
        except Exception:                    # pragma: no cover
            self.skipTest('没有 opencv')
        for cam in self.man['cameras']:
            if not cam['playable']:
                continue
            path = os.path.join(self.outdir, cam['file'])
            cap = cv2.VideoCapture(path)
            self.assertTrue(cap.isOpened(), cam['key'])
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            self.assertEqual(total, cam['frames'], cam['key'])
            for idx in (0, total // 2, total - 1):
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frame = cap.read()
                self.assertTrue(ok, '%s 第 %d 帧读不出来' % (cam['key'], idx))
                self.assertEqual(frame.shape, (1300, 1600, 3), cam['key'])
            cap.release()

    def test_non_idr_first_frame_uses_411(self):
        """原始 412 帧但首帧非 IDR 的那一路，UI/时间戳/MP4 三处都必须是 411 帧"""
        cam = next(c for c in self.man['cameras']
                   if c['topic'] == EXPECT_DROPPED_CHANNEL)
        self.assertEqual(cam['frames_raw'], 412)
        self.assertEqual(cam['dropped'], 1)
        self.assertEqual(cam['frames'], 411)
        times = PREP.read_times(os.path.join(self.outdir, cam['times_file']))
        self.assertEqual(len(times), 411)

        # UI 用的是同一个帧数
        from tests import mcapfix as fx
        self.assertEqual(fx.mp4_sample_count(os.path.join(self.outdir, cam['file'])), 411)

        # 逐帧功能基于真实时间戳
        self.assertAlmostEqual(times[0], cam['start_offset_s'], places=9)
        prim = D.pick_frame(times, times[0])
        self.assertEqual(prim, 0)
        self.assertEqual(D.pick_frame(times, times[0] - 1e-6), -1)   # 早于首帧 → 等待信号
        self.assertEqual(D.pick_frame(times, times[-1] + 10), 410)

    def test_timestamps_within_duration(self):
        dur = self.man['duration_s']
        for cam in self.man['cameras']:
            if not cam['playable']:
                continue
            times = PREP.read_times(os.path.join(self.outdir, cam['times_file']))
            self.assertEqual(len(times), cam['frames'], cam['key'])
            self.assertGreaterEqual(times[0], 0.0, cam['key'])
            self.assertLessEqual(times[-1], dur + 0.05, cam['key'])
            for i in range(1, len(times)):
                self.assertGreater(times[i], times[i - 1], cam['key'])

    def test_all_channels_h264_supported(self):
        """这个样例全是 H.264，桌面端应当全部可播放（README 与实现一致）"""
        for cam in self.man['cameras']:
            self.assertEqual(cam['codec'], 'h264', cam['key'])
            self.assertTrue(cam['playable'], cam.get('error'))

    def test_cache_reusable(self):
        man2 = PREP.load_manifest(self.outdir, source_path=SAMPLE)
        self.assertIsNotNone(man2)
        self.assertEqual(man2['cache_schema_version'], appcache.CACHE_SCHEMA_VERSION)


if __name__ == '__main__':
    unittest.main()
