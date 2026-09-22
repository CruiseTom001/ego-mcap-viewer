"""video_source.py —— P1.6F-D1 + FIX001：统一 VideoSource 双后端抽象

对外统一语义（D1 合同）：
    open() / close() / duration() / cameras() / active_camera()
    seek(media_time) -> VideoFrame        （定位并返回目标帧）
    frame_at(media_time) -> VideoFrame    （无状态随机访问）
    switch_camera(camera, media_time) -> VideoFrame
    stats()

FIX001（canonical timeline + frame identity）：
  * **canonical 零点 = 两路 camera 中「首个实际视频帧」的最小源时间**
    （Direct 用 MCAP log_time；MP4 用 prepare 写入的 start_offset_s）；
    **不再用 chunk 起点**作为时间原点；
  * **MP4 帧身份由 `.times` 索引决定**：`bisect_left(norm_times, t)` → 目标帧号 i；
    PyAV pts 只当 **locator**（预测 seek 位置 + 定位逻辑帧号），绝不作为帧身份判定；
  * 两后端对同一 media_time 必须解析出同一源帧。

本模块只做接口统一：不接 Desktop、不改 QueueManager/缓存管道、不做 fallback。
"""

import bisect
import os
import struct
from dataclasses import dataclass

import appcache
import direct_h264_decoder as DD
import mcap_video_index as VI


class VideoSourceError(Exception):
    """所有 VideoSource 错误的基类"""


class UnsupportedVideoSource(VideoSourceError):
    """该源不适合此 backend（能力不足）"""


class VideoDecodeError(VideoSourceError):
    """解码失败"""


class VideoSeekError(VideoSourceError):
    """seek 越界或定位失败"""


class CorruptVideoSource(VideoSourceError):
    """源数据损坏/截断"""


@dataclass
class VideoFrame:
    image: object                 # numpy BGR24
    media_time: float             # canonical 秒，从 0 开始
    camera: str                   # 'camera2' / 'camera3'
    width: int
    height: int
    frame_index: int = -1         # MP4: .times 索引；Direct: -1（不建全局帧索引）
    source_time_ns: int = 0       # **帧身份权威**：原 MCAP 视频消息 log_time（int64）


@dataclass
class VideoSourceCapability:
    supported: bool
    reason: str = ''
    cameras: tuple = ()


#: 统一相机名 → MCAP topic（沿用仓库既有约定）
CAMERA_TOPICS = {
    'camera2': '/robot0/sensor/camera2/compressed',
    'camera3': '/robot0/sensor/camera3/compressed',
}


def _norm_camera(name):
    n = (name or '').strip().lower().replace('-', '')
    if n in ('camera2', 'cam2', '2') or n.endswith('camera2'):
        return 'camera2'
    if n in ('camera3', 'cam3', '3') or n.endswith('camera3'):
        return 'camera3'
    raise VideoSourceError('未知相机：%r' % name)


def _frame_interval(raw):
    """raw（秒）序列的中位帧间隔；样本不足时给 30fps 的默认值"""
    if len(raw) < 2:
        return 1.0 / 30.0
    d = sorted(raw[k + 1] - raw[k] for k in range(min(len(raw) - 1, 60)))
    med = d[len(d) // 2]
    return med if med > 0 else 1.0 / 30.0


def _read_times(path):
    with open(path, 'rb') as fh:
        data = fh.read()
    n = len(data) // 8
    return list(struct.unpack('<%dd' % n, data[:n * 8])) if n else []


class VideoSource(object):
    """接口定义（子类实现）"""

    def open(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def duration(self):
        raise NotImplementedError

    def cameras(self):
        raise NotImplementedError

    def active_camera(self):
        raise NotImplementedError

    def seek(self, media_time):
        raise NotImplementedError

    def frame_at(self, media_time):
        return self.seek(media_time)              # 默认无状态

    def switch_camera(self, camera, media_time):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError


class Mp4VideoSource(VideoSource):
    """包装现有稳定 MP4 缓存（帧身份 = .times 索引；pts 仅 locator）"""

    def __init__(self, mcap_path, manifest=None, cache_dir=None, camera='camera2'):
        self.mcap_path = os.path.abspath(mcap_path)
        self.cache_dir = cache_dir
        self.manifest = manifest
        self._camera = _norm_camera(camera)
        self._containers = {}
        self._streams = {}
        self._times = {}
        self._t0 = {}
        self._canon_off = None
        self._pts_off = {}          # pts → .times 基准的偏移（仅 locator 用）
        self._last_steps = 0        # 上次 seek 顺序解码的帧数
        self.opened = False

    def open(self):
        if not self.manifest:
            fid = appcache.file_id(self.mcap_path)
            self.cache_dir = self.cache_dir or appcache.cache_dir(
                appcache.cache_key(fid))
            self.manifest = appcache.load_manifest(self.cache_dir,
                                                   source_path=self.mcap_path)
        if not self.manifest:
            raise UnsupportedVideoSource('没有可用的 MP4 缓存')
        import av
        for cam_meta in self.manifest.get('cameras') or []:
            topic = cam_meta.get('topic') or ''
            name = ('camera2' if 'camera2' in topic else
                    'camera3' if 'camera3' in topic else None)
            if name is None:
                continue
            mp4 = os.path.join(self.cache_dir, cam_meta.get('file') or '')
            if not os.path.isfile(mp4):
                continue
            cont = av.open(mp4)
            self._containers[name] = cont
            self._streams[name] = cont.streams.video[0]
            tp = os.path.join(self.cache_dir, cam_meta.get('times_file') or '')
            self._times[name] = _read_times(tp) if os.path.isfile(tp) else []
            self._t0[name] = None
        if not self._containers:
            raise UnsupportedVideoSource('缓存里没有可用的 MP4')
        # pts ↔ .times 基准校准（**仅用于 locator**，不做任何帧号修正）
        for name in list(self._containers.keys()):
            raw = self._times.get(name) or []
            cont, st = self._container(name)
            tb = float(st.time_base)
            off = 0.0
            try:
                cont.seek(0)
                for frame in cont.decode(st):
                    first_pts = frame.pts * tb if frame.pts is not None else 0.0
                    off = float(raw[0]) - first_pts if raw else 0.0
                    break
            except Exception:
                off = 0.0
            self._pts_off[name] = off
            try:
                cont.seek(0)
            except Exception:
                pass
        self._canon_off = self._compute_canonical_offset()
        self.opened = True
        return self

    def _compute_canonical_offset(self):
        """canonical 零点：两路 camera **首个实际视频帧**相对 times 基准的最小偏移"""
        offs = []
        for cam_meta in (self.manifest.get('cameras') or []):
            topic = cam_meta.get('topic') or ''
            if 'camera2' in topic or 'camera3' in topic:
                offs.append(float(cam_meta.get('start_offset_s') or 0.0))
        return min(offs) if offs else 0.0

    def canonical_offset(self):
        if self._canon_off is None:
            self._canon_off = self._compute_canonical_offset()
        return self._canon_off

    def _source_times_ns(self, name):
        """把 .times 恢复成原 MCAP source timestamp（int64 ns）。

        `.times` 原生语义 = (log_time - manifest.time_base_ns) / 1e9 的 float64 秒；
        这里**只做一次** round 转换，之后全部走 int64，不再来回 float。
        """
        raw = self._times.get(name) or []
        tb = int(self.manifest.get('time_base_ns') or 0)
        return [int(round(float(t) * 1e9)) + tb for t in raw]

    def canonical_t0_ns(self):
        """时间原点 = 「首个可显示帧」的 source ns（两路取 min）

        prepare 从**首个 IDR** 开始 mux，因此 source_times_ns[0] 正是首个可显示帧；
        Direct 侧用同一语义（首个 IDR），两后端在此严格一致。
        """
        firsts = []
        for name in self._containers:
            st = self._source_times_ns(name)
            if st:
                firsts.append(st[0])
        return min(firsts) if firsts else 0

    def _media_time_of(self, name, source_ns):
        return (int(source_ns) - self.canonical_t0_ns()) / 1e9

    def _norm_times(self, name):
        """（兼容用）canonical 媒体时间浮点列表"""
        t0 = self.canonical_t0_ns()
        return [(int(s) - t0) / 1e9 for s in self._source_times_ns(name)]

    def cameras(self):
        return tuple(sorted(self._containers.keys()))

    def active_camera(self):
        return self._camera

    def duration(self):
        name = self._camera if self._camera in self._containers else \
            sorted(self._containers)[0]
        st = self._source_times_ns(name)
        if st:
            return max(0.0, (st[-1] - self.canonical_t0_ns()) / 1e9)
        st = self._streams[name]
        return float((st.duration or 0) * float(st.time_base))

    def _clamp(self, t):
        return max(0.0, min(self.duration(), float(t)))

    def _container(self, name):
        return self._containers[name], self._streams[name]

    def _decode_index(self, name, i):
        """**确定性帧序数**：目标索引 i（由 .times 决定）→ 解出第 i 帧。

        PTS 只负责把 decoder 送到 anchor；一旦求出 anchor 的逻辑索引 j0，
        后续完全按**解码帧计数**推进到 i（不再逐帧用 PTS 二分判断）。
        """
        cont, st = self._container(name)
        tb = float(st.time_base)
        raw = self._times.get(name) or []
        if not raw:
            raise UnsupportedVideoSource('缺少 times 索引，无法确定帧身份')
        n = len(raw)
        i = max(0, min(int(i), n - 1))
        off = self._pts_off.get(name, 0.0)
        fi = _frame_interval(raw)
        # ---- locator：跳到 i 前面最近的可解码位置 ----
        try:
            cont.seek(int(float(raw[i]) / tb), backward=True, any_frame=False)
        except Exception:
            pass
        it = cont.decode(st)
        # ---- 第一帧只做一次 j0 定位（最近合法 .times 索引）----
        frame0 = None
        j0 = None
        for frame in it:
            if frame.pts is None:
                continue
            mapped = frame.pts * tb + off
            k = bisect.bisect_left(raw, mapped)
            cands = [c for c in (k, k - 1) if 0 <= c < n]
            if not cands:
                continue
            j0 = min(cands, key=lambda j: abs(raw[j] - mapped))
            if abs(raw[j0] - mapped) > 0.45 * fi:
                raise VideoSeekError('MP4_ANCHOR_MAPPING_FAILED')
            frame0 = frame
            break
        if frame0 is None or j0 is None:
            return None, None
        if j0 > i:
            # 正常 backward seek 不应越过目标；如实失败，不做隐式修正
            raise VideoSeekError('MP4_SEEK_LOCATOR_OVERSHOT')
        # ---- 确定性计数：j0 → i（只计 video 帧）----
        cur = j0
        frame = frame0
        limit = max(30, (i - j0) + 30)
        steps = 0
        while cur < i and steps < limit:
            nxt = None
            for f in it:
                nxt = f
                break
            if nxt is None:
                break
            frame = nxt
            cur += 1
            steps += 1
        if cur != i:
            raise VideoSeekError('MP4_FRAME_ORDINAL_RESOLUTION_FAILED')
        self._last_steps = steps
        return frame, i

    def _frame_at_index(self, name, i):
        frame, idx = self._decode_index(name, i)
        if frame is None:
            return None
        st = self._source_times_ns(name)
        t0 = self.canonical_t0_ns()
        # 帧身份来自 .times[idx]（不由 pts 反推）
        idx = max(0, min(int(idx), len(st) - 1)) if st else 0
        src_ns = int(st[idx]) if st else t0
        return VideoFrame(frame.to_ndarray(format='bgr24'),
                          (src_ns - t0) / 1e9, name,
                          frame.width, frame.height, idx, src_ns)

    def seek(self, media_time):
        if not self.opened:
            raise VideoSourceError('尚未 open()')
        name = self._camera
        nt = self._norm_times(name)
        if not nt:
            raise UnsupportedVideoSource('缺少 times 索引')
        t = self._clamp(media_time)
        i = bisect.bisect_left(nt, float(t))       # 与 Direct 一致：first >= t
        if i >= len(nt):
            i = len(nt) - 1
        try:
            fr = self._frame_at_index(name, i)
        except VideoSourceError:
            raise
        except Exception as e:
            raise VideoDecodeError(str(e))
        if fr is None:
            raise VideoSeekError('定位失败 t=%s' % media_time)
        return fr

    def switch_camera(self, camera, media_time):
        self._camera = _norm_camera(camera)
        return self.seek(media_time)

    def stats(self):
        return dict(backend='mp4', camera=self._camera,
                    cameras=list(self._containers.keys()),
                    canonical_t0_ns=self.canonical_t0_ns(),
                    last_decode_steps=getattr(self, '_last_steps', 0))

    def close(self):
        for cont in self._containers.values():
            try:
                cont.close()
            except Exception:
                pass
        self._containers.clear()
        self._streams.clear()
        self.opened = False


class McapDirectVideoSource(VideoSource):
    """MCAP → ChunkIndex → 按需单 chunk → DirectSeekResolver → PyAV 直解"""

    def __init__(self, mcap_path, camera='camera2'):
        self.mcap_path = os.path.abspath(mcap_path)
        self._camera = _norm_camera(camera)
        self.index = None
        self.resolver = None
        self.decoder = None
        self._t0_ns = None
        self.opened = False
        self.chunks_before_first_frame = None
        self._first_done = False

    def open(self):
        try:
            self.index = VI.McapVideoIndex(self.mcap_path).build(
                camera_pred=lambda t: t in CAMERA_TOPICS.values())
        except Exception as e:
            raise CorruptVideoSource(str(e))
        if not self.index.has_index or not self.index.chunk_index:
            raise UnsupportedVideoSource('NO_CHUNK_INDEX')
        if not self.index.cameras:
            raise UnsupportedVideoSource('MISSING_CAMERA')
        self.resolver = VI.DirectSeekResolver(self.index)
        firsts = []
        for name in CAMERA_TOPICS:
            try:
                cam = self._cam(name)
            except VideoSourceError:
                continue
            # 首个**可显示帧** = 首个 IDR（prepare 的 MP4 正是从首个 IDR 开始封装；
            # 这保证与 MP4 侧 source_times_ns[0] 严格同源，且**不读任何 cache**）
            for ce in (self.index.chunk_index.get(cam.channel_id) or [])[:3]:
                items = self.resolver._frames(cam, ce)
                hit = None
                for ts, pl in items:
                    if VI.payload_has_idr(pl):
                        hit = ts
                        break
                if hit is None and items:
                    hit = items[0][0]
                if hit is not None:
                    firsts.append(hit)
                    break
        self._t0_ns = min(firsts) if firsts else None
        self.decoder = DD.DirectH264Decoder().open()
        self.opened = True
        return self

    def _cam(self, name=None):
        name = name or self._camera
        topic = CAMERA_TOPICS[name]
        for cam in self.index.cameras.values():
            if cam.topic == topic:
                return cam
        raise UnsupportedVideoSource('MISSING_CAMERA:%s' % name)

    def cameras(self):
        out = [n for n, t in CAMERA_TOPICS.items()
               if any(c.topic == t for c in self.index.cameras.values())]
        return tuple(sorted(out))

    def active_camera(self):
        return self._camera

    def _t0(self, cam):
        return self._t0_ns if self._t0_ns else cam.start_ts()

    def duration(self):
        cam = self._cam()
        t0 = self._t0(cam)
        return max(0.0, (cam.end_ts() - t0) / 1e9) if t0 and cam.end_ts() else 0.0

    def _clamp_ns(self, media_time):
        cam = self._cam()
        t0, t1 = self._t0(cam), cam.end_ts()
        if t0 is None or t1 is None:
            raise VideoSeekError('视频时间范围未知')
        t = max(0.0, min(float(media_time), (t1 - t0) / 1e9))
        return t0 + int(t * 1e9)

    def seek(self, media_time):
        if not self.opened:
            raise VideoSourceError('尚未 open()')
        cam = self._cam()
        target_ns = self._clamp_ns(media_time)
        try:
            ts, frame, _info = self.resolver.decode_to_target(
                cam, target_ns, self.decoder)
        except Exception as e:
            raise VideoDecodeError(str(e))
        if frame is None:
            raise VideoDecodeError('no frame decoded')
        if not self._first_done:
            self.chunks_before_first_frame = self.index.cache_stats()['chunks']
            self._first_done = True
        t0 = self._t0(cam)
        med = (ts - t0) / 1e9
        return VideoFrame(frame.image, med, self._camera,
                          frame.width, frame.height, -1, int(ts))

    def switch_camera(self, camera, media_time):
        self._camera = _norm_camera(camera)
        self._cam()
        return self.seek(media_time)

    def stats(self):
        if not self.opened:
            return dict(backend='mcap_direct', opened=False)
        return dict(backend='mcap_direct', camera=self._camera,
                    cameras=list(self.cameras()),
                    ttfi_ms=round(self.index.build_ms or 0, 1),
                    canonical_t0_ns=self._t0_ns,
                    chunk_cache=self.index.cache_stats(),
                    chunks_before_first_frame=self.chunks_before_first_frame,
                    decoder=(self.decoder.stats() if self.decoder else None))

    def close(self):
        if self.decoder is not None:
            try:
                self.decoder.close()
            except Exception:
                pass
            self.decoder = None
        if self.index is not None:
            self.index._cache.clear()
            self.index._cache_order = []
            self.index._cache_bytes = 0
        self.opened = False


def direct_capability(mcap_path):
    """只报告能力（本轮不做自动 fallback）"""
    try:
        idx = VI.McapVideoIndex(mcap_path).build(
            camera_pred=lambda t: t in CAMERA_TOPICS.values())
    except Exception:
        return VideoSourceCapability(False, 'CORRUPT_SOURCE', ())
    if not idx.has_index or not idx.chunk_index:
        return VideoSourceCapability(False, 'NO_CHUNK_INDEX', ())
    names = [n for n, t in CAMERA_TOPICS.items()
             if any(c.topic == t for c in idx.cameras.values())]
    if not names:
        return VideoSourceCapability(False, 'MISSING_CAMERA', ())
    return VideoSourceCapability(True, 'SUPPORTED', tuple(sorted(names)))


def open_source(mcap_path, prefer=None, camera='camera2'):
    """按优先级选择 backend：'mp4' / 'direct'；prefer=None 时 MP4 缓存优先。"""
    order = [prefer] if prefer else ['mp4', 'direct']
    last = None
    for kind in order:
        try:
            if kind == 'mp4':
                return Mp4VideoSource(mcap_path, camera=camera).open()
            return McapDirectVideoSource(mcap_path, camera=camera).open()
        except VideoSourceError as e:
            last = e
            continue
    raise last if last else UnsupportedVideoSource('没有可用后端')
