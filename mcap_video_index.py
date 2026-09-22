"""mcap_video_index.py —— P1.6F-B：基于 MCAP 内建索引的轻量视频定位索引

合同（F-B）：
  * **不解压、不遍历 payload** 建立索引 —— 只用 Summary / ChunkIndex / MessageIndex；
  * 每条 entry 只有十几字节级 metadata（timestamp / chunk_offset / 记录偏移），
    绝不保存 H264 payload、解码图像或 chunk 数据；
  * 排序规则固定为 ``sort_key = (log_time_ns, stable_physical_order)``，
    并显式给出单调性结论（F-A 已发现物理顺序 ≠ log_time 顺序）；
  * 读取时才按需解压所在 chunk，带 2-chunk 缓存（hard cap 128MB）。

读取路径与缓存/播放解耦：本模块不改动任何现有缓存或播放代码。
"""

import bisect
import io
import os

from mcap.reader import ReadDataStream, make_reader
from mcap.records import Chunk, Message
from mcap.stream_reader import get_chunk_data_stream

import h264mp4 as _H

OP_MESSAGE = 0x05


class FrameRef(object):
    """一条视频帧的定位信息（轻量 metadata）"""

    __slots__ = ('ts_ns', 'chunk_offset', 'record_offset', 'seq',
                 'payload_size', 'is_keyframe')

    def __init__(self, ts_ns, chunk_offset, record_offset, seq,
                 payload_size=None, is_keyframe=None):
        self.ts_ns = ts_ns
        self.chunk_offset = chunk_offset
        self.record_offset = record_offset
        self.seq = seq                      # 稳定物理顺序（tie-break）
        self.payload_size = payload_size    # 读取时才知道（懒填）
        self.is_keyframe = is_keyframe      # 由 NAL 类型懒确认


class CameraVideoIndex(object):
    def __init__(self, topic, channel_id):
        self.topic = topic
        self.channel_id = channel_id
        self.frames = []                    # 已按 sort_key 排序
        self.times = []                     # 与 frames 平行（bisect 用）
        self.physical_min_us = 0.0
        self.physical_monotonic = True
        self.sorted_monotonic = True
        self.chunk_start_ns = None          # chunk 级索引（未建帧索引时可用）
        self.chunk_end_ns = None

    def add_physical(self, ts_ns, chunk_offset, record_offset, seq):
        self.frames.append(FrameRef(ts_ns, chunk_offset, record_offset, seq))

    def finalize(self):
        """按 (log_time, 物理顺序) 排序并验证单调性"""
        if self.frames:
            self.physical_monotonic = all(
                self.frames[i].ts_ns <= self.frames[i + 1].ts_ns
                for i in range(len(self.frames) - 1))
        self.frames.sort(key=lambda f: (f.ts_ns, f.seq))
        if self.frames:
            self.sorted_monotonic = all(
                self.frames[i].ts_ns <= self.frames[i + 1].ts_ns
                for i in range(len(self.frames) - 1))
        self.times = [f.ts_ns for f in self.frames]
        return self

    # ---- 查询 ----
    def nearest(self, ts_ns):
        """<= ts 的最近帧；早于首帧则返回首帧"""
        if not self.frames:
            return None
        i = bisect.bisect_right(self.times, ts_ns) - 1
        return self.frames[0 if i < 0 else i]

    def at(self, ts_ns):
        i = bisect.bisect_left(self.times, ts_ns)
        if i < len(self.frames) and self.frames[i].ts_ns == ts_ns:
            return self.frames[i]
        return None

    def start_ts(self):
        if self.times:
            return self.times[0]
        return self.chunk_start_ns

    def end_ts(self):
        if self.times:
            return self.times[-1]
        return self.chunk_end_ns


class McapVideoIndex(object):
    """基于 MCAP Summary 的快速索引（不解压任何 chunk）"""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.size = os.path.getsize(self.path)
        self.cameras = {}                  # cid -> CameraVideoIndex
        self.has_index = False
        self.has_message_index = False
        self.chunk_count = 0
        self.granularity = 'chunk'         # 'message'（有 MessageIndex）| 'chunk'
        self.chunk_index = {}              # cid -> [chunk 元数据]
        self.frame_index_ms = None
        self.build_ms = None
        self._cache = {}                   # chunk_offset -> (bytes, channel_msgs)
        self._cache_order = []
        self.cache_cap_bytes = 128 * 1024 * 1024
        self._cache_bytes = 0

    # ================================================== 建索引（纯 metadata）
    def build(self, camera_ids=None, camera_pred=None):
        t0 = _now()
        with open(self.path, 'rb') as fh:
            r = make_reader(fh, validate_crcs=False)
            summ = r.get_summary()
        if summ is None:
            self.build_ms = (_now() - t0) * 1000.0
            return self                       # 无索引：调用方 fallback
        self.has_index = bool(getattr(summ, 'chunk_indexes', None))
        self.chunk_count = len(getattr(summ, 'chunk_indexes', []) or [])
        chans = getattr(summ, 'channels', {}) or {}
        topics = {cid: ch['topic'] if isinstance(ch, dict) else ch.topic
                  for cid, ch in chans.items()}
        if camera_ids is None:
            camera_ids = [cid for cid, t in topics.items()
                          if camera_pred is None or camera_pred(t)]
        for cid in camera_ids:
            self.cameras[cid] = CameraVideoIndex(topics.get(cid, ''), cid)
        # 先建 **chunk 级** 快速索引（纯元数据、不解压）：立刻知道每个 camera 的
        # chunk 列表与时间范围 —— 足以开始"打开即顺序播放"。
        self.chunk_index = {}
        for ci in sorted(summ.chunk_indexes or [],
                         key=lambda x: int(x.chunk_start_offset)):
            entry = dict(chunk_offset=int(ci.chunk_start_offset),
                         start_ns=int(ci.message_start_time),
                         end_ns=int(ci.message_end_time),
                         length=int(ci.chunk_length),
                         uncompressed=int(ci.uncompressed_size))
            for cid in self.cameras:
                self.chunk_index.setdefault(cid, []).append(entry)
        for cid, lst in self.chunk_index.items():
            if lst:
                self.cameras[cid].chunk_start_ns = min(c['start_ns'] for c in lst)
                self.cameras[cid].chunk_end_ns = max(c['end_ns'] for c in lst)
        mi = getattr(summ, 'message_indexes', None) or {}
        self.has_message_index = bool(mi)
        seq = 0
        for ci in sorted(summ.chunk_indexes or [],
                         key=lambda x: int(x.chunk_start_offset)):
            offs = getattr(ci, 'message_index_offsets', None) or {}
            for cid in list(self.cameras.keys()):
                if cid not in offs:
                    continue
                entries = mi.get((cid, int(ci.chunk_start_offset))) or []
                if entries:
                    for e in entries:                 # MessageIndexEntry
                        self.cameras[cid].add_physical(
                            int(e.timestamp), int(ci.chunk_start_offset),
                            int(e.offset), seq)
                        seq += 1
                else:
                    # 该 chunk 含目标通道但拿不到 message index → 退化为 chunk 粒度
                    self.granularity = 'chunk'
                    self.cameras[cid].add_physical(
                        int(ci.message_start_time), int(ci.chunk_start_offset),
                        0, seq)
                    seq += 1
        for cam in self.cameras.values():
            cam.finalize()
        self.build_ms = (_now() - t0) * 1000.0
        return self

    # ================================================== 帧级索引（后台/lazy）
    def build_frame_index(self, progress=None):
        """顺序解压每个 chunk，只解析**消息记录头**（channel_id / log_time /
        记录偏移），**不做 NAL 解析、不做 protobuf 解码、不写任何缓存**。

        实测这些 MCAP 只写了 ChunkIndex 而没有 MessageIndex，因此帧级定位
        需要这一次顺序扫描；它远快于完整缓存（无解码/无写出），适合后台执行。
        """
        t0 = _now()
        chunks = sorted((c for lst in self.chunk_index.values() for c in lst),
                        key=lambda x: x['chunk_offset'])
        seen = set()
        with open(self.path, 'rb') as fh:
            for cam in self.cameras.values():
                cam.frames = []
            seq = 0
            for i, ci in enumerate(chunks):
                if ci['chunk_offset'] in seen:      # 多 camera 共享 chunk 只扫一次
                    continue
                seen.add(ci['chunk_offset'])
                fh.seek(ci['chunk_offset'] + 1 + 8)
                chunk = Chunk.read(ReadDataStream(fh))
                stream, length = get_chunk_data_stream(chunk, validate_crc=False)
                while stream.count < length:
                    op = stream.read1()
                    ln = stream.read8()
                    rec_start = stream.count - 9
                    if op == OP_MESSAGE:
                        ch_id = stream.read2()
                        stream.read4()              # sequence
                        log_time = stream.read8()
                        stream.read8()              # publish_time
                        stream.read(ln - 22)        # 跳过 payload
                        cam = self.cameras.get(ch_id)
                        if cam is not None:
                            cam.add_physical(int(log_time), ci['chunk_offset'],
                                             rec_start, seq)
                            seq += 1
                    else:
                        stream.read(ln)
                if progress is not None:
                    progress(i + 1, len(chunks))
        for cam in self.cameras.values():
            cam.finalize()
        self.frame_index_ms = (_now() - t0) * 1000.0
        return self

    # ================================================== 读取（按需解压单 chunk）
    def _load_chunk(self, chunk_offset):
        hit = self._cache.get(chunk_offset)
        if hit is not None:
            return hit
        with open(self.path, 'rb') as fh:
            fh.seek(int(chunk_offset) + 1 + 8)
            chunk = Chunk.read(ReadDataStream(fh))
        stream, length = get_chunk_data_stream(chunk, validate_crc=False)
        msgs = {}
        while stream.count < length:
            op = stream.read1()
            ln = stream.read8()
            if op == OP_MESSAGE:
                m = Message.read(stream, ln)
                msgs.setdefault((m.channel_id, m.log_time), m.data)
            else:
                stream.read(ln)
        payload = chunk.data
        self._cache[chunk_offset] = (payload, msgs)
        self._cache_order.append(chunk_offset)
        self._cache_bytes += len(payload)
        while len(self._cache_order) > 2 or self._cache_bytes > self.cache_cap_bytes:
            old = self._cache_order.pop(0)
            got = self._cache.pop(old, None)
            if got:
                self._cache_bytes -= len(got[0])
        return (payload, msgs)

    def read_frame(self, cam, frame):
        """读取某帧的 H264 payload（只解压它所在的那个 chunk）"""
        _p, msgs = self._load_chunk(frame.chunk_offset)
        data = msgs.get((cam.channel_id, frame.ts_ns))
        if data is None and self.granularity == 'chunk':
            for (cid, ts), d in msgs.items():    # chunk 粒度：取该 chunk 内最近帧
                if cid == cam.channel_id and frame.ts_ns - 100_000_000 <= ts <= frame.ts_ns:
                    data = d
                    break
        frame.payload_size = len(data) if data else 0
        return data

    def cache_stats(self):
        return dict(chunks=len(self._cache), bytes=self._cache_bytes)


def _now():
    import time
    return time.perf_counter()


# ====================================================================== D0
def payload_has_idr(payload):
    """该 Access Unit 是否含 IDR 切片（不假设"每帧都是 IDR"）"""
    try:
        return any(_H.nal_type(n) == 5 for n in _H.split_nalus(payload))
    except Exception:
        return False


def payload_has_params(payload):
    """是否自带 SPS/PPS"""
    try:
        types = {_H.nal_type(n) for n in _H.split_nalus(payload)}
        return 7 in types and 8 in types
    except Exception:
        return False


class DirectSeekResolver(object):
    """**统一的 Direct 随机访问解析**（F-D0-A/B）。

    规则（合同第 6/7/8 节）：
      * presentation target = ``first frame >= target_time``；
        当前 chunk 内没有就**继续到下一个该 camera 的 chunk**，直到找到或视频结束；
      * 只有 ``target > 最后一帧`` 才允许返回最后一帧；
      * decode start = ``<= target_frame`` 的**最近 IDR**（先看当前 chunk，再向前找）；
      * 首帧 / seek / camera switch / sparse / catch-up **全部走本类**，
        不允许各写一套"找 IDR"。

    返回 dict：target_ts / target_payload / decode_start_ts / start_payload /
              start_is_idr / start_has_params / chunks_touched
    """

    def __init__(self, index, max_forward=8, max_back=4):
        self.index = index
        self.max_forward = int(max_forward)
        self.max_back = int(max_back)

    # ---------------------------------------------------------------- 内部
    def _chunks(self, cam):
        return self.index.chunk_index.get(cam.channel_id) or []

    def _chunk_idx_for(self, chunks, ts_ns):
        lo, hi, best = 0, len(chunks) - 1, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if chunks[mid]['start_ns'] <= ts_ns:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def _frames(self, cam, chunk_entry):
        _p, msgs = self.index._load_chunk(chunk_entry['chunk_offset'])
        items = [(ts, d) for (cid, ts), d in msgs.items() if cid == cam.channel_id]
        items.sort(key=lambda x: x[0])
        return items

    # ---------------------------------------------------------------- 解析
    def resolve(self, cam, target_ns):
        chunks = self._chunks(cam)
        if not chunks:
            return None
        touched = 0
        i0 = self._chunk_idx_for(chunks, target_ns)
        # 1) presentation target：允许跨到后续 chunk
        target = None
        for k in range(i0, min(i0 + self.max_forward, len(chunks))):
            items = self._frames(cam, chunks[k])
            touched += 1
            for ts, pl in items:
                if ts >= target_ns:
                    target = (ts, pl, k)
                    break
            if target is not None:
                break
        if target is None:
            # 2) 只有超出末尾才回退到最后一帧
            last_ts, last_pl, last_k = None, None, None
            for k in range(len(chunks) - 1, max(-1, len(chunks) - 1 - 3), -1):
                items = self._frames(cam, chunks[k])
                touched += 1
                if items:
                    last_ts, last_pl, last_k = items[-1][0], items[-1][1], k
                    break
            if last_ts is None:
                return None
            target = (last_ts, last_pl, last_k)
        tts, tpl, tk = target
        # 3) decode start = <= target 的最近 IDR（当前 chunk → 向前）
        start = None
        for k in range(tk, max(-1, tk - self.max_back), -1):
            items = self._frames(cam, chunks[k])
            touched += 1
            for ts, pl in reversed(items):
                if ts <= tts and payload_has_idr(pl):
                    start = (ts, pl, k)
                    break
            if start is not None:
                break
        if start is None:
            # 向前找不到 IDR（例如 seek 到视频开头）→ 向后找第一个 IDR 起解。
            # 绝不"直接解目标帧"：目标帧可能是 P 帧，单帧无法独立解码。
            for k in range(tk, min(tk + self.max_forward, len(chunks))):
                items = self._frames(cam, chunks[k])
                touched += 1
                for ts, pl in items:
                    if payload_has_idr(pl):
                        start = (ts, pl, k)
                        break
                if start is not None:
                    break
        if start is None:
            return None
        return dict(target_ts=tts, target_payload=tpl, target_chunk=tk,
                    decode_start_ts=start[0], start_payload=start[1],
                    start_chunk=start[2], start_is_idr=payload_has_idr(start[1]),
                    start_has_params=payload_has_params(start[1]),
                    chunks_touched=touched)

    # ---------------------------------------------------------------- 统一入口
    def decode_to_target(self, cam, target_ns, decoder):
        """一站式：resolve → 从 IDR 起解 → 返回 (selected_ts, frame, info)"""
        info = self.resolve(cam, target_ns)
        if info is None:
            return (None, None, None)
        chunks = self._chunks(cam)
        decoder.reset()
        pair = None
        # 覆盖 start 与 target 之间的所有 chunk（start 可能位于 target 之后：
        # 例如 seek 到视频开头时，最近的可用 IDR 就在目标帧之后）
        lo = min(info['start_chunk'], info['target_chunk'])
        hi = max(info['start_chunk'], info['target_chunk'])
        for k in range(lo, min(hi + 2, len(chunks))):
            items = self._frames(cam, chunks[k])
            started = (k != info['start_chunk'])
            for ts, pl in items:
                if not started:
                    if ts == info['decode_start_ts']:
                        started = True
                    else:
                        continue
                if ts < info['decode_start_ts']:
                    continue
                frames = decoder.decode_access_unit(pl)
                if frames:
                    pair = (ts, frames[-1])
                    if ts >= info['target_ts']:
                        return (ts, frames[-1], info)
        if pair is not None:
            return (pair[0], pair[1], info)
        return (None, None, info)
