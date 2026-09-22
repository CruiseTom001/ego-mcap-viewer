"""读取层优化（OPT-01/02/03/04）的语义与行为测试

覆盖：
  * OPT-01 订阅集合下推：库层过滤与库外过滤结果必须完全一致
  * OPT-02 log_time_order=False：不丢消息、按文件顺序输出，
          且乱序 index 在收尾时会被重排（times 保持单调）
  * OPT-03 无索引文件单遍：lazy 构造不扫描；collect=True 遍历补齐元信息；
          prepare 对无索引文件也能正常出缓存
  * OPT-04 profiling：缓存流程会产出分阶段耗时记录
"""

import os
import json
import shutil
import struct
import unittest

from tests import mcapfix as fx
import mcap_reader as MR
import prepare as PREP
import appcache

NS = 1_000_000_000
BASE = 1_600_000_000_000_000_000


def _video_recs(cid, sid, topic, frames, schema='foxglove.CompressedImage'):
    recs = [fx.schema(sid, schema), fx.channel(cid, sid, topic)]
    for i, (ts, data) in enumerate(frames):
        recs.append(fx.message(cid, i, ts, ts,
                               fx.compressed_image(data, 'h264', topic)))
    return recs


class ReadLayerCase(unittest.TestCase):
    """OPT-01 / OPT-02：读取层过滤与顺序语义"""

    @classmethod
    def setUpClass(cls):
        cls.dir = fx.temp_dir('perfopt')

    def _two_channel_file(self):
        """cam2 四帧 + cam3 四帧（分两个 chunk），时间戳交错"""
        path = os.path.join(self.dir, self._testMethodName + '.mcap')
        f2 = fx.build_h264_frames(4, fps=30.0, t0_ns=BASE, idr_every=1)
        f3 = fx.build_h264_frames(4, fps=30.0, t0_ns=BASE + 16_000_000,
                                  idr_every=1)
        recs = [fx.header()]
        recs += _video_recs(1, 1, '/robot0/sensor/camera2/compressed', f2)
        recs += _video_recs(2, 1, '/robot0/sensor/camera3/compressed', f3)
        chunk = fx.chunk(recs[1:], compression='', start_ns=BASE,
                         end_ns=BASE + 200_000_000)
        summary = [
            fx.schema(1, 'foxglove.CompressedImage'),
            fx.channel(1, 1, '/robot0/sensor/camera2/compressed'),
            fx.channel(2, 1, '/robot0/sensor/camera3/compressed'),
            fx.statistics(8, 1, 2, 1, BASE, BASE + 200_000_000, {1: 4, 2: 4}),
        ]
        fx.assemble(path, [fx.header(), chunk], summary)
        return path

    def _out_of_order_file(self):
        """文件顺序 = chunk(晚时间) 在前、chunk(早时间) 在后（模拟被合并/重写过的文件）"""
        path = os.path.join(self.dir, self._testMethodName + '.mcap')
        late = fx.build_h264_frames(3, fps=30.0, t0_ns=BASE + 10 * NS,
                                    idr_every=1)
        early = fx.build_h264_frames(3, fps=30.0, t0_ns=BASE, idr_every=1)
        recs_late = _video_recs(1, 1, '/robot0/sensor/camera2/compressed', late)
        recs_early = _video_recs(1, 1, '/robot0/sensor/camera2/compressed', early)
        chunk_late = fx.chunk(recs_late, start_ns=BASE + 10 * NS,
                              end_ns=BASE + 10 * NS + NS)
        chunk_early = fx.chunk(recs_early, start_ns=BASE,
                               end_ns=BASE + NS)
        summary = [
            fx.schema(1, 'foxglove.CompressedImage'),
            fx.channel(1, 1, '/robot0/sensor/camera2/compressed'),
            fx.statistics(6, 1, 1, 2, BASE, BASE + 11 * NS, {1: 6}),
        ]
        fx.assemble(path, [fx.header(), chunk_late, chunk_early], summary)
        return path

    def test_pushdown_filter_matches_library_level_filter(self):
        path = self._two_channel_file()
        r = MR.McapReader(path)
        ids = set(r.channels.keys())
        self.assertTrue(ids)
        cam2 = [c['id'] for c in r.summary()['channels']
                if 'camera2' in c['topic']]
        self.assertEqual(len(cam2), 1)
        want = {cam2[0]}

        got_push = [(cid, lg) for cid, lg, _p, _s, _d
                    in r.iter_messages(want, pushdown=True)]
        got_pull = [(cid, lg) for cid, lg, _p, _s, _d
                    in r.iter_messages(want, pushdown=False)]
        self.assertEqual(got_push, got_pull,
                         '下推与库外过滤的结果必须完全一致')
        self.assertTrue(all(cid in want for cid, _lg in got_push))
        self.assertEqual(len(got_push), 4, '应只收到 camera2 的 4 帧')

    def test_log_time_order_false_keeps_all_messages(self):
        path = self._two_channel_file()
        r = MR.McapReader(path)
        a = sorted((cid, lg) for cid, lg, _p, _s, _d
                   in r.iter_messages(None, log_time_order=True))
        b = sorted((cid, lg) for cid, lg, _p, _s, _d
                   in r.iter_messages(None, log_time_order=False))
        self.assertEqual(a, b, '关闭全局排序不能丢消息或改内容')

    def test_log_time_order_false_follows_file_order(self):
        path = self._out_of_order_file()
        r = MR.McapReader(path)
        first_false = next(iter(r.iter_messages(None, log_time_order=False)))[1]
        first_true = next(iter(r.iter_messages(None, log_time_order=True)))[1]
        self.assertGreater(first_false, first_true,
                           'False 应给出文件里靠前的 chunk（时间较晚那条）')
        self.assertEqual(first_true, BASE, 'True 应给出全局最早的消息')

    def test_finalize_h264_reorders_out_of_order_index(self):
        """OPT-02 安全网：乱序 index 会被重排，times 保持单调"""
        stage = fx.temp_dir('perfopt-finalize')
        frames = fx.build_h264_frames(4, fps=30.0, t0_ns=BASE, idr_every=1)
        raw = os.path.join(stage, '_raw.bin')
        _p, offs = fx.write_raw(raw, frames)
        st = dict(raw_path=raw, index=list(reversed(offs)),   # 故意倒序
                  first_ts_ns=offs[-1][0], last_ts_ns=offs[0][0],
                  fmt='h264', formats=['h264'], topic='cam2', schema='x',
                  extra={}, frame_id='', img_pending=[], raw_bytes=os.path.getsize(raw))
        entry = dict(id=1)
        extra_files = []
        PREP._finalize_h264(st, entry, 'cam2_c1', stage, BASE, extra_files)
        self.assertTrue(st.get('reordered'), '乱序 index 应被标记重排')
        times = PREP.read_times(os.path.join(stage, 'cam2_c1.times'))
        self.assertEqual(len(times), entry['frames'])
        self.assertTrue(all(times[i] <= times[i + 1] for i in range(len(times) - 1)),
                        'times 必须单调递增（播放端 bisect 依赖）')
        shutil.rmtree(stage, ignore_errors=True)

    def test_ensure_time_order_helper(self):
        seq = [(3, 'c'), (1, 'a'), (2, 'b')]
        self.assertTrue(PREP._ensure_time_order(seq))
        self.assertEqual([x[0] for x in seq], [1, 2, 3])
        ordered = [(1, 'a'), (2, 'b')]
        self.assertFalse(PREP._ensure_time_order(ordered))
        self.assertEqual(ordered, [(1, 'a'), (2, 'b')])


class NoIndexSinglePassCase(unittest.TestCase):
    """OPT-03：无 summary/index 的文件只完整读一遍"""

    @classmethod
    def setUpClass(cls):
        cls.dir = fx.temp_dir('perfopt-noidx')

    def _no_index_file(self):
        """数据区完整、但没有 summary 区（summary_start=0）"""
        path = os.path.join(self.dir, self._testMethodName + '.mcap')
        f = fx.build_h264_frames(5, fps=30.0, t0_ns=BASE, idr_every=1)
        recs = [fx.header()]
        recs += _video_recs(1, 1, '/robot0/sensor/camera2/compressed', f)
        recs.append(fx.schema(2, 'foxglove.AudioData'))
        recs.append(fx.channel(2, 2, '/robot0/sensor/audio'))
        for i in range(3):
            ts = BASE + i * 64_000_000
            recs.append(fx.message(2, i, ts, ts,
                                   fx.audio_message(b'\x01\x02' * 1024,
                                                    16000, 2, seq=i)))
        chunk = fx.chunk(recs[1:], start_ns=BASE, end_ns=BASE + 200_000_000)
        fx.assemble(path, [fx.header(), chunk], summary_records=None)
        return path

    def test_lazy_skips_construction_scan(self):
        path = self._no_index_file()
        eager = MR.McapReader(path)                     # 默认：构造期补扫
        lazy = MR.McapReader(path, lazy=True)           # OPT-03：不补扫
        self.assertIsNotNone(eager.stats, '默认行为不变：构造期会补齐元信息')
        self.assertIsNone(lazy.stats, 'lazy 构造不应扫描文件')
        self.assertEqual(lazy.channels, {}, 'lazy 构造时通道表为空')

    def test_collect_fills_metadata_in_one_pass(self):
        path = self._no_index_file()
        r = MR.McapReader(path, lazy=True)
        seen = []
        for cid, lg, _p, _s, data in r.iter_messages(
                None, log_time_order=False, collect=True, pushdown=False):
            seen.append((cid, lg, len(data)))
        self.assertTrue(seen)
        self.assertIsNotNone(r.stats, '遍历后应补齐统计')
        self.assertEqual(r.stats['message_count'], len(seen))
        self.assertEqual(len(r.channels), 2)
        self.assertEqual(r.stats['message_start_time_ns'], BASE)
        summ = r.summary(recount=False)
        counts = {c['topic']: c['count'] for c in summ['channels']}
        self.assertEqual(counts['/robot0/sensor/camera2/compressed'], 5)
        self.assertEqual(counts['/robot0/sensor/audio'], 3)
        self.assertGreater(summ['duration_s'], 0.0)
        self.assertFalse(summ['has_index'])

    def test_prepare_works_and_reports_single_pass(self):
        path = self._no_index_file()
        out = os.path.join(self.dir, self._testMethodName + '.cache')
        man = PREP.prepare(path, out,
                           camera_pred=lambda t: 'camera2' in (t or ''))
        self.assertTrue(man.get('cameras'))
        self.assertGreater(man.get('duration_s') or 0, 0.0)
        self.assertGreater(man.get('time_base_ns') or 0, 0)
        perf = man.get('_perf') or {}
        self.assertTrue(perf.get('lazy_single_pass'),
                        '无索引文件应走单遍模式')
        self.assertTrue(perf.get('collect_during_iteration'))
        self.assertEqual(perf.get('total_messages_seen'), 8)
        self.assertIsNotNone(appcache.load_manifest(out, source_path=path))
        shutil.rmtree(out, ignore_errors=True)

    def test_error_when_no_main_camera(self):
        path = self._no_index_file()
        out = os.path.join(self.dir, self._testMethodName + '.cache')
        with self.assertRaises(MR.McapError):
            PREP.prepare(path, out, camera_pred=lambda t: 'camera9' in (t or ''))
        shutil.rmtree(out, ignore_errors=True)


class PerfRecordCase(unittest.TestCase):
    """OPT-04：分阶段耗时记录"""

    @classmethod
    def setUpClass(cls):
        cls.dir = fx.temp_dir('perfopt-perf')
        cls.old_root = appcache.CACHE_ROOT
        cls.root = fx.temp_dir('perfopt-cache')
        appcache.CACHE_ROOT = cls.root

    @classmethod
    def tearDownClass(cls):
        appcache.CACHE_ROOT = cls.old_root
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_perf_record_written_and_returned(self):
        path = os.path.join(self.dir, 'perf.mcap')
        f = fx.build_h264_frames(6, fps=30.0, t0_ns=BASE, idr_every=1)
        recs = [fx.header()] + _video_recs(
            1, 1, '/robot0/sensor/camera2/compressed', f)
        chunk = fx.chunk(recs[1:], start_ns=BASE, end_ns=BASE + NS)
        fx.assemble(path, [fx.header(), chunk],
                    [fx.schema(1, 'foxglove.CompressedImage'),
                     fx.channel(1, 1, '/robot0/sensor/camera2/compressed'),
                     fx.statistics(6, 1, 1, 1, BASE, BASE + NS, {1: 6})])
        out = os.path.join(self.root, 'perf-test@slim')
        man = PREP.prepare(path, out,
                          camera_pred=lambda t: 'camera2' in (t or ''))
        perf = man.get('_perf') or {}
        for key in ('reader_setup_ms', 'mcap_iteration_ms', 'video_finalize_ms',
                    'total_ms', 'total_messages_seen', 'camera_frames',
                    'has_index', 'lazy_single_pass'):
            self.assertIn(key, perf, '缺少性能字段：%s' % key)
        self.assertGreater(perf['total_ms'], 0)
        logs = PREP.read_perf_log()
        self.assertTrue(logs, '应写入 cache-perf.log')
        self.assertEqual(logs[-1]['name'], os.path.basename(path))
        with open(os.path.join(out, 'manifest.json'), encoding='utf-8') as fh:
            on_disk = json.load(fh)
        self.assertNotIn('_perf', on_disk,
                         '性能数据不能写进 manifest（保持缓存格式不变）')
        shutil.rmtree(out, ignore_errors=True)

    def test_hotpath_attribution_is_self_consistent(self):
        """Hot Path 归因：字段齐全 + 各部分之和与总时长自洽（测量口径正确）"""
        path = os.path.join(self.dir, 'hotpath.mcap')
        f2 = fx.build_h264_frames(6, fps=30.0, t0_ns=BASE, idr_every=1)
        f3 = fx.build_h264_frames(6, fps=30.0, t0_ns=BASE + 1_000_000,
                                  idr_every=1)
        recs = [fx.header()]
        recs += _video_recs(1, 1, '/robot0/sensor/camera2/compressed', f2)
        recs += _video_recs(2, 1, '/robot0/sensor/camera3/compressed', f3)
        recs.append(fx.schema(3, 'foxglove.IMUMeasurement'))
        recs.append(fx.channel(3, 3, '/robot0/sensor/imu'))
        for i in range(12):
            ts = BASE + i * 5_000_000
            recs.append(fx.message(3, i, ts, ts, fx.imu_message(ts)))
        recs.append(fx.schema(4, 'foxglove.AudioData'))
        recs.append(fx.channel(4, 4, '/robot0/sensor/audio'))
        for i in range(3):
            ts = BASE + i * 64_000_000
            recs.append(fx.message(4, i, ts, ts,
                                   fx.audio_message(b'\x01\x02' * 512, 16000, 2, seq=i)))
        chunk = fx.chunk(recs[1:], start_ns=BASE, end_ns=BASE + 200_000_000)
        fx.assemble(path, [fx.header(), chunk],
                    [fx.schema(1, 'foxglove.CompressedImage'),
                     fx.channel(1, 1, '/robot0/sensor/camera2/compressed'),
                     fx.channel(2, 1, '/robot0/sensor/camera3/compressed'),
                     fx.channel(3, 3, '/robot0/sensor/imu'),
                     fx.channel(4, 4, '/robot0/sensor/audio'),
                     fx.statistics(27, 1, 4, 1, BASE, BASE + 200_000_000,
                                   {1: 6, 2: 6, 3: 12, 4: 3})])
        out = os.path.join(self.root, 'hotpath-test@slim')
        man = PREP.prepare(path, out,
                          camera_pred=lambda t: ('camera2' in (t or '')
                                                 or 'camera3' in (t or '')))
        perf = man.get('_perf') or {}
        hp = perf.get('hotpath') or {}
        for key in ('reader_next_total_ms', 'reader_next_avg_us',
                    'reader_next_p50_us', 'reader_next_p95_us',
                    'reader_next_max_us', 'reader_next_samples',
                    'app_total_ms', 'progress_report_ms', 'unattributed_ms',
                    'messages_by_kind', 'camera_dispatch_ms', 'camera_decode_ms',
                    'camera_raw_write_ms', 'camera_index_append_ms',
                    'imu_deserialize_ms', 'imu_collect_ms',
                    'audio_deserialize_ms', 'audio_collect_ms',
                    'metadata_decode_ms', 'imu_json_serialize_ms',
                    'imu_json_write_ms', 'reader_next_share', 'app_share'):
            self.assertIn(key, hp, '缺少归因字段：%s' % key)
        # 分类消息数之和 == 遍历到的消息总数
        self.assertEqual(sum(hp['messages_by_kind'].values()),
                         perf['total_messages_seen'])
        self.assertEqual(hp['messages_by_kind']['video'], 12)
        self.assertEqual(hp['messages_by_kind']['imu'], 12)
        self.assertEqual(hp['messages_by_kind']['audio'], 3)
        # 四段之和 == 遍历总时长（允许 1ms 计时误差）
        total = (hp['reader_next_total_ms'] + hp['app_total_ms']
                 + hp['progress_report_ms'] + hp['unattributed_ms'])
        self.assertAlmostEqual(total, hp['iteration_ms'], delta=1.0)
        # 应用侧细分不超过应用侧总时长（允许 5% 计时误差）
        sub = (hp['camera_dispatch_ms'] + hp['imu_deserialize_ms']
               + hp['imu_collect_ms'] + hp['audio_deserialize_ms']
               + hp['audio_collect_ms'] + hp['metadata_decode_ms'])
        self.assertLessEqual(sub, hp['app_total_ms'] * 1.05 + 5.0)
        self.assertGreater(hp['reader_next_samples'], 0)
        self.assertGreater(hp['reader_next_p95_us'], 0)
        # p50 ≤ p95 ≤ max
        self.assertLessEqual(hp['reader_next_p50_us'], hp['reader_next_p95_us'])
        self.assertLessEqual(hp['reader_next_p95_us'], hp['reader_next_max_us'])
        shutil.rmtree(out, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
