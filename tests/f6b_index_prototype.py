"""f6b_index_prototype.py —— P1.6F-B：Indexed VideoIndex + Seek 原型验证

对每个样本：
  1. 用 Summary/ChunkIndex/MessageIndex 建索引（不解压）→ 测 TTFI
  2. 验证 physical order vs sorted order 单调性
  3. 0/10/25/50/75/90/99% × camera2/camera3：index lookup → 读单 chunk → 取 payload
     与**独立路径 ground truth** 比对 SHA256（小样本另做全量 ground truth）
  4. 20 次随机 seek 测 P50/P95（index lookup / 单点访问）
  5. 索引内存（tracemalloc）

只读源文件；不改播放器 / QueueManager / 缓存管道。
"""

import hashlib
import json
import os
import random
import sys
import time
import tracemalloc

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(os.path.dirname(ROOT), 'tmp')
sys.path.insert(0, ROOT)

import mcap_reader as MR                                  # noqa: E402
import mcap_video_index as VI                             # noqa: E402
from mcap.reader import ReadDataStream, make_reader        # noqa: E402
from mcap.records import Chunk, Message                    # noqa: E402
from mcap.stream_reader import get_chunk_data_stream       # noqa: E402

SAMPLES = [
    (r'D:\视频查看软件\DAS-Ego_20260911203440_none_none_689985_65416a8a.mcap',
     'Real 41.8MB', True),
    (r'D:\wendang\xwechat_files\wxid_oz7zj4zmnwgz12_a0ce\msg\file\2026-09'
     r'\DAS-Ego_20260911154513_none_none_689985_371aafac.mcap', 'Real 216.4MB', True),
    (os.path.join(TMP, 'big_2gb.mcap'), 'Synthetic 2GB', False),
]
CAM_PRED = lambda t: ('camera2' in (t or '') or 'camera3' in (t or ''))   # noqa: E731
POINTS = (0.0, 0.10, 0.25, 0.50, 0.75, 0.90, 0.99)


def sha(b):
    return hashlib.sha256(b).hexdigest() if b else None


def gt_chunk_map(path, chunk_offset):
    """独立路径：直接读该 chunk 解压后，逐条 Message 解析（不经索引）→ {(cid,ts): sha}"""
    out = {}
    with open(path, 'rb') as fh:
        fh.seek(int(chunk_offset) + 1 + 8)
        chunk = Chunk.read(ReadDataStream(fh))
    stream, length = get_chunk_data_stream(chunk, validate_crc=False)
    while stream.count < length:
        op = stream.read1()
        ln = stream.read8()
        if op == 0x05:
            m = Message.read(stream, ln)
            out[(m.channel_id, m.log_time)] = sha(m.data)
        else:
            stream.read(ln)
    return out


def gt_full(path, cam_ids):
    """全量 ground truth（仅小样本）：官方排序路径 {(cid,ts): sha}"""
    r = MR.McapReader(path)
    out = {}
    for cid, lg, _p, _s, data in r.iter_messages(set(cam_ids),
                                                 log_time_order=True,
                                                 pushdown=False):
        try:
            fmt, payload, _e = MR.decode_video(r.channels[cid]['schema']
                                               and r.schemas[r.channels[cid]['schema_id']]['name'],
                                               r.channels[cid]['message_encoding'], data)
        except Exception:
            payload = None
        if payload:
            out[(cid, lg)] = sha(payload)
    return out


def run(path, label, do_full_gt):
    rec = dict(label=label, path=path, size_mb=round(os.path.getsize(path) / 1048576, 1))
    tracemalloc.start()
    idx = VI.McapVideoIndex(path).build(camera_pred=CAM_PRED)
    tti = idx.build_ms
    idx.build_frame_index()
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rec['ttfi_ms'] = round(tti, 1)
    rec['frame_index_ms'] = round(idx.frame_index_ms or 0, 1)
    rec['granularity'] = idx.granularity
    rec['has_message_index'] = idx.has_message_index
    rec['chunk_count'] = idx.chunk_count
    cams = list(idx.cameras.keys())
    rec['cameras'] = []
    rec['index_mem_mb'] = round(peak / 1048576, 2)
    full_gt = {}
    if do_full_gt:
        t0 = time.perf_counter()
        full_gt = gt_full(path, cams)
        rec['full_gt_seconds'] = round(time.perf_counter() - t0, 2)
        rec['full_gt_entries'] = len(full_gt)
    for cid in cams:
        cam = idx.cameras[cid]
        c = dict(channel_id=cid, topic=cam.topic, frames=len(cam.frames),
                 physical_monotonic=cam.physical_monotonic,
                 sorted_monotonic=cam.sorted_monotonic,
                 start_ts=cam.start_ts(), end_ts=cam.end_ts(), seeks={})
        if full_gt:
            # 全量一致性：索引时间戳集合 == ground truth 时间戳集合
            idx_ts = set(cam.times)
            gt_ts = {ts for (c2, ts) in full_gt if c2 == cid}
            c['timestamps_match_ground_truth'] = (idx_ts == gt_ts)
            c['gt_frame_count'] = len(gt_ts)
        t0, t1 = cam.start_ts(), cam.end_ts()
        for pt in POINTS:
            target = int(t0 + (t1 - t0) * pt)
            t_l0 = time.perf_counter()
            fr = cam.nearest(target)
            t_lookup = (time.perf_counter() - t_l0) * 1000.0
            t_r0 = time.perf_counter()
            payload = idx.read_frame(cam, fr)
            t_read = (time.perf_counter() - t_r0) * 1000.0
            got = sha(payload)
            exp = full_gt.get((cid, fr.ts_ns)) if full_gt else \
                gt_chunk_map(path, fr.chunk_offset).get((cid, fr.ts_ns))
            c['seeks']['%d%%' % round(pt * 100)] = dict(
                ts=fr.ts_ns, lookup_ms=round(t_lookup, 3),
                access_ms=round(t_read, 2), sha_ok=(got == exp),
                payload_bytes=len(payload) if payload else 0)
        # 随机 20 次：P50/P95
        lat, acc = [], []
        for _ in range(20):
            target = random.randint(t0, t1)
            a = time.perf_counter()
            fr = cam.nearest(target)
            lat.append((time.perf_counter() - a) * 1000.0)
            b = time.perf_counter()
            idx.read_frame(cam, fr)
            acc.append((time.perf_counter() - b) * 1000.0)
        lat.sort(); acc.sort()
        c['lookup_p50_ms'] = round(lat[len(lat) // 2], 3)
        c['lookup_p95_ms'] = round(lat[int(len(lat) * 0.95) - 1], 3)
        c['access_p50_ms'] = round(acc[len(acc) // 2], 2)
        c['access_p95_ms'] = round(acc[int(len(acc) * 0.95) - 1], 2)
        c['all_sha_ok'] = all(v['sha_ok'] for v in c['seeks'].values())
        rec['cameras'].append(c)
    rec['cache_stats'] = idx.cache_stats()
    # 1GB 增量成本参考：Index 内存 / 帧数
    return rec


def main():
    out = {'samples': []}
    for path, label, do_full in SAMPLES:
        if not os.path.isfile(path):
            out['samples'].append(dict(label=label, path=path, error='missing'))
            continue
        r = run(path, label, do_full)
        out['samples'].append(r)
        print('=' * 92)
        print('%s（%.1f MB）  TTFI=%.1f ms  粒度=%s  chunk=%d  Index内存=%.2f MB'
              % (label, r['size_mb'], r['ttfi_ms'], r['granularity'],
                 r['chunk_count'], r['index_mem_mb']))
        if r.get('full_gt_entries'):
            print('  全量 ground truth：%d 条（%.1fs）'
                  % (r['full_gt_entries'], r['full_gt_seconds']))
        for c in r['cameras']:
            print('  cid=%d %-38s frames=%d  物理单调=%s 排序后单调=%s'
                  % (c['channel_id'], c['topic'], c['frames'],
                     c['physical_monotonic'], c['sorted_monotonic']))
            if 'timestamps_match_ground_truth' in c:
                print('     时间戳集合与 ground truth 一致: %s（gt %d 帧）'
                      % (c['timestamps_match_ground_truth'], c['gt_frame_count']))
            print('     lookup P50=%.3fms P95=%.3fms | 单点访问 P50=%.1fms P95=%.1fms'
                  % (c['lookup_p50_ms'], c['lookup_p95_ms'],
                     c['access_p50_ms'], c['access_p95_ms']))
            bad = [k for k, v in c['seeks'].items() if not v['sha_ok']]
            print('     7 点 SHA 校验: %s%s' % ('全过' if not bad else '失败 %s' % bad,
                                                '' if not bad else ''))
    p = os.path.join(TMP, 'f6b_index_prototype.json')
    with open(p, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print('\n汇总已写入', p)
    return 0


if __name__ == '__main__':
    sys.exit(main())
