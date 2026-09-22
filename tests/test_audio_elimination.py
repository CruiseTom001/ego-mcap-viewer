"""P1.6D-R2A：桌面精简缓存「音频消除」合同测试

覆盖：
  A. Desktop profile 的订阅集合（wanted channel）不含任何 audio 通道
  B. chunk 级候选选择：audio-only chunk 在 Desktop 白名单下被排除
  C. 混合 chunk（camera2 + audio）：camera2 正常处理，audio 被忽略
  D. 无索引文件：仍保持 single-pass，且不做 audio decode/collect
  E. 最终缓存：不生成 audio.wav、无音频临时 spool，其余产物齐全
  F. A/B 开关：MCAPVIEWER_KEEP_AUDIO=1 时（仅对照用）音频链路恢复
  G. 非桌面 profile（full / None）仍保留音频（server 路径不受影响）
"""

import os
import json
import shutil
import unittest
from unittest import mock

from tests import mcapfix as fx
import appcache
import mcap_reader as MR
import prepare as PREP

NS = 1_000_000_000
BASE = 1_700_000_000_000_000_000
BIG_INDEXED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           '..', 'tmp', 'big_2gb.mcap')


def add_video(recs, cid, sid, topic, frames):
    recs.append(fx.schema(sid, 'foxglove.CompressedImage'))
    recs.append(fx.channel(cid, sid, topic))
    for i, (ts, data) in enumerate(frames):
        recs.append(fx.message(cid, i, ts, ts,
                               fx.compressed_image(data, 'h264', topic)))


def add_audio(recs, cid, sid, topic, nsamples=4):
    recs.append(fx.schema(sid, 'foxglove.AudioData'))
    recs.append(fx.channel(cid, sid, topic))
    for i in range(nsamples):
        ts = BASE + i * 64_000_000
        recs.append(fx.message(cid, i, ts, ts,
                               fx.audio_message(b'\x01\x02' * 512, 16000, 2, seq=i)))


class AudioPolicyCase(unittest.TestCase):
    """A / G：profile 级的音频策略"""

    def test_desktop_profile_drops_audio(self):
        self.assertFalse(appcache.profile_keeps_audio(appcache.CACHE_PROFILE))

    def test_full_and_legacy_profiles_keep_audio(self):
        for prof in (None, '', 'full', 'server', 'debug', 'legacy'):
            self.assertTrue(appcache.profile_keeps_audio(prof), repr(prof))

    def test_profile_key_reflects_videoonly_contract(self):
        key = appcache.cache_key('0123456789abcdef')
        self.assertEqual(appcache.profile_of(key), appcache.CACHE_PROFILE)
        self.assertIn('videoonly', appcache.CACHE_PROFILE,
                      'profile 名必须体现"只缓存视频"合同（无音频/无 IMU）')
        self.assertNotEqual(appcache.CACHE_PROFILE,
                            'desktop_camera2_camera3_v1')
        self.assertNotEqual(appcache.CACHE_PROFILE,
                            'desktop_camera2_camera3_noaudio_v2')
        for old in appcache.RETIRED_DESKTOP_PROFILES:
            self.assertNotIn(old, appcache.ACTIVE_DESKTOP_PROFILES)


class DesktopCacheCase(unittest.TestCase):
    """A/C/D/E/F：Desktop profile 的实际缓存行为"""

    @classmethod
    def setUpClass(cls):
        cls.dir = fx.temp_dir('audio-elim')

    def setUp(self):
        self.mcap = os.path.join(self.dir, self._testMethodName + '.mcap')
        self.cache = os.path.join(self.dir, self._testMethodName + '.cache')

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def _mixed_mcap(self):
        """camera2/camera3/IMU/audio 各若干消息（音频与视频在同一 chunk）"""
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera2/compressed',
                  fx.build_h264_frames(5, fps=30.0, t0_ns=BASE, idr_every=1))
        add_video(recs, 2, 1, '/robot0/sensor/camera3/compressed',
                  fx.build_h264_frames(5, fps=30.0, t0_ns=BASE, idr_every=1))
        recs.append(fx.schema(3, 'foxglove.IMUMeasurement'))
        recs.append(fx.channel(3, 3, '/robot0/sensor/imu'))
        for i in range(10):
            ts = BASE + i * 5_000_000
            recs.append(fx.message(3, i, ts, ts, fx.imu_message(ts)))
        add_audio(recs, 4, 4, '/robot0/sensor/audio')
        chunk = fx.chunk(recs[1:], start_ns=BASE, end_ns=BASE + 300_000_000)
        summary = [fx.schema(1, 'foxglove.CompressedImage'),
                   fx.channel(1, 1, '/robot0/sensor/camera2/compressed'),
                   fx.channel(2, 1, '/robot0/sensor/camera3/compressed'),
                   fx.channel(3, 3, '/robot0/sensor/imu'),
                   fx.channel(4, 4, '/robot0/sensor/audio'),
                   fx.statistics(24, 1, 4, 1, BASE, BASE + 300_000_000,
                                 {1: 5, 2: 5, 3: 10, 4: 4})]
        fx.assemble(self.mcap, [fx.header(), chunk], summary)
        return self.mcap

    def _desktop_prepare(self, path, keep_audio=None):
        """桌面精简缓存（keep_audio=None 按 profile 合同；True 仅 A/B 对照用）"""
        return PREP.prepare(path, self.cache,
                            profile=appcache.CACHE_PROFILE,
                            camera_pred=appcache.profile_keeps_topic,
                            keep_audio=keep_audio)

    def test_camera_ok_and_no_audio_artifact(self):
        path = self._mixed_mcap()
        man = self._desktop_prepare(path)
        # C：混合 chunk 里 camera2/camera3 正常、音频被忽略
        self.assertEqual(len(man['cameras']), 2)
        for cam in man['cameras']:
            self.assertTrue(cam['playable'])
            self.assertEqual(cam['frames'], 5, 'idr_every=1 → 首帧即 IDR，不丢帧')
        # E：缓存目录内容
        self.assertIsNone(man['audio'], 'Desktop manifest 不得声明音频产物')
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'audio.wav')))
        self.assertEqual([p for p in os.listdir(self.cache)
                          if p.startswith('.audio_packets_')], [],
                         '不得残留音频 spool 临时文件')
        self.assertTrue(os.path.isfile(os.path.join(self.cache, 'manifest.json')))
        # P1.6D-R2B：Desktop videoonly 不再生成 imu.json
        self.assertFalse(os.path.isfile(os.path.join(self.cache, 'imu.json')))
        self.assertIsNone(man['imu'], 'Desktop 合同：无 IMU 产物')
        self.assertEqual(man['cache_profile'], appcache.CACHE_PROFILE)
        # validator：manifest 未声明音频 → 合法
        self.assertIsNotNone(appcache.load_manifest(self.cache, source_path=path))
        perf = man.get('_perf') or {}
        hp = perf.get('hotpath') or {}
        self.assertFalse(perf.get('keep_audio'))
        self.assertEqual(perf.get('audio_messages'), 0)
        self.assertEqual(hp.get('audio_selected_messages'), 0)
        self.assertEqual(hp.get('audio_decode_ms'), 0.0)
        self.assertEqual(hp.get('audio_finalize_ms'), 0.0)
        self.assertEqual(hp.get('audio_output_bytes'), 0)
        self.assertFalse(hp.get('audio_wav_written'))

    def test_wanted_channels_exclude_audio(self):
        """A：音频必须从 Reader 订阅集合里就删掉（而非读出来再丢）"""
        path = self._mixed_mcap()
        seen = {}
        real = MR.McapReader.iter_messages

        def spy(self, wanted=None, **kw):
            seen['wanted'] = None if wanted is None else set(wanted)
            seen['pushdown'] = kw.get('pushdown')
            seen['collect'] = kw.get('collect')
            return real(self, wanted, **kw)

        with mock.patch.object(MR.McapReader, 'iter_messages', spy):
            self._desktop_prepare(path)
        audio_ids = {cid for cid, ch in MR.McapReader(path).channels.items()
                     if 'audio' in ch['topic']}
        self.assertTrue(audio_ids, '样本文件里应有音频通道')
        self.assertIsNotNone(seen.get('wanted'), '有索引时应传精确的订阅集合')
        self.assertFalse(seen['wanted'] & audio_ids,
                         '订阅集合里不得出现音频通道：%s' % (seen['wanted'] & audio_ids))

    def test_keep_audio_switch_restores_audio(self):
        """F：MCAPVIEWER_KEEP_AUDIO 对照开关（仅用于 A/B 实测）"""
        path = self._mixed_mcap()
        man = self._desktop_prepare(path, keep_audio=True)
        self.assertIsNotNone(man['audio'], '开关打开时音频链路恢复')
        self.assertTrue(os.path.isfile(os.path.join(self.cache, 'audio.wav')))
        perf = man.get('_perf') or {}
        self.assertEqual(perf.get('audio_messages'), 4)
        self.assertTrue(perf.get('keep_audio'))

    def test_full_profile_still_writes_audio(self):
        """G：非桌面 profile（server/完整缓存）行为不变"""
        path = self._mixed_mcap()
        man = PREP.prepare(path, self.cache, profile='full')
        self.assertIsNotNone(man['audio'])
        self.assertTrue(os.path.isfile(os.path.join(self.cache, 'audio.wav')))
        self.assertEqual(man['cache_profile'], 'full')

    def test_no_index_single_pass_skips_audio(self):
        """D：无索引文件保持 single-pass，且不做音频 decode/collect/write"""
        path = os.path.join(self.dir, 'noidx.mcap')
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera2/compressed',
                  fx.build_h264_frames(5, fps=30.0, t0_ns=BASE, idr_every=1))
        add_audio(recs, 4, 4, '/robot0/sensor/audio')
        chunk = fx.chunk(recs[1:], start_ns=BASE, end_ns=BASE + 300_000_000)
        fx.assemble(path, [fx.header(), chunk], summary_records=None)
        man = self._desktop_prepare(path)
        perf = man.get('_perf') or {}
        hp = perf.get('hotpath') or {}
        self.assertTrue(perf.get('lazy_single_pass'), '无索引文件仍应单遍扫描')
        self.assertEqual(perf.get('audio_messages'), 0)
        self.assertEqual(hp.get('audio_decode_ms'), 0.0)
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'audio.wav')))
        self.assertEqual(len(man['cameras']), 1)

    def test_cancel_leaves_no_audio_leftover(self):
        """异常/取消：不得残留 audio.wav 或音频 spool（staging 整体清理）"""
        path = self._mixed_mcap()
        calls = {'n': 0}

        def boom(_ev):
            calls['n'] += 1
            if calls['n'] > 8:          # 前几次正常，随后在流程中途取消
                raise RuntimeError('cancel for test')

        with mock.patch.object(PREP, '_check_cancel', boom):
            with self.assertRaises(RuntimeError):
                self._desktop_prepare(path)
        self.assertGreater(calls['n'], 8, '取消应发生在流程进行中')
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'audio.wav')))
        self.assertFalse(os.path.isdir(self.cache + '.staging'),
                         'staging 目录必须被清理')
        self.assertEqual([p for p in (os.listdir(self.cache)
                                      if os.path.isdir(self.cache) else [])
                          if p.startswith('.audio_packets_')], [])


class ChunkSkipCase(unittest.TestCase):
    """B：chunk 级候选选择（真实带索引样本上有 audio-only chunk 时跳过）"""

    def setUp(self):
        self.path = os.path.abspath(BIG_INDEXED)
        if not os.path.isfile(self.path):
            self.skipTest('缺少 2GB 固定性能样本 %s' % self.path)

    def test_audio_excluded_from_candidate_topics(self):
        import chunk_streaming_reader as CSR
        r = MR.McapReader(self.path)
        summary = r._official_summary
        self.assertIsNotNone(summary, '样本应带 Summary/ChunkIndex')
        audio_topics = {ch['topic'] for ch in r.channels.values()
                        if 'audio' in ch['topic']}
        self.assertTrue(audio_topics)
        keep_topics = {ch['topic'] for ch in r.channels.values()
                       if 'audio' not in ch['topic']}
        with_audio = CSR.candidate_chunks(summary, topics=keep_topics | audio_topics)
        without_audio = CSR.candidate_chunks(summary, topics=keep_topics)
        self.assertLessEqual(len(without_audio), len(with_audio),
                             '去掉音频白名单后候选 chunk 不应变多')
        self.assertGreater(len(without_audio), 0)
        # 候选集合必须是 with_audio 的子集（保守跳过的只能是音频相关块）
        set_wo = {int(c.chunk_start_offset) for c in without_audio}
        set_wa = {int(c.chunk_start_offset) for c in with_audio}
        self.assertTrue(set_wo <= set_wa)


class RetiredProfileCase(unittest.TestCase):
    """旧 v1 桌面缓存：不复用、不占 active 槽位、可安全退休清理"""

    @classmethod
    def setUpClass(cls):
        cls.dir = fx.temp_dir('retired')

    def setUp(self):
        self.old_root = appcache.CACHE_ROOT
        self.root = fx.temp_dir('retired-root-%d' % id(self))
        appcache.CACHE_ROOT = self.root

    def tearDown(self):
        appcache.CACHE_ROOT = self.old_root
        shutil.rmtree(self.root, ignore_errors=True)

    def _make_v1(self, fid='0123456789abcdef'):
        d = os.path.join(self.root, '%s@desktop_camera2_camera3_v1' % fid)
        os.makedirs(d)
        with open(os.path.join(d, 'junk.bin'), 'wb') as fh:
            fh.write(b'x' * 1024)
        return d

    def test_retired_listed_but_not_in_ledger(self):
        d = self._make_v1()
        self.assertIn(os.path.basename(d), appcache.retired_desktop_entries())
        entries = appcache.list_cache_entries()
        self.assertFalse(any('desktop_camera2_camera3_v1' in (e.get('key') or '')
                             for e in entries),
                         '退休缓存不得进入 active 台账/槽位')

    def test_purge_removes_retired_v1(self):
        d = self._make_v1()
        res = appcache.purge_retired_desktop_caches(running_jobs=[])
        self.assertEqual(len(res['deleted']), 1)
        self.assertFalse(os.path.isdir(d), '退休缓存应被安全清理')

    def test_purge_protects_running_jobs(self):
        d = self._make_v1()
        key = os.path.basename(d)
        res = appcache.purge_retired_desktop_caches(running_jobs=[key])
        self.assertIn(key, res['skipped'])
        self.assertTrue(os.path.isdir(d), '正在使用的退休目录必须保留')

    def test_purge_does_not_touch_other_profiles(self):
        keep1 = os.path.join(self.root, '0123456789abcdef')       # legacy 无后缀
        os.makedirs(keep1)
        keep2 = os.path.join(self.root, '1111111111111111@full')  # server/full
        os.makedirs(keep2)
        keep3 = os.path.join(self.root,
                             '2222222222222222@' + appcache.CACHE_PROFILE)
        os.makedirs(keep3)                                        # active 桌面
        res = appcache.purge_retired_desktop_caches(running_jobs=[])
        self.assertEqual(res['deleted'], [], '不得误删其它 profile')
        for k in (keep1, keep2, keep3):
            self.assertTrue(os.path.isdir(k))


class SafetyCase(unittest.TestCase):
    """取消 / 坏 CRC / 截断：Desktop noaudio 一样安全（无残留）"""

    @classmethod
    def setUpClass(cls):
        cls.dir = fx.temp_dir('audio-safety')

    def setUp(self):
        self.mcap = os.path.join(self.dir, self._testMethodName + '.mcap')
        self.cache = os.path.join(self.dir, self._testMethodName + '.cache')

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def _mcap(self, bad_crc=False, truncate=None):
        recs = [fx.header()]
        add_video(recs, 1, 1, '/robot0/sensor/camera2/compressed',
                  fx.build_h264_frames(6, fps=30.0, t0_ns=BASE, idr_every=1))
        add_audio(recs, 4, 4, '/robot0/sensor/audio')
        chunk = fx.chunk(recs[1:], start_ns=BASE, end_ns=BASE + 300_000_000,
                         crc_override=(0xDEADBEEF if bad_crc else None))
        summary = [fx.schema(1, 'foxglove.CompressedImage'),
                   fx.channel(1, 1, '/robot0/sensor/camera2/compressed'),
                   fx.channel(4, 4, '/robot0/sensor/audio'),
                   fx.statistics(10, 1, 2, 1, BASE, BASE + 300_000_000,
                                 {1: 6, 4: 4})]
        fx.assemble(self.mcap, [fx.header(), chunk], summary,
                    truncate_to=truncate)
        return self.mcap

    def _assert_no_residue(self):
        self.assertFalse(os.path.isdir(self.cache + '.staging'),
                         'staging 必须被清理')
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'audio.wav')))
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'manifest.json')))

    def test_corrupted_payload_fails_cleanly(self):
        """磁盘位翻转（破坏 Chunk CRC）：必须失败且无残留"""
        path = self._mcap()
        with open(path, 'r+b') as fh:
            data = bytearray(fh.read())
            # 翻转 chunk 数据区内的一个字节（header 之后不远即是 chunk 记录；
            # 文件尾部的 summary/footer 区有自己的可选项，翻转那里不影响数据）
            pos = 200
            assert pos < len(data)
            data[pos] ^= 0xFF
            fh.seek(pos)
            fh.write(bytes(data[pos:pos + 1]))
        with self.assertRaises(MR.McapError):
            PREP.prepare(path, self.cache, profile=appcache.CACHE_PROFILE,
                         camera_pred=appcache.profile_keeps_topic)
        self._assert_no_residue()

    def test_truncated_file_fails_cleanly(self):
        self._mcap(truncate=4096)
        with self.assertRaises(Exception):
            PREP.prepare(self.mcap, self.cache, profile=appcache.CACHE_PROFILE,
                         camera_pred=appcache.profile_keeps_topic)
        self._assert_no_residue()

    def test_cancel_leaves_no_residue(self):
        path = self._mcap()
        calls = {'n': 0}

        def boom(_ev):
            calls['n'] += 1
            if calls['n'] > 6:
                raise RuntimeError('cancel')
        with mock.patch.object(PREP, '_check_cancel', boom):
            with self.assertRaises(RuntimeError):
                PREP.prepare(path, self.cache, profile=appcache.CACHE_PROFILE,
                             camera_pred=appcache.profile_keeps_topic)
        self._assert_no_residue()

    def test_env_override_cannot_break_noaudio_contract(self):
        """生产合同收口：环境变量 MCAPVIEWER_KEEP_AUDIO 不再生效"""
        os.environ['MCAPVIEWER_KEEP_AUDIO'] = '1'
        try:
            path = self._mcap()
            man = PREP.prepare(path, self.cache,
                               profile=appcache.CACHE_PROFILE,
                               camera_pred=appcache.profile_keeps_topic)
            perf = man.get('_perf') or {}
            self.assertFalse(perf.get('keep_audio'),
                             '环境变量不得改写 noaudio_v2 的产物合同')
            self.assertFalse(os.path.exists(
                os.path.join(self.cache, 'audio.wav')))
        finally:
            os.environ.pop('MCAPVIEWER_KEEP_AUDIO', None)


if __name__ == '__main__':
    unittest.main()
