"""h264mp4.py —— H.264 Annex-B(裸流) 无损封装成 MP4（avc1），浏览器 / OpenCV 可直接读

纯标准库实现，不需要 ffmpeg。

流程（两遍式流式写入，避免把整路码流和完整 mdat 同时留在内存里）
  第一遍：只扫描 raw + 索引，收集 SPS/PPS、每帧样本大小、时间戳、关键帧位置，
          校验中途不换 SPS/PPS、不换分辨率，算出 mdat 大小与 moov。
  第二遍：从 raw 顺序读，逐帧转成 AVCC 直接写进 MP4 文件。

容器能力
  * ftyp + mdat + moov（moov 放文件末尾，便于流式写）
  * mdat 超过 4GB 自动使用 largesize（size==1 + 64 位长度）
  * 文件超过 4GB 时 chunk offset 自动使用 co64
  * 时长超出 32 位时 mvhd/mdhd/tkhd 自动使用 version 1（64 位时间字段）
"""

import os
import io
import struct
from concurrent.futures import CancelledError

# ------------------------------------------------------------------ NAL 常量
NAL_SLICE = 1
NAL_SLICE_IDR = 5
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9

HIGH_PROFILES = (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135)

#: 单文件尺寸上限（超过就明确报错，而不是 struct.pack 崩掉或写出损坏文件）
MAX_TOTAL_BYTES = 256 * 1024 ** 3


class MuxError(Exception):
    """H.264 封装失败"""


# ================================================================== NAL 处理
def split_nalus(buf):
    """把 Annex-B 数据拆成 NAL 单元（不含起始码）。

    用 bytes.find() 在 C 层扫描，比逐字节 Python 循环快一个数量级
    （7MB 码流从 ~2 秒降到 ~20 毫秒）。
    """
    n = len(buf)
    sc = b'\x00\x00\x01'
    starts = []
    i = 0
    while True:
        j = buf.find(sc, i)
        if j < 0:
            break
        starts.append(j)
        i = j + 3
    for k, j in enumerate(starts):
        s = j + 3
        if k + 1 < len(starts):
            nj = starts[k + 1]
            # 4 字节起始码（00 00 00 01）时，多出来的那个 0 属于起始码，不计入上一个 NAL
            e = nj - 1 if (nj > 0 and buf[nj - 1] == 0) else nj
        else:
            e = n
        if e > s:
            yield buf[s:e]


def nal_type(nalu):
    return nalu[0] & 0x1F


def unescape_rbsp(data):
    """去掉 H.264 防竞争字节（emulation prevention bytes: 00 00 03 → 00 00）

    解析 SPS/PPS 前必须先做这一步，否则码流里出现 00 00 03 时位读取会错位。
    """
    n = len(data)
    if b'\x00\x00\x03' not in data:
        return bytes(data)
    out = bytearray()
    zeros = 0
    i = 0
    while i < n:
        b = data[i]
        if zeros >= 2 and b == 0x03 and i + 1 < n and data[i + 1] <= 0x03:
            zeros = 0
            i += 1
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
        i += 1
    return bytes(out)


# ================================================================== SPS 解析
class BitReader:
    __slots__ = ('d', 'p', 'n')

    def __init__(self, data, bitpos=0):
        self.d = data
        self.p = bitpos
        self.n = len(data) * 8

    def bit(self):
        if self.p >= self.n:
            raise EOFError('SPS 位流提前结束')
        v = (self.d[self.p >> 3] >> (7 - (self.p & 7))) & 1
        self.p += 1
        return v

    def u(self, k):
        v = 0
        for _ in range(k):
            v = (v << 1) | self.bit()
        return v

    def ue(self):
        z = 0
        while self.bit() == 0:
            z += 1
            if z > 32:
                raise ValueError('SPS 指数哥伦布编码异常')
        v = 0
        for _ in range(z):
            v = (v << 1) | self.bit()
        return (1 << z) - 1 + v

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def _skip_scaling_list(br, size):
    next_scale = 8
    for _ in range(size):
        if next_scale != 0:
            next_scale = (next_scale + br.se() + 256) % 256


def parse_sps(nalu):
    """解析 SPS，返回 dict(width, height, profile, level, chroma_format_idc, bit_depth)"""
    if len(nalu) < 4:
        raise MuxError('SPS 长度不足，无法解析')
    profile_idc = nalu[1]
    constraint_flags = nalu[2]
    level_idc = nalu[3]

    rbsp = unescape_rbsp(nalu[1:])      # 去掉 NAL header 字节，再去防竞争字节
    br = BitReader(rbsp, 24)            # 跳过 profile_idc / constraint / level_idc
    br.ue()                             # seq_parameter_set_id

    chroma_format_idc = 1
    separate_colour_plane = 0
    bit_depth_luma = 0
    bit_depth_chroma = 0

    if profile_idc in HIGH_PROFILES:
        chroma_format_idc = br.ue()
        if chroma_format_idc == 3:
            separate_colour_plane = br.bit()
        bit_depth_luma = br.ue()
        bit_depth_chroma = br.ue()
        br.bit()
        if br.bit():
            count = 12 if chroma_format_idc == 3 else 8
            for i in range(count):
                if br.bit():
                    _skip_scaling_list(br, 16 if i < 6 else 64)

    br.ue()                             # log2_max_frame_num_minus4
    poc_type = br.ue()
    if poc_type == 0:
        br.ue()
    elif poc_type == 1:
        br.bit()
        br.se()
        br.se()
        nref = br.ue()
        for _ in range(nref):
            br.se()
    br.ue()                             # max_num_ref_frames
    br.bit()                            # gaps_in_frame_num_value_allowed_flag

    pic_width_in_mbs = br.ue() + 1
    pic_height_in_map_units = br.ue() + 1
    frame_mbs_only = br.bit()
    if not frame_mbs_only:
        br.bit()
    br.bit()                            # direct_8x8_inference_flag

    crop_left = crop_right = crop_top = crop_bottom = 0
    if br.bit():
        crop_left = br.ue()
        crop_right = br.ue()
        crop_top = br.ue()
        crop_bottom = br.ue()

    width = pic_width_in_mbs * 16
    height = pic_height_in_map_units * 16 * (2 - frame_mbs_only)

    if chroma_format_idc == 0 or separate_colour_plane:
        crop_unit_x = 1
        crop_unit_y = 1
    else:
        crop_unit_x = 2
        crop_unit_y = 2 if chroma_format_idc == 1 else 1
    crop_unit_y *= (2 - frame_mbs_only)

    width -= (crop_left + crop_right) * crop_unit_x
    height -= (crop_top + crop_bottom) * crop_unit_y
    if width <= 0 or height <= 0:
        raise MuxError('SPS 解析出非法分辨率 %dx%d' % (width, height))

    return dict(width=width, height=height, profile=profile_idc,
                constraint=constraint_flags, level=level_idc,
                chroma_format_idc=chroma_format_idc,
                bit_depth=8 + max(bit_depth_luma, bit_depth_chroma))


# ================================================================== MP4 盒子
def _box(typ, payload=b''):
    return struct.pack('>I', len(payload) + 8) + typ + payload


def _full_box(typ, version, flags, payload=b''):
    return _box(typ, struct.pack('>B', version) + struct.pack('>I', flags)[1:] + payload)


UNITY_MATRIX = struct.pack('>9i', 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)


def build_avcc(sps_list, pps_list, nal_length_size=4):
    if not sps_list or not pps_list:
        raise MuxError('缺少 SPS 或 PPS，无法生成 avcC')
    sps0 = sps_list[0]
    out = bytearray()
    out.append(1)
    out.append(sps0[1])
    out.append(sps0[2])
    out.append(sps0[3])
    out.append(0xFC | ((nal_length_size - 1) & 0x03))
    out.append(0xE0 | (len(sps_list) & 0x1F))
    for s in sps_list:
        out += struct.pack('>H', len(s)) + bytes(s)
    out.append(len(pps_list) & 0xFF)
    for p in pps_list:
        out += struct.pack('>H', len(p)) + bytes(p)
    if sps0[1] in (100, 110, 122, 144):
        info = parse_sps(sps0)
        out.append(0xFC | (info['chroma_format_idc'] & 0x03))
        out.append(0xF8)
        out.append(0xF8)
        out.append(0)
    return bytes(out)


def _avc1_box(width, height, avcc):
    name = b'Genrobot H264'
    compressor = bytes([len(name)]) + name + b'\x00' * (31 - len(name))
    payload = (
        b'\x00' * 6 + struct.pack('>H', 1) +
        struct.pack('>HH', 0, 0) +
        b'\x00' * 12 +
        struct.pack('>HH', width, height) +
        struct.pack('>II', 0x00480000, 0x00480000) +
        struct.pack('>I', 0) +
        struct.pack('>H', 1) +
        compressor +
        struct.pack('>H', 0x0018) +
        struct.pack('>h', -1)
    )
    return _box(b'avc1', payload + _box(b'avcC', avcc))


def _stts(durations):
    entries = []
    for d in durations:
        if entries and entries[-1][1] == d:
            entries[-1][0] += 1
        else:
            entries.append([1, d])
    payload = struct.pack('>I', len(entries))
    for cnt, d in entries:
        payload += struct.pack('>II', cnt, d)
    return _full_box(b'stts', 0, 0, payload)


def _stsc_stco(sample_count, chunk_offsets, use_co64):
    stsc = _full_box(b'stsc', 0, 0,
                     struct.pack('>I', 1) + struct.pack('>III', 1, sample_count, 1))
    if use_co64:
        payload = struct.pack('>I', len(chunk_offsets))
        for off in chunk_offsets:
            payload += struct.pack('>Q', off)
        return stsc, _full_box(b'co64', 0, 0, payload)
    payload = struct.pack('>I', len(chunk_offsets))
    for off in chunk_offsets:
        payload += struct.pack('>I', off)
    return stsc, _full_box(b'stco', 0, 0, payload)


def _stsz(sample_sizes):
    payload = struct.pack('>II', 0, len(sample_sizes))
    for s in sample_sizes:
        payload += struct.pack('>I', s)
    return _full_box(b'stsz', 0, 0, payload)


def _stss(sync_sample_numbers):
    payload = struct.pack('>I', len(sync_sample_numbers))
    for i in sync_sample_numbers:
        payload += struct.pack('>I', i)
    return _full_box(b'stss', 0, 0, payload)


# ---- 时长字段：超过 32 位自动升级 version 1 -------------------------------
_MAX_U32 = 0xFFFFFFFF


def _movie_header(timescale, duration):
    if duration > _MAX_U32:
        payload = (struct.pack('>QQ', 0, 0) + struct.pack('>I', timescale) +
                   struct.pack('>Q', duration) +
                   struct.pack('>IHH', 0x00010000, 0x0100, 0) + b'\x00' * 8 +
                   UNITY_MATRIX + b'\x00' * 24 + struct.pack('>I', 2))
        return _full_box(b'mvhd', 1, 0, payload)
    payload = (struct.pack('>II', 0, 0) + struct.pack('>I', timescale) +
               struct.pack('>I', duration) +
               struct.pack('>IHH', 0x00010000, 0x0100, 0) + b'\x00' * 8 +
               UNITY_MATRIX + b'\x00' * 24 + struct.pack('>I', 2))
    return _full_box(b'mvhd', 0, 0, payload)


def _track_header(duration, width, height):
    if duration > _MAX_U32:
        payload = (struct.pack('>QQII', 0, 0, 1, 0) + struct.pack('>Q', duration) +
                   b'\x00' * 8 + struct.pack('>hhhh', 0, 0, 0, 0) + UNITY_MATRIX +
                   struct.pack('>II', width << 16, height << 16))
        return _full_box(b'tkhd', 1, 0x000007, payload)
    payload = (struct.pack('>IIIII', 0, 0, 1, 0, duration) +
               b'\x00' * 8 + struct.pack('>hhhh', 0, 0, 0, 0) + UNITY_MATRIX +
               struct.pack('>II', width << 16, height << 16))
    return _full_box(b'tkhd', 0, 0x000007, payload)


def _media_header(timescale, duration):
    if duration > _MAX_U32:
        payload = (struct.pack('>QQ', 0, 0) + struct.pack('>I', timescale) +
                   struct.pack('>Q', duration) + struct.pack('>HH', 0x55C4, 0))
        return _full_box(b'mdhd', 1, 0, payload)
    payload = (struct.pack('>IIII', 0, 0, timescale, duration) +
               struct.pack('>HH', 0x55C4, 0))
    return _full_box(b'mdhd', 0, 0, payload)


def _edit_list(offset_units, media_duration):
    """生成空白前导 + 媒体段。任一时长超 32 位时使用 version 1。"""
    if offset_units > _MAX_U32 or media_duration > _MAX_U32:
        payload = (struct.pack('>I', 2) +
                   struct.pack('>QqHH', offset_units, -1, 1, 0) +
                   struct.pack('>QqHH', media_duration, 0, 1, 0))
        return _full_box(b'elst', 1, 0, payload)
    payload = (struct.pack('>I', 2) +
               struct.pack('>IiHH', offset_units, -1, 1, 0) +
               struct.pack('>IiHH', media_duration, 0, 1, 0))
    return _full_box(b'elst', 0, 0, payload)


# ================================================================== 第一遍
def _frame_data(item):
    """统一 iter_factory 产出的两种形态：(ts, data) 或 (ts, off, len, data)"""
    if len(item) == 2:
        return item[0], item[1]
    return item[0], item[3]


def _plan(iterable, cancelled=None):
    """扫描一遍，产出封装计划。不保留样本数据，只留大小 / 时间 / 关键帧标记。"""
    sps = None
    pps = None
    sps_list = []
    pps_list = []
    sizes = []
    times = []
    sync_flags = []
    first_idr = None
    info = None
    warnings = []

    for idx, item in enumerate(iterable):
        if cancelled and cancelled():
            raise CancelledError('视频封装已取消')
        ts, data = _frame_data(item)
        nalus = list(split_nalus(data))
        has_idr = False
        size = 0
        for nalu in nalus:
            t = nal_type(nalu)
            if t == NAL_SPS:
                if sps is None:
                    sps = bytes(nalu)
                    sps_list.append(bytes(nalu))
                elif bytes(nalu) != sps:
                    # 先按解析后的关键属性判断：属性一致就只记警告
                    # （部分编码器会重复写等价 SPS），属性不同才报错
                    try:
                        a, b = parse_sps(sps), parse_sps(bytes(nalu))
                    except MuxError as e:
                        raise MuxError('SPS 中途变化且无法解析：%s' % e)
                    if (a['width'], a['height'], a['profile'], a['level'],
                            a['chroma_format_idc']) != (b['width'], b['height'],
                                                        b['profile'], b['level'],
                                                        b['chroma_format_idc']):
                        raise MuxError(
                            '视频中途更换了 SPS（分辨率/档次变化：%dx%d → %dx%d）'
                            % (a['width'], a['height'], b['width'], b['height']))
                    if bytes(nalu) not in sps_list:
                        sps_list.append(bytes(nalu))
                        warnings.append('第 %d 帧出现内容不同但属性一致的 SPS' % idx)
                continue
            if t == NAL_PPS:
                if pps is None:
                    pps = bytes(nalu)
                    pps_list.append(bytes(nalu))
                elif bytes(nalu) != pps:
                    if bytes(nalu) not in pps_list:
                        pps_list.append(bytes(nalu))
                        warnings.append('第 %d 帧出现内容不同的 PPS' % idx)
                continue
            if t == NAL_SLICE_IDR:
                has_idr = True
            size += 4 + len(nalu)
        if info is None and sps is not None:
            info = parse_sps(sps)
        if has_idr and first_idr is None:
            first_idr = idx
        sizes.append(size)
        times.append(ts)
        sync_flags.append(has_idr)

    if not sizes:
        raise MuxError('码流里没有任何视频帧')
    if sps is None or pps is None:
        raise MuxError('码流里找不到 SPS/PPS，无法封装成 MP4')
    if first_idr is None:
        raise MuxError('码流里找不到 IDR 关键帧，无法确定可解码起点（不伪造同步帧）')
    if info is None:
        info = parse_sps(sps)

    keep = slice(first_idr, None)
    return dict(
        info=info,
        sps_list=sps_list,
        pps_list=pps_list,
        sizes=sizes[keep],
        times=times[keep],
        sync_flags=sync_flags[keep],
        first_idr=first_idr,
        dropped=first_idr,
        raw_count=len(sizes),
        warnings=warnings,
    )


def _to_avcc(data):
    """Annex-B 访问单元 → AVCC（4 字节长度前缀），并剥掉 SPS/PPS（已放进 avcC）"""
    out = bytearray()
    for nalu in split_nalus(data):
        t = nal_type(nalu)
        if t in (NAL_SPS, NAL_PPS) or not nalu:
            continue
        out += struct.pack('>I', len(nalu))
        out += nalu
    return bytes(out)


# ================================================================== 主封装
def mux(iter_factory, out, start_offset_ns=None, time_base_ns=0,
        timescale=90000, progress=None, cancelled=None):
    """两遍式封装。

    iter_factory:    可重复调用的函数，返回可迭代对象，元素为
                     ``(ts_ns, data)`` 或 ``(ts_ns, offset, length, data)``
    out:             输出文件路径，或已打开的二进制文件对象
    start_offset_ns: edit list 的起始偏移。传 None 时自动取
                     「第一个实际保留帧的时间 − time_base_ns」，
                     这样首帧非 IDR、被丢掉若干帧时偏移依然正确。
    """
    plan = _plan(iter_factory(), cancelled=cancelled)
    sizes = plan['sizes']
    times = plan['times']
    sync_flags = plan['sync_flags']
    info = dict(plan['info'])
    total_payload = sum(sizes)

    if total_payload > MAX_TOTAL_BYTES:
        raise MuxError('视频数据 %.1f GB 超过本程序单文件上限 %d GB，请先切分文件'
                       % (total_payload / 1024 ** 3, MAX_TOTAL_BYTES // 1024 ** 3))

    if start_offset_ns is None:
        start_offset_ns = max(0, times[0] - int(time_base_ns or 0))

    if len(times) == 1:
        durations = [max(1, int(round(timescale / 30.0)))]
    else:
        durations = []
        for i in range(len(times)):
            delta = (times[i + 1] - times[i]) if i + 1 < len(times) else (times[-1] - times[-2])
            durations.append(max(1, int(round(delta * timescale / 1e9))))
    total_dur = sum(durations)
    offset_units = max(0, int(round(start_offset_ns * timescale / 1e9)))

    avcc = build_avcc(plan['sps_list'], plan['pps_list'])
    width, height = info['width'], info['height']
    sync_numbers = [i + 1 for i, s in enumerate(sync_flags) if s]
    if not sync_numbers:
        raise MuxError('保留帧里没有关键帧')

    ftyp = _box(b'ftyp', b'isom' + struct.pack('>I', 0x200) + b'isomiso2avc1mp41')
    use_large_mdat = (total_payload + 8) > _MAX_U32
    mdat_header_len = 16 if use_large_mdat else 8
    chunk_offset = len(ftyp) + mdat_header_len
    use_co64 = (chunk_offset + total_payload) > _MAX_U32

    def build_moov():
        stsc, stco = _stsc_stco(len(sizes), [chunk_offset], use_co64)
        stbl = (_full_box(b'stsd', 0, 0, struct.pack('>I', 1) + _avc1_box(width, height, avcc)) +
                _stts(durations) + stsc + _stsz(sizes) + stco +
                (_stss(sync_numbers) if len(sync_numbers) < len(sizes) else b''))
        minf = (_full_box(b'vmhd', 0, 1, struct.pack('>HHHH', 0, 0, 0, 0)) +
                _box(b'dinf', _full_box(b'dref', 0, 0,
                                        struct.pack('>I', 1) + _full_box(b'url ', 0, 1))) +
                _box(b'stbl', stbl))
        mdia = _box(b'mdia', _media_header(timescale, total_dur) +
                    _full_box(b'hdlr', 0, 0, struct.pack('>I', 0) + b'vide' + b'\x00' * 12 +
                              b'VideoHandler\x00') + minf)
        tkhd = _track_header(total_dur + offset_units, width, height)
        if offset_units > 0:
            elst = _edit_list(offset_units, total_dur)
            trak = _box(b'trak', tkhd + _box(b'edts', elst) + mdia)
        else:
            trak = _box(b'trak', tkhd + mdia)
        return _box(b'moov', _movie_header(timescale, total_dur + offset_units) + trak)

    close_out = False
    fh = None
    try:
        if isinstance(out, (str, bytes, os.PathLike)):
            fh = open(out, 'wb')
            close_out = True
        else:
            fh = out
        fh.write(ftyp)
        if use_large_mdat:
            fh.write(struct.pack('>I', 1) + b'mdat' + struct.pack('>Q', total_payload + 16))
        else:
            fh.write(struct.pack('>I', total_payload + 8) + b'mdat')

        written = 0
        n = len(sizes)
        for idx, item in enumerate(iter_factory()):
            if cancelled and cancelled():
                raise CancelledError('视频封装已取消')
            if idx < plan['first_idr']:
                continue
            _ts, data = _frame_data(item)
            sample = _to_avcc(data)
            if len(sample) != sizes[idx - plan['first_idr']]:
                raise MuxError('第二遍读到的样本大小与第一遍不一致（第 %d 帧）' % idx)
            fh.write(sample)
            written += len(sample)
            if progress and n and (idx % 64 == 0):
                progress(min(1.0, (idx - plan['first_idr']) / float(n)))
        if written != total_payload:
            raise MuxError('写入 mdat 的字节数（%d）与计划（%d）不一致' % (written, total_payload))
        fh.write(build_moov())
        if progress:
            progress(1.0)
    finally:
        if close_out and fh is not None:
            try:
                fh.close()
            except Exception:
                pass

    info.update(
        frames=len(sizes),
        frames_raw=plan['raw_count'],
        dropped=plan['dropped'],
        duration_ns=(times[-1] - times[0]) if len(times) > 1 else 0,
        duration_s=(times[-1] - times[0]) / 1e9 if len(times) > 1 else 0.0,
        times_ns=list(times),
        sync_samples=len(sync_numbers),
        mdat_bytes=total_payload,
        co64=use_co64,
        large_mdat=use_large_mdat,
        warnings=plan['warnings'],
        start_time_ns=times[0],
        start_offset_ns=start_offset_ns,
    )
    return info


def _list_factory(frames):
    return lambda: iter(frames)


def _raw_factory(raw_path, index):
    def gen():
        with open(raw_path, 'rb') as fh:
            for ts, off, ln in index:
                fh.seek(off)
                yield (ts, fh.read(ln))
    return gen


def mux_frames(frames, out, start_offset_ns=None, time_base_ns=0,
               timescale=90000, progress=None, cancelled=None):
    """frames: [(ts_ns, annexb_bytes), ...]（按时间升序，需可重复遍历）"""
    if not isinstance(frames, (list, tuple)):
        frames = list(frames)
    return mux(_list_factory(frames), out, start_offset_ns=start_offset_ns,
               time_base_ns=time_base_ns, timescale=timescale, progress=progress,
               cancelled=cancelled)


def mux_raw(raw_path, index, out, start_offset_ns=None, time_base_ns=0,
            timescale=90000, progress=None, cancelled=None):
    """index: [(ts_ns, offset, length), ...]，从 raw 文件流式读取（内存与文件大小无关）"""
    return mux(_raw_factory(raw_path, index), out, start_offset_ns=start_offset_ns,
               time_base_ns=time_base_ns, timescale=timescale, progress=progress,
               cancelled=cancelled)


def annexb_to_mp4(frames, start_offset_ns=None, time_base_ns=0, timescale=90000):
    """兼容入口：返回 (mp4_bytes, info)。只建议小数据量使用。"""
    buf = io.BytesIO()
    info = mux_frames(frames, buf, start_offset_ns=start_offset_ns,
                      time_base_ns=time_base_ns, timescale=timescale)
    return buf.getvalue(), info
