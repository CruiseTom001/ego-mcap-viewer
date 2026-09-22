"""mcap_reader.py —— MCAP 解析器（容器层用官方 mcap 库，语义层自研）

分工
  * 容器层：交给官方 ``mcap`` 包（``mcap.reader.make_reader``）。它已经正确处理
    未分块的数据区 Message、Chunk 内的 Schema/Channel/Message、Chunk 记录长度字段、
    Map<string,string> 元数据与 CRC 校验；损坏 / 截断文件会抛出带长度信息的异常。
  * 语义层：本模块自己解 protobuf / ROS1 / JSON，把消息还原成视频、IMU、音频等。

时间单位约定（全文统一）
  * 原始 MCAP 时间戳一律以 ``*_ns`` 结尾，单位纳秒。
  * 交给界面 / 缓存的时间一律以 ``*_s`` 结尾，单位秒。

对外主入口
    info = scan(path)                      -> 文件摘要
    data = read(path, progress=cb)          -> 分类后的数据
"""

import os
import json
import struct

# ---------------------------------------------------------------- 依赖探测
_zstd = None
try:
    import zstandard as _zstd          # noqa: F401  （容器层由官方 mcap 使用）
except Exception:
    pass

_lz4 = None
try:
    import lz4.frame as _lz4           # noqa: F401
except Exception:
    pass

try:
    from mcap.reader import make_reader
    from mcap.stream_reader import StreamReader, breakup_chunk
    from mcap.records import Chunk, Message as _ChunkMessage, Schema as _RecSchema
    from mcap.records import Channel as _RecChannel
    _HAS_MCAP = True
except Exception:                      # pragma: no cover
    make_reader = None
    StreamReader = None
    breakup_chunk = None
    Chunk = _ChunkMessage = _RecSchema = _RecChannel = None
    _HAS_MCAP = False

try:
    from chunk_streaming_reader import ChunkStreamingIndexedReader
except Exception:                      # pragma: no cover
    ChunkStreamingIndexedReader = None

NS = 1_000_000_000


class McapError(Exception):
    """本模块对外统一抛出的异常"""


def _reader_mode():
    """Select the indexed reader; no UI setting is exposed."""
    mode = os.environ.get('MCAPVIEWER_READER_MODE', 'chunk_streaming').strip().lower()
    aliases = {'chunk_streaming': 'chunk_streaming',
               'chunk-streaming': 'chunk_streaming',
               'official': 'official', 'seeking': 'official'}
    if mode not in aliases:
        raise McapError('未知 MCAPVIEWER_READER_MODE: %s' % mode)
    return aliases[mode]


# ================================================================= 格式分类
VIDEO_SCHEMAS = ('foxglove.CompressedImage', 'foxglove.CompressedVideo',
                 'foxglove.RawImage', 'foxglove.Image',
                 'sensor_msgs/CompressedImage', 'sensor_msgs/Image')
IMU_SCHEMAS = ('foxglove.IMUMeasurement', 'sensor_msgs/Imu')

#: 各 schema 的 protobuf 字段号。CompressedImage 与 CompressedVideo 字段号不同，
#: 混用会把 format / data 读反，必须分开处理。
VIDEO_FIELDS = {
    'foxglove.CompressedImage': dict(data=2, format=3, frame_id=4, metadata=9,
                                     floats=(10, 11, 12, 13, 14), wb_ct=15),
    'foxglove.CompressedVideo': dict(data=3, format=4, frame_id=2, metadata=None,
                                     floats=(), wb_ct=None),
}

H264_ALIASES = ('h264', 'avc', 'avc1', 'x264')
IMAGE_ALIASES = ('jpeg', 'jpg', 'png', 'mjpeg')
UNSUPPORTED_CODECS = ('h265', 'hevc', 'vp8', 'vp9', 'av1', 'av01', 'mpeg4', 'mjpg')


def classify_video_format(fmt):
    """把 format 字符串归一为 'h264' / 'image' / 'unsupported' / 'unknown'"""
    f = (fmt or '').strip().lower()
    if not f:
        return 'unknown'
    if any(a in f for a in H264_ALIASES):
        return 'h264'
    if any(a in f for a in UNSUPPORTED_CODECS):
        return 'unsupported'
    if any(a in f for a in IMAGE_ALIASES):
        return 'image'
    return 'unknown'


def classify(topic, schema_name):
    t = (topic or '').lower()
    s = schema_name or ''
    if 'camera_info' in t or 'Calibration' in s:
        return 'calibration'
    if s in VIDEO_SCHEMAS or '/compressed' in t or '/image' in t or '/camera' in t:
        return 'video'
    if s in IMU_SCHEMAS or '/imu' in t:
        return 'imu'
    if 'AudioData' in s or '/audio' in t:
        return 'audio'
    if 'SystemInfo' in s or 'system_info' in t:
        return 'system'
    return 'other'


# ================================================================= protobuf
def pb_fields(buf):
    """解析 protobuf 消息，返回 {字段号: [(wire_type, value), ...]}

    wire_type: 0=varint(int) 1=fixed64(原始 uint64) 2=bytes 5=fixed32(原始 uint32)
    定长字段保留原始整数，由调用方决定按 double / float / int 解释。
    """
    d = {}
    i = 0
    n = len(buf)
    while i < n:
        key = 0
        shift = 0
        while True:
            if i >= n:
                return d
            byte = buf[i]
            i += 1
            key |= (byte & 0x7F) << shift
            shift += 7
            if not (byte & 0x80):
                break
        fno = key >> 3
        wt = key & 0x07
        if fno == 0:
            return d
        if wt == 0:
            v = 0
            shift = 0
            while True:
                if i >= n:
                    return d
                byte = buf[i]
                i += 1
                v |= (byte & 0x7F) << shift
                shift += 7
                if not (byte & 0x80):
                    break
            d.setdefault(fno, []).append((0, v))
        elif wt == 1:
            if i + 8 > n:
                return d
            d.setdefault(fno, []).append((1, struct.unpack_from('<Q', buf, i)[0]))
            i += 8
        elif wt == 2:
            ln = 0
            shift = 0
            while True:
                if i >= n:
                    return d
                byte = buf[i]
                i += 1
                ln |= (byte & 0x7F) << shift
                shift += 7
                if not (byte & 0x80):
                    break
            if i + ln > n:
                ln = n - i
            d.setdefault(fno, []).append((2, bytes(buf[i:i + ln])))
            i += ln
        elif wt == 5:
            if i + 4 > n:
                return d
            d.setdefault(fno, []).append((5, struct.unpack_from('<I', buf, i)[0]))
            i += 4
        else:
            return d
    return d


def _first(d, fno):
    v = d.get(fno)
    return v[0] if v else None


def pb_int(d, fno, default=None):
    e = _first(d, fno)
    return e[1] if e and e[0] == 0 else default


def pb_u32(d, fno, default=0):
    e = _first(d, fno)
    if not e:
        return default
    return e[1] if e[0] in (5, 0) else default


def pb_u64(d, fno, default=0):
    e = _first(d, fno)
    if not e:
        return default
    return e[1] if e[0] in (1, 0) else default


def pb_f64(d, fno, default=0.0):
    e = _first(d, fno)
    if not e:
        return default
    if e[0] == 1:
        return struct.unpack('<d', struct.pack('<Q', e[1]))[0]
    if e[0] == 0:
        return float(e[1])
    return default


def pb_f32(d, fno, default=0.0):
    e = _first(d, fno)
    if not e:
        return default
    if e[0] == 5:
        return struct.unpack('<f', struct.pack('<I', e[1]))[0]
    if e[0] == 0:
        return float(e[1])
    return default


def pb_str(d, fno, default=None):
    v = d.get(fno)
    if not v:
        return default
    t, raw = v[0]
    if t == 2:
        try:
            return raw.decode('utf-8')
        except Exception:
            return default
    return default


def pb_bytes(d, fno, default=None):
    v = d.get(fno)
    if not v:
        return default
    t, raw = v[0]
    return raw if t == 2 else default


def pb_packed_doubles(d, fno):
    v = d.get(fno)
    if not v:
        return []
    out = []
    for t, raw in v:
        if t == 1:
            out.append(struct.unpack('<d', struct.pack('<Q', raw))[0])
        elif t == 5:
            out.append(struct.unpack('<f', struct.pack('<I', raw))[0])
        elif t == 2 and raw:
            cnt = len(raw) // 8
            if cnt:
                out.extend(struct.unpack_from('<%dd' % cnt, raw, 0))
    return out


def pb_vec3(buf):
    d = pb_fields(buf)
    return (pb_f64(d, 1, 0.0), pb_f64(d, 2, 0.0), pb_f64(d, 3, 0.0))


def pb_timestamp_ns(buf):
    d = pb_fields(buf)
    return int(pb_int(d, 1, 0) or 0) * NS + int(pb_int(d, 2, 0) or 0)


# ================================================================= ROS1
class _Ros1Cursor:
    def __init__(self, b):
        self.b = b
        self.o = 0

    def u32(self):
        v = struct.unpack_from('<I', self.b, self.o)[0]
        self.o += 4
        return v

    def string(self):
        n = self.u32()
        v = self.b[self.o:self.o + n].decode('utf-8', 'replace')
        self.o += n
        return v

    def bytes_field(self):
        n = self.u32()
        v = self.b[self.o:self.o + n]
        self.o += n
        return bytes(v)


def ros1_compressed_image(buf):
    """sensor_msgs/CompressedImage (ROS1 序列化)"""
    c = _Ros1Cursor(buf)
    c.u32()             # header.seq
    c.u32(); c.u32()    # header.stamp
    c.string()          # header.frame_id
    fmt = c.string()
    data = c.bytes_field()
    return fmt, data


# ================================================================= 语义解码
def decode_video(schema_name, message_encoding, data):
    """解码一路视频消息，返回 (format, payload, extra)。

    ``schema_name`` 决定 protobuf 字段号；CompressedImage 与 CompressedVideo 不同。
    不认识的 schema 退化为 CompressedImage 字段号。
    """
    enc = (message_encoding or '').lower()
    if enc == 'protobuf':
        spec = VIDEO_FIELDS.get(schema_name) or VIDEO_FIELDS['foxglove.CompressedImage']
        d = pb_fields(data)
        fmt = pb_str(d, spec['format'], '') or ''
        payload = pb_bytes(d, spec['data'])
        extra = dict(frame_id=pb_str(d, spec['frame_id'], '') or '')
        if spec.get('metadata'):
            extra['metadata'] = pb_str(d, spec['metadata'], '')
        for key, fno in zip(('integration_time', 'analog_gain', 'aperture',
                             'wb_rgain', 'wb_bgain'), spec.get('floats') or ()):
            extra[key] = pb_f32(d, fno)
        if spec.get('wb_ct'):
            extra['wb_ct'] = pb_int(d, spec['wb_ct'])
        return fmt, payload, extra
    if enc == 'ros1':
        fmt, payload = ros1_compressed_image(data)
        return fmt, payload, {}
    if enc == 'json':
        try:
            j = json.loads(data.decode('utf-8', 'replace'))
        except Exception:
            return '', None, {}
        import base64
        raw = j.get('data')
        try:
            payload = base64.b64decode(raw) if raw else None
        except Exception:
            payload = None
        return j.get('format', ''), payload, dict(frame_id=j.get('frame_id', ''))
    return '', None, {}


#: 兼容旧调用名（两参数形式仍可用）
def _decode_compressed_image(encoding, data, schema_name=None):
    return decode_video(schema_name, encoding, data)


def _decode_imu(encoding, data):
    enc = (encoding or '').lower()
    if enc == 'protobuf':
        d = pb_fields(data)
        av = pb_bytes(d, 3)
        la = pb_bytes(d, 4)
        return dict(
            frame_id=pb_str(d, 2, ''),
            angular_velocity=list(pb_vec3(av)) if av else [0.0, 0.0, 0.0],
            linear_acceleration=list(pb_vec3(la)) if la else [0.0, 0.0, 0.0],
        )
    if enc == 'ros1':
        c = _Ros1Cursor(data)
        c.u32(); c.u32(); c.u32()
        c.string()
        ori = struct.unpack_from('<4d', data, c.o); c.o += 32
        c.o += 72
        ang = struct.unpack_from('<3d', data, c.o); c.o += 24
        c.o += 72
        lin = struct.unpack_from('<3d', data, c.o); c.o += 24
        return dict(frame_id='', angular_velocity=list(ang),
                    linear_acceleration=list(lin), orientation=list(ori))
    if enc == 'json':
        try:
            j = json.loads(data.decode('utf-8', 'replace'))
        except Exception:
            return None
        return dict(frame_id=j.get('frame_id', ''),
                    angular_velocity=j.get('angular_velocity', [0, 0, 0]),
                    linear_acceleration=j.get('linear_acceleration', [0, 0, 0]))
    return None


def _decode_audio(encoding, data):
    if (encoding or '').lower() != 'protobuf':
        return None
    d = pb_fields(data)
    cfg = {}
    cbuf = pb_bytes(d, 3)
    if cbuf:
        c = pb_fields(cbuf)
        cfg = dict(
            sample_rate=pb_int(c, 1, 0),
            channels=pb_int(c, 2, 0),
            bit_depth=pb_int(c, 3, 0),
            format=pb_str(c, 4, ''),
            device_name=pb_str(c, 5, ''),
            period_size=pb_int(c, 6, 0),
        )
    return dict(
        config=cfg,
        data=pb_bytes(d, 4) or b'',
        sample_count=pb_int(d, 5, 0),
        chunk_duration=pb_f32(d, 6, 0.0),
        chunk_sequence=pb_int(d, 7, 0),
        total_chunks=pb_int(d, 8, 0),
        frame_id=pb_str(d, 2, ''),
    )


def _decode_calibration(encoding, data):
    if (encoding or '').lower() != 'protobuf':
        return None
    d = pb_fields(data)
    return dict(
        width=pb_u32(d, 2, 0),
        height=pb_u32(d, 3, 0),
        distortion_model=pb_str(d, 4, ''),
        K=pb_packed_doubles(d, 6),
        P=pb_packed_doubles(d, 8),
        D=pb_packed_doubles(d, 5),
        T_b_c=pb_packed_doubles(d, 10),
        frame_id=pb_str(d, 9, ''),
    )


def _decode_system_info(encoding, data):
    if (encoding or '').lower() != 'protobuf':
        return None
    d = pb_fields(data)
    return dict(
        pid=pb_int(d, 3),
        memory_kb=pb_int(d, 4),
        rss_kb=pb_int(d, 5),
        cpu_percent=pb_f32(d, 6),
        memory_percent=pb_f32(d, 7),
        version=pb_str(d, 10, ''),
    )


# ================================================================= 容器层
def _wrap_error(path, exc, offset=None):
    detail = str(exc)
    where = os.path.basename(path)
    if offset is not None:
        where += ' 偏移 0x%X' % max(0, offset)
    return McapError('MCAP 解析失败 [%s]：%s' % (where, detail))


def _iter_data_records(fh):
    """线性遍历数据区记录（Chunk 会被 StreamReader 自动展开成内部记录）。

    StreamReader 默认不校验 CRC，而官方 SeekingReader 在没有 chunk 索引（没有 summary 区）
    时也会退化成不校验 CRC 的线性读取 —— 所以这里显式打开 validate_crcs，
    保证任何文件的 Chunk CRC 与 DataEnd CRC 都被检查。
    """
    sr = StreamReader(fh, skip_magic=False, validate_crcs=True)
    for rec in sr.records:
        yield rec


class McapReader:
    """容器 + 索引。``channels`` / ``schemas`` 结构与旧版保持一致。"""

    def __init__(self, path, lazy=False):
        if not _HAS_MCAP:
            raise McapError('缺少官方 mcap 解析库，请先安装：pip install "mcap>=1.3,<2"')
        self.path = os.path.abspath(path)
        if not os.path.isfile(self.path):
            raise McapError('文件不存在：%s' % self.path)
        self.size = os.path.getsize(self.path)
        self.header = dict(profile='', library='')
        self.schemas = {}       # schema_id -> dict
        self.channels = {}      # channel_id -> dict
        self.stats = None
        self.chunk_count = 0
        self.attachment_count = 0
        self.metadata_count = 0
        self.had_statistics = False     # 文件里是否真的写了 Statistics 记录
        self.has_index = False          # 是否有可用的 chunk 索引（summary 区）
        self._official_summary = None   # indexed streaming 路径复用的只读 Summary
        #: lazy=True：没有索引时**不**在这里做整文件扫描，交给调用方在正式遍历里
        #: 用 collect=True 一边遍历一边补齐（OPT-03：全文件只完整读一遍）
        self.lazy = bool(lazy)
        self._iter_offset = 0
        self._load_index()

    def _load_index(self):
        offset = 0
        hdr = None
        summ = None
        try:
            with open(self.path, 'rb') as fh:
                r = make_reader(fh, validate_crcs=True)
                try:
                    hdr = r.get_header()
                except Exception as e:
                    raise _wrap_error(self.path, e, fh.tell())
                offset = fh.tell()
                try:
                    summ = r.get_summary()
                except Exception as e:
                    raise _wrap_error(self.path, e, fh.tell())
                offset = fh.tell()
        except McapError:
            raise
        except Exception as e:
            raise _wrap_error(self.path, e, offset)

        self.header = dict(profile=getattr(hdr, 'profile', '') or '',
                           library=getattr(hdr, 'library', '') or '')

        if summ is not None:
            self._official_summary = summ
            for sid, s in (getattr(summ, 'schemas', None) or {}).items():
                self.schemas[sid] = dict(id=s.id, name=s.name,
                                         encoding=s.encoding, data=s.data)
            for cid, c in (getattr(summ, 'channels', None) or {}).items():
                self.channels[cid] = dict(
                    id=c.id, topic=c.topic, schema_id=c.schema_id,
                    message_encoding=c.message_encoding,
                    metadata=dict(c.metadata or {}))
            st = getattr(summ, 'statistics', None)
            if st is not None:
                self.had_statistics = True
                self.stats = dict(
                    message_count=st.message_count,
                    schema_count=st.schema_count,
                    channel_count=st.channel_count,
                    attachment_count=st.attachment_count,
                    metadata_count=st.metadata_count,
                    chunk_count=st.chunk_count,
                    message_start_time_ns=st.message_start_time,
                    message_end_time_ns=st.message_end_time,
                    channel_message_counts=dict(st.channel_message_counts or {}),
                )
                self.chunk_count = st.chunk_count
                self.attachment_count = st.attachment_count
                self.metadata_count = st.metadata_count
            self.has_index = bool(getattr(summ, 'chunk_indexes', None))

        # 没有 summary 区 / 没有 Statistics：整文件线性扫一遍补齐，
        # 顺带用 breakup_chunk(validate_crc=True) 校验每个 Chunk 的 CRC。
        # lazy 模式跳过这一步：调用方会在正式遍历时用 collect=True 补齐，
        # 从而避免"补元信息扫一遍 + 抽取再扫一遍"（OPT-03）。
        if not self.lazy and (not self.channels or not self.stats):
            self._scan_all()

    def _scan_all(self):
        counts = {}
        start_ns = None
        end_ns = None
        offset = 0
        try:
            with open(self.path, 'rb') as fh:
                for rec in _iter_data_records(fh):
                    offset = fh.tell()
                    if _RecSchema is not None and isinstance(rec, _RecSchema):
                        self.schemas.setdefault(rec.id, dict(
                            id=rec.id, name=rec.name,
                            encoding=rec.encoding, data=rec.data))
                    elif _RecChannel is not None and isinstance(rec, _RecChannel):
                        self.channels.setdefault(rec.id, dict(
                            id=rec.id, topic=rec.topic, schema_id=rec.schema_id,
                            message_encoding=rec.message_encoding,
                            metadata=dict(rec.metadata or {})))
                    elif _ChunkMessage is not None and isinstance(rec, _ChunkMessage):
                        counts[rec.channel_id] = counts.get(rec.channel_id, 0) + 1
                        t = rec.log_time
                        if start_ns is None or t < start_ns:
                            start_ns = t
                        if end_ns is None or t > end_ns:
                            end_ns = t
        except McapError:
            raise
        except Exception as e:
            raise _wrap_error(self.path, e, offset)
        self.stats = dict(
            message_count=sum(counts.values()),
            schema_count=len(self.schemas),
            channel_count=len(self.channels),
            attachment_count=self.attachment_count,
            metadata_count=self.metadata_count,
            chunk_count=self.chunk_count,
            message_start_time_ns=start_ns or 0,
            message_end_time_ns=end_ns or 0,
            channel_message_counts=counts,
        )

    def iter_messages(self, wanted=None, log_time_order=True, collect=False,
                      pushdown=True):
        """流式遍历。yield (channel_id, log_time_ns, publish_time_ns, sequence, data)

        * ``wanted``：只返回这些 channel id 的消息（库外精确过滤，语义不变）；
        * ``pushdown``：把 ``wanted`` 映射成 topic 白名单下推给官方库，
          被排除通道的 payload 不会被读出来（OPT-01：省掉 camera1/4/5/6 的数据搬运）。
          仅在有索引（channels 已知）时生效；``pushdown=False`` 可关闭；
        * ``log_time_order``：True = 官方库做全局时间排序（默认，保持旧行为）；
          False = 按文件 chunk 顺序输出（缓存抽取用，OPT-02）；
        * ``collect``：边遍历边登记 channels/schemas 与统计（时间范围、每通道计数），
          遍历结束后写入 ``self.stats`` —— 供无索引文件的**单遍**缓存使用（OPT-03）。
        """
        fh = None
        offset = 0
        counts = {}
        start_ns = None
        end_ns = None
        try:
            fh = open(self.path, 'rb')
            topics = None
            if pushdown and not collect and wanted is not None and self.channels:
                topics = {self.channels[c]['topic'] for c in wanted
                          if c in self.channels}
            use_streaming = (
                _reader_mode() == 'chunk_streaming'
                and not log_time_order
                and self.has_index
                and self._official_summary is not None
                and ChunkStreamingIndexedReader is not None)
            if use_streaming:
                iterator = ChunkStreamingIndexedReader(
                    fh, self._official_summary,
                    validate_crcs=True).iter_messages(topics=topics)
            else:
                # official is retained for debug/A-B, log-time ordered reads,
                # and capability fallback when Summary/ChunkIndex is absent.
                r = make_reader(fh, validate_crcs=True)
                iterator = r.iter_messages(
                    topics=topics, log_time_order=log_time_order)
            for schema, channel, message in iterator:
                offset = fh.tell()
                self._iter_offset = offset
                if collect:
                    # 统计覆盖所有通道（在 wanted 过滤之前），保证
                    # time_base / duration 与"全文件扫描"口径一致
                    if schema is not None:
                        self.schemas.setdefault(schema.id, dict(
                            id=schema.id, name=schema.name,
                            encoding=schema.encoding, data=schema.data))
                    self.channels.setdefault(channel.id, dict(
                        id=channel.id, topic=channel.topic,
                        schema_id=channel.schema_id,
                        message_encoding=channel.message_encoding,
                        metadata=dict(channel.metadata or {})))
                    counts[channel.id] = counts.get(channel.id, 0) + 1
                    t = message.log_time
                    if start_ns is None or t < start_ns:
                        start_ns = t
                    if end_ns is None or t > end_ns:
                        end_ns = t
                if wanted is not None and channel.id not in wanted:
                    continue
                yield (channel.id, message.log_time, message.publish_time,
                       message.sequence, message.data)
        except McapError:
            raise
        except Exception as e:
            raise _wrap_error(self.path, e, offset)
        finally:
            if collect and start_ns is not None:
                self.stats = dict(
                    message_count=sum(counts.values()),
                    schema_count=len(self.schemas),
                    channel_count=len(self.channels),
                    attachment_count=self.attachment_count,
                    metadata_count=self.metadata_count,
                    chunk_count=self.chunk_count,
                    message_start_time_ns=start_ns,
                    message_end_time_ns=end_ns,
                    channel_message_counts=counts,
                )
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass

    def summary(self, recount='auto'):
        st = self.stats or {}
        counts = dict(st.get('channel_message_counts') or {})
        # lazy 模式（无索引预扫描）绝不在这里触发扫描：调用方负责在正式遍历时
        # 用 collect=True 收集（OPT-03：全文件只完整读一遍）
        if not self.lazy and (recount is True or
                              (recount == 'auto' and
                               (not counts or len(counts) != len(self.channels)))):
            self._scan_all()
            st = self.stats
            counts = dict(st.get('channel_message_counts') or {})

        start_ns = st.get('message_start_time_ns', 0) or 0
        end_ns = st.get('message_end_time_ns', 0) or 0
        duration_s = (end_ns - start_ns) / NS if end_ns > start_ns else 0.0

        chans = []
        for cid, ch in sorted(self.channels.items()):
            cnt = counts.get(cid, 0)
            sch = self.schemas.get(ch['schema_id'], {})
            chans.append(dict(
                id=cid,
                topic=ch['topic'],
                schema=sch.get('name', ''),
                schema_encoding=sch.get('encoding', ''),
                message_encoding=ch['message_encoding'],
                metadata=ch.get('metadata') or {},
                count=cnt,
                hz=round(cnt / duration_s, 2) if duration_s > 0 else 0.0,
                kind=classify(ch['topic'], sch.get('name', '')),
            ))
        return dict(
            path=self.path,
            name=os.path.basename(self.path),
            size=self.size,
            profile=self.header.get('profile', ''),
            library=self.header.get('library', ''),
            # 显式单位
            start_time_ns=start_ns,
            end_time_ns=end_ns,
            duration_s=duration_s,
            # 兼容网页版 server.py 以秒为单位的旧字段名
            start_time=start_ns,
            end_time=end_ns,
            duration=duration_s,
            message_count=st.get('message_count', 0),
            chunk_count=st.get('chunk_count', self.chunk_count),
            attachment_count=st.get('attachment_count', self.attachment_count),
            metadata_count=st.get('metadata_count', self.metadata_count),
            has_statistics=self.had_statistics,
            has_index=self.has_index,
            channels=chans,
            schemas=[dict(id=s['id'], name=s['name'], encoding=s['encoding'],
                          length=len(s['data'] or b'')) for s in self.schemas.values()],
        )


# ================================================================= 便捷入口
def scan(path):
    return McapReader(path).summary()


def read(path, want_video=True, want_imu=True, want_audio=True,
         want_calibration=True, progress=None):
    """一次性读出并按语义归类（视频返回原始 payload + 时间戳，不落盘）"""
    r = McapReader(path)
    summ = r.summary()
    kinds = {c['id']: c['kind'] for c in summ['channels']}
    by_id = {c['id']: c for c in summ['channels']}

    cams = {}
    imus = {}
    audios = {}
    cals = {}
    systems = {}
    total = summ['message_count'] or 1
    done = 0
    step = max(1, total // 100)

    wanted = set()
    for cid, kind in kinds.items():
        if kind == 'video' and want_video:
            wanted.add(cid)
        elif kind == 'imu' and want_imu:
            wanted.add(cid)
        elif kind == 'audio' and want_audio:
            wanted.add(cid)
        elif kind == 'calibration' and want_calibration:
            wanted.add(cid)
        elif kind == 'system':
            wanted.add(cid)

    for cid, lg, pub, seq, data in r.iter_messages(wanted):
        done += 1
        if progress and done % step == 0:
            progress(done / total)
        kind = kinds.get(cid, 'other')
        info = by_id[cid]
        if kind == 'video':
            fmt, payload, extra = decode_video(info['schema'], info['message_encoding'], data)
            if not payload:
                continue
            c = cams.get(cid)
            if c is None:
                c = cams[cid] = dict(id=cid, topic=info['topic'], schema=info['schema'],
                                     format=fmt, frame_id=extra.get('frame_id', ''),
                                     frames=[], total_bytes=0, extra=extra, formats=set())
            c['formats'].add((fmt or '').lower())
            c['frames'].append((lg, payload))
            c['total_bytes'] += len(payload)
            if not c['frame_id'] and extra.get('frame_id'):
                c['frame_id'] = extra['frame_id']
        elif kind == 'imu':
            m = _decode_imu(info['message_encoding'], data)
            if m:
                imus.setdefault(cid, dict(id=cid, topic=info['topic'], schema=info['schema'],
                                          samples=[]))['samples'].append((lg, m))
        elif kind == 'audio':
            m = _decode_audio(info['message_encoding'], data)
            if m:
                audios.setdefault(cid, dict(id=cid, topic=info['topic'], schema=info['schema'],
                                            config=m['config'], chunks=[]))
                audios[cid]['chunks'].append((lg, m))
        elif kind == 'calibration':
            m = _decode_calibration(info['message_encoding'], data)
            if m:
                cals.setdefault(info['topic'], m)
        elif kind == 'system':
            m = _decode_system_info(info['message_encoding'], data)
            if m:
                systems.setdefault(cid, []).append((lg, m))

    if progress:
        progress(1.0)
    for c in cams.values():
        c['formats'] = sorted(c['formats'])
    return dict(
        summary=summ,
        cameras=[cams[k] for k in sorted(cams)],
        imus=[imus[k] for k in sorted(imus)],
        audios=[audios[k] for k in sorted(audios)],
        calibrations=cals,
        systems=[dict(id=k, samples=v) for k, v in sorted(systems.items())],
    )
