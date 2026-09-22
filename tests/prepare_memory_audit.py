"""P1.6D-R1 full-prepare memory attribution audit.

Audit-only tool.  Each sample runs a fresh base-Python child, reads the MCAP
read-only, and samples the child RSS from the parent every 10 ms.  The child
wraps only test-process symbols to emit lifecycle checkpoints; no production
file is changed and no audit fields enter manifest.json.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import reader_memory_audit as mem


def _event(path, phase, **extra):
    cur, peak = mem._rss_current()
    item = dict(t=time.perf_counter(), phase=phase, python_rss_bytes=cur,
                python_peak_rss_bytes=peak, ffmpeg_child_rss_bytes=0,
                process_tree_rss_bytes=cur, **extra)
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(item, ensure_ascii=False) + '\n')
        fh.flush()


def _make_tracking_reader(event_path):
    """Wrap McapReader only in the child to count live message categories."""
    import mcap_reader as MR
    Original = MR.McapReader

    class TrackingReader:
        def __init__(self, *args, **kwargs):
            _event(event_path, 'before_reader_create')
            self._inner = Original(*args, **kwargs)
            self.__dict__.update({k: getattr(self._inner, k)
                                  for k in ('path', 'size', 'channels', 'schemas',
                                            'stats', 'has_index', 'lazy')})
            _event(event_path, 'after_reader_create')

        def summary(self, *args, **kwargs):
            out = self._inner.summary(*args, **kwargs)
            self.__dict__.update(channels=self._inner.channels,
                                 schemas=self._inner.schemas,
                                 stats=self._inner.stats)
            _event(event_path, 'after_summary',
                   summary_message_count=out.get('message_count'),
                   summary_chunk_count=out.get('chunk_count'),
                   summary_has_index=out.get('has_index'))
            return out

        def iter_messages(self, wanted=None, log_time_order=True,
                          collect=False, pushdown=True):
            total = int((self._inner.stats or {}).get('message_count') or 0)
            if wanted is not None:
                counts_by_channel = (self._inner.stats or {}).get(
                    'channel_message_counts') or {}
                total = sum(int(counts_by_channel.get(cid, 0))
                            for cid in wanted)
            counts = dict(video=0, imu=0, audio=0, metadata=0, other=0)
            payload = dict(video=0, imu=0, audio=0, metadata=0, other=0)
            seen = 0
            first = True
            marks = set()
            # Prepare's wanted IDs are already selected; classification uses
            # the reader's channel/schema tables and mirrors production kinds.
            import mcap_reader as M
            for item in self._inner.iter_messages(
                    wanted, log_time_order=log_time_order,
                    collect=collect, pushdown=pushdown):
                cid, log_time, pub, seq, data = item
                seen += 1
                ch = self._inner.channels.get(cid) or {}
                sch = self._inner.schemas.get(ch.get('schema_id')) or {}
                kind = M.classify(ch.get('topic', ''), sch.get('name', ''))
                key = kind if kind in counts else 'other'
                counts[key] += 1
                payload[key] += len(data or b'')
                frac = seen / float(total or 1)
                for q, name in ((.25, 'iteration_25_percent'),
                                (.50, 'iteration_50_percent'),
                                (.75, 'iteration_75_percent')):
                    if frac >= q and name not in marks:
                        marks.add(name)
                        _event(event_path, name, message_processed_count=seen,
                               message_total=total, **counts,
                               video_payload_bytes=payload['video'],
                               imu_payload_bytes=payload['imu'],
                               audio_payload_bytes=payload['audio'])
                if first:
                    first = False
                    _event(event_path, 'first_selected_message',
                           message_processed_count=seen, **counts)
                yield item
            _event(event_path, 'iteration_100_percent',
                   message_processed_count=seen, **counts,
                   video_payload_bytes=payload['video'],
                   imu_payload_bytes=payload['imu'],
                   audio_payload_bytes=payload['audio'])

    return TrackingReader


def _wrap_stage_functions(event_path):
    """Wrap finalize/publish functions in the child process only."""
    import prepare as PREP
    original_cameras = PREP._finish_cameras
    original_imu = PREP._finish_imu
    original_audio = PREP._finish_audio
    original_write = PREP.appcache.write_json_atomic
    original_publish = PREP._publish

    def cameras(*args, **kwargs):
        cams = args[1] if len(args) > 1 else {}
        cam_meta = {}
        for cid, st in cams.items():
            cam_meta[str(cid)] = {
                'index_length': len(st.get('index') or []),
                'times_length': len(st.get('index') or []) + len(st.get('img_pending') or []),
                'payload_retained': bool(st.get('img_pending')),
            }
        _event(event_path, 'before_video_finalize', camera_accumulators=cam_meta)
        out = original_cameras(*args, **kwargs)
        _event(event_path, 'after_video_finalize')
        return out

    def imu(*args, **kwargs):
        import sys as _sys
        _event(event_path, 'before_imu_finalize',
               imu_accumulator_type='array.array columnar (7 arrays)',
               imu_accumulator_length=sum(len(c.get('samples', {}).get('timestamps', []))
                                          for c in (args[1] if len(args) > 1 else [])),
               imu_estimated_memory_bytes=sum(
                   int(len(c.get('samples', {}).get('timestamps', [])) * 8 * 7)
                   for c in (args[1] if len(args) > 1 else [])))
        out = original_imu(*args, **kwargs)
        _event(event_path, 'after_imu_finalize')
        return out

    def audio(*args, **kwargs):
        import sys as _sys
        chans = args[1] if len(args) > 1 else []
        packets = sum(len(c.get('chunks', [])) for c in chans)
        bytes_total = sum(int(c.get('payload_bytes') or 0) for c in chans)
        _event(event_path, 'before_audio_finalize',
               audio_accumulator_type='list[tuple[timestamp, dict]]',
               audio_packet_count=packets, audio_payload_bytes=bytes_total,
               audio_estimated_memory_bytes=_estimate_samples(
                   [x for c in chans for x in c.get('chunks', [])]))
        out = original_audio(*args, **kwargs)
        _event(event_path, 'after_audio_finalize',
               audio_packet_count=packets, audio_payload_bytes=bytes_total)
        return out

    def write(path, obj):
        _event(event_path, 'before_manifest')
        out = original_write(path, obj)
        _event(event_path, 'after_manifest')
        return out

    def publish(stage, outdir):
        out = original_publish(stage, outdir)
        _event(event_path, 'after_publish')
        return out

    PREP._finish_cameras = cameras
    PREP._finish_imu = imu
    PREP._finish_audio = audio
    PREP.appcache.write_json_atomic = write
    PREP._publish = publish


def _estimate_samples(items, limit=500):
    """Shallow + one-level sampled estimate; never walks the full accumulator."""
    import sys
    n = len(items)
    if not n:
        return 0
    sample = items[:min(limit, n)]
    total = 0
    for item in sample:
        total += sys.getsizeof(item)
        if isinstance(item, tuple):
            for value in item:
                total += sys.getsizeof(value)
                if isinstance(value, dict):
                    total += sys.getsizeof(value)
                    total += sum(sys.getsizeof(v) for v in value.values())
    return int(total / len(sample) * n)


def _child(src, mode, event_path, result_path, cache_root):
    import appcache
    import mcap_reader as MR
    import prepare as PREP
    appcache.CACHE_ROOT = cache_root
    os.environ['MCAPVIEWER_READER_MODE'] = mode
    MR.McapReader = _make_tracking_reader(event_path)
    _wrap_stage_functions(event_path)
    _event(event_path, 'process_start', mode=mode)
    outdir = os.path.join(cache_root, 'prepare-out')
    try:
        man = PREP.prepare(
            src, outdir,
            progress=lambda frac, text: _event(
                event_path, 'progress', progress=frac, text=str(text)),
            camera_pred=lambda t: ('camera2' in (t or '') or
                                   'camera3' in (t or '')),
            profile=appcache.CACHE_PROFILE)
        _event(event_path, 'after_cleanup')
        before = mem._rss_current()[0]
        gc.collect()
        after = mem._rss_current()[0]
        _event(event_path, 'after_gc', rss_before_gc=before,
               rss_after_gc=after)
        # Semantic fingerprint is deliberately small and excludes volatile
        # cached_at/_dir/perf fields.
        semantic = {k: v for k, v in man.items()
                    if k not in ('_perf', '_dir')}
        Path(result_path).write_text(json.dumps({
            'ok': True, 'mode': mode, 'manifest': semantic,
            'perf': man.get('_perf') or {},
        }, ensure_ascii=False), encoding='utf-8')
        return 0
    except Exception as exc:
        _event(event_path, 'prepare_error', error='%s: %s' %
               (type(exc).__name__, exc))
        Path(result_path).write_text(json.dumps({
            'ok': False, 'error': '%s: %s' % (type(exc).__name__, exc)},
            ensure_ascii=False), encoding='utf-8')
        return 2


def _run_one(src, mode, sample_ms):
    with tempfile.TemporaryDirectory(prefix='prepare-mem-audit-') as td:
        event_path = os.path.join(td, 'events.jsonl')
        result_path = os.path.join(td, 'result.json')
        cache_root = os.path.join(td, 'cache')
        env = dict(os.environ)
        runtime_site = ROOT / 'runtime' / 'Lib' / 'site-packages'
        env['PYTHONPATH'] = os.pathsep.join(
            (str(ROOT), str(HERE), str(runtime_site), env.get('PYTHONPATH', '')))
        cmd = [getattr(sys, '_base_executable', sys.executable),
               str(Path(__file__).resolve()), '--child', '--mcap', src,
               '--mode', mode, '--event-file', event_path,
               '--result-file', result_path, '--cache-root', cache_root,
               '--out', result_path]
        p = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        h = mem._open_process(p.pid)
        samples = []
        try:
            while p.poll() is None:
                rss, peak = mem._rss_pid(h, p.pid)
                if rss is not None:
                    samples.append({'t': time.perf_counter(), 'rss': rss})
                time.sleep(max(0.001, sample_ms / 1000.0))
        finally:
            mem._close_process(h)
        # Drain child events after exit.
        events = []
        try:
            with open(event_path, encoding='utf-8') as fh:
                events = [json.loads(line) for line in fh if line.strip()]
        except FileNotFoundError:
            pass
        err = (p.stderr.read() if p.stderr else b'').decode('utf-8', errors='replace')
        if p.returncode != 0 or not os.path.isfile(result_path):
            raise RuntimeError('%s failed: %s' % (mode, err[-2000:]))
        result = json.loads(Path(result_path).read_text(encoding='utf-8'))
        result['events'] = events
        result['parent_sampled_peak_rss_bytes'] = max(
            (s['rss'] for s in samples), default=None)
        result['sample_count'] = len(samples)
        phase_peaks = {}
        ordered = sorted(events, key=lambda e: e.get('t', 0))
        for sample in samples:
            phase = 'process_start'
            for event in ordered:
                if event.get('phase') == 'progress':
                    continue
                if event.get('t', 0) <= sample['t']:
                    phase = event.get('phase', phase)
                else:
                    break
            phase_peaks[phase] = max(phase_peaks.get(phase, 0), sample['rss'])
        result['phase_sampled_peak_rss_bytes'] = phase_peaks
        result['source_size_bytes'] = os.path.getsize(src)
        return result


def _timeline(row):
    phases = {}
    for e in row.get('events', []):
        phase = e.get('phase')
        if phase == 'progress':
            continue
        old = phases.get(phase)
        if old is None or (e.get('python_rss_bytes') or 0) >= (old.get('python_rss_bytes') or 0):
            phases[phase] = e
    sampled = row.get('phase_sampled_peak_rss_bytes') or {}
    for phase, peak in sampled.items():
        phases.setdefault(phase, {})['sampled_peak_rss_bytes'] = peak
    return phases


def _accumulator(row):
    phases = _timeline(row)
    end = phases.get('iteration_100_percent', {})
    imu = phases.get('before_imu_finalize', {})
    audio = phases.get('before_audio_finalize', {})
    return {
        'camera2_index': (phases.get('before_video_finalize', {}).get(
            'camera_accumulators', {}).get('3')),
        'camera3_index': (phases.get('before_video_finalize', {}).get(
            'camera_accumulators', {}).get('4')),
        'video_message_count': end.get('video'),
        'video_payload_bytes_seen': end.get('video_payload_bytes'),
        'imu_item_count': imu.get('imu_accumulator_length'),
        'imu_accumulator_type': imu.get('imu_accumulator_type'),
        'imu_estimated_memory_bytes': imu.get('imu_estimated_memory_bytes'),
        'audio_packet_count': audio.get('audio_packet_count'),
        'audio_payload_bytes': audio.get('audio_payload_bytes'),
        'audio_accumulator_type': audio.get('audio_accumulator_type'),
        'audio_estimated_memory_bytes': audio.get('audio_estimated_memory_bytes'),
        'metadata_message_count': end.get('metadata'),
    }


def _run_scaling(src, mode, sample_ms):
    return _run_one(src, mode, sample_ms)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mcap', required=True)
    ap.add_argument('--mode', choices=('official', 'chunk_streaming'), default='chunk_streaming')
    ap.add_argument('--sample-ms', type=int, default=10)
    ap.add_argument('--out', required=True)
    ap.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--event-file', help=argparse.SUPPRESS)
    ap.add_argument('--result-file', help=argparse.SUPPRESS)
    ap.add_argument('--cache-root', help=argparse.SUPPRESS)
    a = ap.parse_args()
    path = os.path.abspath(a.mcap)
    if a.child:
        return _child(path, a.mode, a.event_file, a.result_file, a.cache_root)
    if not 1 <= a.sample_ms <= 20:
        ap.error('--sample-ms must be 1..20')
    row = _run_scaling(path, a.mode, a.sample_ms)
    row['timeline'] = _timeline(row)
    row['accumulators'] = _accumulator(row)
    Path(a.out).write_text(json.dumps(row, ensure_ascii=False, indent=2),
                           encoding='utf-8')
    tl = row['timeline']
    print(json.dumps({
        'mode': a.mode, 'source_mb': round(os.path.getsize(path) / 1048576, 1),
        'peak_python_mb': round((row.get('parent_sampled_peak_rss_bytes') or 0) / 1048576, 1),
        'after_gc_mb': round((tl.get('after_gc', {}).get('rss_after_gc') or 0) / 1048576, 1),
        'iteration_end_mb': round((tl.get('iteration_100_percent', {}).get('python_rss_bytes') or 0) / 1048576, 1),
        'imu_count': row['accumulators'].get('imu_item_count'),
        'audio_packets': row['accumulators'].get('audio_packet_count'),
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
