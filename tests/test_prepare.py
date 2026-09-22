"""prepare 缓存层测试

覆盖：音频偏移纳秒→秒 / 首帧非 IDR 时帧数与时间戳一致 / safe_name 冲突不互相覆盖 /
      缓存缺文件自动失效 / 同一 file_id 并发 prepare 只跑一次 / 缓存版本与来源签名 /
      不支持的编码 / 中途换编码 / 多路音频不混写 / 图片序列通道
"""

import os
import shutil
import struct
import threading
import time
import unittest
import wave
from concurrent.futures import CancelledError
from unittest import mock

from tests import mcapfix as fx
import appcache
import prepare as PREP

NS = 1_000_000_000
BASE = 1_000_000_000_000          # 统一的时间基准


def add_video(recs, cid, sid, topic, frames, fmt='h264',
              schema='foxglove.CompressedImage'):
    recs.append(fx.schema(sid, schema))
    recs.append(fx.channel(cid, sid, topic))
    for i, (ts, data) in enumerate(frames):
        if schema.endswith('CompressedVideo'):
            body = fx.compressed_video(data, fmt, topic)
        else:
            body = fx.compressed_image(data, fmt, topic)
        recs.append(fx.message(cid, i, ts, ts, body))
    return cid


def add_audio(recs, cid, sid, topic, first_ns, chunks=4, chunk_ms=64,
              sample_rate=16000, channels=2):
    recs.append(fx.schema(sid, 'foxglove.AudioData'))
    recs.append(fx.channel(cid, sid, topic))
    per = max(1, sample_rate * chunk_ms // 1000)
    pcm = b'\x01\x02' * per
    for i in range(chunks):
        ts = first_ns + i * chunk_ms * 1_000_000
        recs.append(fx.message(cid, i, ts, ts,
                               fx.audio_message(pcm, sample_rate, channels, seq=i)))
    return cid


def add_imu(recs, cid, sid, topic, first_ns, count=20, step_ns=5_000_000):
    recs.append(fx.schema(sid, 'foxglove.IMUMeasurement'))
    recs.append(fx.channel(cid, sid, topic))
    for i in range(count):
        ts = first_ns + i * step_ns
        recs.append(fx.message(cid, i, ts, ts, fx.imu_message(ts)))
    return cid


class PrepareCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fx.temp_dir('prepare')

    def setUp(self):
        self.mcap = os.path.join(self.dir, self._testMethodName + '.mcap')
        self.cache = os.path.join(self.dir, self._testMethodName + '.cache')

    def tearDown(self):
        for p in (self.mcap,):
            try:
                os.remove(p)
            except OSError:
                pass
        shutil.rmtree(self.cache, ignore_errors=True)
        shutil.rmtree(self.cache + '.staging', ignore_errors=True)

    # -------------------------------------------------------- 1 音频偏移
    def test_audio_offset_ns_converted_to_seconds(self):
        """52,275,000 ns 必须变成 0.052275 s，而不是原样当秒用"""
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(30, t0_ns=BASE))
        add_audio(recs, 2, 2, '/robot0/sensor/audio', BASE + 52_275_000)
        add_imu(recs, 3, 3, '/robot0/sensor/imu', BASE + 39_568_000)
        fx.assemble(self.mcap, recs)

        man = PREP.prepare(self.mcap, self.cache)
        a = man['audio']
        self.assertEqual(a['start_offset_ns'], 52_275_000)
        self.assertAlmostEqual(a['start_offset_s'], 0.052275, places=9)
        self.assertNotAlmostEqual(a['start_offset_s'], 52_275_000)

        imu = man['imu']
        self.assertAlmostEqual(imu['count'], 20)
        # IMU 的时间也是相对 time_base 的秒
        self.assertGreaterEqual(imu['count'], 1)
        with open(os.path.join(self.cache, 'imu.json'), encoding='utf-8') as fh:
            import json
            d = json.load(fh)
        self.assertAlmostEqual(d['t'][0], 0.039568, places=9)

    def test_manifest_time_units(self):
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(31, fps=30.0, t0_ns=BASE))
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        self.assertEqual(man['time_base_ns'], BASE)
        self.assertAlmostEqual(man['duration_s'], 1.0, places=6)
        self.assertIn('duration', man)                       # 兼容网页版的字段名
        self.assertAlmostEqual(man['duration'], man['duration_s'], places=9)
        cam = man['cameras'][0]
        self.assertAlmostEqual(cam['start_offset_s'], 0.0, places=9)
        self.assertAlmostEqual(cam['start_offset_ns'], 0, places=0)
        self.assertEqual(cam['start_offset_ns'],
                         int(round(cam['start_offset_s'] * NS)))

    # -------------------------------------------------------- 2 首帧非 IDR
    def test_first_frame_not_idr_timestamps_match_mp4(self):
        """首帧非 IDR 的那一路：manifest 时间戳、帧数、MP4 实际帧数必须一致"""
        recs = [fx.header()]
        frames = fx.build_h264_frames(40, t0_ns=BASE + 100_000_000,
                                      first_idr_at=1, idr_every=30)
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed', frames)
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        cam = man['cameras'][0]

        self.assertEqual(cam['frames_raw'], 40)
        self.assertEqual(cam['dropped'], 1)
        self.assertEqual(cam['frames'], 39)
        times = PREP.read_times(os.path.join(self.cache, cam['times_file']))
        self.assertEqual(len(times), cam['frames'])
        self.assertEqual(fx.mp4_sample_count(os.path.join(self.cache, cam['file'])),
                         cam['frames'])
        # 偏移取第一个「实际保留帧」，基准是全文件的 time_base
        want = (frames[1][0] - man['time_base_ns']) / NS
        self.assertAlmostEqual(cam['start_offset_s'], want, places=9)
        self.assertAlmostEqual(times[0], cam['start_offset_s'], places=9)
        # 被丢弃的首帧时间戳不应该出现在时间数组里
        first_raw = (frames[0][0] - man['time_base_ns']) / NS
        self.assertNotAlmostEqual(times[0], first_raw, places=9)

    def test_camera_offsets_differ(self):
        """两路相机起点不同：各自的 offset 与时间数组都要独立正确"""
        recs = [fx.header()]
        f1 = fx.build_h264_frames(20, t0_ns=BASE + 33_361_000)
        f2 = fx.build_h264_frames(20, t0_ns=BASE + 43_000)
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed', f1)
        add_video(recs, 2, 2, '/robot0/sensor/camera1/compressed', f2)
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        by = {c['id']: c for c in man['cameras']}
        tb = man['time_base_ns']
        self.assertAlmostEqual(by[1]['start_offset_s'], (f1[0][0] - tb) / NS, places=9)
        self.assertAlmostEqual(by[2]['start_offset_s'], (f2[0][0] - tb) / NS, places=9)
        # 两路起点差必须被如实保留
        self.assertAlmostEqual(
            by[1]['start_offset_s'] - by[2]['start_offset_s'],
            (f1[0][0] - f2[0][0]) / NS, places=9)
        for cid, frames in ((1, f1), (2, f2)):
            times = PREP.read_times(os.path.join(self.cache, by[cid]['times_file']))
            self.assertEqual(len(times), len(frames))
            self.assertAlmostEqual(times[0], (frames[0][0] - tb) / NS, places=9)
            self.assertAlmostEqual(times[-1], (frames[-1][0] - tb) / NS, places=9)

    # -------------------------------------------------------- 3 safe_name
    def test_safe_name_collision_topics_do_not_overwrite(self):
        """两个会映射到同一 key 的 topic，缓存文件必须互不覆盖"""
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/cam/compressed',
                  fx.build_h264_frames(12, t0_ns=BASE))
        add_video(recs, 2, 2, '/robot0/cam/compressed',
                  fx.build_h264_frames(12, t0_ns=BASE))
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        cams = man['cameras']
        self.assertEqual(len(cams), 2)
        files = [c['file'] for c in cams]
        self.assertEqual(len(set(files)), 2, files)
        for c in cams:
            fp = os.path.join(self.cache, c['file'])
            self.assertTrue(os.path.isfile(fp), fp)
            self.assertGreater(os.path.getsize(fp), 0)
        # 文件名里带通道号，天然唯一
        self.assertIn('_c1', files[0])
        self.assertIn('_c2', files[1])

    # -------------------------------------------------------- 4 缓存失效
    def test_cache_invalid_when_declared_file_missing(self):
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(15, t0_ns=BASE))
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        self.assertIsNotNone(PREP.load_manifest(self.cache, source_path=self.mcap))

        mp4 = os.path.join(self.cache, man['cameras'][0]['file'])
        os.remove(mp4)
        self.assertIsNone(PREP.load_manifest(self.cache, source_path=self.mcap))

    def test_cache_invalid_when_file_emptied(self):
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(15, t0_ns=BASE))
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        mp4 = os.path.join(self.cache, man['cameras'][0]['file'])
        open(mp4, 'wb').close()
        self.assertIsNone(PREP.load_manifest(self.cache, source_path=self.mcap))

    def test_cache_invalid_when_mtime_changes(self):
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(15, t0_ns=BASE))
        fx.assemble(self.mcap, recs)
        PREP.prepare(self.mcap, self.cache)
        st = os.stat(self.mcap)
        os.utime(self.mcap, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        self.assertIsNone(PREP.load_manifest(self.cache, source_path=self.mcap))

    def test_cache_invalid_on_schema_version_mismatch(self):
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(15, t0_ns=BASE))
        fx.assemble(self.mcap, recs)
        PREP.prepare(self.mcap, self.cache)
        import json
        mp = os.path.join(self.cache, 'manifest.json')
        with open(mp, encoding='utf-8') as fh:
            man = json.load(fh)
        man['cache_schema_version'] = 999
        with open(mp, 'w', encoding='utf-8') as fh:
            json.dump(man, fh)
        self.assertIsNone(PREP.load_manifest(self.cache, source_path=self.mcap))

    # -------------------------------------------------------- 5 并发
    def test_concurrent_prepare_runs_once(self):
        """同一缓存目录并发 prepare 只应该真正跑一次"""
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(20, t0_ns=BASE))
        fx.assemble(self.mcap, recs)

        calls = []
        real = PREP._prepare_impl

        def slow(path, outdir, progress=None, cancel_event=None, **_kw):
            calls.append(1)
            time.sleep(0.35)
            return real(path, outdir, progress, cancel_event=cancel_event)

        PREP._prepare_impl = slow
        try:
            results = []
            errors = []

            def run():
                try:
                    results.append(PREP.prepare(self.mcap, self.cache))
                except Exception as e:                     # pragma: no cover
                    errors.append(e)

            threads = [threading.Thread(target=run) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(calls), 1, '并发时重复执行了 prepare')
            self.assertEqual(len(results), 3)
            self.assertTrue(all(r is results[0] for r in results))
        finally:
            PREP._prepare_impl = real

    # -------------------------------------------------------- 6 版本与签名
    def test_manifest_version_and_signature(self):
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(10, t0_ns=BASE))
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        self.assertEqual(man['cache_schema_version'], appcache.CACHE_SCHEMA_VERSION)
        self.assertEqual(man['manifest_version'], PREP.MANIFEST_VERSION)
        st = os.stat(self.mcap)
        self.assertEqual(man['source'], os.path.abspath(self.mcap))
        self.assertEqual(man['source_signature']['size'], st.st_size)
        self.assertEqual(man['source_signature']['mtime_ns'], st.st_mtime_ns)
        self.assertEqual(man['source_signature']['mtime_ns'] % 10 ** 9,
                         stab_mtime_ns_fraction(st))

    def test_manifest_source_remains_server_compatible_string(self):
        """共享 prepare.py 不能让未修改的 server.py 对 source 调用 replace 时崩溃。"""
        frames = fx.build_h264_frames(4, t0_ns=1_000_000_000)
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed', frames)
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        self.assertIsInstance(man['source'], str)
        self.assertEqual(man['source'].replace('\\', '/'),
                         os.path.abspath(self.mcap).replace('\\', '/'))

    def test_file_id_uses_mtime_ns(self):
        """file_id 不能把 mtime 截断到秒"""
        a = os.path.join(self.dir, 'fid_a.mcap')
        b = os.path.join(self.dir, 'fid_b.mcap')
        for p in (a, b):
            with open(p, 'wb') as fh:
                fh.write(b'x' * 10)
        st = os.stat(a)
        os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns))
        st = os.stat(b)
        os.utime(b, ns=(st.st_atime_ns, st.st_mtime_ns + 1))   # 只差 1 纳秒
        self.assertNotEqual(appcache.file_id(a), appcache.file_id(b))
        os.remove(a)
        os.remove(b)

    # -------------------------------------------------------- 7 不支持的编码
    def test_unsupported_codec_marked_not_muxed(self):
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(10, t0_ns=BASE), fmt='h265')
        add_video(recs, 2, 2, '/robot0/sensor/camera1/compressed',
                  fx.build_h264_frames(10, t0_ns=BASE), fmt='h264')
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        by = {c['id']: c for c in man['cameras']}
        self.assertFalse(by[1]['playable'])
        self.assertEqual(by[1]['kind'], 'unsupported')
        self.assertIn('不支持', by[1]['error'])
        self.assertIn('h265', by[1]['error'])
        self.assertFalse(by[1].get('file'))
        self.assertTrue(by[2]['playable'])

    def test_format_change_detected(self):
        """同一通道中途换编码必须明确报错，不能静默按第一帧格式处理"""
        recs = [fx.header()]
        frames = fx.build_h264_frames(10, t0_ns=BASE)
        recs.append(fx.schema(1, 'foxglove.CompressedImage'))
        recs.append(fx.channel(1, 1, '/robot0/sensor/camera0/compressed'))
        for i, (ts, data) in enumerate(frames):
            fmt = 'h264' if i < 5 else 'h265'
            recs.append(fx.message(1, i, ts, ts, fx.compressed_image(data, fmt, 'cam')))
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        cam = man['cameras'][0]
        self.assertFalse(cam['playable'])
        self.assertIn('中途', cam['error'])
        self.assertIn('h264', cam['error'])
        self.assertIn('h265', cam['error'])

    # -------------------------------------------------------- 8 图片序列
    def test_image_sequence_channel(self):
        recs = [fx.header()]
        png = fx.make_png(1600, 1200)
        recs.append(fx.schema(1, 'foxglove.CompressedImage'))
        recs.append(fx.channel(1, 1, '/robot0/sensor/camera0/compressed'))
        for i in range(6):
            ts = BASE + i * 33_000_000
            recs.append(fx.message(1, i, ts, ts, fx.compressed_image(png, 'png', 'cam')))
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        cam = man['cameras'][0]
        self.assertTrue(cam['playable'])
        self.assertEqual(cam['kind'], 'images')
        self.assertEqual(cam['frames'], 6)
        self.assertEqual(len(cam['frames_list']), 6)
        for fn in cam['frames_list']:
            self.assertTrue(os.path.isfile(os.path.join(self.cache, cam['dir'], fn)))
        times = PREP.read_times(os.path.join(self.cache, cam['times_file']))
        self.assertEqual(len(times), 6)

    # -------------------------------------------------------- 9 多通道音频
    def test_multi_channel_audio_not_merged(self):
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(10, t0_ns=BASE))
        add_audio(recs, 2, 2, '/robot0/sensor/audio', BASE, sample_rate=16000, channels=2)
        add_audio(recs, 3, 3, '/robot0/sensor/audio2', BASE, sample_rate=48000, channels=1)
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        a = man['audio']
        self.assertEqual(a['sample_rate'], 16000)
        self.assertEqual(a['channels'], 2)
        self.assertEqual(len(a['other_channels']), 1)
        self.assertIn('note', a)
        with wave.open(os.path.join(self.cache, a['file']), 'rb') as wf:
            self.assertEqual(wf.getframerate(), 16000)
            self.assertEqual(wf.getnchannels(), 2)

    def test_non_pcm16_audio_is_not_written_as_fake_wav(self):
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(4, t0_ns=BASE))
        recs.append(fx.schema(2, 'foxglove.AudioData'))
        recs.append(fx.channel(2, 2, '/robot0/sensor/audio'))
        body = fx.audio_message(b'not-pcm', bit_depth=8, audio_format='AAC')
        recs.append(fx.message(2, 0, BASE, BASE, body))
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        self.assertFalse(man['audio']['playable'])
        self.assertNotIn('file', man['audio'])
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'audio.wav')))

    def test_multi_imu_uses_first(self):
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(10, t0_ns=BASE))
        add_imu(recs, 2, 2, '/robot0/sensor/imu', BASE, count=10)
        add_imu(recs, 3, 3, '/robot0/sensor/imu2', BASE, count=7)
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        self.assertEqual(man['imu']['count'], 10)
        self.assertEqual(len(man['imu']['other_channels']), 1)
        self.assertIn('note', man['imu'])

    # -------------------------------------------------------- 10 标定关联
    def test_calibration_attached_to_camera(self):
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(10, t0_ns=BASE))
        recs.append(fx.schema(9, 'foxglove.CameraCalibration'))
        recs.append(fx.channel(9, 9, '/robot0/sensor/camera0/camera_info'))
        body = (fx.pb_int(2, 1600) + fx.pb_int(3, 1300) + fx.pb_str(4, 'ds') +
                fx.pb_packed_doubles(6, [500.0, 0.0, 800.0,
                                         0.0, 500.0, 640.0, 0.0, 0.0, 1.0]))
        recs.append(fx.message(9, 0, BASE, BASE, body))
        fx.assemble(self.mcap, recs)
        man = PREP.prepare(self.mcap, self.cache)
        cal = man['cameras'][0]['calibration']
        self.assertIsNotNone(cal)
        self.assertEqual(cal['width'], 1600)
        self.assertEqual(cal['distortion_model'], 'ds')

    # -------------------------------------------------------- 11 staging 发布
    def test_staging_dir_removed_after_publish(self):
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(10, t0_ns=BASE))
        fx.assemble(self.mcap, recs)
        PREP.prepare(self.mcap, self.cache)
        self.assertFalse(os.path.isdir(self.cache + '.staging'))
        self.assertTrue(os.path.isfile(os.path.join(self.cache, 'manifest.json')))

    def test_publish_failure_restores_previous_complete_cache(self):
        stage = self.cache + '.staging'
        os.makedirs(self.cache)
        os.makedirs(stage)
        with open(os.path.join(self.cache, 'old.marker'), 'wb') as fh:
            fh.write(b'old')
        with open(os.path.join(stage, 'new.marker'), 'wb') as fh:
            fh.write(b'new')
        real_replace = PREP.os.replace

        def fail_new_publish(src, dst):
            if os.path.abspath(src) == os.path.abspath(stage):
                raise OSError('simulated publish failure')
            return real_replace(src, dst)

        with mock.patch.object(PREP.os, 'replace', side_effect=fail_new_publish):
            with self.assertRaises(OSError):
                PREP._publish(stage, self.cache)
        self.assertTrue(os.path.isfile(os.path.join(self.cache, 'old.marker')))
        self.assertFalse(os.path.exists(self.cache + '.old'))

    def test_failed_prepare_leaves_no_cache(self):
        """解析失败不能留下半截缓存目录"""
        recs = [fx.header()]        # 没有任何视频通道
        add_imu(recs, 2, 2, '/robot0/sensor/imu', BASE, count=3)
        fx.assemble(self.mcap, recs)
        with self.assertRaises(Exception):
            PREP.prepare(self.mcap, self.cache)
        self.assertFalse(os.path.isdir(self.cache))
        self.assertFalse(os.path.isdir(self.cache + '.staging'))

    def test_cancelled_prepare_leaves_no_partial_cache(self):
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera0/compressed',
                  fx.build_h264_frames(20, t0_ns=BASE))
        fx.assemble(self.mcap, recs)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(CancelledError):
            PREP.prepare(self.mcap, self.cache, cancel_event=stop)
        self.assertFalse(os.path.isdir(self.cache))
        self.assertFalse(os.path.isdir(self.cache + '.staging'))


def stab_mtime_ns_fraction(st):
    return st.st_mtime_ns % 10 ** 9


if __name__ == '__main__':
    unittest.main()
