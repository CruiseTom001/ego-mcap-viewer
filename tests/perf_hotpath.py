"""perf_hotpath.py —— Hot Path Attribution Audit 基准（三组固定样本）

把 `mcap_iteration_ms` 拆成：
  * 官方 reader 等待（next() 总/均值/p50/p95/max）
  * 应用侧处理（camera / IMU / audio / metadata 明细）
  * 进度上报
  * 未归因余量

外加收尾阶段：video finalize（含 mux）、IMU JSON（数组构造/序列化/写盘）、音频写盘。

用法：
  python tests/perf_hotpath.py --mcap <小> --mcap <2GB> --mcap <2GB无索引> \
      --runs 2 --warmup 1
"""

import os
import sys
import json
import time
import argparse
import statistics
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(os.path.dirname(ROOT), 'tmp')

#: 需要在多组样本间取中位数的指标（点分路径）
METRICS = [
    'wall_ms', 'total_ms', 'mcap_iteration_ms',
    'hotpath.reader_next_total_ms', 'hotpath.reader_next_avg_us',
    'hotpath.reader_next_p50_us', 'hotpath.reader_next_p95_us',
    'hotpath.app_total_ms', 'hotpath.progress_report_ms',
    'hotpath.unattributed_ms',
    'hotpath.camera_dispatch_ms', 'hotpath.camera_decode_ms',
    'hotpath.camera_raw_write_ms', 'hotpath.camera_index_append_ms',
    'hotpath.imu_deserialize_ms', 'hotpath.imu_collect_ms',
    'hotpath.audio_deserialize_ms', 'hotpath.audio_collect_ms',
    'hotpath.metadata_decode_ms',
    'video_finalize_ms', 'camera_mux_ms', 'imu_process_ms',
    'hotpath.imu_build_arrays_ms', 'hotpath.imu_json_serialize_ms',
    'hotpath.imu_json_write_ms', 'audio_process_ms', 'hotpath.audio_write_ms',
    'manifest_write_ms', 'publish_ms', 'reader_setup_ms',
]

COUNTS = ['total_messages_seen', 'selected_messages', 'imu_messages',
          'audio_messages']


def _get(d, path, default=None):
    cur = d
    for part in path.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def run_once(mcap, idx, stamp):
    base = 'hotpath_%s_%d' % (stamp, idx)
    out = os.path.join(TMP, base + '.json')
    log = os.path.join(TMP, base + '.log')
    cmd = [sys.executable, os.path.join(HERE, 'perf_bench.py'),
           '--mcap', mcap, '--label', 'hotpath', '--out', out]
    with open(log, 'w', encoding='utf-8') as fh:
        r = subprocess.run(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)
    if r.returncode != 0 or not os.path.isfile(out):
        print('  !! run %d 失败，见 %s' % (idx, log))
        return None
    with open(out, encoding='utf-8') as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mcap', action='append', required=True)
    ap.add_argument('--runs', type=int, default=2)
    ap.add_argument('--warmup', type=int, default=1)
    a = ap.parse_args()

    stamp = time.strftime('%m%d-%H%M%S') + '_%d' % os.getpid()
    summary = []
    for mcap in a.mcap:
        size_mb = os.path.getsize(mcap) / 1048576.0
        print('=' * 96)
        print('%s（%.1f MB）  warmup %d + runs %d（取中位数）'
              % (os.path.basename(mcap), size_mb, a.warmup, a.runs))
        print('=' * 96)
        for w in range(a.warmup):
            run_once(mcap, 900 + w, stamp)
        runs = [d for d in (run_once(mcap, i, stamp) for i in range(a.runs))
                if d]
        if not runs:
            print('  没有有效结果')
            continue
        med = {}
        for m in METRICS:
            vals = []
            for d in runs:
                v = _get(d['perf'], m)
                if isinstance(v, dict):      # 例如 camera_mux_ms（每相机）
                    v = sum(x for x in v.values()
                            if isinstance(x, (int, float)))
                if isinstance(v, (int, float)):
                    vals.append(v)
            if vals:
                med[m] = statistics.median(vals)
        counts = {}
        for c in COUNTS:
            vals = [_get(d['perf'], c) for d in runs]
            vals = [v for v in vals if isinstance(v, (int, float))]
            if vals:
                counts[c] = statistics.median(vals)
        kinds = {}
        for k in ('video', 'imu', 'audio', 'calibration', 'system', 'other'):
            vals = [_get(d['perf'], 'hotpath.messages_by_kind.' + k) for d in runs]
            vals = [v for v in vals if isinstance(v, (int, float))]
            if vals:
                kinds[k] = statistics.median(vals)

        total = med.get('total_ms') or med.get('wall_ms') or 0.0
        it = med.get('mcap_iteration_ms') or 0.0

        def line(label, ms, denom=total):
            share = ('%5.1f%%' % (ms / denom * 100.0)) if (ms and denom) else '   -  '
            print('  %-34s %10.1f ms   %s' % (label, ms, share))

        print('  ---- 总计 ----')
        line('总缓存时间', total)
        line('  其中 MCAP 遍历', it)
        line('  其中 视频封装(含 mux)', med.get('video_finalize_ms', 0.0))
        line('  其中 IMU 处理(收尾)', med.get('imu_process_ms', 0.0))
        line('  其中 音频处理(收尾)', med.get('audio_process_ms', 0.0))
        line('  其中 manifest+发布', (med.get('manifest_write_ms') or 0.0)
             + (med.get('publish_ms') or 0.0))
        line('  其中 reader 构造+索引', med.get('reader_setup_ms', 0.0))
        print('  ---- 遍历内部归因（占遍历 %.1f s）----' % (it / 1000.0))
        line('A. 官方 reader next() 等待', med.get('hotpath.reader_next_total_ms', 0.0), it)
        line('B. 应用侧处理', med.get('hotpath.app_total_ms', 0.0), it)
        line('   进度上报', med.get('hotpath.progress_report_ms', 0.0), it)
        line('   未归因余量', med.get('hotpath.unattributed_ms', 0.0), it)
        print('  ---- 应用侧细分（占应用侧 %.1f s）----'
              % ((med.get('hotpath.app_total_ms') or 0.0) / 1000.0))
        app = med.get('hotpath.app_total_ms') or 6.0
        line('camera 分支合计', med.get('hotpath.camera_dispatch_ms', 0.0), app)
        line('  其中 解码', med.get('hotpath.camera_decode_ms', 0.0), app)
        line('  其中 写 raw', med.get('hotpath.camera_raw_write_ms', 0.0), app)
        line('  其中 帧索引 append', med.get('hotpath.camera_index_append_ms', 0.0), app)
        line('IMU protobuf 解码', med.get('hotpath.imu_deserialize_ms', 0.0), app)
        line('IMU 入列', med.get('hotpath.imu_collect_ms', 0.0), app)
        line('audio protobuf 解码', med.get('hotpath.audio_deserialize_ms', 0.0), app)
        line('audio 入列', med.get('hotpath.audio_collect_ms', 0.0), app)
        line('metadata 解码', med.get('hotpath.metadata_decode_ms', 0.0), app)
        print('  ---- 收尾细分 ----')
        line('视频封装', med.get('video_finalize_ms', 0.0))
        line('  其中 mux(含写 mp4)', med.get('camera_mux_ms', 0.0))
        line('IMU JSON 构造数组', med.get('hotpath.imu_build_arrays_ms', 0.0))
        line('IMU JSON 序列化', med.get('hotpath.imu_json_serialize_ms', 0.0))
        line('IMU JSON 写盘', med.get('hotpath.imu_json_write_ms', 0.0))
        line('audio 写 WAV', med.get('hotpath.audio_write_ms', 0.0))
        print('  ---- 计数 ----')
        print('    遍历消息 %s  其中 %s' % (counts.get('total_messages_seen'),
                                          kinds or {}))
        print('    reader_next 均值 %.1f µs  p50 %.1f µs  p95 %.1f µs'
              % (med.get('hotpath.reader_next_avg_us', 0.0),
                 med.get('hotpath.reader_next_p50_us', 0.0),
                 med.get('hotpath.reader_next_p95_us', 0.0)))
        print()
        summary.append(dict(file=os.path.basename(mcap), size_mb=round(size_mb, 1),
                            median=med, counts=counts, kinds=kinds))

    out = os.path.join(TMP, 'hotpath_summary_%s.json' % stamp)
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print('汇总已写入', out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
