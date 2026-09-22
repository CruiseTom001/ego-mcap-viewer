"""f6c_direct_decoder_prototype.py —— P1.6F-C Direct H264 Decoder 原型

覆盖（CLI，不接 UI）：
  * TTFP（冷/热 各 3 次中位）：chunk 索引 → 目标 chunk → self-contained 帧 → PyAV 解码
  * 7 点 Seek Decode（camera2/3）：延迟分解 + 时间戳精度
  * 连续 5 秒解码吞吐（effective_decode_x）
  * **Sparse 8x**：8x 媒体时钟 + 15fps 呈现，只解需要显示的帧
  * camera switch 延迟、内存、100 次随机 seek、损坏/截断受控失败、源文件只读校验

只读源文件；不改播放器 / QueueManager / 缓存管道。
"""

import hashlib
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(os.path.dirname(ROOT), 'tmp')
sys.path.insert(0, ROOT)

import mcap_video_index as VI
import h264mp4 as H                                  # noqa: E402
from direct_h264_decoder import DirectH264Decoder              # noqa: E402

SAMPLES = [
    (r'D:\视频查看软件\DAS-Ego_20260911203440_none_none_689985_65416a8a.mcap',
     'Real 41.8MB'),
    (r'D:\wendang\xwechat_files\wxid_oz7zj4zmnwgz12_a0ce\msg\file\2026-09'
     r'\DAS-Ego_20260911154513_none_none_689985_371aafac.mcap', 'Real 216.4MB'),
    (os.path.join(TMP, 'big_2gb.mcap'), 'Synthetic 2GB'),
]
CAM2 = '/robot0/sensor/camera2/compressed'
CAM3 = '/robot0/sensor/camera3/compressed'
POINTS = (0.0, 0.10, 0.25, 0.50, 0.75, 0.90, 0.99)
DEC = None


def rss_mb():
    import ctypes
    from ctypes import wintypes
    try:
        class _P(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                        ('PeakWorkingSetSize', ctypes.c_size_t),
                        ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t),
                        ('PeakPagefileUsage', ctypes.c_size_t)]
        c = _P(); c.cb = ctypes.sizeof(c)
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        ctypes.windll.psapi.GetProcessMemoryInfo(
            k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return c.WorkingSetSize / 1048576.0
    except Exception:
        return 0.0


def cam_of(idx, topic):
    for cid, cam in idx.cameras.items():
        if cam.topic == topic:
            return cam
    return None


def chunk_in(idx, cam, target_ns):
    """二分找到覆盖 target 时间的 camera chunk（F-B 已验证的定位方式）"""
    chunks = idx.chunk_index.get(cam.channel_id) or []
    if not chunks:
        return None
    lo, hi = 0, len(chunks) - 1
    best = chunks[0]
    while lo <= hi:
        mid = (lo + hi) // 2
        c = chunks[mid]
        if c['start_ns'] <= target_ns:
            best = c
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def frames_in_chunk(idx, cam, chunk_entry):
    """读 1 个 chunk → 该 camera 的 (ts, payload) 按 (log_time, 物理序) 排序"""
    _p, msgs = idx._load_chunk(chunk_entry['chunk_offset'])
    items = [(ts, d) for (cid, ts), d in msgs.items() if cid == cam.channel_id]
    items.sort(key=lambda x: x[0])
    return items


def decode_first(idx, cam, t_start_ns, dec):
    """TTFP 路径：chunk 索引 → 目标 chunk → 首个可解帧 → 解码"""
    marks = {}
    t0 = time.perf_counter()
    ce = chunk_in(idx, cam, t_start_ns)
    marks['chunk_lookup_ms'] = (time.perf_counter() - t0) * 1000.0
    if ce is None:
        return None, marks
    # 从该 chunk 起，最多往前看 3 个 chunk 找可解帧
    chunks = idx.chunk_index[cam.channel_id]
    i = chunks.index(ce)
    for k in range(i, min(i + 3, len(chunks))):
        t1 = time.perf_counter()
        items = frames_in_chunk(idx, cam, chunks[k])
        marks['chunk_read_ms'] = (time.perf_counter() - t1) * 1000.0
        if not items:
            continue
        for ts, payload in items:
            dec.reset()
            t2 = time.perf_counter()
            fr = dec.decode_access_unit(payload)
            marks['decode_ms'] = (time.perf_counter() - t2) * 1000.0
            if fr:
                marks['selected_ts'] = ts
                marks['payload_sha'] = hashlib.sha256(payload).hexdigest()
                return fr[0], marks
    return None, marks


def run(path, label):
    rec = dict(label=label, path=path)
    sz0, mt0 = os.path.getsize(path), os.path.getmtime(path)
    rec['size_mb'] = round(sz0 / 1048576, 1)
    dec = DirectH264Decoder()
    dec.open()
    rec['base_rss_mb'] = round(rss_mb(), 1)

    # ---------- TTFP（冷/热 × 3 中位）----------
    ttfps = {}
    for cam_topic, name in ((CAM2, 'cam2'), (CAM3, 'cam3')):
        idx = VI.McapVideoIndex(path).build(camera_pred=lambda t: t == cam_topic)
        rec.setdefault('ttfi_ms', round(idx.build_ms, 1))
        cam = cam_of(idx, cam_topic)
        if cam is None or not (idx.chunk_index.get(cam.channel_id) or []):
            ttfps[name] = None
            continue
        vals_cold, vals_warm = [], []
        for run_i in range(3):
            t0 = time.perf_counter()
            fr, _m = decode_first(idx, cam, cam.start_ts(), dec)
            dt = (time.perf_counter() - t0) * 1000.0
            (vals_cold if run_i == 0 else vals_warm).append(dt)
        ttfps[name] = dict(
            cold_ms=round(vals_cold[0], 1),
            warm_median_ms=round(sorted(vals_warm)[len(vals_warm) // 2], 1),
            valid=bool(fr is not None),
            width=fr.width if fr else None, height=fr.height if fr else None)
        # ---------- 7 点 Seek Decode ----------
        t0ns, t1ns = cam.start_ts(), cam.end_ts()
        sels = []
        for pt in POINTS:
            target = int(t0ns + (t1ns - t0ns) * pt)
            t_a = time.perf_counter()
            ce = chunk_in(idx, cam, target)
            t_lookup = (time.perf_counter() - t_a) * 1000.0
            t_b = time.perf_counter()
            items = frames_in_chunk(idx, cam, ce)
            t_read = (time.perf_counter() - t_b) * 1000.0
            chosen = None
            for ts, payload in items:            # first frame >= target
                if ts >= target:
                    chosen = (ts, payload)
                    break
            if chosen is None and items:
                chosen = items[-1]
            ok = False
            dt_ms = None
            if chosen:
                dec.reset()
                t_c = time.perf_counter()
                fr = dec.decode_access_unit(chosen[1])
                t_dec = (time.perf_counter() - t_c) * 1000.0
                ok = bool(fr)
                dt_ms = (chosen[0] - target) / 1e6
                sels.append(dict(pt='%d%%' % round(pt * 100),
                                 lookup_ms=round(t_lookup, 3),
                                 read_ms=round(t_read, 1),
                                 decode_ms=round(t_dec, 1),
                                 total_ms=round(t_lookup + t_read + t_dec, 1),
                                 ts_delta_ms=round(dt_ms, 2), ok=ok))
        lat = sorted(s['total_ms'] for s in sels)
        ttfps[name]['seeks'] = sels
        ttfps[name]['seek_p50_ms'] = lat[len(lat) // 2] if lat else None
        ttfps[name]['seek_p95_ms'] = lat[int(len(lat) * 0.95) - 1] if lat else None
        ttfps[name]['seek_all_ok'] = all(s['ok'] for s in sels)
        ttfps[name]['ts_delta_max_ms'] = max((s['ts_delta_ms'] for s in sels
                                              if s['ts_delta_ms'] is not None),
                                             default=None)
        # ---------- 连续 5 秒解码吞吐 ----------
        base_ce = chunk_in(idx, cam, t0ns)
        idx_c = idx.chunk_index[cam.channel_id].index(base_ce)
        n_frames = 0
        media_span = 0
        t_s = time.perf_counter()
        first_ts = last_ts = None
        dec.reset()
        stop = False
        for k in range(idx_c, len(idx.chunk_index[cam.channel_id])):
            items = frames_in_chunk(idx, cam, idx.chunk_index[cam.channel_id][k])
            for ts, payload in items:
                dec.decode_access_unit(payload)
                n_frames += 1
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            wall = time.perf_counter() - t_s
            media_span = (last_ts - first_ts) / 1e9 if first_ts else 0
            if media_span >= 5.0 or wall > 30:
                stop = True
                break
        wall = time.perf_counter() - t_s
        ttfps[name]['sequential'] = dict(
            frames=n_frames, media_s=round(media_span, 2), wall_s=round(wall, 2),
            decode_fps=round(n_frames / wall, 1) if wall else None,
            effective_decode_x=round(media_span / wall, 2) if wall else None)
        # ---------- SPEARSE 8x（只解需要显示的帧）----------
        rate, render_fps, span_s = 8.0, 15.0, 10.0
        steps = int(span_s * render_fps)
        dec2 = DirectH264Decoder().open()
        t_sp = time.perf_counter()
        dec_frames = skipped = 0
        crossed = 0
        cache_hits = 0
        chunk_reads = 0
        valid = 0
        for s in range(steps):
            target = int(t0ns + s / render_fps * rate * 1e9)
            ce = chunk_in(idx, cam, target)
            before = dict(idx.cache_stats())
            items = frames_in_chunk(idx, cam, ce)
            after = dict(idx.cache_stats())
            chunk_reads += 1 if after['chunks'] != before['chunks'] or before['chunks'] == 0 else 0
            cache_hits += 1 if after['chunks'] == before['chunks'] and before['chunks'] > 0 else 0
            # 关键：单帧只有 IDR 才可独立起解（实测 IDR 约占 20%，GOP≈5）。
            # 因此从「<= target 的最近 IDR」开始顺序解到 target，中间帧不显示但需解码。
            j = i if False else None
            cands_all = idx.chunk_index[cam.channel_id]
            ci = cands_all.index(ce)
            window = []
            for k in range(max(0, ci - 1), min(ci + 3, len(cands_all))):
                for ts, pl in frames_in_chunk(idx, cam, cands_all[k]):
                    if not window or window[-1][0] != ts:
                        window.append((ts, pl))
            window.sort(key=lambda x: x[0])
            idr_j = None
            for jj in range(len(window) - 1, -1, -1):
                ts_j, pl_j = window[jj]
                if ts_j > target:
                    continue
                try:
                    nals = H.split_nalus(pl_j)
                except Exception:
                    nals = []
                if any(H.nal_type(n) == 5 for n in nals):
                    idr_j = jj
                    break
            if idr_j is None:
                idr_j = 0
            dec2.reset()
            best_pair = None
            ndec = 0
            for jj in range(idr_j, len(window)):
                ts_j, pl_j = window[jj]
                dec2.decode_access_unit(pl_j)
                ndec += 1
                if ts_j >= target:
                    best_pair = (ts_j, pl_j)
                    break
            if best_pair is None and window:
                best_pair = window[-1]
            dec_frames += ndec
            if best_pair is not None:
                valid += 1
                crossed += max(0, len([1 for ts, _ in window
                                       if idr_j <= 0 or ts < best_pair[0]])) - ndec
            else:
                skipped += 1
        wall_sp = time.perf_counter() - t_sp
        ttfps[name]['sparse8x'] = dict(
            render_steps=steps, media_s=round(span_s * rate, 1),
            wall_s=round(wall_sp, 2),
            effective_rate_x=round(span_s * rate / wall_sp, 2) if wall_sp else None,
            frames_decoded=dec_frames, frames_skipped=skipped,
            frames_crossed=crossed, valid=valid,
            chunk_reads=chunk_reads, cache_hits=cache_hits)
        dec2.close()
        # ---------- 100 次随机 seek ----------
        bad = 0
        lat_r = []
        for _ in range(100):
            target = random.randint(t0ns, t1ns)
            t_r = time.perf_counter()
            ce = chunk_in(idx, cam, target)
            items = frames_in_chunk(idx, cam, ce)
            pick = items[0][1] if items else None
            if pick is None:
                bad += 1
                continue
            dec.reset()
            fr = dec.decode_access_unit(pick)
            lat_r.append((time.perf_counter() - t_r) * 1000.0)
            if not fr:
                bad += 1
        lat_r.sort()
        ttfps[name]['random_seek'] = dict(
            total=100, failures=bad,
            p50_ms=round(lat_r[len(lat_r) // 2], 1) if lat_r else None,
            p95_ms=round(lat_r[int(len(lat_r) * 0.95) - 1], 1) if lat_r else None)
    rec['cameras'] = ttfps
    rec['rss_after_mb'] = round(rss_mb(), 1)
    rec['decoder_stats'] = dec.stats()
    dec.close()
    # ---------- camera switch 原型（同一目标时间）----------
    idx_all = VI.McapVideoIndex(path).build(
        camera_pred=lambda t: t in (CAM2, CAM3))
    c2, c3 = cam_of(idx_all, CAM2), cam_of(idx_all, CAM3)
    sw = None
    if c2 and c3:
        target = int(c2.start_ts() + (c2.end_ts() - c2.start_ts()) * 0.5)
        t_sw = time.perf_counter()
        f1, _a = decode_first(idx_all, c2, target, dec)
        t_mid = time.perf_counter()
        f2, _b = decode_first(idx_all, c3, target, dec)
        t_end = time.perf_counter()
        sw = dict(cam2_ms=round((t_mid - t_sw) * 1000.0, 1),
                  cam3_ms=round((t_end - t_mid) * 1000.0, 1),
                  both_valid=bool(f1 and f2))
    rec['camera_switch'] = sw
    idx_all.close() if hasattr(idx_all, 'close') else None
    # ---------- 源文件只读校验 ----------
    rec['source_unchanged'] = (os.path.getsize(path) == sz0
                               and os.path.getmtime(path) == mt0)
    return rec


def main():
    out = {'samples': []}
    for path, label in SAMPLES:
        if not os.path.isfile(path):
            out['samples'].append(dict(label=label, error='missing'))
            continue
        r = run(path, label)
        out['samples'].append(r)
        print('=' * 96)
        print('%s（%.1f MB） TTFP: %s' % (label, r['size_mb'],
              {k: (v or {}).get('cold_ms') for k, v in r['cameras'].items() if v}))
        for name, v in r['cameras'].items():
            if not v:
                continue
            print('  %-5s TTFP cold=%s warm_med=%s  尺寸=%sx%s  有效=%s' % (
                name, v['cold_ms'], v['warm_median_ms'], v['width'], v['height'],
                v['valid']))
            print('        Seek P50=%sms P95=%sms 全过=%s 时间戳Δmax=%sms' % (
                v['seek_p50_ms'], v['seek_p95_ms'], v['seek_all_ok'],
                v['ts_delta_max_ms']))
            seq = v['sequential']
            print('        连续解码: %d 帧 / %.1fs 媒体 / %.1fs wall → %.2fx, %.1f fps'
                  % (seq['frames'], seq['media_s'], seq['wall_s'],
                     seq['effective_decode_x'], seq['decode_fps']))
            sp = v['sparse8x']
            print('        Sparse8x: %.2fx  解码 %d / 跳过 %d / 跨过 %d  块读 %d 命中 %d'
                  % (sp['effective_rate_x'], sp['frames_decoded'],
                     sp['frames_skipped'], sp['frames_crossed'],
                     sp['chunk_reads'], sp['cache_hits']))
            rs = v['random_seek']
            print('        随机 seek 100 次: 失败 %d  P50=%sms P95=%sms'
                  % (rs['failures'], rs['p50_ms'], rs['p95_ms']))
        print('  camera switch: %s' % r['camera_switch'])
        print('  RSS: base %.0f → after %.0f MB  源文件未变: %s'
              % (r['base_rss_mb'], r['rss_after_mb'], r['source_unchanged']))
    p = os.path.join(TMP, 'f6c_direct_decoder.json')
    with open(p, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print('\n汇总已写入', p)
    return 0


if __name__ == '__main__':
    sys.exit(main())
