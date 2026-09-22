"""perf_ab_audio.py —— 音频消除 A/B 交错基准（P1.6D-R2A 验收）

交错顺序 A B B A A B（A=Keep Audio，B=No Audio），消除系统性漂移。
每组 warmup 1 次（不计入）。输出速度 / 内存(RSS) / 缓存体积 对比。

用法：
  python tests/perf_ab_audio.py --mcap <2GB indexed> [--mcap <2GB noidx>] --runs 3
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


def _ab(which, mcap, idx, stamp, dim):
    """A/B 语义：dim=audio → A=保留音频 / B=不保留（IMU 均按 profile OFF）
             dim=imu   → A=Audio OFF+IMU ON（noaudio_v2 行为）
                         B=Audio OFF+IMU OFF（videoonly_v3 行为）"""
    if dim == 'imu':
        return run_once(mcap, keep_audio=False, idx=idx, stamp=stamp,
                        keep_imu=(which == 'keep'))
    return run_once(mcap, keep_audio=(which == 'keep'), idx=idx, stamp=stamp,
                    keep_imu=False)


def run_once(mcap, keep_audio, idx, stamp, keep_imu=None):
    base = 'ab_%s_%d' % (stamp, idx)
    out = os.path.join(TMP, base + '.json')
    log = os.path.join(TMP, base + '.log')
    cmd = [sys.executable, os.path.join(HERE, 'perf_bench.py'),
           '--mcap', mcap, '--out', out]
    if keep_audio:
        cmd.append('--keep-audio')
    if keep_imu:
        cmd.append('--keep-imu')
    with open(log, 'w', encoding='utf-8') as fh:
        r = subprocess.run(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)
    if r.returncode != 0 or not os.path.isfile(out):
        print('  !! 第 %d 次运行失败，见 %s' % (idx, log))
        return None
    with open(out, encoding='utf-8') as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mcap', action='append', required=True)
    ap.add_argument('--dim', choices=('audio', 'imu'), default='imu',
                    help='A/B 维度：audio 或 imu')
    ap.add_argument('--runs', type=int, default=3)
    ap.add_argument('--warmup', type=int, default=1)
    a = ap.parse_args()

    stamp = time.strftime('%m%d-%H%M%S') + '_%d' % os.getpid()
    order = ['keep', 'no', 'no', 'keep', 'keep', 'no']    # A B B A A B
    summary = []
    for mcap in a.mcap:
        size_mb = os.path.getsize(mcap) / 1048576.0
        print('=' * 96)
        print('[%s 维度] %s（%.1f MB）  warmup %d + 交错 %d 次取中位（%s）'
              % (a.dim, os.path.basename(mcap), size_mb, a.warmup, a.runs,
                 ' '.join(order[:a.runs])))
        print('=' * 96)
        # warmup（各一次，丢弃）
        for w in range(a.warmup):
            _ab('keep', mcap, 900 + w, stamp, a.dim)
            _ab('no', mcap, 901 + w, stamp, a.dim)
        # 交错测量
        seq = []
        i = 0
        while len(seq) < a.runs:
            seq.append(order[len(seq) % len(order)])
        runs = {'keep': [], 'no': []}
        for idx, which in enumerate(seq):
            d = _ab(which, mcap, idx, stamp, a.dim)
            if d:
                runs[which].append(d)
        med = {}
        for which in ('keep', 'no'):
            rows = runs[which]
            if not rows:
                continue

            def m(key):
                xs = [v for d in rows
                      for v in [_get(d, key)] if isinstance(v, (int, float))]
                return statistics.median(xs) if xs else None
            med[which] = dict(
                total_ms=m('perf.wall_ms'),
                iteration_ms=m('perf.mcap_iteration_ms'),
                reader_ms=m('perf.reader_setup_ms'),
                video_finalize_ms=m('perf.video_finalize_ms'),
                audio_process_ms=m('perf.audio_process_ms'),
                audio_finalize_ms=m('perf.hotpath.audio_finalize_ms'),
                imu_process_ms=m('perf.imu_process_ms'),
                manifest_ms=m('perf.manifest_write_ms'),
                publish_ms=m('perf.publish_ms'),
                peak_rss_mb=m('perf.rss_peak_mb'),
                cache_mb=m('fingerprint.on_disk_bytes') and
                         round(m('fingerprint.on_disk_bytes') / 1048576.0, 1),
                audio_wav_bytes=m('fingerprint.audio.file_bytes'),
                audio_selected=m('perf.hotpath.audio_selected_messages'),
                audio_decode_calls=m('perf.hotpath.audio_decode_calls'),
                audio_spool_written=m('perf.hotpath.audio_spool_bytes_written'),
                imu_selected=m('perf.hotpath.imu_selected_messages'),
                imu_decode_calls=m('perf.hotpath.imu_decode_calls'),
                imu_json_bytes=m('perf.hotpath.imu_json_bytes'),
            )
        for which, r in med.items():
            print('  %-12s total=%9.1f ms  遍历=%9.1f ms  peakRSS=%7.1f MB  '
                  'cache=%8.1f MB  audio.wav=%s'
                  % (which, r['total_ms'], r['iteration_ms'], r['peak_rss_mb'],
                     r['cache_mb'] or 0,
                     ('%.1f MB' % (r['audio_wav_bytes'] / 1048576.0))
                     if r['audio_wav_bytes'] else '无'))
            print('               audio_selected=%s audio_decode_calls=%s '
                  'spool_written=%s audio_finalize_ms=%s'
                  % (r['audio_selected'], r['audio_decode_calls'],
                     r['audio_spool_written'], r['audio_finalize_ms']))
            if a.dim == 'imu':
                print('               imu_selected=%s imu_decode_calls=%s '
                      'imu_json_bytes=%s'
                      % (r['imu_selected'], r['imu_decode_calls'],
                         r['imu_json_bytes']))
        if 'keep' in med and 'no' in med:
            ka, na = med['keep']['total_ms'], med['no']['total_ms']
            print('  ⟹ 速度：无音频 %+.1f%%（正数=更快）' % ((ka - na) / ka * 100.0))
            dmem = (med['keep']['peak_rss_mb'] or 0) - (med['no']['peak_rss_mb'] or 0)
            print('  ⟹ 内存：峰值下降 %.1f MB' % dmem)
            dsz = ((med['keep']['cache_mb'] or 0) - (med['no']['cache_mb'] or 0))
            if med['keep']['cache_mb']:
                print('  ⟹ 缓存体积：减少 %.1f MB（%.1f%%）'
                      % (dsz, dsz / med['keep']['cache_mb'] * 100.0))
        summary.append(dict(file=os.path.basename(mcap), size_mb=round(size_mb, 1),
                            medians=med, runs={k: len(v) for k, v in runs.items()}))
        print()

    out = os.path.join(TMP, 'ab_audio_summary_%s.json' % stamp)
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print('汇总已写入', out)
    return 0


def _get(d, path, default=None):
    cur = d
    for part in path.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


if __name__ == '__main__':
    sys.exit(main())
