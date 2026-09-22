"""perf_matrix.py —— 读取层优化的 A/B 矩阵基准（多组合 × 多次取中位数）

背景：单次测量波动可达 2 倍，必须重复测量取中位数才能下结论。
本脚本用子进程分别以不同开关组合跑 tests/perf_bench.py，输出中位数对比表：

  combo                 OPT_LAZY OPT_TOPICS OPT_ORDER   说明
  1 before-all-off          0        0          0        等价于优化前行为
  2 order-only              0        0          1        只关掉全局时间排序
  3 topics-only             0        1          0        只把订阅集合下推到库层
  4 topics+order            0        1          1        两项读取层优化
  5 all-on(默认)            1        1          1        再开无索引单遍

用法：
  python tests/perf_matrix.py --mcap <file.mcap> --runs 3 [--label small]
  python tests/perf_matrix.py --mcap a.mcap --mcap b.mcap --runs 3
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

COMBOS = [
    ('before-all-off', '0', '0', '0'),
    ('order-only',     '0', '0', '1'),
    ('topics-only',    '0', '1', '0'),
    ('topics+order',   '0', '1', '1'),
    ('all-on',         '1', '1', '1'),
]

KEYS = ('wall_ms', 'mcap_iteration_ms', 'reader_setup_ms',
        'video_finalize_ms', 'mcap')


def run_once(mcap, lazy, topics, order, tag, idx, stamp, extra_env=None, keep_audio=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    env['MCAPVIEWER_OPT_LAZY'] = lazy
    env['MCAPVIEWER_OPT_TOPICS'] = topics
    env['MCAPVIEWER_OPT_ORDER'] = order
    base = 'matrix_%s_%s_%d' % (stamp, tag.replace('+', '_'), idx)
    out = os.path.join(TMP, base + '.json')
    log = os.path.join(TMP, base + '.log')
    cmd = [sys.executable, os.path.join(HERE, 'perf_bench.py'),
           '--mcap', mcap, '--label', tag, '--out', out]
    if keep_audio is not None:
        cmd.append('--keep-audio')
    with open(log, 'w', encoding='utf-8') as fh:
        r = subprocess.run(cmd, cwd=ROOT, env=env, stdout=fh,
                           stderr=subprocess.STDOUT)
    if r.returncode != 0 or not os.path.isfile(out):
        print('  !! %s 第 %d 次失败，见 %s' % (tag, idx + 1, log))
        return None
    with open(out, encoding='utf-8') as fh:
        data = json.load(fh)
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mcap', action='append', required=True)
    ap.add_argument('--runs', type=int, default=3,
                    help='每个组合的测量次数（取中位数）')
    ap.add_argument('--warmup', type=int, default=1,
                    help='每个组合先跑几次丢弃（消除冷盘影响）')
    ap.add_argument('--combos', default='',
                    help='只跑指定组合（逗号分隔，如 order-only,all-on）')
    ap.add_argument('--label', default='matrix')
    ap.add_argument('--keep-audio', type=int, choices=(0, 1), default=None,
                    help='A/B 对照：1=保留音频 0=无音频（默认按 profile 合同）')
    ap.add_argument('--interleave', action='store_true', default=True,
                    help='交错运行（A B B A …），消除系统性漂移，默认开')
    ap.add_argument('--env', action='append', default=[],
                    help='附加环境变量 K=V（可多次），用于 A/B 对照')
    a = ap.parse_args()

    want = [c.strip() for c in a.combos.split(',') if c.strip()]
    extra_env = {}
    for item in a.env:
        if '=' in item:
            k, v = item.split('=', 1)
            extra_env[k.strip()] = v.strip()
    combos = [c for c in COMBOS if (not want or c[0] in want)]
    stamp = time.strftime('%m%d-%H%M%S') + '_%d' % os.getpid()
    summary = []

    for mcap in a.mcap:
        size_mb = os.path.getsize(mcap) / 1048576.0
        ka = (True if a.keep_audio == 1 else False) if a.keep_audio is not None else None
        print('=' * 96)
        print('%s（%.1f MB）  预热 %d 次 + 测量 %d 次取中位数   stamp=%s'
              % (os.path.basename(mcap), size_mb, a.warmup, a.runs, stamp))
        print('=' * 96)
        print('%-18s %10s %12s %12s %10s %11s' % (
            '组合', '总耗时(ms)', '遍历(ms)', 'reader(ms)', '封装(ms)', '消息数'))
        rows = {}
        vals_by_tag = {tag: {'wall_ms': [], 'mcap_iteration_ms': [],
                             'reader_setup_ms': [], 'video_finalize_ms': [],
                             'seen': []}
                       for tag, _l, _t, _o in combos}
        raw_by_tag = {tag: [] for tag, _l, _t, _o in combos}
        # 交错运行：同一轮里依次跑各组合（A B B A A B …），消除系统性漂移
        for w in range(a.warmup):
            for tag, lazy, topics, order in combos:
                run_once(mcap, lazy, topics, order, tag, 900 + w, stamp,
                         extra_env, keep_audio=ka)
        for i in range(a.runs):
            for tag, lazy, topics, order in combos:
                d = run_once(mcap, lazy, topics, order, tag, i, stamp,
                             extra_env, keep_audio=ka)
                if not d:
                    continue
                p = d['perf']
                vals = vals_by_tag[tag]
                vals['wall_ms'].append(p.get('wall_ms'))
                vals['mcap_iteration_ms'].append(p.get('mcap_iteration_ms'))
                vals['reader_setup_ms'].append(p.get('reader_setup_ms'))
                vals['video_finalize_ms'].append(p.get('video_finalize_ms'))
                vals['seen'].append(p.get('total_messages_seen'))
                raw_by_tag[tag].append(p.get('wall_ms'))
        for tag, lazy, topics, order in combos:
            vals = vals_by_tag[tag]
            raw = raw_by_tag.get(tag) or []
            if not vals['wall_ms']:
                continue

            def med(k):
                xs = [v for v in vals[k] if v is not None]
                return statistics.median(xs) if xs else None
            rows[tag] = dict(wall=med('wall_ms'), it=med('mcap_iteration_ms'),
                             rd=med('reader_setup_ms'),
                             fin=med('video_finalize_ms'), seen=med('seen'),
                             raw_wall=raw, runs=len(vals['wall_ms']),
                             file=os.path.basename(mcap), size_mb=round(size_mb, 1))
            r = rows[tag]
            print('%-18s %10.1f %12.1f %12.1f %10.1f %11.0f   raw=%s' % (
                tag, r['wall'], r['it'], r['rd'], r['fin'], r['seen'] or 0,
                ['%.0f' % x for x in raw]))

        base = rows.get('before-all-off')
        if base and base['it']:
            print()
            print('相对基线（before-all-off）的变化：')
            for tag, _l, _t, _o in combos:
                r = rows.get(tag)
                if not r or not r['it'] or tag == 'before-all-off':
                    continue
                print('  %-18s 遍历 %+7.1f%%   总耗时 %+7.1f%%'
                      % (tag, (r['it'] - base['it']) / base['it'] * 100.0,
                         (r['wall'] - base['wall']) / base['wall'] * 100.0))
        summary.append(dict(file=os.path.basename(mcap), size_mb=round(size_mb, 1),
                            rows={k: {kk: vv for kk, vv in v.items()}
                                  for k, v in rows.items()}))
        print()

    out = os.path.join(TMP, 'matrix_summary_%s.json' % stamp)
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    for k, v in extra_env.items():
        print('  附加环境变量:', k, '=', v)
    print('汇总已写入', out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
