"""P1.6D-R2B：桌面精简缓存「IMU 消除」合同测试

覆盖：Desktop wanted topics 不含 IMU；IMU 计数/解码/accumulator 全为 0/None；
imu.json 不生成；mixed chunk 保视频；no-index single-pass；full 保留 IMU；
时间轴独立于 IMU；旧 v2 退休清理；env 不可改写合同。
"""

import os
import shutil
import unittest
from unittest import mock

from tests import mcapfix as fx
import appcache
import mcap_reader as MR
import prepare as PREP

BASE = 1_700_000_000_000_000_000
NS = 1_000_000_000


def _mcap(path, with_imu=True, summary=True):
    recs = [fx.header()]
    recs.append(fx.schema(1, 'foxglove.CompressedImage'))
    recs.append(fx.channel(1, 1, '/robot0/sensor/camera2/compressed'))
    for i, (ts, data) in enumerate(
            fx.build_h264_frames(5, fps=30.0, t0_ns=BASE, idr_every=1)):
        recs.append(fx.message(1, i, ts, ts,
                               fx.compressed_image(data, 'h264', 'cam')))
    recs.append(fx.schema(2, 'foxglove.IMUMeasurement'))
    recs.append(fx.channel(2, 2, '/robot0/sensor/imu'))
    for i in range(12):
        ts = BASE + i * 5_000_000
        recs.append(fx.message(2, i, ts, ts, fx.imu_message(ts)))
    chunk = fx.chunk(recs[1:], start_ns=BASE, end_ns=BASE + 300_000_000)
    srecs = [fx.schema(1, 'foxglove.CompressedImage'),
             fx.channel(1, 1, '/robot0/sensor/camera2/compressed'),
             fx.channel(2, 2, '/robot0/sensor/imu'),
             fx.statistics(17, 1, 2, 1, BASE, BASE + 300_000_000, {1: 5, 2: 12})]
    fx.assemble(path, [fx.header(), chunk],
                srecs if summary else None)
    return path


class ImuPolicyCase(unittest.TestCase):
    def test_desktop_drops_imu(self):
        self.assertFalse(appcache.profile_keeps_imu(appcache.CACHE_PROFILE))
        self.assertFalse(appcache.profile_keeps_audio(appcache.CACHE_PROFILE))

    def test_full_and_legacy_keep_imu(self):
        for prof in (None, '', 'full', 'server', 'debug', 'legacy'):
            self.assertTrue(appcache.profile_keeps_imu(prof), repr(prof))

    def test_profile_key_videoonly_and_retired(self):
        self.assertIn('videoonly', appcache.CACHE_PROFILE)
        self.assertEqual(appcache.RETIRED_DESKTOP_PROFILES,
                         ('desktop_camera2_camera3_v1',
                          'desktop_camera2_camera3_noaudio_v2'))
        key = appcache.cache_key('0123456789abcdef')
        self.assertEqual(appcache.profile_of(key), appcache.CACHE_PROFILE)


class ImuEliminationCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fx.temp_dir('imu-elim')

    def setUp(self):
        self.mcap = os.path.join(self.dir, self._testMethodName + '.mcap')
        self.cache = os.path.join(self.dir, self._testMethodName + '.cache')

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def _desktop_prepare(self, path, **kw):
        return PREP.prepare(path, self.cache,
                            profile=appcache.CACHE_PROFILE,
                            camera_pred=appcache.profile_keeps_topic,
                            **kw)

    def test_wanted_channels_exclude_imu(self):
        """Reader 订阅集合不含 IMU（从入口删除，不是读出来再丢）"""
        path = _mcap(self.mcap)
        seen = {}
        real = MR.McapReader.iter_messages

        def spy(self, wanted=None, **kw):
            seen['wanted'] = None if wanted is None else set(wanted)
            return real(self, wanted, **kw)

        with mock.patch.object(MR.McapReader, 'iter_messages', spy):
            self._desktop_prepare(path)
        imu_ids = {cid for cid, ch in MR.McapReader(path).channels.items()
                   if 'imu' in ch['topic']}
        self.assertTrue(imu_ids)
        self.assertFalse(seen['wanted'] & imu_ids,
                         '订阅集合不得含 IMU 通道：%s' % (seen['wanted'] & imu_ids))
        self.assertTrue(any('camera2' in c['topic']
                            for cid, c in MR.McapReader(path).channels.items()
                            if cid in seen['wanted']))

    def test_no_imu_artifact_and_zero_counters(self):
        path = _mcap(self.mcap)
        man = self._desktop_prepare(path)
        perf = man.get('_perf') or {}
        hp = perf.get('hotpath') or {}
        self.assertIsNone(man.get('imu'), 'Desktop 合同：无 IMU 产物')
        self.assertFalse(os.path.isfile(os.path.join(self.cache, 'imu.json')))
        self.assertFalse(os.path.isfile(
            os.path.join(self.cache, '.imu_packets_2.tmp')))
        self.assertEqual(perf.get('imu_messages'), 0)
        self.assertEqual(hp.get('imu_selected_messages'), 0)
        self.assertEqual(hp.get('imu_decode_calls'), 0)
        self.assertEqual(hp.get('imu_finalize_ms'), 0.0)
        self.assertEqual(hp.get('imu_output_bytes'), 0)
        # 视频不受影响
        self.assertEqual(len(man['cameras']), 1)
        self.assertEqual(man['cameras'][0]['frames'], 5)
        self.assertIsNotNone(appcache.load_manifest(self.cache, source_path=path))

    def test_full_profile_still_writes_imu(self):
        path = _mcap(self.mcap)
        man = PREP.prepare(path, self.cache, profile='full')
        self.assertIsNotNone(man['imu'])
        self.assertTrue(os.path.isfile(os.path.join(self.cache, 'imu.json')))
        self.assertEqual(man['imu']['count'], 12)

    def test_no_index_single_pass_skips_imu(self):
        path = _mcap(os.path.join(self.dir, 'noidx.mcap'), summary=False)
        man = self._desktop_prepare(path)
        perf = man.get('_perf') or {}
        hp = perf.get('hotpath') or {}
        self.assertTrue(perf.get('lazy_single_pass'))
        self.assertEqual(perf.get('imu_messages'), 0)
        self.assertEqual(hp.get('imu_decode_calls'), 0)
        self.assertFalse(os.path.isfile(os.path.join(self.cache, 'imu.json')))
        self.assertEqual(len(man['cameras']), 1)

    def test_timeline_independent_from_imu(self):
        """时间轴（duration/time_base）来自视频与 manifest，与 IMU 无关"""
        p_on = _mcap(os.path.join(self.dir, 'tl_on.mcap'))
        p_off = _mcap(os.path.join(self.dir, 'tl_off.mcap'))
        c_on = os.path.join(self.dir, 'tl_on.cache')
        c_off = os.path.join(self.dir, 'tl_off.cache')
        try:
            m_on = PREP.prepare(p_on, c_on, profile='full',
                                camera_pred=appcache.profile_keeps_topic)
            m_off = self._desktop_prepare(p_off)
            self.assertEqual(m_on['duration_s'], m_off['duration_s'])
            self.assertEqual(m_on['time_base_ns'], m_off['time_base_ns'])
            self.assertEqual(m_on['cameras'][0]['frames'],
                             m_off['cameras'][0]['frames'])
            self.assertEqual(m_on['cameras'][0]['start_offset_s'],
                             m_off['cameras'][0]['start_offset_s'])
        finally:
            shutil.rmtree(c_on, ignore_errors=True)
            shutil.rmtree(c_off, ignore_errors=True)

    def test_env_override_cannot_break_contract(self):
        os.environ['MCAPVIEWER_KEEP_IMU'] = '1'
        try:
            path = _mcap(self.mcap)
            man = self._desktop_prepare(path)
            self.assertFalse(man['_perf'].get('keep_imu'),
                             '环境变量不得改写 videoonly_v3 合同')
            self.assertFalse(os.path.isfile(os.path.join(self.cache, 'imu.json')))
        finally:
            os.environ.pop('MCAPVIEWER_KEEP_IMU', None)


class RetiredV2Case(unittest.TestCase):
    """noaudio_v2 退休：不复用、不占槽位、可安全清理；v1 与其它 profile 不受影响"""

    @classmethod
    def setUpClass(cls):
        cls.dir = fx.temp_dir('retired-imu')

    def setUp(self):
        self.old_root = appcache.CACHE_ROOT
        self.root = fx.temp_dir('retired2-root-%d' % id(self))
        appcache.CACHE_ROOT = self.root

    def tearDown(self):
        appcache.CACHE_ROOT = self.old_root
        shutil.rmtree(self.root, ignore_errors=True)

    def _make(self, profile):
        d = os.path.join(self.root, '0123456789abcdef@%s' % profile)
        os.makedirs(d)
        with open(os.path.join(d, 'junk.bin'), 'wb') as fh:
            fh.write(b'x' * 512)
        return d

    def test_v2_listed_as_retired(self):
        d = self._make('desktop_camera2_camera3_noaudio_v2')
        self.assertIn(os.path.basename(d), appcache.retired_desktop_entries())

    def test_v2_purged(self):
        d = self._make('desktop_camera2_camera3_noaudio_v2')
        res = appcache.purge_retired_desktop_caches(running_jobs=[])
        self.assertEqual(len(res['deleted']), 1)
        self.assertFalse(os.path.isdir(d))

    def test_v1_still_purged(self):
        d = self._make('desktop_camera2_camera3_v1')
        res = appcache.purge_retired_desktop_caches(running_jobs=[])
        self.assertEqual(len(res['deleted']), 1)
        self.assertFalse(os.path.isdir(d))

    def test_v2_protected_when_running(self):
        d = self._make('desktop_camera2_camera3_noaudio_v2')
        key = os.path.basename(d)
        res = appcache.purge_retired_desktop_caches(running_jobs=[key])
        self.assertIn(key, res['skipped'])
        self.assertTrue(os.path.isdir(d))

    def test_active_and_full_untouched(self):
        keep_v3 = self._make(appcache.CACHE_PROFILE)
        keep_full = self._make('full')
        keep_legacy = os.path.join(self.root, '9999999999999999')
        os.makedirs(keep_legacy)
        res = appcache.purge_retired_desktop_caches(running_jobs=[])
        self.assertEqual(res['deleted'], [])
        for k in (keep_v3, keep_full, keep_legacy):
            self.assertTrue(os.path.isdir(k))


if __name__ == '__main__':
    unittest.main()
