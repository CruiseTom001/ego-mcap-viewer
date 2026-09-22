"""P1.6A MCAP reader audit (SeekingReader vs NonSeekingReader).

This is deliberately an audit tool, not a production switch.  It opens the
same input file in a fresh subprocess for every sample, so peak RSS and file
cache effects do not leak between reader modes.  The default run interleaves
R0/R1/R2 samples and compares an exact semantic fingerprint of the selected
messages:

  R0  SeekingReader, topics pushed down, log_time_order=False
  R1  NonSeekingReader, topics pushed down, log_time_order=False
  R2  NonSeekingReader, topics=None, application-side topic filtering

Example:
  python tests/reader_audit.py --mcap ..\\tmp\\big_2gb.mcap --runs 3

The source file is opened read-only and is never modified.  A CRC probe can
be run against a separate small corrupted fixture with --crc-probe; it is
intentionally opt-in so auditing a multi-GB file never creates a second copy.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _rss_bytes():
    """Return (working_set, peak_working_set) on Windows, else (None, None)."""
    if os.name != 'nt':
        return None, None
    class Counters(ctypes.Structure):
        _fields_ = [
            ('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
            ('PeakWorkingSetSize', ctypes.c_size_t),
            ('WorkingSetSize', ctypes.c_size_t),
            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
            ('QuotaPagedPoolUsage', ctypes.c_size_t),
            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
            ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
            ('PagefileUsage', ctypes.c_size_t),
            ('PeakPagefileUsage', ctypes.c_size_t),
        ]
    c = Counters()
    c.cb = ctypes.sizeof(c)
    fn = ctypes.windll.psapi.GetProcessMemoryInfo
    # ctypes otherwise assumes int arguments; on 64-bit Windows that makes
    # the call fail silently for the PROCESS_MEMORY_COUNTERS structure.
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    ok = fn(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    if not ok:
        return None, None
    return int(c.WorkingSetSize), int(c.PeakWorkingSetSize)


def _percentile(values, p):
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return round(xs[0], 3)
    pos = (len(xs) - 1) * p
    lo = int(pos)
    hi = min(len(xs) - 1, lo + 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo), 3)


def _summary_topics(path):
    from mcap.reader import SeekingReader
    with open(path, 'rb') as fh:
        reader = SeekingReader(fh, validate_crcs=True)
        summary = reader.get_summary()
    if summary is None:
        return []
    return sorted({c.topic for c in summary.channels.values()})


def _default_topics(path, all_topics):
    """Mirror desktop.prepare's pushdown set.

    Keep camera2/3 video plus every non-video stream (IMU/audio/calibration),
    instead of benchmarking cameras alone.  This makes the application-side
    R2 comparison representative of the real cache workload.
    """
    try:
        import mcap_reader as app_reader
        channels = app_reader.McapReader(path).summary().get('channels') or []
        wanted = []
        for c in channels:
            topic = c.get('topic', '')
            if c.get('kind') != 'video' or 'camera2' in topic.lower() or 'camera3' in topic.lower():
                wanted.append(topic)
        return sorted(set(wanted)) or all_topics
    except Exception:
        camera = [t for t in all_topics
                  if 'camera2' in t.lower() or 'camera3' in t.lower()]
        return camera or all_topics


def _fingerprint_update(digest, by_topic, channel, message):
    topic = channel.topic
    # Include identity and timestamps so equal payloads on different channels
    # cannot accidentally make two reader paths look equivalent.
    head = struct.pack('<IQQI', int(channel.id), int(message.log_time),
                       int(message.publish_time), int(message.sequence))
    blob = head + struct.pack('<Q', len(message.data)) + message.data
    digest.update(blob)
    item = by_topic.setdefault(topic, [0, hashlib.sha256()])
    item[0] += 1
    item[1].update(blob)


def _single(path, mode, topics):
    from mcap.reader import NonSeekingReader, SeekingReader

    wanted = set(topics)
    reader_cls = SeekingReader if mode == 'R0' else NonSeekingReader
    digest = hashlib.sha256()
    by_topic = {}
    next_us = []
    app_us = []
    raw_count = selected = 0
    rss0, peak0 = _rss_bytes()
    t0 = time.perf_counter()
    try:
        with open(path, 'rb') as fh:
            reader = reader_cls(fh, validate_crcs=True)
            it = reader.iter_messages(
                topics=(topics if mode != 'R2' else None),
                log_time_order=False)
            while True:
                t_next = time.perf_counter()
                try:
                    _schema, channel, message = next(it)
                except StopIteration:
                    break
                next_us.append((time.perf_counter() - t_next) * 1e6)
                raw_count += 1
                t_app = time.perf_counter()
                if channel.topic in wanted:
                    selected += 1
                    _fingerprint_update(digest, by_topic, channel, message)
                app_us.append((time.perf_counter() - t_app) * 1e6)
    except Exception as exc:
        print(json.dumps({'ok': False, 'mode': mode,
                          'error': '%s: %s' % (type(exc).__name__, exc)},
                         ensure_ascii=False))
        return 2
    wall_ms = (time.perf_counter() - t0) * 1000.0
    rss1, peak1 = _rss_bytes()
    result = {
        'ok': True, 'mode': mode, 'path': os.path.basename(path),
        'raw_count': raw_count, 'selected_count': selected,
        'topics': topics,
        'fingerprint': digest.hexdigest(),
        'by_topic': {k: {'count': v[0], 'sha256': v[1].hexdigest()}
                    for k, v in sorted(by_topic.items())},
        'wall_ms': round(wall_ms, 3),
        'reader_next_p50_us': _percentile(next_us, .50),
        'reader_next_p95_us': _percentile(next_us, .95),
        'reader_next_max_us': round(max(next_us), 3) if next_us else None,
        'app_p50_us': _percentile(app_us, .50),
        'app_p95_us': _percentile(app_us, .95),
        'app_max_us': round(max(app_us), 3) if app_us else None,
        'rss_start_bytes': rss0, 'rss_end_bytes': rss1,
        'rss_peak_bytes': peak1 if peak1 is not None else peak0,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _run_one(path, mode, topics):
    cmd = [sys.executable, str(Path(__file__).resolve()), '--single',
           '--mcap', str(path), '--mode', mode,
           '--topics-json', json.dumps(topics, ensure_ascii=False)]
    p = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
    line = (p.stdout or '').strip().splitlines()
    if p.returncode != 0 or not line:
        raise RuntimeError('%s %s failed (%d): %s' %
                           (mode, path, p.returncode, p.stderr.strip()[-1000:]))
    try:
        result = json.loads(line[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError('invalid child output: %s\n%s' % (exc, p.stdout[-2000:]))
    if not result.get('ok'):
        raise RuntimeError('%s failed: %s' % (mode, result.get('error')))
    return result


def _crc_probe():
    """Verify the streaming reader and the app scan reject a bad chunk CRC.

    This uses a tiny generated fixture rather than copying the multi-GB input.
    """
    from tests import mcapfix as fx
    from mcap.reader import NonSeekingReader
    import mcap_reader as app_reader

    with tempfile.TemporaryDirectory(prefix='mcap-crc-probe-') as td:
        path = os.path.join(td, 'bad.mcap')
        inner = [fx.schema(1, 'foxglove.CompressedImage'),
                 fx.channel(1, 1, '/camera2/compressed'),
                 fx.message(1, 0, 1000, 1000, fx.compressed_image(b'x', 'h264'))]
        fx.assemble(path, [fx.header(),
                           fx.chunk(inner, crc_override=0xDEADBEEF)])
        direct_rejected = False
        try:
            with open(path, 'rb') as fh:
                list(NonSeekingReader(fh, validate_crcs=True).iter_messages(
                    log_time_order=False))
        except Exception:
            direct_rejected = True
        app_rejected = False
        try:
            app_reader.scan(path)
        except Exception:
            app_rejected = True
        return {'direct_nonseeking_rejected': direct_rejected,
                'app_scan_rejected': app_rejected,
                'ok': direct_rejected and app_rejected}


def _median(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return round(statistics.median(vals), 3) if vals else None


def audit(path, runs, warmup, topics, out, crc_probe=False):
    modes = ('R0', 'R1', 'R2')
    rows = {m: [] for m in modes}
    # Interleave modes to reduce a one-direction cold-cache bias.
    for i in range(warmup):
        for mode in modes:
            _run_one(path, mode, topics)
    for i in range(runs):
        for mode in modes:
            print('run %d/%d %s ...' % (i + 1, runs, mode), flush=True)
            rows[mode].append(_run_one(path, mode, topics))

    med = {}
    for mode, vals in rows.items():
        med[mode] = {
            'wall_ms': _median(vals, 'wall_ms'),
            'reader_next_p50_us': _median(vals, 'reader_next_p50_us'),
            'reader_next_p95_us': _median(vals, 'reader_next_p95_us'),
            'reader_next_max_us': _median(vals, 'reader_next_max_us'),
            'app_p95_us': _median(vals, 'app_p95_us'),
            'rss_peak_bytes': _median(vals, 'rss_peak_bytes'),
            'raw_count': _median(vals, 'raw_count'),
            'selected_count': _median(vals, 'selected_count'),
            'runs': len(vals),
        }

    ref = rows['R0'][0]
    consistency = []
    for mode in modes:
        for row in rows[mode]:
            consistency.append({
                'mode': mode, 'selected_count_equal':
                row['selected_count'] == ref['selected_count'],
                'fingerprint_equal': row['fingerprint'] == ref['fingerprint'],
                'by_topic_equal': row['by_topic'] == ref['by_topic'],
            })
    same = all(x['selected_count_equal'] and x['fingerprint_equal']
               and x['by_topic_equal'] for x in consistency)

    r0 = med['R0']['wall_ms'] or 0
    r1 = med['R1']['wall_ms'] or 0
    speedup = ((r0 - r1) / r0 * 100.0) if r0 else None
    if same and speedup is not None and speedup >= 20.0:
        verdict = 'GO-candidate'
    elif same and speedup is not None and speedup >= 5.0:
        verdict = 'HOLD'
    elif same:
        verdict = 'STOP-no-material-gain'
    else:
        verdict = 'STOP-correctness'

    result = {
        'audit': 'P1.6A', 'source': str(path), 'source_bytes': os.path.getsize(path),
        'topics': topics, 'runs': runs, 'warmup': warmup,
        'medians': med, 'speedup_r1_vs_r0_pct': round(speedup, 2)
        if speedup is not None else None,
        'consistency': consistency, 'correctness_ok': same,
        'verdict': verdict,
        'raw': rows,
        'note': '审计不会修改生产默认读取器；GO-candidate 仍需人工复核后才可切换。',
    }
    if crc_probe:
        result['crc_probe'] = _crc_probe()
        if not result['crc_probe']['ok']:
            result['correctness_ok'] = False
            result['verdict'] = 'STOP-crc'
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if out:
        Path(out).write_text(text, encoding='utf-8')
    print('\nP1.6A 结果：%s  R1 相对 R0：%s%%  正确性：%s' %
          (verdict, result['speedup_r1_vs_r0_pct'], 'PASS' if same else 'FAIL'))
    print('R0 %.1f ms / R1 %.1f ms / R2 %.1f ms' %
          (med['R0']['wall_ms'], med['R1']['wall_ms'], med['R2']['wall_ms']))
    if out:
        print('报告已写入', out)
    return 0 if same else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mcap', required=True)
    ap.add_argument('--runs', type=int, default=3)
    ap.add_argument('--warmup', type=int, default=1)
    ap.add_argument('--topic', action='append', dest='topics',
                    help='重复指定 topic；默认选择 camera2/camera3')
    ap.add_argument('--out')
    ap.add_argument('--crc-probe', action='store_true',
                    help='额外生成一个极小坏 CRC fixture 验证拒绝行为')
    ap.add_argument('--single', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--mode', choices=('R0', 'R1', 'R2'), help=argparse.SUPPRESS)
    ap.add_argument('--topics-json', help=argparse.SUPPRESS)
    a = ap.parse_args()
    path = os.path.abspath(a.mcap)
    if not os.path.isfile(path):
        ap.error('文件不存在：%s' % path)
    if a.single:
        if not a.mode or a.topics_json is None:
            ap.error('--single 需要 --mode 与 --topics-json')
        return _single(path, a.mode, json.loads(a.topics_json))
    all_topics = _summary_topics(path)
    topics = a.topics or _default_topics(path, all_topics)
    if not topics:
        ap.error('无法从 summary 找到 topic；请用 --topic 指定')
    return audit(path, max(1, a.runs), max(0, a.warmup), topics, a.out,
                 crc_probe=a.crc_probe)


if __name__ == '__main__':
    sys.exit(main())
