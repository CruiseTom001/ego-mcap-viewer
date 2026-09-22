"""P1.6C indexed chunk-streaming reader mechanism audit.

This is an experiment-only reader.  It is intentionally isolated under
tests/ and never replaces ``mcap_reader.py``.  C0 uses the installed
SeekingReader; C1 reads one candidate Chunk at a time with public MCAP record
primitives and yields selected messages immediately.  Every sample runs in a
fresh base-Python process and the parent samples its RSS every 10 ms.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
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
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import reader_audit as base
import reader_memory_audit as mem


def _candidate_chunks(summary, topics, start_time=None, end_time=None):
    """Small public-semantics clone of mcap 1.4 candidate selection."""
    wanted = None if topics is None else set(topics)
    out = []
    for ci in sorted(summary.chunk_indexes,
                     key=lambda x: int(x.chunk_start_offset)):
        if start_time is not None and ci.message_end_time < start_time:
            continue
        if end_time is not None and ci.message_start_time >= end_time:
            continue
        if wanted is None or not ci.message_index_offsets:
            out.append(ci)
            continue
        if any(summary.channels[cid].topic in wanted
               for cid in ci.message_index_offsets):
            out.append(ci)
    return out


class ChunkStreamingIndexedReader:
    """Audit-only reader; no global Message queue is retained."""

    def __init__(self, stream, validate_crcs=True):
        from mcap.reader import SeekingReader
        self._stream = stream
        self._validate_crcs = bool(validate_crcs)
        self._reader = SeekingReader(stream, validate_crcs=validate_crcs)
        self.summary = None
        self.candidate_chunks = []
        self.max_messages_in_chunk = 0
        self.max_compressed_chunk_bytes = 0
        self.max_uncompressed_chunk_bytes = 0
        self.processed_chunk_count = 0

    def get_summary(self):
        if self.summary is None:
            self.summary = self._reader.get_summary()
        return self.summary

    def iter_messages(self, topics=None, start_time=None, end_time=None):
        from mcap.reader import ReadDataStream
        from mcap.records import Chunk, Message
        from mcap.stream_reader import breakup_chunk

        summary = self.get_summary()
        self.candidate_chunks = _candidate_chunks(
            summary, topics, start_time, end_time)
        wanted = None if topics is None else set(topics)
        for ordinal, ci in enumerate(self.candidate_chunks, 1):
            self.max_compressed_chunk_bytes = max(
                self.max_compressed_chunk_bytes, int(ci.compressed_size))
            self.max_uncompressed_chunk_bytes = max(
                self.max_uncompressed_chunk_bytes, int(ci.uncompressed_size))
            self._stream.seek(int(ci.chunk_start_offset) + 1 + 8)
            chunk = Chunk.read(ReadDataStream(self._stream))
            records = breakup_chunk(chunk, validate_crc=self._validate_crcs)
            self.max_messages_in_chunk = max(self.max_messages_in_chunk,
                                             len(records))
            self.processed_chunk_count = ordinal
            for record in records:
                if not isinstance(record, Message):
                    continue
                channel = summary.channels.get(record.channel_id)
                if channel is None:
                    continue
                if wanted is not None and channel.topic not in wanted:
                    continue
                if start_time is not None and record.log_time < start_time:
                    continue
                if end_time is not None and record.log_time >= end_time:
                    continue
                schema = (None if channel.schema_id == 0 else
                          summary.schemas[channel.schema_id])
                yield schema, channel, record
            # Explicitly drop the current chunk's record list before reading
            # the next chunk.  No payload or Message escapes this generator.
            del records
            del chunk


class _PrepareStreamingAdapter:
    """Project-reader adapter used only by the C1 prepare artifact check."""

    _base_reader_cls = None

    def __init__(self, path, lazy=False):
        reader_cls = self._base_reader_cls
        if reader_cls is None:
            import mcap_reader as project_reader
            reader_cls = project_reader.McapReader
        self._base = reader_cls(path, lazy=lazy)
        self.path = self._base.path
        self.size = self._base.size
        self.channels = self._base.channels
        self.schemas = self._base.schemas
        self.has_index = self._base.has_index
        self.stats = self._base.stats
        self._iter_offset = 0

    def summary(self, recount='auto'):
        return self._base.summary(recount=recount)

    def iter_messages(self, wanted=None, log_time_order=True, collect=False,
                      pushdown=True):
        if log_time_order:
            # The experiment is only valid for the formal file-order cache
            # path.  Do not silently change semantics for another caller.
            raise ValueError('C1 adapter requires log_time_order=False')
        topics = None
        if wanted is not None:
            topics = [self.channels[c]['topic'] for c in wanted
                      if c in self.channels]
        with open(self.path, 'rb') as fh:
            stream_reader = ChunkStreamingIndexedReader(fh, validate_crcs=True)
            for schema, channel, message in stream_reader.iter_messages(topics):
                yield (channel.id, message.log_time, message.publish_time,
                       message.sequence, message.data)


def _fingerprint_update(digest, by_topic, channel, message):
    head = struct.pack('<IQQI', int(channel.id), int(message.log_time),
                       int(message.publish_time), int(message.sequence))
    blob = head + struct.pack('<Q', len(message.data)) + message.data
    digest.update(blob)
    item = by_topic.setdefault(channel.topic, [0, hashlib.sha256()])
    item[0] += 1
    item[1].update(blob)


def _event(path, phase, **extra):
    cur, peak = mem._rss_current()
    item = dict(t=time.perf_counter(), phase=phase, rss_bytes=cur,
                process_peak_rss_bytes=peak)
    item.update(extra)
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(item, ensure_ascii=False) + '\n')
        fh.flush()


def _prepare_child(path, mode, cache_root, result_path):
    """Run the unchanged prepare handlers with C0 or the audit adapter."""
    import appcache
    import prepare as PREP
    from tests.perf_bench import run_bench

    if mode == 'C1':
        _PrepareStreamingAdapter._base_reader_cls = PREP.MR.McapReader
        PREP.MR.McapReader = _PrepareStreamingAdapter
    result = run_bench(path, label='p1.6c-' + mode.lower(),
                       cache_root=cache_root, slim=True, keep_cache=True)
    manifest_path = os.path.join(result['perf']['cache_dir'], 'manifest.json')
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    # cached_at is intentionally volatile; every other manifest field is
    # compared by the parent, including source signature and artifact lists.
    manifest.get('builder', {}).pop('cached_at', None)
    result['manifest_semantic'] = manifest
    Path(result_path).write_text(json.dumps(result, ensure_ascii=False,
                                            indent=2), encoding='utf-8')
    return 0


def _child(path, mode, topics, event_path, result_path):
    from mcap.reader import SeekingReader

    digest = hashlib.sha256()
    by_topic = {}
    selected = raw_count = 0
    selected_payload_bytes = 0
    reader_next_total_ms = 0.0
    first_selected_ms = None
    reader = None
    stream = None
    summary = None
    error = None
    _event(event_path, 'process_start', mode=mode)
    reader_start = time.perf_counter()
    try:
        stream = open(path, 'rb')
        reader = (SeekingReader(stream, validate_crcs=True)
                  if mode == 'C0' else ChunkStreamingIndexedReader(
                      stream, validate_crcs=True))
        _event(event_path, 'reader_create')
        summary = reader.get_summary()
        _event(event_path, 'summary_loaded',
               chunk_count=len(summary.chunk_indexes),
               channel_count=len(summary.channels))
        candidate = _candidate_chunks(summary, topics)
        max_c = max((int(c.compressed_size) for c in candidate), default=0)
        max_u = max((int(c.uncompressed_size) for c in candidate), default=0)
        _event(event_path, 'candidate_selection_complete',
               candidate_chunk_count=len(candidate),
               max_compressed_chunk_bytes=max_c,
               max_uncompressed_chunk_bytes=max_u)
        _event(event_path, 'iteration_start')
        iteration_start = time.perf_counter()
        iterator = (reader.iter_messages(topics=topics, log_time_order=False)
                    if mode == 'C0' else reader.iter_messages(topics=topics))
        last_chunk_checkpoint = 0
        while True:
            t_next = time.perf_counter()
            try:
                _schema, channel, message = next(iterator)
            except StopIteration:
                break
            reader_next_total_ms += (time.perf_counter() - t_next) * 1000.0
            raw_count += 1
            if channel.topic in set(topics):
                selected += 1
                selected_payload_bytes += len(message.data)
                _fingerprint_update(digest, by_topic, channel, message)
                if first_selected_ms is None:
                    first_selected_ms = (time.perf_counter() - reader_start) * 1000.0
                    _event(event_path, 'first_selected_message',
                           time_to_first_selected_ms=first_selected_ms)
            if mode == 'C1' and reader.processed_chunk_count >= last_chunk_checkpoint + 100:
                last_chunk_checkpoint = (reader.processed_chunk_count // 100) * 100
                _event(event_path, 'chunk_checkpoint',
                       processed_chunk_count=reader.processed_chunk_count,
                       selected_message_count=selected,
                       selected_payload_bytes=selected_payload_bytes)
        iteration_ms = (time.perf_counter() - iteration_start) * 1000.0
        _event(event_path, 'iteration_complete', raw_count=raw_count,
               selected_message_count=selected,
               selected_payload_bytes=selected_payload_bytes,
               reader_iteration_ms=iteration_ms)
        c1_max_messages = (reader.max_messages_in_chunk
                           if mode == 'C1' else None)
        c1_processed_chunks = (reader.processed_chunk_count
                               if mode == 'C1' else None)
        iterator = None
        _event(event_path, 'after_iteration')
        if stream is not None:
            stream.close()
            stream = None
        reader = None
        summary = None
        rss_after_reader = mem._rss_current()[0]
        _event(event_path, 'after_reader', rss_after_reader=rss_after_reader)
        rss_before_gc = mem._rss_current()[0]
        gc.collect()
        rss_after_gc = mem._rss_current()[0]
        _event(event_path, 'after_gc', rss_before_gc=rss_before_gc,
               rss_after_gc=rss_after_gc)
        cur, peak = mem._rss_current()
        if mode == 'C1':
            metrics = {
                'processed_chunk_count': reader.processed_chunk_count
                if reader is not None else None,
                'max_messages_in_chunk': reader.max_messages_in_chunk
                if reader is not None else None,
            }
        else:
            metrics = {'processed_chunk_count': len(candidate),
                       'processed_chunk_count_note': 'SeekingReader inferred all candidates processed'}
        result = {
            'ok': True, 'mode': mode, 'selected_count': selected,
            'raw_count': raw_count, 'selected_payload_bytes': selected_payload_bytes,
            'fingerprint': digest.hexdigest(),
            'by_topic': {k: {'count': v[0], 'sha256': v[1].hexdigest()}
                         for k, v in sorted(by_topic.items())},
            'total_cache_ms': (time.perf_counter() - reader_start) * 1000.0,
            'reader_iteration_ms': iteration_ms,
            'reader_next_total_ms': reader_next_total_ms,
            'time_to_first_selected_message_ms': first_selected_ms,
            'rss_after_reader_bytes': rss_after_reader,
            'rss_before_gc_bytes': rss_before_gc,
            'rss_after_gc_bytes': rss_after_gc,
            'process_peak_rss_bytes': peak,
            'candidate_chunk_count': len(candidate),
            'processed_chunk_count': (c1_processed_chunks
                                      if mode == 'C1' else len(candidate)),
            'max_messages_in_chunk': c1_max_messages,
            'max_compressed_chunk_bytes': max_c,
            'max_uncompressed_chunk_bytes': max_u,
        }
        if mode == 'C1':
            # Reader was deliberately released above; preserve audit metrics
            # before release via event data rather than retaining objects.
            result['processed_chunk_count_note'] = 'all candidate chunks completed'
    except Exception as exc:
        error = '%s: %s' % (type(exc).__name__, exc)
        result = {'ok': False, 'mode': mode, 'error': error}
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
    Path(result_path).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                 encoding='utf-8')
    return 0 if result.get('ok') else 2


def _run_child(path, mode, topics, sample_ms):
    with tempfile.TemporaryDirectory(prefix='mcap-chunk-audit-') as td:
        event_path = os.path.join(td, mode + '.events.jsonl')
        result_path = os.path.join(td, mode + '.result.json')
        cmd = [sys.executable, str(Path(__file__).resolve()), '--child',
               '--mcap', path, '--mode', mode,
               '--topics-json', json.dumps(topics, ensure_ascii=False),
               '--event-file', event_path, '--result-file', result_path]
        env = dict(os.environ)
        runtime_site = ROOT / 'runtime' / 'Lib' / 'site-packages'
        env['PYTHONPATH'] = os.pathsep.join(
            (str(ROOT), str(HERE), str(runtime_site), env.get('PYTHONPATH', '')))
        cmd[0] = getattr(sys, '_base_executable', sys.executable)
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        handle = mem._open_process(proc.pid)
        samples, events = [], []
        phase, pos, next_tasklist = 'process_start', 0, 0.0
        try:
            while proc.poll() is None:
                try:
                    with open(event_path, 'r', encoding='utf-8') as fh:
                        fh.seek(pos)
                        block = fh.read()
                        pos = fh.tell()
                    for line in block.splitlines():
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        events.append(item)
                        phase = item.get('phase', phase)
                except FileNotFoundError:
                    pass
                rss, os_peak = mem._rss_pid(handle, proc.pid)
                if rss is not None:
                    samples.append({'phase': phase, 'rss_bytes': rss,
                                    'os_peak_bytes': os_peak})
                now = time.perf_counter()
                if now >= next_tasklist:
                    next_tasklist = now + 0.1
                time.sleep(max(0.001, sample_ms / 1000.0))
        finally:
            mem._close_process(handle)
        try:
            with open(event_path, 'r', encoding='utf-8') as fh:
                fh.seek(pos)
                block = fh.read()
            for line in block.splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        except FileNotFoundError:
            pass
        stderr = (proc.stderr.read() if proc.stderr else b'').decode(
            'utf-8', errors='replace')
        if proc.returncode != 0 or not os.path.isfile(result_path):
            raise RuntimeError('%s failed (%d): %s' % (mode, proc.returncode,
                                                       stderr[-2000:]))
        result = json.loads(Path(result_path).read_text(encoding='utf-8'))
        if not result.get('ok'):
            raise RuntimeError('%s child: %s' % (mode, result.get('error')))
        result['parent_sample_interval_ms'] = sample_ms
        result['parent_sample_count'] = len(samples)
        result['parent_sampled_peak_rss_bytes'] = max(
            (s['rss_bytes'] for s in samples), default=None)
        result['phase_peak_rss_bytes'] = {
            p: max(s['rss_bytes'] for s in samples if s['phase'] == p)
            for p in {s['phase'] for s in samples}}
        result['events'] = events
        return result


def _run_prepare_variant(path, mode):
    with tempfile.TemporaryDirectory(prefix='mcap-p1.6c-prepare-') as td:
        cache_root = os.path.join(td, 'cache')
        result_path = os.path.join(td, mode + '.json')
        cmd = [sys.executable, str(Path(__file__).resolve()), '--prepare-child',
               '--mcap', path, '--mode', mode,
               '--prepare-cache-root', cache_root,
               '--prepare-result-file', result_path]
        env = dict(os.environ)
        runtime_site = ROOT / 'runtime' / 'Lib' / 'site-packages'
        env['PYTHONPATH'] = os.pathsep.join(
            (str(ROOT), str(HERE), str(runtime_site), env.get('PYTHONPATH', '')))
        cmd[0] = getattr(sys, '_base_executable', sys.executable)
        p = subprocess.run(cmd, cwd=str(ROOT), env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if p.returncode != 0 or not os.path.isfile(result_path):
            raise RuntimeError('prepare %s failed: %s' %
                               (mode, p.stderr.decode('utf-8', errors='replace')[-2000:]))
        return json.loads(Path(result_path).read_text(encoding='utf-8'))


def _prepare_compare(path):
    c0 = _run_prepare_variant(path, 'C0')
    c1 = _run_prepare_variant(path, 'C1')
    fp_equal = c0.get('fingerprint') == c1.get('fingerprint')
    manifest_equal = c0.get('manifest_semantic') == c1.get('manifest_semantic')
    return {
        'ok': fp_equal and manifest_equal,
        'fingerprint_equal': fp_equal,
        'manifest_semantic_equal': manifest_equal,
        'c0_fingerprint': c0.get('fingerprint'),
        'c1_fingerprint': c1.get('fingerprint'),
    }


def _crc_probe():
    """C1 must reject a corrupt Chunk CRC before any cache is published."""
    from tests import mcapfix as fx
    with tempfile.TemporaryDirectory(prefix='mcap-p1.6c-crc-') as td:
        src = os.path.join(td, 'bad.mcap')
        inner = [fx.schema(1, 'foxglove.CompressedImage'),
                 fx.channel(1, 1, '/camera2/compressed'),
                 fx.message(1, 0, 1000, 1000, fx.compressed_image(b'x', 'h264'))]
        fx.assemble(src, [fx.header(),
                          fx.chunk(inner, crc_override=0xDEADBEEF)])
        rejected = False
        try:
            with open(src, 'rb') as fh:
                list(ChunkStreamingIndexedReader(fh).iter_messages(
                    topics=['/camera2/compressed']))
        except Exception:
            rejected = True
        prepare_failed = False
        outdir = os.path.join(td, 'cache-out')
        staging = outdir + '.staging'
        try:
            import prepare as PREP
            original = PREP.MR.McapReader
            _PrepareStreamingAdapter._base_reader_cls = original
            PREP.MR.McapReader = _PrepareStreamingAdapter
            try:
                PREP.prepare(src, outdir,
                             camera_pred=lambda t: 'camera2' in (t or ''),
                             profile=appcache.CACHE_PROFILE)
            except Exception:
                prepare_failed = True
            finally:
                PREP.MR.McapReader = original
        except Exception:
            prepare_failed = True
        ready_created = os.path.isfile(os.path.join(outdir, 'manifest.json'))
        staging_left = os.path.exists(staging)
        return {'c1_rejected_bad_chunk_crc': rejected,
                'prepare_failed': prepare_failed,
                'cache_ready_created': ready_created,
                'staging_left': staging_left,
                'ok': rejected and prepare_failed and not ready_created
                and not staging_left}


def _median(rows, key):
    xs = sorted(r[key] for r in rows if r.get(key) is not None)
    return xs[len(xs) // 2] if xs else None


def audit(path, topics, runs, warmup, sample_ms, out,
          prepare_compare=False, crc_probe=False):
    rows = {'C0': [], 'C1': []}
    for _ in range(warmup):
        for mode in rows:
            _run_child(path, mode, topics, sample_ms)
    for i in range(runs):
        for mode in rows:
            print('run %d/%d %s ...' % (i + 1, runs, mode), flush=True)
            rows[mode].append(_run_child(path, mode, topics, sample_ms))
    ref = rows['C0'][0]
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
    med = {}
    for mode in rows:
        med[mode] = {k: _median(rows[mode], k) for k in (
            'total_cache_ms', 'reader_iteration_ms', 'reader_next_total_ms',
            'time_to_first_selected_message_ms', 'process_peak_rss_bytes',
            'parent_sampled_peak_rss_bytes', 'rss_after_reader_bytes',
            'rss_after_gc_bytes', 'candidate_chunk_count',
            'processed_chunk_count', 'selected_count', 'selected_payload_bytes',
            'max_messages_in_chunk', 'max_compressed_chunk_bytes',
            'max_uncompressed_chunk_bytes')}
    c0 = med['C0']['total_cache_ms'] or 0
    c1 = med['C1']['total_cache_ms'] or 0
    slowdown = ((c1 - c0) / c0 * 100.0) if c0 else None
    c1_rss = med['C1']['process_peak_rss_bytes'] or 0
    if not correctness_ok or c1_rss > 300 * 1048576 or (slowdown is not None and slowdown > 10):
        decision = 'CHUNK_STREAMING_INDEXED_READER_NOT_SUPPORTED'
    elif slowdown is not None and slowdown <= 5 and c1_rss <= 150 * 1048576:
        decision = 'CHUNK_STREAMING_INDEXED_READER_STRONGLY_SUPPORTED'
    elif slowdown is not None and slowdown <= 10 and c1_rss <= 150 * 1048576:
        decision = 'CHUNK_STREAMING_INDEXED_READER_SUPPORTED_WITH_PERFORMANCE_TRADEOFF'
    else:
        decision = 'CHUNK_STREAMING_INDEXED_READER_NOT_SUPPORTED'
    result = {
        'audit': 'MCAP_CACHE_OPT_P1_6C_CHUNK_STREAMING_INDEXED_READER_AUDIT',
        'source': path, 'source_bytes': os.path.getsize(path), 'topics': topics,
        'runs': runs, 'warmup': warmup, 'sample_interval_ms': sample_ms,
        'independent_process_per_sample': True, 'medians': med,
        'slowdown_c1_vs_c0_pct': slowdown, 'correctness_ok': correctness_ok,
        'consistency': consistency, 'decision': decision, 'raw': rows,
        'production_changed': False,
    }
    if crc_probe:
        result['crc_probe'] = _crc_probe()
        correctness_ok = correctness_ok and result['crc_probe']['ok']
    if prepare_compare:
        result['prepare_compare'] = _prepare_compare(path)
        correctness_ok = correctness_ok and result['prepare_compare']['ok']
    result['correctness_ok'] = correctness_ok
    if not correctness_ok:
        decision = 'CHUNK_STREAMING_INDEXED_READER_NOT_SUPPORTED'
        result['decision'] = decision
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if out:
        Path(out).write_text(text, encoding='utf-8')
    print('\nP1.6C decision=%s correctness=%s' %
          (decision, 'PASS' if correctness_ok else 'FAIL'))
    for mode in ('C0', 'C1'):
        print('%s total=%.1f ms reader=%.1f ms first=%.1f ms peak=%.1f MB' %
              (mode, med[mode]['total_cache_ms'] / 1.0,
               med[mode]['reader_iteration_ms'],
               med[mode]['time_to_first_selected_message_ms'],
               med[mode]['process_peak_rss_bytes'] / 1048576.0))
    print('slowdown C1 vs C0: %.2f%%' % slowdown)
    if out:
        print('报告已写入', out)
    return 0 if correctness_ok else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mcap', required=True)
    ap.add_argument('--runs', type=int, default=3)
    ap.add_argument('--warmup', type=int, default=1)
    ap.add_argument('--sample-ms', type=int, default=10)
    ap.add_argument('--topic', action='append', dest='topics')
    ap.add_argument('--out')
    ap.add_argument('--prepare-compare', action='store_true',
                    help='在小样本上用未修改 prepare handlers 比较 C0/C1 产物')
    ap.add_argument('--crc-probe', action='store_true',
                    help='生成极小损坏 Chunk，验证 C1 CRC 拒绝')
    ap.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--prepare-child', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--mode', choices=('C0', 'C1'), help=argparse.SUPPRESS)
    ap.add_argument('--topics-json', help=argparse.SUPPRESS)
    ap.add_argument('--event-file', help=argparse.SUPPRESS)
    ap.add_argument('--result-file', help=argparse.SUPPRESS)
    ap.add_argument('--prepare-cache-root', help=argparse.SUPPRESS)
    ap.add_argument('--prepare-result-file', help=argparse.SUPPRESS)
    a = ap.parse_args()
    path = os.path.abspath(a.mcap)
    if not os.path.isfile(path):
        ap.error('文件不存在：%s' % path)
    if a.child:
        return _child(path, a.mode, json.loads(a.topics_json),
                      a.event_file, a.result_file)
    if getattr(a, 'prepare_child', False):
        return _prepare_child(path, a.mode, a.prepare_cache_root,
                              a.prepare_result_file)
    if not 1 <= a.sample_ms <= 20:
        ap.error('--sample-ms 必须在 1..20 之间')
    all_topics = base._summary_topics(path)
    topics = a.topics or base._default_topics(path, all_topics)
    return audit(path, topics, max(1, a.runs), max(0, a.warmup),
                 a.sample_ms, a.out,
                 prepare_compare=a.prepare_compare, crc_probe=a.crc_probe)


if __name__ == '__main__':
    sys.exit(main())
