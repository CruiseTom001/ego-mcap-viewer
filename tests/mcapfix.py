"""tests/mcapfix.py —— 构造测试用 MCAP 文件的最小写入器

只实现测试需要的记录类型，严格按 MCAP 规范的字段顺序与长度前缀编码：

    Header(0x01)     profile:string, library:string
    Schema(0x03)     id:u16, name:string, encoding:string, data:bytes(u32 长度前缀)
    Channel(0x04)    id:u16, schema_id:u16, topic:string, message_encoding:string,
                     metadata:Map<string,string>
    Message(0x05)    channel_id:u16, sequence:u32, log_time:u64, publish_time:u64, data:bytes
    Chunk(0x06)      message_start_time:u64, message_end_time:u64, uncompressed_size:u64,
                     uncompressed_crc:u32, compression:string,
                     data:bytes(**u64** 长度前缀)   ← 注意这里是 8 字节长度
    Statistics(0x0B) message_count:u64, schema_count:u16, channel_count:u32,
                     attachment_count:u32, metadata_count:u32, chunk_count:u32,
                     message_start_time:u64, message_end_time:u64,
                     channel_message_counts:Map<u16,u64>

    string / bytes 都是 u32 长度前缀；Map 是 u32 字节长度 + 重复的键值对。
"""

import os
import zlib
import struct

MAGIC = b'\x89MCAP0\r\n'

OP_HEADER = 0x01
OP_FOOTER = 0x02
OP_SCHEMA = 0x03
OP_CHANNEL = 0x04
OP_MESSAGE = 0x05
OP_CHUNK = 0x06
OP_STATISTICS = 0x0B
OP_DATA_END = 0x0F


# ------------------------------------------------------------------ 编码工具
def u16(v):
    return struct.pack('<H', v)


def u32(v):
    return struct.pack('<I', v)


def u64(v):
    return struct.pack('<Q', v)


def pstr(s):
    b = s.encode('utf-8') if isinstance(s, str) else bytes(s)
    return u32(len(b)) + b


def pbytes(b):
    return u32(len(b)) + bytes(b)


def pmap(m):
    body = b''
    for k, v in (m or {}).items():
        body += pstr(k) + pstr(v)
    return u32(len(body)) + body


def record(op, body):
    return bytes([op]) + u64(len(body)) + body


# ------------------------------------------------------------------ 记录构造
def header(profile='test-profile', library='test-lib'):
    return record(OP_HEADER, pstr(profile) + pstr(library))


def schema(sid, name, encoding='protobuf', data=b''):
    return record(OP_SCHEMA, u16(sid) + pstr(name) + pstr(encoding) + pbytes(data))


def channel(cid, sid, topic, encoding='protobuf', metadata=None):
    return record(OP_CHANNEL,
                  u16(cid) + u16(sid) + pstr(topic) + pstr(encoding) + pmap(metadata))


def message(cid, seq, log_time, publish_time, data):
    return record(OP_MESSAGE,
                  u16(cid) + u32(seq) + u64(log_time) + u64(publish_time) + bytes(data))


def chunk(records, compression='', start_ns=0, end_ns=0, use_crc=True, crc_override=None):
    """把若干记录打包成一个 Chunk。compression: '' / 'zstd' / 'lz4'"""
    raw = b''.join(records)
    if compression == 'zstd':
        import zstandard
        payload = zstandard.ZstdCompressor().compress(raw)
    elif compression == 'lz4':
        import lz4.frame
        payload = lz4.frame.compress(raw)
    elif compression == '':
        payload = raw
    else:
        raise ValueError('不支持的压缩: %r' % compression)
    if crc_override is not None:
        crc = crc_override
    else:
        crc = zlib.crc32(raw) if use_crc else 0
    body = (u64(start_ns) + u64(end_ns) + u64(len(raw)) + u32(crc) +
            pstr(compression) + u64(len(payload)) + payload)
    return record(OP_CHUNK, body)


def statistics(message_count, schema_count, channel_count, chunk_count,
               start_ns, end_ns, counts):
    body = (u64(message_count) + u16(schema_count) + u32(channel_count) +
            u32(0) + u32(0) + u32(chunk_count) + u64(start_ns) + u64(end_ns))
    pairs = b''.join(u16(k) + u64(v) for k, v in counts.items())
    body += u32(len(pairs)) + pairs
    return record(OP_STATISTICS, body)


def data_end(crc=0):
    return record(OP_DATA_END, u32(crc))


def footer(summary_start=0, summary_offset_start=0, crc=0):
    return record(OP_FOOTER, u64(summary_start) + u64(summary_offset_start) + u32(crc))


# ------------------------------------------------------------------ 组装文件
def assemble(path, data_records, summary_records=None, trailing_magic=True,
             truncate_to=None, corrupt_length_at=None, length_value=None):
    """按 MCAP 结构写出文件：
        MAGIC + 数据区记录 + DataEnd + [summary 记录] + Footer + MAGIC
    """
    parts = [MAGIC] + list(data_records) + [data_end()]
    body_len = sum(len(p) for p in parts)
    if summary_records:
        summary_start = body_len
        parts.extend(summary_records)
    else:
        summary_start = 0
    parts.append(footer(summary_start, 0, 0))
    if trailing_magic:
        parts.append(MAGIC)
    blob = bytearray(b''.join(parts))

    if corrupt_length_at is not None and length_value is not None:
        # 把某个记录的 8 字节长度字段改成异常值
        blob[corrupt_length_at + 1:corrupt_length_at + 9] = u64(length_value)
    if truncate_to is not None:
        blob = blob[:truncate_to]

    with open(path, 'wb') as fh:
        fh.write(bytes(blob))
    return path


# ------------------------------------------------------------------ protobuf
def pb_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_len(fno, payload):
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    return pb_varint((fno << 3) | 2) + pb_varint(len(payload)) + payload


def pb_f32(fno, v):
    return pb_varint((fno << 3) | 5) + struct.pack('<f', v)


def pb_f64(fno, v):
    return pb_varint((fno << 3) | 1) + struct.pack('<d', v)


def pb_msg(fno, payload):
    return pb_len(fno, payload)


def pb_str(fno, s):
    return pb_len(fno, s.encode('utf-8'))


def pb_bytes(fno, b):
    return pb_len(fno, bytes(b))


def pb_int(fno, v):
    return pb_varint(fno << 3) + pb_varint(int(v))


def pb_packed_doubles(fno, values):
    payload = struct.pack('<%dd' % len(values), *values)
    return pb_len(fno, payload)


# ------------------------------------------------------------------ 消息体
#: foxglove.CompressedImage: data=2, format=3, frame_id=4
def compressed_image(data, fmt='h264', frame_id='cam'):
    return pb_bytes(2, data) + pb_str(3, fmt) + pb_str(4, frame_id)


#: foxglove.CompressedVideo: frame_id=2, data=3, format=4
def compressed_video(data, fmt='h264', frame_id='cam'):
    return pb_str(2, frame_id) + pb_bytes(3, data) + pb_str(4, fmt)


def imu_message(t_ns, av=(1.0, 2.0, 3.0), la=(4.0, 5.0, 6.0), frame_id='imu'):
    ts = pb_msg(1, pb_int(1, t_ns // 1_000_000_000) + pb_int(2, t_ns % 1_000_000_000))
    avb = pb_f64(1, av[0]) + pb_f64(2, av[1]) + pb_f64(3, av[2])
    lab = pb_f64(1, la[0]) + pb_f64(2, la[1]) + pb_f64(3, la[2])
    return ts + pb_str(2, frame_id) + pb_msg(3, avb) + pb_msg(4, lab)


def audio_message(pcm, sample_rate=16000, channels=2, bit_depth=16, seq=0,
                  audio_format='PCM_16'):
    cfg = (pb_int(1, sample_rate) + pb_int(2, channels) + pb_int(3, bit_depth) +
           pb_str(4, audio_format) + pb_str(5, 'testdev') + pb_int(6, 1024))
    return (pb_msg(3, cfg) + pb_bytes(4, pcm) + pb_int(5, len(pcm)) +
            pb_int(7, seq) + pb_int(8, 1))


# ------------------------------------------------------------------ H.264 素材
#: 一段真实的 H.264 参数集（1600x1300, High profile, level 4.2）。
#: 只用到 SPS / PPS，切片数据用结构等价的占位字节，封装器只关心 NAL 结构。
SPS = bytes.fromhex('6764102aac1b1aa0640297e79b808080a0000003002000000791e1108d40')
PPS = bytes.fromhex('68ee31b21b')

START_CODE = b'\x00\x00\x00\x01'


def nal(header_byte, seq=0, payload_size=18):
    return START_CODE + bytes([header_byte]) + bytes([0x88, seq & 0xFF]) + b'\x55' * payload_size


def au_sps_pps_idr(seq=0, payload_size=18):
    """第 0 帧：SPS + PPS + IDR"""
    return (START_CODE + SPS + START_CODE + PPS +
            nal(0x65, seq, payload_size))


def au_idr(seq=0, payload_size=18):
    """后续 IDR（同样重复参数集）"""
    return (START_CODE + SPS + START_CODE + PPS +
            nal(0x65, seq, payload_size))


def au_p(seq=0, payload_size=18):
    """非 IDR 的 P 帧"""
    return START_CODE + nal(0x41, seq, payload_size)


def build_h264_frames(count, fps=30.0, t0_ns=0, first_is_idr=True, idr_every=30,
                      step_ns=None, payload_size=18, level=None, first_idr_at=None,
                      no_idr=False):
    """生成 [(ts_ns, annexb)]，用于封装器测试。

    level:       把 SPS 的 level_idc 换成别的字节（模拟「中途换 SPS」）
    first_idr_at: 下标小于它的帧带参数集但不是 IDR（模拟「首帧非 IDR」），
                  等于它的那一帧是 IDR；None 表示按 first_is_idr 处理
    no_idr:      True 时所有帧都带参数集但都不是 IDR（整条流没有关键帧）
    """
    sps = SPS
    if level is not None:
        sps = SPS[:3] + bytes([level]) + SPS[4:]
    params = START_CODE + sps + START_CODE + PPS
    if first_idr_at is None:
        # first_is_idr=False 表示「首帧不是 IDR」，最贴近真实数据的是第 2 帧才是 IDR
        first_idr_at = 0 if first_is_idr else 1

    step = step_ns if step_ns is not None else int(round(1e9 / fps))
    frames = []
    for i in range(count):
        t = t0_ns + i * step
        if no_idr:
            data = params + nal(0x41, i, payload_size)
        elif first_idr_at is not None and i < first_idr_at:
            data = params + nal(0x41, i, payload_size)
        elif first_idr_at is not None and i == first_idr_at:
            data = params + nal(0x65, i, payload_size)
        elif idr_every and first_idr_at is not None and \
                (i - first_idr_at) % idr_every == 0:
            data = params + nal(0x65, i, payload_size)
        else:
            data = au_p(i, payload_size)
        frames.append((t, data))
    return frames


def write_raw(path, frames):
    """把 [(ts, annexb)] 落成 raw 文件 + 索引，返回 (raw_path, index)"""
    index = []
    with open(path, 'wb') as fh:
        for ts, data in frames:
            off = fh.tell()
            fh.write(data)
            index.append((ts, off, len(data)))
    return path, index


def mp4_sample_count(path):
    """从 MP4 的 stsz box 里读样本数（用于校验 MP4 实际帧数）"""
    with open(path, 'rb') as fh:
        data = fh.read()
    i = data.find(b'stsz')
    if i < 0:
        return -1
    return struct.unpack_from('>I', data, i + 12)[0]


def mp4_has_co64(path):
    with open(path, 'rb') as fh:
        return b'co64' in fh.read()


def mp4_large_mdat(path):
    with open(path, 'rb') as fh:
        data = fh.read()
    i = data.find(b'mdat')
    if i < 0:
        return False
    return struct.unpack_from('>I', data, i - 4)[0] == 1


def add_emulation_prevention(sps):
    """在 SPS 里人为插入防竞争字节（00 00 xx → 00 00 03 xx）"""
    out = bytearray()
    zeros = 0
    for b in sps:
        if zeros >= 2:
            out.append(0x03)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def make_png(width=1600, height=1200, color=(20, 120, 200)):
    """生成一张纯色 PNG（用于图片序列通道测试）"""
    import numpy as np
    import cv2
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:, :] = color
    ok, buf = cv2.imencode('.png', img)
    if not ok:
        raise RuntimeError('PNG 编码失败')
    return buf.tobytes()


def temp_dir(name):
    import tempfile
    d = os.path.join(tempfile.gettempdir(), 'mcapview-tests', name)
    os.makedirs(d, exist_ok=True)
    return d


def file_sha256(path):
    """流式 SHA256（with 关闭文件句柄，杜绝 ResourceWarning）"""
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()
