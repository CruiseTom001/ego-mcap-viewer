"""H.264 → MP4 封装器测试

覆盖：NAL 拆分 / 防竞争字节 / SPS 解析 / 无 IDR 必须报错 / 首帧非 IDR 的丢弃与偏移 /
      SPS 中途变化 / co64 与 largesize 与 version-1 时间字段 / 流式内存不随体积倍增
"""

import os
import shutil
import struct
import unittest
from concurrent.futures import CancelledError

from tests import mcapfix as fx
import h264mp4 as H

NS = 1_000_000_000


class NalCase(unittest.TestCase):
    def test_split_nalus_basic(self):
        buf = b'\x00\x00\x00\x01\x67\x11\x22\x00\x00\x01\x68\x33'
        nalus = list(H.split_nalus(buf))
        self.assertEqual(len(nalus), 2)
        self.assertEqual(nalus[0], b'\x67\x11\x22')
        self.assertEqual(nalus[1], b'\x68\x33')

    def test_split_nalus_4byte_start_code_excludes_padding_zero(self):
        buf = b'\x00\x00\x00\x01\x65\xAB\x00\x00\x00\x01\x41\xCD'
        nalus = list(H.split_nalus(buf))
        self.assertEqual(nalus[0], b'\x65\xAB')
        self.assertEqual(nalus[1], b'\x41\xCD')

    def test_unescape_rbsp(self):
        self.assertEqual(H.unescape_rbsp(b'\x00\x00\x03\x01'), b'\x00\x00\x01')
        self.assertEqual(H.unescape_rbsp(b'\x00\x00\x03\x00'), b'\x00\x00\x00')
        self.assertEqual(H.unescape_rbsp(b'\x00\x00\x03\x04'), b'\x00\x00\x03\x04')
        self.assertEqual(H.unescape_rbsp(b'\xAA\xBB'), b'\xAA\xBB')

    def test_sps_parsed_with_emulation_prevention_bytes(self):
        """SPS 里带 00 00 03 时也必须解析出正确分辨率"""
        base = H.parse_sps(fx.SPS)
        self.assertEqual((base['width'], base['height']), (1600, 1300))
        escaped = fx.add_emulation_prevention(fx.SPS)
        self.assertNotEqual(escaped, fx.SPS)
        got = H.parse_sps(escaped)
        self.assertEqual((got['width'], got['height']), (1600, 1300))
        self.assertEqual(got['profile'], base['profile'])

    def test_parse_sps_rejects_short(self):
        with self.assertRaises(H.MuxError):
            H.parse_sps(b'\x67\x64')


class MuxCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fx.temp_dir('mux')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _out(self, name):
        return os.path.join(self.dir, name + '.mp4')

    # -------------------------------------------------------- 基本封装
    def test_basic_mux(self):
        frames = fx.build_h264_frames(60, fps=30.0)
        out = self._out('basic')
        info = H.mux_frames(frames, out)
        self.assertEqual(info['frames'], 60)
        self.assertEqual(info['dropped'], 0)
        self.assertEqual((info['width'], info['height']), (1600, 1300))
        self.assertEqual(len(info['times_ns']), 60)
        self.assertEqual(fx.mp4_sample_count(out), 60)
        with open(out, 'rb') as fh:
            head = fh.read(12)
        self.assertEqual(head[4:8], b'ftyp')
        self.assertFalse(info['co64'])
        self.assertFalse(info['large_mdat'])

    # -------------------------------------------------------- 没有 IDR
    def test_no_idr_raises_and_does_not_fake_sync_sample(self):
        """整条流只有 P 帧（有参数集但没有关键帧）：必须明确报错，不能伪造同步帧"""
        frames = fx.build_h264_frames(30, no_idr=True)
        with self.assertRaises(H.MuxError) as ctx:
            H.mux_frames(frames, self._out('noidr'))
        self.assertIn('IDR', str(ctx.exception))

    def test_missing_sps_pps_raises(self):
        frames = [(0, b'\x00\x00\x00\x01\x41\x80\x11'),
                  (NS // 30, b'\x00\x00\x00\x01\x41\x80\x12')]
        with self.assertRaises(H.MuxError):
            H.mux_frames(frames, self._out('nosps'))

    def test_empty_stream_raises(self):
        with self.assertRaises(H.MuxError):
            H.mux_frames([], self._out('empty'))

    def test_cancelled_mux_does_not_publish_complete_file(self):
        frames = fx.build_h264_frames(20)
        with self.assertRaises(CancelledError):
            H.mux_frames(frames, self._out('cancelled'), cancelled=lambda: True)

    # -------------------------------------------------------- 首帧非 IDR
    def test_first_frame_not_idr_drops_and_offsets(self):
        """首帧非 IDR：丢弃前面的帧，时间戳/帧数/MP4 样本数三者一致，偏移取首个保留帧"""
        t0 = 7_000_000_000
        frames = fx.build_h264_frames(40, fps=30.0, t0_ns=t0, first_idr_at=1,
                                      idr_every=30)
        out = self._out('drop')
        info = H.mux_frames(frames, out, time_base_ns=t0)
        self.assertEqual(info['dropped'], 1)
        self.assertEqual(info['frames'], 39)
        self.assertEqual(info['frames_raw'], 40)
        self.assertEqual(len(info['times_ns']), 39)
        self.assertEqual(fx.mp4_sample_count(out), 39)
        # 偏移必须来自第一个「实际保留」的帧（第 2 帧）
        self.assertEqual(info['times_ns'][0], frames[1][0])
        self.assertEqual(info['start_offset_ns'], frames[1][0] - t0)

    def test_multiple_leading_p_frames_dropped(self):
        t0 = 2_000_000_000
        frames = fx.build_h264_frames(20, t0_ns=t0, first_idr_at=5, idr_every=10)
        info = H.mux_frames(frames, self._out('drop5'), time_base_ns=t0)
        self.assertEqual(info['dropped'], 5)
        self.assertEqual(info['frames'], 15)
        self.assertEqual(info['times_ns'][0], frames[5][0])

    def test_start_offset_auto_from_time_base(self):
        t0 = 1_000_000_000
        frames = fx.build_h264_frames(10, t0_ns=t0 + 500_000_000)
        info = H.mux_frames(frames, self._out('off'), time_base_ns=t0)
        self.assertEqual(info['start_offset_ns'], 500_000_000)

    # -------------------------------------------------------- SPS 变化
    def test_sps_change_detected(self):
        frames = fx.build_h264_frames(20, idr_every=5)
        # 第 10 帧起换成 level 不同的 SPS
        tail = fx.build_h264_frames(10, t0_ns=frames[10][0], idr_every=0, level=50)
        mixed = frames[:10] + [(ts, d) for ts, d in tail]
        with self.assertRaises(H.MuxError) as ctx:
            H.mux_frames(mixed, self._out('spschange'))
        self.assertIn('SPS', str(ctx.exception))

    def test_identical_sps_repeat_ok(self):
        """每 30 帧重复写同样的 SPS/PPS 不应报错"""
        frames = fx.build_h264_frames(90, idr_every=30)
        info = H.mux_frames(frames, self._out('repeat'))
        self.assertEqual(info['frames'], 90)
        self.assertEqual(info['warnings'], [])

    # -------------------------------------------------------- 大文件能力
    def test_co64_and_large_mdat_flags_on_normal_file(self):
        frames = fx.build_h264_frames(20)
        info = H.mux_frames(frames, self._out('flags'))
        self.assertFalse(info['co64'])
        self.assertFalse(info['large_mdat'])
        self.assertFalse(fx.mp4_has_co64(self._out('flags')))

    def test_version1_time_fields_for_long_duration(self):
        """时长超过 32 位时 mvhd/mdhd/tkhd 必须用 version 1，不能 struct.pack 崩掉"""
        timescale = 1000
        need = 0x1_0000_0000 + 1000        # 明确的 33 位时长
        mvhd = H._movie_header(timescale, need)
        mdhd = H._media_header(timescale, need)
        tkhd = H._track_header(need, 1600, 1300)
        self.assertEqual(mvhd[8], 1)       # full box version = 1
        self.assertEqual(mdhd[8], 1)
        self.assertEqual(tkhd[8], 1)
        self.assertGreater(len(mvhd), 100)
        # 短时长仍然用 version 0
        self.assertEqual(H._movie_header(timescale, 1000)[8], 0)
        self.assertEqual(H._edit_list(1000, 1000)[8], 0)
        self.assertEqual(H._edit_list(0x1_0000_0000, 1000)[8], 1)
        self.assertEqual(H._edit_list(1000, 0x1_0000_0000)[8], 1)

    def test_sample_count_limits(self):
        old = H.MAX_TOTAL_BYTES
        try:
            H.MAX_TOTAL_BYTES = 100          # 人为把上限压到 100 字节
            frames = fx.build_h264_frames(5, payload_size=200)
            with self.assertRaises(H.MuxError) as ctx:
                H.mux_frames(frames, self._out('toolarge'))
            self.assertIn('上限', str(ctx.exception))
        finally:
            H.MAX_TOTAL_BYTES = old

    # -------------------------------------------------------- 流式内存
    def test_streaming_memory_does_not_scale_with_payload(self):
        """峰值内存不随视频体积成倍增长（证明没有把整路码流 / 整个 mdat 留在内存）"""
        import tracemalloc

        peaks = {}
        totals = {}
        for tag, payload in (('small', 200), ('big', 20000)):
            frames = fx.build_h264_frames(60, payload_size=payload)
            raw, index = fx.write_raw(
                os.path.join(self.dir, 'mem_%s.raw' % tag), frames)
            total = os.path.getsize(raw)
            tracemalloc.start()
            H.mux_raw(raw, index, os.path.join(self.dir, 'mem_%s.mp4' % tag))
            cur, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            peaks[tag] = peak
            totals[tag] = total

        self.assertGreater(totals['big'], totals['small'] * 20)
        extra = peaks['big'] - peaks['small']
        self.assertLess(extra, totals['big'] // 2,
                        '峰值内存随体积明显增长：%d -> %d（体积 %d -> %d）'
                        % (peaks['small'], peaks['big'], totals['small'], totals['big']))
        # 峰值远小于整个 mdat
        self.assertLess(peaks['big'], totals['big'])

    # -------------------------------------------------------- 句柄
    def test_failed_plan_leaves_no_partial_output(self):
        """第一遍就失败时不应该留下半截输出文件"""
        out = self._out('nosps_out')
        with self.assertRaises(H.MuxError):
            H.mux_frames([(0, b'\x00\x00\x00\x01\x41\x80\x11')], out)
        self.assertFalse(os.path.isfile(out))

    def test_caller_supplied_stream_not_closed_on_error(self):
        """调用方传进来的文件对象不应被我们关掉（异常路径也要保证）"""
        import io as _io
        buf = _io.BytesIO()
        frames = fx.build_h264_frames(5, no_idr=True)
        with self.assertRaises(H.MuxError):
            H.mux_frames(frames, buf)
        self.assertFalse(buf.closed)
        buf.write(b'still-usable')
        self.assertEqual(buf.getvalue(), b'still-usable')

    def test_output_file_closed_after_success(self):
        out = self._out('okclose')
        H.mux_frames(fx.build_h264_frames(5), out)
        os.replace(out, out + '.moved')
        self.assertTrue(os.path.isfile(out + '.moved'))

    def test_raw_index_roundtrip_matches_list_mode(self):
        frames = fx.build_h264_frames(45, first_is_idr=False, idr_every=15)
        raw, index = fx.write_raw(os.path.join(self.dir, 'rt.raw'), frames)
        a = self._out('rt_a')
        b = self._out('rt_b')
        ia = H.mux_frames(frames, a)
        ib = H.mux_raw(raw, index, b)
        self.assertEqual(ia['frames'], ib['frames'])
        self.assertEqual(ia['times_ns'], ib['times_ns'])
        self.assertEqual(ia['start_offset_ns'], ib['start_offset_ns'])
        self.assertEqual(os.path.getsize(a), os.path.getsize(b))


class BuildAvccCase(unittest.TestCase):
    def test_avcc_contains_sps_pps(self):
        avcc = H.build_avcc([fx.SPS], [fx.PPS])
        self.assertEqual(avcc[0], 1)
        self.assertEqual(avcc[1], fx.SPS[1])      # profile
        self.assertEqual(avcc[2], fx.SPS[2])
        self.assertEqual(avcc[3], fx.SPS[3])      # level
        self.assertEqual(avcc[4] & 0x03, 3)       # lengthSizeMinusOne = 3
        self.assertIn(fx.SPS, avcc)
        self.assertIn(fx.PPS, avcc)

    def test_avcc_requires_both(self):
        with self.assertRaises(H.MuxError):
            H.build_avcc([fx.SPS], [])
        with self.assertRaises(H.MuxError):
            H.build_avcc([], [fx.PPS])


if __name__ == '__main__':
    unittest.main()
