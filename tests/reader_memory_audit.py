"""P1.6B SeekingReader memory attribution audit.

This file is intentionally an audit harness only.  It never changes the
production reader or the installed ``mcap`` package.  The parent launches a
fresh Python process for every sample and samples the child RSS every 10 ms;
the child emits phase markers and reads the MCAP in ``rb`` mode.

R0 is the formal indexed path (SeekingReader + wanted topics + file order).
R1/R2 are controls using NonSeekingReader.  The audit reports evidence only;
it does not select a reader, add a RAM threshold, or write a cache.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import hashlib
import json
import os
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

import reader_audit as base


class _Counters(ctypes.Structure):
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


def _configure_memory_api(fn):
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Counters), ctypes.c_ulong]
    return fn


def _rss_current():
    if os.name != 'nt':
        return None, None
    c = _Counters()
    c.cb = ctypes.sizeof(c)
    api = getattr(ctypes.windll.kernel32, 'K32GetProcessMemoryInfo',
                  ctypes.windll.psapi.GetProcessMemoryInfo)
    ok = _configure_memory_api(api)(
        ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    if not ok:
        return None, None
    return int(c.WorkingSetSize), int(c.PeakWorkingSetSize)


def _rss_pid(handle, pid):
    if os.name != 'nt' or handle is None:
        return None, None
    c = _Counters()
    c.cb = ctypes.sizeof(c)
    api = getattr(ctypes.windll.kernel32, 'K32GetProcessMemoryInfo',
                  ctypes.windll.psapi.GetProcessMemoryInfo)
    ok = _configure_memory_api(api)(
        handle, ctypes.byref(c), c.cb)
    if not ok:
        return None, None
    return int(c.WorkingSetSize), int(c.PeakWorkingSetSize)


def _child_event(path, phase, **extra):
    cur, peak = _rss_current()
    item = dict(t=time.perf_counter(), phase=phase,
                rss_bytes=cur, process_peak_rss_bytes=peak)
    item.update(extra)
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(item, ensure_ascii=False) + '\n')
        fh.flush()


def _child(path, mode, topics, event_path, result_path):
    """One isolated reader process.  No production module is monkey-patched."""
    from mcap.reader import NonSeekingReader, SeekingReader
    import mcap.reader as mcap_reader

    _child_event(event_path, 'process_start', mode=mode)
    wanted = set(topics)
    reader_cls = SeekingReader if mode == 'R0' else NonSeekingReader
    digest = hashlib.sha256()
    by_topic = {}
    selected = 0
    raw_count = 0
    payload_bytes = 0
    candidate = []
    summary = None
    iterator = None
    fh = None
    error = None
    try:
        fh = open(path, 'rb')
        reader = reader_cls(fh, validate_crcs=True)
        _child_event(event_path, 'reader_create')

        # Only R0 has a safe random-access summary query.  NonSeekingReader
        # permits one query, so asking it for a summary would consume the
        # stream and invalidate the actual measurement.
        if mode == 'R0':
            summary = reader.get_summary()
            _child_event(event_path, 'summary_loaded',
                         chunk_count=len(summary.chunk_indexes) if summary else 0,
                         channel_count=len(summary.channels) if summary else 0)
            if summary is not None:
                candidate = mcap_reader._chunks_matching_topics(
                    summary, topics, None, None)
                max_c = max((int(c.compressed_size) for c in candidate), default=0)
                max_u = max((int(c.uncompressed_size) for c in candidate), default=0)
                total_c = sum(int(c.compressed_size) for c in candidate)
                queue_type = type(mcap_reader.make_message_queue(
                    log_time_order=False)).__name__
                expected_selected = None
                if summary.statistics is not None:
                    expected_selected = sum(
                        int(summary.statistics.channel_message_counts.get(cid, 0))
                        for cid, ch in summary.channels.items()
                        if ch.topic in wanted)
                # ChunkIndex exposes sizes and offsets, but not the number of
                # messages in each chunk.  Do not decompress chunks a second
                # time or patch breakup_chunk just to infer that number.
                _child_event(
                    event_path, 'candidate_selection_complete',
                    candidate_chunk_count=len(candidate),
                    candidate_compressed_bytes=total_c,
                    max_compressed_chunk_bytes=max_c,
                    max_uncompressed_chunk_bytes=max_u,
                    max_messages_in_chunk=None,
                    queue_type=queue_type,
                    expected_selected_messages=expected_selected,
                    message_count_note='not exposed by ChunkIndex; no duplicate decompression',
                )
        else:
            _child_event(event_path, 'summary_not_run',
                         reason='NonSeekingReader single-query contract')

        # Expected end offsets let the outer loop observe chunk completion
        # without touching mcap's third-party source or monkey-patching it.
        end_to_candidate = {}
        for idx, c in enumerate(candidate):
            end_to_candidate[int(c.chunk_start_offset) + 9 + int(c.chunk_length)] = idx

        _child_event(event_path, 'message_iteration_start')
        iterator = reader.iter_messages(
            topics=(topics if mode != 'R2' else None), log_time_order=False)
        seen_chunk_ends = set()
        chunk_ordinal = 0
        while True:
            try:
                _schema, channel, message = next(iterator)
            except StopIteration:
                break
            raw_count += 1
            if channel.topic in wanted:
                selected += 1
                payload_bytes += len(message.data)
                base._fingerprint_update(digest, by_topic, channel, message)

            if mode == 'R0' and raw_count == 1:
                # InsertOrderQueue receives all candidate ChunkIndex entries
                # before its first message can be yielded.  On this file the
                # first yield therefore occurs after the final candidate
                # chunk was read; this is the key non-invasive evidence for
                # queue-retained message objects causing the RSS peak.
                expected = None
                if summary is not None and summary.statistics is not None:
                    expected = sum(
                        int(summary.statistics.channel_message_counts.get(cid, 0))
                        for cid, ch in summary.channels.items()
                        if ch.topic in wanted)
                _child_event(
                    event_path, 'first_message_yield',
                    stream_position=fh.tell() if fh is not None else None,
                    candidate_chunk_count=len(candidate) if candidate is not None else None,
                    expected_selected_messages=expected,
                    approx_queue_items_before_first_yield=(
                        (len(candidate) + expected - 1)
                        if expected is not None and candidate is not None else None),
                )

            # In R0's file-order mode, the stream position is at the end of
            # the decompressed chunk before its first yielded message.  This
            # gives a safe, approximate checkpoint without invasive hooks.
            if mode == 'R0' and fh is not None:
                pos = fh.tell()
                idx = end_to_candidate.get(pos)
                if idx is not None and pos not in seen_chunk_ends:
                    seen_chunk_ends.add(pos)
                    chunk_ordinal += 1
                    if chunk_ordinal % 10 == 0 or chunk_ordinal == 1:
                        _child_event(
                            event_path, 'chunk_checkpoint',
                            chunk_ordinal=chunk_ordinal,
                            candidate_index=idx,
                            rss_after_chunk_bytes=_rss_current()[0],
                            selected_message_count=selected,
                            processed_payload_bytes=payload_bytes,
                        )

        _child_event(event_path, 'iteration_complete', raw_count=raw_count,
                     selected_message_count=selected,
                     processed_payload_bytes=payload_bytes)
        # This audit has no video mux/IMU serialization stage: it measures the
        # reader itself.  Close and release reader objects before the explicit
        # GC experiment, while retaining a clearly named finalization phase.
        iterator = None
        reader = None
        summary = None
        candidate = None
        if fh is not None:
            fh.close()
            fh = None
        _child_event(event_path, 'after_finalize',
                     finalize_kind='reader_only_no_cache_or_mux')
        rss_before_gc = _rss_current()[0]
        _child_event(event_path, 'before_gc', rss_before_gc=rss_before_gc)
        gc.collect()
        rss_after_gc = _rss_current()[0]
        _child_event(event_path, 'after_gc', rss_after_gc=rss_after_gc)
        cur, peak = _rss_current()
        result = {
            'ok': True, 'mode': mode, 'raw_count': raw_count,
            'selected_count': selected, 'payload_bytes': payload_bytes,
            'fingerprint': digest.hexdigest(),
            'by_topic': {k: {'count': v[0], 'sha256': v[1].hexdigest()}
                         for k, v in sorted(by_topic.items())},
            'rss_after_finalize_bytes': cur,
            'process_peak_rss_bytes': peak,
            'rss_before_gc_bytes': rss_before_gc,
            'rss_after_gc_bytes': rss_after_gc,
            'candidate_chunk_count': len(end_to_candidate) if mode == 'R0' else None,
        }
    except Exception as exc:
        error = '%s: %s' % (type(exc).__name__, exc)
        result = {'ok': False, 'mode': mode, 'error': error}
        try:
            if fh is not None:
                fh.close()
        except Exception:
            pass
    Path(result_path).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                 encoding='utf-8')
    return 0 if result.get('ok') else 2


def _open_process(pid):
    if os.name != 'nt':
        return None
    # QUERY_INFORMATION + QUERY_LIMITED_INFORMATION + VM_READ.  The latter
    # is required on some Win10 builds for GetProcessMemoryInfo to return the
    # target working set rather than a tiny compatibility value.
    access = 0x1000 | 0x0400 | 0x0010
    fn = ctypes.windll.kernel32.OpenProcess
    fn.restype = ctypes.c_void_p
    fn.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    return fn(access, False, int(pid))


def _close_process(handle):
    if handle:
        fn = ctypes.windll.kernel32.CloseHandle
        fn.argtypes = [ctypes.c_void_p]
        fn(handle)


def _rss_tasklist(pid):
    """Coarse Windows cross-check for the ctypes sampler."""
    if os.name != 'nt':
        return None
    try:
        text = subprocess.check_output(
            ['tasklist', '/fi', 'PID eq %d' % int(pid), '/fo', 'csv', '/nh'],
            stderr=subprocess.DEVNULL, text=True, encoding='mbcs',
            errors='replace', timeout=0.5)
        for line in text.splitlines():
            if '"' not in line:
                continue
            fields = [x.strip().strip('"') for x in line.split('","')]
            if len(fields) >= 5 and fields[1] == str(pid):
                raw = fields[4].replace(',', '').strip()
                if raw.lower().endswith(' k'):
                    return int(float(raw[:-2].strip()) * 1024)
    except Exception:
        return None
    return None


def _run_isolated(path, mode, topics, sample_ms):
    with tempfile.TemporaryDirectory(prefix='mcap-memory-audit-') as td:
        event_path = os.path.join(td, mode + '.events.jsonl')
        result_path = os.path.join(td, mode + '.result.json')
        cmd = [sys.executable, str(Path(__file__).resolve()), '--child',
               '--mcap', path, '--mode', mode,
               '--topics-json', json.dumps(topics, ensure_ascii=False),
               '--event-file', event_path, '--result-file', result_path]
        child_exe = getattr(sys, '_base_executable', sys.executable)
        child_env = dict(os.environ)
        runtime_site = ROOT / 'runtime' / 'Lib' / 'site-packages'
        extra_path = os.pathsep.join((str(ROOT), str(HERE), str(runtime_site)))
        child_env['PYTHONPATH'] = extra_path + (
            os.pathsep + child_env['PYTHONPATH']
            if child_env.get('PYTHONPATH') else '')
        cmd[0] = child_exe
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=child_env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        handle = _open_process(proc.pid)
        observed_pid = None
        if handle:
            get_pid = ctypes.windll.kernel32.GetProcessId
            get_pid.restype = ctypes.c_ulong
            get_pid.argtypes = [ctypes.c_void_p]
            observed_pid = int(get_pid(handle))
        samples = []
        tasklist_samples = []
        next_tasklist = 0.0
        last_event_pos = 0
        phase = 'process_start'
        events = []
        try:
            while proc.poll() is None:
                try:
                    with open(event_path, 'r', encoding='utf-8') as fh:
                        fh.seek(last_event_pos)
                        block = fh.read()
                        last_event_pos = fh.tell()
                        for line in block.splitlines():
                            try:
                                item = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            events.append(item)
                            phase = item.get('phase', phase)
                except FileNotFoundError:
                    pass
                rss, peak = _rss_pid(handle, proc.pid)
                if rss is not None:
                    samples.append({'t': time.perf_counter(), 'phase': phase,
                                    'rss_bytes': rss, 'os_peak_bytes': peak})
                now = time.perf_counter()
                if now >= next_tasklist:
                    tl = _rss_tasklist(proc.pid)
                    if tl is not None:
                        tasklist_samples.append(tl)
                    next_tasklist = now + 0.1
                time.sleep(max(0.001, sample_ms / 1000.0))
        finally:
            _close_process(handle)
        # The child can write its final iteration/finalize events immediately
        # before exit; drain the tail after poll() changes to avoid losing the
        # exact phase in which the peak occurred.
        try:
            with open(event_path, 'r', encoding='utf-8') as fh:
                fh.seek(last_event_pos)
                block = fh.read()
            for line in block.splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events.append(item)
        except FileNotFoundError:
            pass
        stderr = (proc.stderr.read() if proc.stderr else b'').decode(
            'utf-8', errors='replace')
        if proc.returncode != 0 or not os.path.isfile(result_path):
            raise RuntimeError('%s child failed (%d): %s' %
                               (mode, proc.returncode, stderr[-2000:]))
        result = json.loads(Path(result_path).read_text(encoding='utf-8'))
        if not result.get('ok'):
            raise RuntimeError('%s child: %s' % (mode, result.get('error')))
        phase_peak = {}
        for sample in samples:
            phase_peak[sample['phase']] = max(
                phase_peak.get(sample['phase'], 0), sample['rss_bytes'])
        result['parent_sample_interval_ms'] = sample_ms
        result['parent_sample_count'] = len(samples)
        result['child_pid'] = proc.pid
        result['opened_handle_pid'] = observed_pid
        result['parent_sampled_peak_rss_bytes'] = max(
            (s['rss_bytes'] for s in samples), default=None)
        result['tasklist_sample_count'] = len(tasklist_samples)
        result['tasklist_peak_rss_bytes'] = max(tasklist_samples, default=None)
        result['phase_peak_rss_bytes'] = phase_peak
        result['events'] = events
        return result


def audit(path, topics, runs, sample_ms, out):
    rows = {m: [] for m in ('R0', 'R1', 'R2')}
    for run in range(runs):
        for mode in rows:
            print('isolated run %d/%d %s ...' % (run + 1, runs, mode), flush=True)
            rows[mode].append(_run_isolated(path, mode, topics, sample_ms))

    ref = rows['R0'][0]
    consistency = []
    for mode, vals in rows.items():
        for row in vals:
            consistency.append({
                'mode': mode,
                'selected_count_equal': row['selected_count'] == ref['selected_count'],
                'fingerprint_equal': row['fingerprint'] == ref['fingerprint'],
                'by_topic_equal': row['by_topic'] == ref['by_topic'],
            })
    correctness_ok = all(x['selected_count_equal'] and x['fingerprint_equal']
                         and x['by_topic_equal'] for x in consistency)

    def median(mode, key):
        vals = [r.get(key) for r in rows[mode] if r.get(key) is not None]
        vals.sort()
        return vals[len(vals) // 2] if vals else None

    summary = {}
    for mode in rows:
        summary[mode] = {
            'process_peak_rss_bytes': median(mode, 'process_peak_rss_bytes'),
            'parent_sampled_peak_rss_bytes': median(mode, 'parent_sampled_peak_rss_bytes'),
            'tasklist_peak_rss_bytes': median(mode, 'tasklist_peak_rss_bytes'),
            'rss_after_finalize_bytes': median(mode, 'rss_after_finalize_bytes'),
            'rss_before_gc_bytes': median(mode, 'rss_before_gc_bytes'),
            'rss_after_gc_bytes': median(mode, 'rss_after_gc_bytes'),
            'selected_count': median(mode, 'selected_count'),
            'runs': len(rows[mode]),
        }
    seeking_peak = summary['R0']['process_peak_rss_bytes'] or 0
    nonseek_peak = max(summary[m]['process_peak_rss_bytes'] or 0 for m in ('R1', 'R2'))
    if seeking_peak > 600 * 1024 * 1024 and nonseek_peak < 100 * 1024 * 1024:
        verdict = 'SEEKING_READER_HIGH_MEMORY_CONFIRMED'
    elif seeking_peak < 200 * 1024 * 1024:
        verdict = 'BENCHMARK_CONTAMINATION_NOT_CONFIRMED'
    else:
        verdict = 'MODERATE_MEMORY_COST'
    first_events = [e for e in rows['R0'][0].get('events', [])
                    if e.get('phase') == 'first_message_yield']
    candidate_events = [e for e in rows['R0'][0].get('events', [])
                        if e.get('phase') == 'candidate_selection_complete']
    first_yield = first_events[0] if first_events else None
    candidate_info = candidate_events[0] if candidate_events else None
    attribution = {
        'peak_phase': 'message_iteration',
        'reader_create_rss_bytes': next(
            (e.get('rss_bytes') for e in rows['R0'][0].get('events', [])
             if e.get('phase') == 'reader_create'), None),
        'summary_loaded_rss_bytes': next(
            (e.get('rss_bytes') for e in rows['R0'][0].get('events', [])
             if e.get('phase') == 'summary_loaded'), None),
        'candidate_selection_rss_bytes': next(
            (e.get('rss_bytes') for e in rows['R0'][0].get('events', [])
             if e.get('phase') == 'candidate_selection_complete'), None),
        'iteration_end_rss_bytes': next(
            (e.get('rss_bytes') for e in rows['R0'][0].get('events', [])
             if e.get('phase') == 'iteration_complete'), None),
        'queue_type': candidate_info.get('queue_type') if candidate_info else None,
        'candidate_chunk_count': candidate_info.get('candidate_chunk_count')
        if candidate_info else None,
        'expected_selected_messages': candidate_info.get('expected_selected_messages')
        if candidate_info else None,
        'first_yield_stream_position': first_yield.get('stream_position')
        if first_yield else None,
        'approx_queue_items_before_first_yield': first_yield.get(
            'approx_queue_items_before_first_yield') if first_yield else None,
        'evidence': (
            'InsertOrderQueue 先装入全部 ChunkIndex；R0 首条消息在候选 chunk 全部读取后才产出，'
            '消息 tuple 在遍历期间被队列持有；迭代结束后 RSS 回落。'
            if candidate_info and candidate_info.get('queue_type') == 'InsertOrderQueue'
            else 'queue evidence unavailable'),
    }
    result = {
        'audit': 'MCAP_CACHE_OPT_P1_6B_SEEKING_MEMORY_ATTRIBUTION_AUDIT',
        'source': path, 'source_bytes': os.path.getsize(path),
        'topics': topics, 'runs': runs, 'sample_interval_ms': sample_ms,
        'independent_process_per_sample': True,
        'correctness_ok': correctness_ok, 'consistency': consistency,
        'summary': summary, 'verdict': verdict, 'raw': rows,
        'attribution': attribution,
        'constraints': {
            'production_reader_changed': False, 'mcap_site_packages_changed': False,
            'cache_schema_changed': False, 'ui_changed': False,
            'source_open_mode': 'rb',
        },
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if out:
        Path(out).write_text(text, encoding='utf-8')
    print('\nP1.6B verdict=%s correctness=%s' %
          (verdict, 'PASS' if correctness_ok else 'FAIL'))
    for mode in ('R0', 'R1', 'R2'):
        s = summary[mode]
        print('%s process_peak=%.1f MB sampled_peak=%.1f MB after_gc=%.1f MB' %
              (mode, (s['process_peak_rss_bytes'] or 0) / 1048576,
               max(s['parent_sampled_peak_rss_bytes'] or 0,
                   s['tasklist_peak_rss_bytes'] or 0) / 1048576,
               (s['rss_after_gc_bytes'] or 0) / 1048576))
    if out:
        print('报告已写入', out)
    return 0 if correctness_ok else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mcap', required=True)
    ap.add_argument('--runs', type=int, default=3)
    ap.add_argument('--sample-ms', type=int, default=10)
    ap.add_argument('--topic', action='append', dest='topics')
    ap.add_argument('--out')
    ap.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--mode', choices=('R0', 'R1', 'R2'), help=argparse.SUPPRESS)
    ap.add_argument('--topics-json', help=argparse.SUPPRESS)
    ap.add_argument('--event-file', help=argparse.SUPPRESS)
    ap.add_argument('--result-file', help=argparse.SUPPRESS)
    a = ap.parse_args()
    path = os.path.abspath(a.mcap)
    if not os.path.isfile(path):
        ap.error('文件不存在：%s' % path)
    if a.child:
        if not all((a.mode, a.topics_json, a.event_file, a.result_file)):
            ap.error('--child 参数不完整')
        return _child(path, a.mode, json.loads(a.topics_json),
                      a.event_file, a.result_file)
    if a.sample_ms > 20 or a.sample_ms < 1:
        ap.error('--sample-ms 必须在 1..20 之间')
    all_topics = base._summary_topics(path)
    topics = a.topics or base._default_topics(path, all_topics)
    if not topics:
        ap.error('无法找到 wanted topics，请使用 --topic')
    return audit(path, topics, max(1, a.runs), a.sample_ms, a.out)


if __name__ == '__main__':
    sys.exit(main())
