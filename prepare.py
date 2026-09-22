"""prepare.py —— 把 MCAP 里的视频 / 音频 / IMU 落盘成可秒开的缓存

产物（缓存目录）
    manifest.json          元数据（通道、时长、逐帧时间戳索引…）
    <key>_c<id>.mp4        OpenCV / 浏览器可直接播放的视频
    <key>_c<id>.raw        原始 H.264 码流（用于导出）
    <key>_c<id>.times      逐帧相对时间戳（float64 小端数组，秒，基准 time_base）
    img_<key>_c<id>/       逐帧图片通道（jpeg/png）
    audio.wav / imu.json   （audio.wav 仅完整缓存 profile 生成；
                            桌面精简缓存 noaudio_v2 不含音频，P1.6D-R2A 起）

时间单位约定
    * 原始 MCAP 时间戳一律 ``*_ns``（纳秒）；
    * manifest 里交给界面的一律 ``*_s``（秒），基准是 ``time_base_ns``。

并发
    同一缓存目录（同一个 file_id）同时只会有一个 prepare 在跑；后到的调用会等待
    前一个完成并复用结果，避免前台打开与后台预热把同一份缓存写坏。
"""

import os
import array
import json
import wave
import time
import shutil
import struct
import threading
from concurrent.futures import CancelledError

import mcap_reader as MR
import h264mp4 as H
import appcache

NS = 1_000_000_000

MANIFEST_VERSION = 2

_MIN_FREE_BYTES = 64 * 1024 * 1024


# ================================================================== 并发控制
class _Job:
    __slots__ = ('event', 'manifest', 'error')

    def __init__(self):
        self.event = threading.Event()
        self.manifest = None
        self.error = None


_JOBS = {}
_JOBS_LOCK = threading.Lock()


def running_jobs():
    with _JOBS_LOCK:
        return list(_JOBS.keys())


# ================================================================== 工具
def _safe_key(topic, used):
    parts = [p for p in (topic or '').strip('/').split('/')
             if p and p not in ('robot0', 'sensor')]
    base = '_'.join(parts) or 'channel'
    base = base.replace('compressed', '').replace('camera_info', 'calib')
    base = ''.join(ch if (ch.isalnum() or ch in '_-') else '_' for ch in base)
    base = base.strip('_') or 'channel'
    name = base
    n = 2
    while name in used:
        name = '%s_%d' % (base, n)
        n += 1
    used.add(name)
    return name


def _median_fps(times_ns):
    if len(times_ns) < 3:
        return 0.0 if len(times_ns) < 2 else round(
            NS / max(1, times_ns[1] - times_ns[0]), 3)
    deltas = sorted(times_ns[i + 1] - times_ns[i] for i in range(len(times_ns) - 1))
    mid = deltas[len(deltas) // 2]
    return round(NS / mid, 3) if mid > 0 else 0.0


def _ensure_time_order(seq):
    """确保 (时间戳, …) 序列按时间递增；乱序则原地重排，返回是否重排过。

    OPT-02 安全网：缓存遍历用 log_time_order=False（按文件 chunk 顺序），
    正常录制文件天然有序；被重写/合并过的文件可能乱序，重排后与"全局时间序"
    的结果完全一致（times / IMU / 音频都保持单调，播放端依赖这一点）。
    """
    if len(seq) < 2:
        return False
    if all(seq[i][0] <= seq[i + 1][0] for i in range(len(seq) - 1)):
        return False
    seq.sort(key=lambda x: x[0])
    return True


def _new_imu_accumulator():
    return dict(
        timestamps=array.array('q'),
        gx=array.array('d'), gy=array.array('d'), gz=array.array('d'),
        ax=array.array('d'), ay=array.array('d'), az=array.array('d'),
        ordered=True, last_ts=None)


def _append_imu(acc, timestamp, sample):
    av = sample.get('angular_velocity') or (0.0, 0.0, 0.0)
    la = sample.get('linear_acceleration') or (0.0, 0.0, 0.0)
    if acc['last_ts'] is not None and timestamp < acc['last_ts']:
        acc['ordered'] = False
    acc['last_ts'] = timestamp
    acc['timestamps'].append(int(timestamp))
    acc['gx'].append(float(av[0])); acc['gy'].append(float(av[1])); acc['gz'].append(float(av[2]))
    acc['ax'].append(float(la[0])); acc['ay'].append(float(la[1])); acc['az'].append(float(la[2]))


def _imu_rows(acc):
    n = len(acc['timestamps'])
    if acc.get('ordered', True):
        return zip(acc['timestamps'], acc['gx'], acc['gy'], acc['gz'],
                   acc['ax'], acc['ay'], acc['az'])
    rows = list(zip(acc['timestamps'], acc['gx'], acc['gy'], acc['gz'],
                    acc['ax'], acc['ay'], acc['az']))
    rows.sort(key=lambda x: x[0])
    return iter(rows)


def _imu_json_columns(acc):
    """Materialize only compact numeric lists for the final json.dumps call."""
    if acc.get('ordered', True):
        ts = list(acc['timestamps'])
        cols = [list(acc[k]) for k in ('gx', 'gy', 'gz', 'ax', 'ay', 'az')]
    else:
        rows = list(_imu_rows(acc))
        ts = [r[0] for r in rows]
        cols = [[r[i] for r in rows] for i in range(1, 7)]
    return ts, cols


def _write_times(path, times_ns, time_base_ns):
    """逐帧时间写为 float64 小端数组（相对 time_base 的秒）"""
    arr = struct.pack('<%dd' % len(times_ns),
                      *[(t - time_base_ns) / NS for t in times_ns])
    with open(path, 'wb') as fh:
        fh.write(arr)
    return len(times_ns)


def read_times(path):
    """读取 .times 文件，返回 float 列表（秒）"""
    with open(path, 'rb') as fh:
        data = fh.read()
    cnt = len(data) // 8
    return list(struct.unpack('<%dd' % cnt, data[:cnt * 8])) if cnt else []


def _publish(stage, outdir):
    """把 staging 目录发布成正式缓存目录（同层 rename，尽量原子）"""
    parent = os.path.dirname(os.path.abspath(outdir))
    if os.path.dirname(os.path.abspath(stage)) != parent:
        raise RuntimeError('staging 与目标目录不在同一层，拒绝发布')
    if os.path.isdir(outdir):
        old = outdir + '.old'
        if os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)
        os.replace(outdir, old)
        try:
            os.replace(stage, outdir)
        except BaseException:
            # 发布失败时恢复上一份完整缓存，不能在 finally 中无条件删掉它。
            if not os.path.exists(outdir) and os.path.isdir(old):
                os.replace(old, outdir)
            raise
        else:
            shutil.rmtree(old, ignore_errors=True)
    else:
        os.replace(stage, outdir)


def _report(progress, p, msg):
    if progress:
        try:
            progress(min(max(p, 0.0), 1.0), msg)
        except Exception:
            pass


# ================================================================== 性能采样
#: 缓存流程的分阶段耗时日志（一行一条 JSON，便于前后对比）
PERF_LOG_NAME = 'cache-perf.log'

#: 读取层优化的基准开关（默认全开；仅供 tests/perf_matrix.py 做 A/B 对照）
def _opt_flag(name, default=True):
    v = os.environ.get(name)
    if v is None:
        return default
    return v not in ('0', 'false', 'False', '')


OPT_LAZY = _opt_flag('MCAPVIEWER_OPT_LAZY')        # 无索引文件不预扫描（OPT-03）
OPT_TOPICS = _opt_flag('MCAPVIEWER_OPT_TOPICS')    # 订阅集合下推到库层（OPT-01）
OPT_ORDER = _opt_flag('MCAPVIEWER_OPT_ORDER')      # log_time_order=False（OPT-02）



def _rss_mb():
    """当前进程工作集（MB）；同时返回峰值。取不到时返回 (0.0, 0.0)。

    只用于性能审计（RSS 采样点），失败绝不影响缓存主流程。
    """
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD),
                            ('PageFaultCount', wintypes.DWORD),
                            ('PeakWorkingSetSize', ctypes.c_size_t),
                            ('WorkingSetSize', ctypes.c_size_t),
                            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                            ('PagefileUsage', ctypes.c_size_t),
                            ('PeakPagefileUsage', ctypes.c_size_t)]

            c = _PMC()
            c.cb = ctypes.sizeof(c)
            # 64 位下必须显式声明句柄类型，否则 GetCurrentProcess 的伪句柄
            # 会被截断成 32 位 int（ERROR_INVALID_HANDLE）
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            h = k32.GetCurrentProcess()
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                                   ctypes.POINTER(_PMC),
                                                   wintypes.DWORD]
            if psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
                return (c.WorkingSetSize / 1048576.0,
                        c.PeakWorkingSetSize / 1048576.0)
        else:
            import resource
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
            return rss, rss
    except Exception:
        pass
    return 0.0, 0.0


def _perf_on():
    return os.environ.get('MCAPVIEWER_PERF', '1') not in ('0', 'false', 'False')


def _perf_write(record):
    """把一次缓存的分阶段耗时追加到缓存根目录的 cache-perf.log。

    只写日志，不写进 manifest —— 缓存格式（CACHE_SCHEMA_VERSION）保持不变。
    """
    if not _perf_on():
        return
    try:
        path = os.path.join(appcache.CACHE_ROOT, PERF_LOG_NAME)
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception:
        pass


def read_perf_log(limit=50):
    """读回最近的性能记录（供验收脚本对比用）"""
    path = os.path.join(appcache.CACHE_ROOT, PERF_LOG_NAME)
    out = []
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        return []
    return out[-limit:]


def _cal_for(topic, cals):
    """把 <topic>/camera_info 关联到对应的视频通道"""
    base = topic or ''
    for suffix in ('/compressed', '/compressed_video'):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    return cals.get(base + '/camera_info') or cals.get(base) or cals.get(topic)


# ================================================================== 主流程
def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError('缓存生成已取消')


def prepare(path, outdir, progress=None, force=False, cancel_event=None,
            camera_pred=None, profile=None, keep_audio=None,
            keep_imu=None):
    """读取 MCAP 并生成缓存，返回 manifest。

    同一 outdir 并发调用只会真正执行一次，其余调用等待并复用结果。

    camera_pred: callable(topic) -> bool。返回 False 的视频通道完全不落盘
                 （连解码都不做），用于精简缓存（appcache.profile_keeps_topic）。
                 若过滤后没有任何通道被保留，则自动回退为全部通道，保证任何
                 文件都能打开。None 表示不过滤。
    profile:     写入 manifest 的缓存配置名（如 appcache.CACHE_PROFILE），
                仅作标记与界面展示，不影响校验逻辑。
    """
    key = os.path.abspath(outdir)
    mine = False
    with _JOBS_LOCK:
        job = _JOBS.get(key)
        if job is None:
            job = _JOBS[key] = _Job()
            mine = True

    if not mine:
        # 复用在跑的任务，等它结束后返回同一个 manifest
        while not job.event.wait(0.1):
            _check_cancel(cancel_event)
        if job.error is not None:
            raise job.error
        return job.manifest

    try:
        man = _prepare_impl(path, outdir, progress, cancel_event=cancel_event,
                            camera_pred=camera_pred, profile=profile,
                            keep_audio=keep_audio, keep_imu=keep_imu)
        job.manifest = man
        return man
    except BaseException as e:            # noqa: BLE001 - 原样传给等待者
        job.error = e
        raise
    finally:
        with _JOBS_LOCK:
            _JOBS.pop(key, None)
        job.event.set()


def _prepare_impl(path, outdir, progress=None, cancel_event=None,
                  camera_pred=None, profile=None, keep_audio=None,
            keep_imu=None):
    src = os.path.abspath(path)
    outdir = os.path.abspath(outdir)
    stage = outdir + '.staging'

    _report(progress, 0.01, '建立索引…')
    _check_cancel(cancel_event)
    _t_start = time.perf_counter()
    _t_reader = time.perf_counter()
    # lazy=True：没有索引时**不做**预先整文件扫描，元信息在下面这一次遍历里补齐
    #（OPT-03：无 summary 的文件也只完整读一遍）
    reader = MR.McapReader(src, lazy=OPT_LAZY)
    _reader_create_ms = (time.perf_counter() - _t_reader) * 1000.0
    _t_summ = time.perf_counter()
    summ = reader.summary()
    _summary_read_ms = (time.perf_counter() - _t_summ) * 1000.0
    has_index = bool(summ.get('has_index'))
    had_statistics = bool(summ.get('has_statistics'))

    # 无索引 → 单遍模式：按 topic 过滤（应用层），并在遍历中收集元信息
    lazy_mode = not summ.get('channels')
    # 有索引但没有可用统计（缺 Statistics / 计数不全）时，也借这次遍历顺手收集
    counts_ok = bool(summ.get('channels')) and all(
        (c.get('count') or 0) > 0 for c in summ['channels'])
    need_collect = lazy_mode or not counts_ok

    perf = dict(
        name=os.path.basename(src),
        source_file_size=(os.path.getsize(src) if os.path.isfile(src) else 0),
        has_index=has_index,
        had_statistics=had_statistics,
        chunk_count=summ.get('chunk_count') or 0,
        reader_create_ms=round(_reader_create_ms, 1),
        summary_read_ms=round(_summary_read_ms, 1),
        reader_setup_ms=round(_reader_create_ms + _summary_read_ms, 1),
        lazy_single_pass=bool(lazy_mode),
        collect_during_iteration=bool(need_collect),
        opt_lazy=bool(OPT_LAZY), opt_topics=bool(OPT_TOPICS), opt_order=bool(OPT_ORDER),
    )
    time_base_ns = summ.get('start_time_ns') or 0
    duration_s = summ.get('duration_s') or 0.0

    kinds = {c['id']: c['kind'] for c in summ['channels']}
    by_id = {c['id']: c for c in summ['channels']}

    # 精简缓存：先按 summary 里的通道列表决定要保留的视频通道。
    # 在这里过滤，未选中的通道连解码都不做。
    # 没有任何主视角时明确报错，不做「回退缓存全部通道」的假回退——
    # 桌面端只显示 camera2/3，全通道桌面缓存既浪费空间又会显示 0 路。
    # 原始文件保留不动；server.py 的完整缓存不受影响。
    slim_ids = None
    if camera_pred is not None and not lazy_mode:
        video = [c for c in summ['channels'] if kinds.get(c['id']) == 'video']
        kept = {c['id'] for c in video if camera_pred(c.get('topic'))}
        if not kept:
            raise MR.McapError('该文件没有 camera2/camera3 主视角，桌面模式不支持。')
        if len(kept) < len(video):
            slim_ids = kept

    # P1.6D-R2A：桌面精简缓存不再包含音频（业务上不需要声音）。
    # 关键：音频必须**从订阅集合里就删掉**（而不是读出来再丢弃）——
    # 有索引时 ChunkStreamingIndexedReader / 官方库会用 topic 白名单做 chunk 级
    # 候选选择，audio-only chunk 直接不进候选集合（少读/少 seek/少解压）。
    # 无索引顺序扫描时虽无法跳 chunk，但应用层不再 decode/collect/写盘。
    # keep_audio 由 profile 决定（noaudio_v2 → 永远 False）；
    # 显式参数仅供 tests/perf_* 做 A/B 对照，生产代码没有环境变量可以改写它。
    if keep_audio is None:
        keep_audio = appcache.profile_keeps_audio(profile)
    # P1.6D-R2B：Desktop 只用于看视频 → IMU 同样从订阅集合里删掉
    # （与音频同理：不需要的数据，最快的处理方式是不处理）。
    # keep_imu 显式参数仅供 tests/perf_* 做 A/B 对照，生产没有环境变量可以改写。
    if keep_imu is None:
        keep_imu = appcache.profile_keeps_imu(profile)
    perf['keep_audio'] = bool(keep_audio)
    perf['keep_imu'] = bool(keep_imu)
    perf['cache_profile'] = profile or 'full'

    # OPT-01：把订阅集合收窄，并让 iter_messages 映射成 topic 白名单下推给库层，
    # 被排除通道（camera1/4/5/6、音频）的 payload 不会被读出来。
    # 无索引（lazy）时 channels 未知、且需要完整统计 → 不做下推、读全部通道。
    want_ids = None
    if not lazy_mode:
        want_ids = set(kinds.keys())
        if slim_ids is not None:
            want_ids -= {c['id'] for c in summ['channels']
                         if kinds.get(c['id']) == 'video' and c['id'] not in slim_ids}
        if not keep_audio:
            want_ids -= {c['id'] for c in summ['channels']
                         if kinds.get(c['id']) == 'audio'}
        if not keep_imu:
            want_ids -= {c['id'] for c in summ['channels']
                         if kinds.get(c['id']) == 'imu'}
    # 下推只在「有索引且统计完整」时启用：此时不需要遍历期收集，
    # 被排除通道的数据完全不读（OPT-01）。
    pushdown = (not lazy_mode) and (not need_collect) and OPT_TOPICS
    log_time_order = not OPT_ORDER      # OPT-02：默认按文件顺序，不做全局排序

    if os.path.isdir(stage):
        shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)

    cams = {}
    cam_order = []
    #: 桌面精简缓存不含音频：这里连 accumulator 都不创建（None = 不参与音频链路），
    #: 避免"初始化一个永不使用的 list"。完整缓存（full/server）仍创建正常容器。
    audio_chans = [] if keep_audio else None
    #: 桌面 videoonly：不创建 IMU accumulator（None = 不参与 IMU 链路）
    imu_chans = [] if keep_imu else None
    cals = {}
    system_count = 0
    raw_count = 0

    total = summ['message_count'] or 1
    done = 0
    _report(progress, 0.03, '读取消息…')
    _t_loop = time.perf_counter()
    _seen = 0                # 遍历到的消息总数（含被精简过滤掉的）
    _sel = 0                 # 真正参与处理的消息数（OPT-04 统计）
    _decode_ms = {}          # 每路视频的 protobuf/编解码耗时
    _rawwrite_ms = {}        # 每路视频写原始 H.264 的耗时
    _lazy_kept_video = 0     # 单遍模式下通过 camera_pred 的视频通道数
    # ---- Hot Path Attribution：只统计，不改变任何行为 ----
    _rn = array.array('d')   # 官方 iterator next() 的等待耗时样本（微秒）
    _app_s = 0.0             # 拿到 message 之后的应用处理时长
    _prog_s = 0.0            # 进度上报耗时
    _cam_dispatch_s = 0.0    # video 分支总时长（含被精简过滤的判定）
    _cam_index_s = 0.0       # 帧索引 append 耗时
    _imu_deser_s = 0.0       # IMU protobuf 解码
    _imu_collect_s = 0.0     # IMU 入列
    _audio_deser_s = 0.0     # 音频 protobuf 解码
    _audio_decode_calls = 0  # 音频解码调用次数（桌面 noaudio 下必须为 0）
    _imu_decode_calls = 0    # IMU 解码调用次数（桌面 videoonly 下必须为 0）
    _audio_collect_s = 0.0   # 音频入列
    _meta_decode_s = 0.0     # calibration / system 解码
    _msg_count = {'video': 0, 'imu': 0, 'audio': 0,
                  'calibration': 0, 'system': 0, 'other': 0}
    _rss_pts = {'rss_start_mb': round(_rss_mb()[0], 1)}   # RSS 采样点（性能审计）

    try:
        # OPT-02：log_time_order 默认 False（按文件 chunk 顺序），避免官方库做全局
        # 时间排序；单通道内若出现乱序，收尾时会检测并重排（见 _finalize_h264）。
        # 这里用显式 next() 循环（语义与 for 完全一致），以便把"等官方 reader
        # 产出消息"与"我们自己的处理"分开计时（Hot Path Attribution Audit）。
        _it = reader.iter_messages(want_ids, log_time_order=log_time_order,
                                  collect=need_collect, pushdown=pushdown)
        while True:
            _t_next = time.perf_counter()
            try:
                item = next(_it)
            except StopIteration:
                break
            _rn.append((time.perf_counter() - _t_next) * 1e6)
            cid, lg, _pub, _seq, data = item
            _check_cancel(cancel_event)
            _seen += 1
            done += 1
            _t_prog = time.perf_counter()
            if lazy_mode:
                # 单遍模式：进度按已读文件比例估算
                frac = (reader._iter_offset / reader.size) if reader.size else 0.0
                if done % 300 == 0:
                    _report(progress, 0.03 + 0.58 * min(1.0, max(0.0, frac)),
                            '读取消息 %d（%.0f%%）' % (done, frac * 100.0))
            elif done % 300 == 0:
                _report(progress, 0.03 + 0.58 * done / total,
                        '读取消息 %d / %d' % (done, total))
            _prog_s += time.perf_counter() - _t_prog
            if done % 300 == 0:
                # RSS 采样点（25/50/75%），只用于内存审计，不参与业务逻辑
                if not lazy_mode:
                    _frac = done / total
                else:
                    _frac = (reader._iter_offset / reader.size) if reader.size else 0.0
                for _q in (0.25, 0.50, 0.75):
                    _k = 'rss_iter_%02d_mb' % int(round(_q * 100))
                    if _k not in _rss_pts and _frac >= _q:
                        _rss_pts[_k] = round(_rss_mb()[0], 1)
            info = by_id.get(cid)
            if info is None:
                # 单遍模式：首次遇到该通道，用已登记的 channel/schema 现场分类
                ch = reader.channels.get(cid) or {}
                sch = reader.schemas.get(ch.get('schema_id')) or {}
                info = dict(id=cid, topic=ch.get('topic', ''),
                            schema=sch.get('name', ''),
                            schema_encoding=sch.get('encoding', ''),
                            message_encoding=ch.get('message_encoding', ''),
                            metadata=ch.get('metadata') or {},
                            count=0, hz=0.0,
                            kind=MR.classify(ch.get('topic', ''), sch.get('name', '')))
                by_id[cid] = info
                kinds[cid] = info['kind']
            kind = kinds.get(cid)
            _msg_count[kind if kind in _msg_count else 'other'] += 1
            _t_app = time.perf_counter()

            if kind == 'video':
                _t_cam = time.perf_counter()
                if slim_ids is not None and cid not in slim_ids:
                    _app_s += time.perf_counter() - _t_app
                    continue          # 精简缓存：非保留通道直接跳过，不解码
                if lazy_mode and camera_pred is not None:
                    # 单遍模式：通道表未知，按 topic 现场判定是否保留
                    # （与精简缓存的通道筛选等价）
                    if not camera_pred(info.get('topic')):
                        _app_s += time.perf_counter() - _t_app
                        continue
                    _lazy_kept_video += 1
                _t = time.perf_counter()
                fmt, payload, extra = MR.decode_video(info['schema'],
                                                      info['message_encoding'], data)
                _decode_ms[cid] = _decode_ms.get(cid, 0.0) + (
                    time.perf_counter() - _t)
                if not payload:
                    _app_s += time.perf_counter() - _t_app
                    continue
                st = cams.get(cid)
                if st is None:
                    st = cams[cid] = dict(
                        id=cid, topic=info['topic'], schema=info['schema'],
                        schema_encoding=info.get('schema_encoding', ''),
                        formats=[], fmt='', frame_id='', extra=extra,
                        raw_fh=None, raw_path=None, raw_bytes=0,
                        index=[], img_pending=[], image_payloads=0,
                        first_ts_ns=lg, last_ts_ns=lg)
                    cam_order.append(cid)
                f = (fmt or '').strip().lower()
                if f and f not in st['formats']:
                    st['formats'].append(f)
                if not st['fmt'] and fmt:
                    st['fmt'] = fmt
                st['last_ts_ns'] = lg
                if not st['frame_id'] and extra.get('frame_id'):
                    st['frame_id'] = extra['frame_id']

                codec = MR.classify_video_format(st['fmt'])
                if codec == 'h264':
                    if st['raw_fh'] is None:
                        st['raw_path'] = os.path.join(stage, '_raw_%d.bin' % cid)
                        st['raw_fh'] = open(st['raw_path'], 'wb')
                    off = st['raw_fh'].tell()
                    _t = time.perf_counter()
                    st['raw_fh'].write(payload)
                    _rawwrite_ms[cid] = _rawwrite_ms.get(cid, 0.0) + (
                        time.perf_counter() - _t)
                    st['raw_bytes'] += len(payload)
                    _t = time.perf_counter()
                    st['index'].append((lg, off, len(payload)))
                    _cam_index_s += time.perf_counter() - _t
                elif codec == 'image':
                    st['img_pending'].append((lg, payload))
                    st['image_payloads'] += 1
                raw_count += 1
                _sel += 1
                _cam_dispatch_s += time.perf_counter() - _t_cam

            elif kind == 'imu':
                if imu_chans is None:
                    # 桌面 videoonly：IMU 不在订阅集合里（有索引时连 chunk 都不读；
                    # 无索引顺序扫描时在这里直接丢弃）——不解码、不收集、不落盘
                    _app_s += time.perf_counter() - _t_app
                    continue
                _imu_decode_calls += 1
                _t = time.perf_counter()
                m = MR._decode_imu(info['message_encoding'], data)
                _imu_deser_s += time.perf_counter() - _t
                if m:
                    _t = time.perf_counter()
                    ch = next((c for c in imu_chans if c['id'] == cid), None)
                    if ch is None:
                        ch = dict(id=cid, topic=info['topic'], schema=info['schema'],
                                  samples=_new_imu_accumulator())
                        imu_chans.append(ch)
                    _append_imu(ch['samples'], lg, m)
                    _imu_collect_s += time.perf_counter() - _t
                    _sel += 1

            elif kind == 'audio':
                if audio_chans is None:
                    # 桌面精简缓存：音频不在订阅集合里（有索引时连 chunk 都不读；
                    # 无索引顺序扫描时在这里直接丢弃）——不解码、不收集、不落盘。
                    _app_s += time.perf_counter() - _t_app
                    continue
                _audio_decode_calls += 1
                _t = time.perf_counter()
                m = MR._decode_audio(info['message_encoding'], data)
                _audio_deser_s += time.perf_counter() - _t
                if m:
                    _t = time.perf_counter()
                    ch = next((c for c in audio_chans if c['id'] == cid), None)
                    if ch is None:
                        spool_path = os.path.join(stage, '.audio_packets_%d.tmp' % cid)
                        ch = dict(id=cid, topic=info['topic'], schema=info['schema'],
                                  config=dict(m['config'] or {}), chunks=[],
                                  spool_path=spool_path,
                                  spool_fh=open(spool_path, 'wb'),
                                  packet_count=0, payload_bytes=0)
                        audio_chans.append(ch)
                    payload = m.get('data') or b''
                    off = ch['spool_fh'].tell()
                    ch['spool_fh'].write(payload)
                    ch['chunks'].append((lg, off, len(payload)))
                    ch['packet_count'] += 1
                    ch['payload_bytes'] += len(payload)
                    _audio_collect_s += time.perf_counter() - _t
                    _sel += 1

            elif kind == 'calibration':
                _t = time.perf_counter()
                m = MR._decode_calibration(info['message_encoding'], data)
                _meta_decode_s += time.perf_counter() - _t
                if m:
                    cals[info['topic']] = m
                    _sel += 1

            elif kind == 'system':
                _t = time.perf_counter()
                ok = MR._decode_system_info(info['message_encoding'], data)
                _meta_decode_s += time.perf_counter() - _t
                if ok:
                    system_count += 1
                    _sel += 1

            _app_s += time.perf_counter() - _t_app

        _report(progress, 0.61, '封装视频…')
        _check_cancel(cancel_event)
        # ---- OPT-04：遍历阶段结束，记录耗时与计数 ----
        perf['mcap_iteration_ms'] = round(
            (time.perf_counter() - _t_loop) * 1000.0, 1)
        perf['total_messages_seen'] = _seen
        perf['selected_messages'] = _sel
        _rss_pts['rss_iter_end_mb'] = round(_rss_mb()[0], 1)
        perf['decode_video_ms'] = {str(k): round(v * 1000.0, 1)
                                  for k, v in _decode_ms.items()}
        perf['raw_write_ms'] = {str(k): round(v * 1000.0, 1)
                                for k, v in _rawwrite_ms.items()}
        perf['camera_frames'] = {str(cid): (len(st['index']) + len(st['img_pending']))
                                 for cid, st in cams.items()}
        perf['camera_bytes'] = {str(cid): int(st.get('raw_bytes') or 0)
                                for cid, st in cams.items()}
        # ---- Hot Path Attribution：把遍历耗时拆成「官方 reader 等待」与「应用处理」 ----
        _iter_ms = perf['mcap_iteration_ms']
        _rn_sorted = sorted(_rn) if _rn else []
        _rn_total_ms = (sum(_rn) / 1000.0) if _rn else 0.0
        _app_ms = _app_s * 1000.0
        _prog_ms = _prog_s * 1000.0

        def _pct(q):
            if not _rn_sorted:
                return 0.0
            k = int(round(q * (len(_rn_sorted) - 1)))
            return _rn_sorted[min(len(_rn_sorted) - 1, max(0, k))]

        hp = perf.setdefault('hotpath', {})
        hp.update(
            reader_next_total_ms=round(_rn_total_ms, 1),
            reader_next_avg_us=round((_rn_total_ms * 1000.0 / len(_rn)), 1) if _rn else 0.0,
            reader_next_p50_us=round(_pct(0.50), 1),
            reader_next_p95_us=round(_pct(0.95), 1),
            reader_next_max_us=round(_rn_sorted[-1], 1) if _rn_sorted else 0.0,
            reader_next_samples=len(_rn),
            app_total_ms=round(_app_ms, 1),
            progress_report_ms=round(_prog_ms, 1),
            unattributed_ms=round(_iter_ms - _rn_total_ms - _app_ms - _prog_ms, 1),
            messages_by_kind=dict(_msg_count),
            camera_dispatch_ms=round(_cam_dispatch_s * 1000.0, 1),
            camera_decode_ms=round(sum(_decode_ms.values()) * 1000.0, 1),
            camera_raw_write_ms=round(sum(_rawwrite_ms.values()) * 1000.0, 1),
            camera_index_append_ms=round(_cam_index_s * 1000.0, 1),
            imu_deserialize_ms=round(_imu_deser_s * 1000.0, 1),
            imu_transform_ms=0.0,        # 代码里没有独立的坐标变换步骤（如实记 0）
            imu_collect_ms=round(_imu_collect_s * 1000.0, 1),
            audio_deserialize_ms=round(_audio_deser_s * 1000.0, 1),
            audio_collect_ms=round(_audio_collect_s * 1000.0, 1),
            metadata_decode_ms=round(_meta_decode_s * 1000.0, 1),
            iteration_ms=round(_iter_ms, 1),
            reader_next_share=round(_rn_total_ms / _iter_ms, 4) if _iter_ms else 0.0,
            app_share=round(_app_ms / _iter_ms, 4) if _iter_ms else 0.0,
        )
        # ---- OPT-03：单遍/补齐模式 —— 用这次遍历收集到的元信息重建 summary，
        #      不再额外扫描文件（无索引文件到此只完整读过一遍） ----
        if need_collect:
            summ = reader.summary(recount=False)
            time_base_ns = summ.get('start_time_ns') or 0
            duration_s = summ.get('duration_s') or 0.0
        perf['time_base_ns'] = int(time_base_ns or 0)
        perf['duration_s'] = round(float(duration_s or 0.0), 6)
        if lazy_mode and camera_pred is not None and _lazy_kept_video == 0:
            raise MR.McapError('该文件没有 camera2/camera3 主视角，桌面模式不支持。')
        extra_files = []
        _t = time.perf_counter()
        cameras = _finish_cameras(stage, cams, cam_order, time_base_ns, cals,
                                  progress, extra_files, cancel_event, perf=perf)
        perf['video_finalize_ms'] = round((time.perf_counter() - _t) * 1000.0, 1)

        # 桌面精简缓存不含音频：不调用 _finish_audio，也不会生成 audio.wav
        audio_meta = None
        if audio_chans is not None:
            _report(progress, 0.86, '写入音频…')
            _check_cancel(cancel_event)
            _t = time.perf_counter()
            audio_meta = _finish_audio(stage, audio_chans, time_base_ns, perf=perf)
            perf['audio_process_ms'] = round((time.perf_counter() - _t) * 1000.0, 1)

        # 桌面 videoonly：不调用 _finish_imu，也不生成 imu.json
        imu_meta = None
        if imu_chans is not None:
            _report(progress, 0.92, '写入 IMU…')
            _check_cancel(cancel_event)
            _t = time.perf_counter()
            imu_meta = _finish_imu(stage, imu_chans, time_base_ns, perf=perf)
            perf['imu_process_ms'] = round((time.perf_counter() - _t) * 1000.0, 1)
            _rss_pts['rss_after_imu_mb'] = round(_rss_mb()[0], 1)

        if not cameras:
            raise MR.McapError('这个文件里没有可显示的视频通道')

        manifest = dict(
            cache_schema_version=appcache.CACHE_SCHEMA_VERSION,
            manifest_version=MANIFEST_VERSION,
            cache_profile=profile or 'full',
            # server.py 未参与本次桌面改造，仍期待 source 是路径字符串。
            # 详细签名单独保存，让桌面端获得 mtime_ns 校验且保持向后兼容。
            source=src,
            source_signature=appcache.source_signature(src),
            time_base_ns=time_base_ns,
            duration_s=duration_s,
            duration=duration_s,          # 兼容网页版字段名（秒）
            start_time_ns=summ.get('start_time_ns', 0),
            end_time_ns=summ.get('end_time_ns', 0),
            summary=summ,
            cameras=cameras,
            audio=audio_meta,
            imu=imu_meta,
            calibration=cals,
            system_count=system_count,
            raw_message_count=raw_count,
            extra_files=extra_files,
            builder=dict(app='MCAP 视频查看器', cached_at=int(time.time()),
                         manifest_version=MANIFEST_VERSION),
        )
        _report(progress, 0.97, '发布缓存…')
        _t = time.perf_counter()
        appcache.write_json_atomic(os.path.join(stage, 'manifest.json'), manifest)
        perf['manifest_write_ms'] = round((time.perf_counter() - _t) * 1000.0, 1)
        _t = time.perf_counter()
        _publish(stage, outdir)
        perf['publish_ms'] = round((time.perf_counter() - _t) * 1000.0, 1)
        perf['total_ms'] = round((time.perf_counter() - _t_start) * 1000.0, 1)
        perf['cached_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        perf['cache_dir'] = outdir
        perf['imu_messages'] = (sum(len(c['samples']['timestamps'])
                                    for c in imu_chans)
                                if imu_chans else 0)
        perf.setdefault('imu_process_ms', 0.0)
        perf['audio_messages'] = (sum(len(c['chunks']) for c in audio_chans)
                                  if audio_chans else 0)
        perf.setdefault('audio_process_ms', 0.0)
        _hp = perf.setdefault('hotpath', {})
        _hp['imu_selected_messages'] = perf['imu_messages']
        _hp['imu_decode_calls'] = _imu_decode_calls
        _hp['imu_finalize_ms'] = perf.get('imu_process_ms', 0.0)
        _hp['imu_output_bytes'] = int(_hp.get('imu_json_bytes', 0))
        _hp['audio_selected_messages'] = perf['audio_messages']
        _hp['audio_decode_ms'] = round(_audio_deser_s * 1000.0, 1)
        _hp['audio_decode_calls'] = _audio_decode_calls
        _hp['audio_spool_bytes_written'] = int(
            sum(c.get('payload_bytes', 0) for c in (audio_chans or ())))
        _hp['audio_spool_bytes_reread'] = _hp['audio_spool_bytes_written']
        _hp['audio_finalize_ms'] = perf.get('audio_process_ms', 0.0)
        _hp['audio_output_bytes'] = int((audio_meta or {}).get('pcm_bytes') or 0)
        _hp['audio_wav_written'] = bool(audio_meta and audio_meta.get('file'))
        _cur, _peak = _rss_mb()
        perf['rss_after_publish_mb'] = round(_cur, 1)
        perf['rss_peak_mb'] = round(_peak, 1)
        perf.update(_rss_pts)
        # P1.6E-R3：慢缓存诊断（>=45 秒自动记录；只写 perf 日志，不弹窗）
        if (perf.get('total_ms') or 0) >= 45000.0:
            _hp0 = perf.get('hotpath') or {}
            _cbf = perf.get('camera_frames') or {}
            _cbb = perf.get('camera_bytes') or {}
            _secs = max(0.001, perf['total_ms'] / 1000.0)
            _src = perf.get('source_file_size') or 0
            _out = sum(int(v) for v in _cbb.values() if isinstance(v, (int, float)))
            _hp0['slow_trace'] = dict(
                source_size=_src, duration_s=round(duration_s or 0.0, 3),
                indexed=bool(summ.get('has_index')),
                reader_mode=(MR._reader_mode() if hasattr(MR, '_reader_mode') else ''),
                chunk_count=summ.get('chunk_count') or 0,
                candidate_chunks=None, processed_chunks=None,
                selected_message_count=_seen,
                selected_payload_bytes=sum(
                    int(v) for v in _cbb.values() if isinstance(v, (int, float))),
                camera2_frames=_cbf.get('3'), camera3_frames=_cbf.get('4'),
                camera2_raw_bytes=_cbb.get('3'), camera3_raw_bytes=_cbb.get('4'),
                reader_setup_ms=perf.get('reader_setup_ms'),
                summary_ms=perf.get('summary_read_ms'),
                iteration_ms=perf.get('mcap_iteration_ms'),
                reader_next_ms=_hp0.get('reader_next_total_ms'),
                camera_processing_ms=_hp0.get('camera_dispatch_ms'),
                raw_write_ms=round(sum(_rawwrite_ms.values()) * 1000.0, 1),
                video_finalize_ms=perf.get('video_finalize_ms'),
                manifest_ms=perf.get('manifest_write_ms'),
                publish_ms=perf.get('publish_ms'),
                total_ms=perf.get('total_ms'),
                effective_read_mbps=round(_src / 1048576.0 / _secs, 1),
                effective_write_mbps=round(_out / 1048576.0 / _secs, 1))
        _perf_write(perf)
        _report(progress, 1.0, '完成')
    except BaseException:
        for st in cams.values():
            if st.get('raw_fh'):
                try:
                    st['raw_fh'].close()
                except Exception:
                    pass
        for ch in (audio_chans or ()):
            fh = ch.get('spool_fh')
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
        shutil.rmtree(stage, ignore_errors=True)
        raise

    manifest['_dir'] = outdir
    manifest['_perf'] = perf        # 仅内存返回（不写进 manifest 文件，保持缓存格式不变）
    return manifest


# ---------------------------------------------------------------- 视频收尾
def _finish_cameras(stage, cams, order, time_base_ns, cals, progress, extra_files,
                    cancel_event=None, perf=None):
    out = []
    used = set()
    n = max(1, len(order))
    for i, cid in enumerate(order):
        _check_cancel(cancel_event)
        st = cams[cid]
        if st.get('raw_fh'):
            st['raw_fh'].close()
            st['raw_fh'] = None

        key = _safe_key(st['topic'], used)
        base = '%s_c%d' % (key, cid)
        fmts = st['formats']
        codec = MR.classify_video_format(st['fmt'])

        if len(fmts) > 1:
            out.append(_bad_entry(st, key, cid, cals,
                                  '该通道中途更换了编码格式：%s' % ' → '.join(fmts)))
            _report(progress, 0.61 + 0.25 * (i + 1) / n, '封装视频 %d / %d' % (i + 1, n))
            continue
        if codec == 'unsupported':
            out.append(_bad_entry(st, key, cid, cals,
                                  '不支持该编码：%s（本程序只封装 H.264）' % st['fmt']))
            _report(progress, 0.61 + 0.25 * (i + 1) / n, '封装视频 %d / %d' % (i + 1, n))
            continue
        if codec == 'unknown':
            out.append(_bad_entry(st, key, cid, cals,
                                  '无法识别的视频编码：%s' % (st['fmt'] or '空')))
            _report(progress, 0.61 + 0.25 * (i + 1) / n, '封装视频 %d / %d' % (i + 1, n))
            continue

        entry = dict(
            key=key, id=cid, topic=st['topic'], schema=st['schema'],
            schema_encoding=st.get('schema_encoding', ''),
            format=st['fmt'], codec=codec, frame_id=st['frame_id'],
            frames=0, frames_raw=0, dropped=0,
            duration_s=0.0, start_offset_s=0.0, start_offset_ns=0,
            first_ts_ns=st['first_ts_ns'], last_ts_ns=st['last_ts_ns'],
            fps=0.0, playable=False, kind=codec, width=0, height=0,
            calibration=_cal_for(st['topic'], cals),
            extra={k: v for k, v in (st.get('extra') or {}).items()
                   if v not in (None, '')},
            warnings=[],
        )
        try:
            if codec == 'h264':
                _finalize_h264(st, entry, base, stage, time_base_ns, extra_files,
                               cancel_event, perf=perf)
            else:
                _finalize_images(st, entry, base, stage, time_base_ns, extra_files,
                                 cancel_event)
        except (H.MuxError, MR.McapError) as e:
            entry['playable'] = False
            entry['kind'] = 'unsupported'
            entry['error'] = str(e)
        out.append(entry)
        _report(progress, 0.61 + 0.25 * (i + 1) / n,
                '封装视频 %d / %d' % (i + 1, n))
    return out


def _bad_entry(st, key, cid, cals, msg):
    return dict(
        key=key, id=cid, topic=st['topic'], schema=st['schema'],
        schema_encoding=st.get('schema_encoding', ''),
        format=st['fmt'], codec=MR.classify_video_format(st['fmt']),
        frame_id=st['frame_id'], frames=0,
        frames_raw=len(st.get('index') or []) + len(st.get('img_pending') or []),
        dropped=0, duration_s=0.0, start_offset_s=0.0, start_offset_ns=0,
        fps=0.0, playable=False, kind='unsupported', width=0, height=0,
        calibration=_cal_for(st['topic'], cals), extra={},
        error=msg, warnings=[],
    )


def _finalize_h264(st, entry, base, stage, time_base_ns, extra_files,
                   cancel_event=None, perf=None):
    index = st['index']
    if not index:
        raise H.MuxError('该通道没有任何视频帧')
    # OPT-02 安全网：缓存遍历用 log_time_order=False（按文件 chunk 顺序），
    # 单通道内通常天然按时间递增；若文件被重写/合并导致乱序，这里按时间重排。
    # 注意重排的是 (时间戳, 原始偏移, 长度) 三元组，raw 字节本身不动，
    # 因此结果与"全局时间序"完全一致，times 也保持单调（播放端 bisect 依赖）。
    if len(index) > 1 and any(index[i + 1][0] < index[i][0]
                              for i in range(len(index) - 1)):
        index = sorted(index, key=lambda x: x[0])
        st['index'] = index
        st['reordered'] = True
        st['first_ts_ns'] = index[0][0]
        st['last_ts_ns'] = index[-1][0]
    raw_name = '%s.raw' % base
    raw_path = os.path.join(stage, raw_name)
    os.replace(st['raw_path'], raw_path)

    mp4_name = '%s.mp4' % base
    mp4_path = os.path.join(stage, mp4_name)
    _t_mux = time.perf_counter()
    info = H.mux_raw(raw_path, index, mp4_path, start_offset_ns=None,
                     time_base_ns=time_base_ns,
                     cancelled=(cancel_event.is_set if cancel_event is not None else None))
    if perf is not None:
        d = perf.setdefault('camera_mux_ms', {})
        d[str(entry.get('id'))] = round(
            d.get(str(entry.get('id')), 0.0) + (time.perf_counter() - _t_mux) * 1000.0, 1)

    times_ns = info['times_ns']
    times_name = '%s.times' % base
    _write_times(os.path.join(stage, times_name), times_ns, time_base_ns)
    extra_files.append(times_name)

    entry.update(
        kind='mp4', playable=True, file=mp4_name, source_raw=raw_name,
        times_file=times_name,
        frames=info['frames'], frames_raw=info['frames_raw'],
        dropped=info['dropped'],
        width=info['width'], height=info['height'],
        start_offset_ns=info['start_offset_ns'],
        start_offset_s=info['start_offset_ns'] / NS,
        duration_s=info['duration_s'],
        fps=_median_fps(times_ns),
        mp4_bytes=info['mdat_bytes'],
        sync_samples=info['sync_samples'],
        co64=info['co64'], large_mdat=info['large_mdat'],
        warnings=list(info.get('warnings') or []),
    )
    if entry['dropped']:
        entry['warnings'].append(
            '首帧不是关键帧，已丢弃前 %d 帧；实际可解码 %d 帧'
            % (entry['dropped'], entry['frames']))


def _finalize_images(st, entry, base, stage, time_base_ns, extra_files,
                     cancel_event=None):
    pending = st['img_pending']
    if not pending:
        raise MR.McapError('该通道没有任何图片帧')
    ext = 'png' if 'png' in (st['fmt'] or '').lower() else 'jpg'
    dir_name = 'img_%s' % base
    os.makedirs(os.path.join(stage, dir_name), exist_ok=True)
    names = []
    times_ns = []
    for i, (ts, payload) in enumerate(pending):
        _check_cancel(cancel_event)
        fn = '%06d.%s' % (i + 1, ext)
        with open(os.path.join(stage, dir_name, fn), 'wb') as fh:
            fh.write(payload)
        names.append(fn)
        times_ns.append(ts)
    times_name = '%s.times' % base
    _write_times(os.path.join(stage, times_name), times_ns, time_base_ns)
    extra_files.append(times_name)
    entry.update(
        kind='images', playable=True, dir=dir_name,
        frames_list=names, times_file=times_name,
        frames=len(names), frames_raw=len(pending), dropped=0,
        start_offset_ns=times_ns[0] - time_base_ns,
        start_offset_s=(times_ns[0] - time_base_ns) / NS,
        duration_s=(times_ns[-1] - times_ns[0]) / NS if len(times_ns) > 1 else 0.0,
        fps=_median_fps(times_ns),
    )


# ---------------------------------------------------------------- 音频
def _finish_audio(stage, chans, time_base_ns, perf=None):
    """多路音频只取第一路；其余仅记录，绝不把不同采样率/声道配置混进同一个 WAV"""
    if not chans:
        return None
    ch = chans[0]
    def close_spool():
        fh = ch.get('spool_fh')
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
            ch['spool_fh'] = None
        try:
            os.remove(ch.get('spool_path', ''))
        except OSError:
            pass
    # Unused audio channels also opened staging spools. Close/remove them
    # before publishing the staging directory (Windows rename semantics).
    for unused in chans[1:]:
        fh = unused.get('spool_fh')
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
            unused['spool_fh'] = None
        try:
            os.remove(unused.get('spool_path', ''))
        except OSError:
            pass
    cfg = ch['config'] or {}
    sr = int(cfg.get('sample_rate') or 0) or 16000
    nch = int(cfg.get('channels') or 0) or 1
    bits = int(cfg.get('bit_depth') or 0) or 16
    fmt = str(cfg.get('format') or '').strip().upper().replace('-', '_')
    other = [dict(id=c['id'], topic=c['topic'], config=c['config'])
             for c in chans[1:]]
    base_meta = dict(
        channel_id=ch['id'], topic=ch['topic'], sample_rate=sr,
        channels=nch, bit_depth=bits, format=cfg.get('format', ''),
        device=cfg.get('device_name', ''), chunks=len(ch['chunks']),
        other_channels=other)
    if bits != 16 or fmt not in ('', 'PCM_16', 'PCM16', 'S16LE', 'PCM_S16LE'):
        close_spool()
        base_meta.update(
            playable=False, pcm_bytes=0, duration_s=0.0,
            note='不支持的音频格式：%s / %dbit；桌面端只播放 PCM_16。'
                 % (cfg.get('format') or '未声明', bits))
        return base_meta
    if not (1 <= nch <= 32 and 1 <= sr <= 768000):
        close_spool()
        base_meta.update(playable=False, pcm_bytes=0, duration_s=0.0,
                         note='音频参数异常：%d Hz / %d 通道。' % (sr, nch))
        return base_meta

    sample_bytes = bits // 8
    frame_bytes = nch * sample_bytes
    _ensure_time_order(ch['chunks'])         # OPT-02 safety net
    spool = ch.get('spool_fh')
    if spool is not None:
        spool.flush()
        spool.close()
        ch['spool_fh'] = None
    for _lg, _off, ln in ch['chunks']:
        if ln % frame_bytes:
            close_spool()
            base_meta.update(playable=False, pcm_bytes=0, duration_s=0.0,
                             note='音频块长度不是采样帧大小的整数倍，已拒绝播放。')
            return base_meta
    name = 'audio.wav'
    pcm_bytes = 0
    _t = time.perf_counter()
    with wave.open(os.path.join(stage, name), 'wb') as wf:
        wf.setnchannels(nch)
        wf.setsampwidth(sample_bytes)
        wf.setframerate(sr)
        with open(ch['spool_path'], 'rb') as sf:
            for _lg, off, ln in ch['chunks']:
                sf.seek(off)
                payload = sf.read(ln)
                wf.writeframesraw(payload)
                pcm_bytes += len(payload)
    try:
        os.remove(ch['spool_path'])
    except OSError:
        pass
    if perf is not None:
        perf.setdefault('hotpath', {})['audio_write_ms'] = round(
            (time.perf_counter() - _t) * 1000.0, 1)
    first_ns = ch['chunks'][0][0]
    byte_rate = max(1, sr * nch * sample_bytes)
    meta = dict(base_meta,
        playable=True, file=name, pcm_bytes=pcm_bytes,
        duration_s=pcm_bytes / byte_rate,
        start_offset_ns=first_ns - time_base_ns,
        start_offset_s=(first_ns - time_base_ns) / NS,
    )
    if len(chans) > 1:
        meta['note'] = ('文件里有 %d 路音频通道，本程序只播放第一路（%s）；'
                        '其余通道没有混入同一个 WAV。' % (len(chans), ch['topic']))
    return meta


# ---------------------------------------------------------------- IMU
def _finish_imu(stage, chans, time_base_ns, perf=None):
    """多路 IMU 只取第一路；其余仅记录

    perf 非空时把"构造数组 / JSON 序列化 / 落盘"三段耗时记入
    perf['hotpath']（只写日志，不改变输出字节：依然是同样的 json.dumps 文本）。
    """
    if not chans:
        return None
    ch = chans[0]
    name = 'imu.json'
    acc = ch['samples']
    _t = time.perf_counter()
    timestamps, cols = _imu_json_columns(acc)
    t = [(ts - time_base_ns) / NS for ts in timestamps]
    av = cols[:3]
    la = cols[3:]
    _build_ms = (time.perf_counter() - _t) * 1000.0
    _t = time.perf_counter()
    with open(os.path.join(stage, name), 'w', encoding='utf-8') as fh:
        fh.write('{"topic":')
        fh.write(json.dumps(ch['topic'], separators=(',', ':')))
        fh.write(',"t":')
        fh.write(json.dumps(t, separators=(',', ':')))
        fh.write(',"av":')
        fh.write(json.dumps(av, separators=(',', ':')))
        fh.write(',"la":')
        fh.write(json.dumps(la, separators=(',', ':')))
        fh.write('}')
    _ser_ms = (time.perf_counter() - _t) * 1000.0
    _write_ms = _ser_ms
    if perf is not None:
        hp = perf.setdefault('hotpath', {})
        hp['imu_build_arrays_ms'] = round(_build_ms, 1)
        hp['imu_json_serialize_ms'] = round(_ser_ms, 1)
        hp['imu_json_write_ms'] = round(_write_ms, 1)
        hp['imu_json_bytes'] = os.path.getsize(os.path.join(stage, name))
    meta = dict(file=name, channel_id=ch['id'], topic=ch['topic'],
                count=len(acc['timestamps']),
                fields=['gyro_x', 'gyro_y', 'gyro_z', 'acc_x', 'acc_y', 'acc_z'],
                other_channels=[dict(id=c['id'], topic=c['topic'])
                                for c in chans[1:]])
    if len(chans) > 1:
        meta['note'] = ('文件里有 %d 路 IMU 通道，本程序只显示第一路（%s）。'
                        % (len(chans), ch['topic']))
    return meta


# ================================================================== 读取
def load_manifest(outdir, source_path=None, check_files=True):
    """读取并校验缓存清单；无效返回 None（调用方负责安全重建）"""
    return appcache.load_manifest(outdir, source_path=source_path,
                                  check_files=check_files)


def cache_is_valid(outdir, source_path):
    return load_manifest(outdir, source_path=source_path) is not None


def free_space_ok(outdir):
    try:
        target = os.path.dirname(os.path.abspath(outdir)) or os.getcwd()
        return shutil.disk_usage(target).free > _MIN_FREE_BYTES
    except Exception:
        return True


__all__ = ['prepare', 'load_manifest', 'cache_is_valid', 'read_times',
           'MANIFEST_VERSION', 'free_space_ok', 'running_jobs']
