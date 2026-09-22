"""MCAP 解析层测试

覆盖：未分块数据区消息 / Chunk 内 Schema+Channel / 三种压缩 /
      截断与异常长度与 CRC 错误 / CompressedImage vs CompressedVideo 字段号 /
      无 Statistics 时自行统计 / Map<string,string> 元数据
"""

import os
import shutil
import unittest

from tests import mcapfix as fx
import mcap_reader as MR

NS = 1_000_000_000


class ReaderCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fx.temp_dir('reader')

    def setUp(self):
        self.path = os.path.join(self.dir, self._testMethodName + '.mcap')

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    # ------------------------------------------------------------ 1 未分块
    def test_unchunked_messages_and_duration(self):
        """合法未分块 MCAP：消息能读到，时长正确"""
        recs = [fx.header()]
        recs.append(fx.schema(1, 'foxglove.IMUMeasurement'))
        recs.append(fx.channel(1, 1, '/imu'))
        t0 = 1_000_000_000_000
        for i in range(10):
            recs.append(fx.message(1, i, t0 + i * 100_000_000,
                                   t0 + i * 100_000_000, fx.imu_message(t0 + i * 100_000_000)))
        # 没有 summary 区，也没有 Statistics
        fx.assemble(self.path, recs, summary_records=None)

        r = MR.McapReader(self.path)
        s = r.summary()
        self.assertEqual(s['message_count'], 10)
        self.assertEqual(len(s['channels']), 1)
        self.assertAlmostEqual(s['duration_s'], 0.9, places=6)
        self.assertEqual(s['start_time_ns'], t0)
        self.assertEqual(s['end_time_ns'], t0 + 9 * 100_000_000)
        self.assertFalse(s['has_statistics'])

        got = list(r.iter_messages())
        self.assertEqual(len(got), 10)
        self.assertEqual(got[0][1], t0)

    # ------------------------------------------------------------ 2 Chunk 内 Schema/Channel
    def test_schema_channel_inside_chunk_without_statistics(self):
        """Schema / Channel 位于 Chunk 内且文件没有 Statistics"""
        t0 = 5_000_000_000
        inner = [fx.schema(7, 'foxglove.CompressedImage'),
                 fx.channel(9, 7, '/cam/compressed')]
        for i in range(6):
            inner.append(fx.message(9, i, t0 + i * NS // 10, t0 + i * NS // 10,
                                    fx.compressed_image(b'\x00\x01\x02', 'h264')))
        recs = [fx.header(), fx.chunk(inner, '', t0, t0 + 5 * NS // 10)]
        fx.assemble(self.path, recs, summary_records=None)

        r = MR.McapReader(self.path)
        s = r.summary()
        self.assertEqual(s['message_count'], 6)
        self.assertEqual(len(s['channels']), 1)
        self.assertEqual(s['channels'][0]['topic'], '/cam/compressed')
        self.assertEqual(s['channels'][0]['schema'], 'foxglove.CompressedImage')
        self.assertAlmostEqual(s['duration_s'], 0.5, places=6)
        self.assertFalse(s['has_statistics'])

    # ------------------------------------------------------------ 3 三种压缩
    def test_chunk_compressions(self):
        """none / zstd / lz4 三种 Chunk 压缩都要能解"""
        t0 = 2_000_000_000
        for comp in ('', 'zstd', 'lz4'):
            with self.subTest(compression=comp or 'none'):
                path = os.path.join(self.dir, 'comp_%s.mcap' % (comp or 'none'))
                inner = [fx.schema(1, 'foxglove.CompressedImage'),
                         fx.channel(1, 1, '/c/compressed')]
                for i in range(8):
                    inner.append(fx.message(1, i, t0 + i * 1000, t0 + i * 1000,
                                            fx.compressed_image(b'x' * (50 + i), 'h264')))
                recs = [fx.header(), fx.chunk(inner, comp, t0, t0 + 7000)]
                fx.assemble(path, recs, summary_records=None)
                try:
                    s = MR.scan(path)
                    self.assertEqual(s['message_count'], 8, comp)
                    self.assertEqual(len(s['channels']), 1, comp)
                    msgs = list(MR.McapReader(path).iter_messages())
                    self.assertEqual(len(msgs), 8, comp)
                    fmt, payload, _ = MR.decode_video(
                        'foxglove.CompressedImage', 'protobuf', msgs[0][4])
                    self.assertEqual(payload, b'x' * 50, comp)
                    self.assertEqual(fmt, 'h264', comp)
                finally:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    # ------------------------------------------------------------ 4 损坏文件
    def test_truncated_record_raises_with_offset(self):
        """截断文件必须抛出带文件偏移的 McapError"""
        recs = [fx.header(), fx.schema(1, 'foxglove.CompressedImage'),
                fx.channel(1, 1, '/c/compressed')]
        for i in range(50):
            recs.append(fx.message(1, i, 1000 + i, 1000 + i,
                                   fx.compressed_image(b'y' * 200, 'h264')))
        blob = fx.assemble(self.path, recs)
        size = os.path.getsize(blob)
        with open(self.path, 'r+b') as fh:
            fh.truncate(size // 2)          # 从中间切断
        with self.assertRaises(MR.McapError) as ctx:
            MR.scan(self.path)
        msg = str(ctx.exception)
        self.assertIn('偏移', msg)
        self.assertIn('0x', msg)

    def test_absurd_record_length_raises(self):
        """记录长度异常必须报错，而不是默默读空"""
        recs = [fx.header(), fx.schema(1, 'foxglove.CompressedImage'),
                fx.channel(1, 1, '/c/compressed'),
                fx.message(1, 0, 1000, 1000, fx.compressed_image(b'z' * 32, 'h264'))]
        # 定位最后一条 Message 记录（记录起始 = 文件头 magic + 之前所有记录长度）
        offset = len(fx.MAGIC) + sum(len(r) for r in recs[:3])
        fx.assemble(self.path, recs, corrupt_length_at=offset,
                    length_value=0x7FFFFFFFFFFF)
        with self.assertRaises(MR.McapError):
            MR.scan(self.path)

    def test_bad_crc_raises(self):
        """Chunk 的 CRC 非零且不匹配时必须报错"""
        t0 = 3_000_000_000
        inner = [fx.schema(1, 'foxglove.CompressedImage'),
                 fx.channel(1, 1, '/c/compressed'),
                 fx.message(1, 0, t0, t0, fx.compressed_image(b'q' * 64, 'h264'))]
        recs = [fx.header(), fx.chunk(inner, '', t0, t0, crc_override=0xDEADBEEF)]
        fx.assemble(self.path, recs, summary_records=None)
        with self.assertRaises(MR.McapError):
            MR.scan(self.path)

    def test_zero_crc_is_accepted(self):
        """CRC 为 0 表示「未计算」，不应该报错"""
        t0 = 3_000_000_000
        inner = [fx.schema(1, 'foxglove.CompressedImage'),
                 fx.channel(1, 1, '/c/compressed'),
                 fx.message(1, 0, t0, t0, fx.compressed_image(b'q' * 64, 'h264'))]
        recs = [fx.header(), fx.chunk(inner, '', t0, t0, crc_override=0)]
        fx.assemble(self.path, recs, summary_records=None)
        s = MR.scan(self.path)
        self.assertEqual(s['message_count'], 1)

    # ------------------------------------------------------------ 5 字段号
    def test_compressed_image_vs_video_field_numbers(self):
        """CompressedImage 与 CompressedVideo 的 protobuf 字段号不同，不能混用"""
        payload = b'\x00\x00\x00\x01\x65\x88' + b'A' * 24
        img_msg = fx.compressed_image(payload, 'h264', 'frame_img')
        vid_msg = fx.compressed_video(payload, 'h264', 'frame_vid')

        fmt, data, extra = MR.decode_video('foxglove.CompressedImage', 'protobuf', img_msg)
        self.assertEqual(fmt, 'h264')
        self.assertEqual(data, payload)
        self.assertEqual(extra['frame_id'], 'frame_img')

        fmt, data, extra = MR.decode_video('foxglove.CompressedVideo', 'protobuf', vid_msg)
        self.assertEqual(fmt, 'h264')
        self.assertEqual(data, payload)
        self.assertEqual(extra['frame_id'], 'frame_vid')

        # 用错字段号会读不到数据 —— 证明确实是分开解析的
        fmt, data, _ = MR.decode_video('foxglove.CompressedImage', 'protobuf', vid_msg)
        self.assertNotEqual(data, payload)
        fmt, data, _ = MR.decode_video('foxglove.CompressedVideo', 'protobuf', img_msg)
        self.assertNotEqual(data, payload)

    def test_classify_video_format(self):
        self.assertEqual(MR.classify_video_format('h264'), 'h264')
        self.assertEqual(MR.classify_video_format('H264'), 'h264')
        self.assertEqual(MR.classify_video_format('avc1'), 'h264')
        self.assertEqual(MR.classify_video_format('h265'), 'unsupported')
        self.assertEqual(MR.classify_video_format('hevc'), 'unsupported')
        self.assertEqual(MR.classify_video_format('vp9'), 'unsupported')
        self.assertEqual(MR.classify_video_format('av1'), 'unsupported')
        self.assertEqual(MR.classify_video_format('jpeg'), 'image')
        self.assertEqual(MR.classify_video_format('png'), 'image')
        self.assertEqual(MR.classify_video_format(''), 'unknown')
        self.assertEqual(MR.classify_video_format('weird'), 'unknown')

    # ------------------------------------------------------------ 元数据
    def test_channel_metadata_is_map(self):
        """Channel metadata 必须按 MCAP Map<string,string> 解析成字典"""
        recs = [fx.header(), fx.schema(1, 'foxglove.CompressedImage'),
                fx.channel(1, 1, '/c/compressed', metadata={'k1': 'v1', 'k2': 'v2'})]
        fx.assemble(self.path, recs, summary_records=None)
        s = MR.scan(self.path)
        self.assertEqual(s['channels'][0]['metadata'], {'k1': 'v1', 'k2': 'v2'})

    # ------------------------------------------------------------ 统计
    def test_statistics_used_when_present(self):
        t0 = 10_000_000_000
        inner = [fx.schema(1, 'foxglove.CompressedImage'),
                 fx.channel(1, 1, '/c/compressed')]
        for i in range(4):
            inner.append(fx.message(1, i, t0 + i, t0 + i,
                                    fx.compressed_image(b'w' * 16, 'h264')))
        recs = [fx.header(), fx.chunk(inner, '', t0, t0 + 3)]
        summary = [fx.schema(1, 'foxglove.CompressedImage'),
                   fx.channel(1, 1, '/c/compressed'),
                   fx.statistics(4, 1, 1, 1, t0, t0 + 3, {1: 4})]
        fx.assemble(self.path, recs, summary_records=summary)
        s = MR.scan(self.path)
        self.assertTrue(s['has_statistics'])
        self.assertEqual(s['message_count'], 4)
        self.assertEqual(s['channels'][0]['count'], 4)
        self.assertEqual(s['chunk_count'], 1)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
