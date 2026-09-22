"""Independent-process full prepare RSS sampler for P1.6D."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import reader_memory_audit as mem


def _raw_child(src, mode, cache_root, result_path):
    import appcache
    import prepare as PREP
    appcache.CACHE_ROOT = cache_root
    outdir = os.path.join(cache_root, 'prepare-out')
    man = PREP.prepare(src, outdir,
                       camera_pred=lambda t: ('camera2' in (t or '') or
                                              'camera3' in (t or '')),
                       profile=appcache.CACHE_PROFILE)
    # Keep only semantic output/perf fields; the parent owns RSS sampling.
    Path(result_path).write_text(json.dumps({
        'perf': man.get('_perf') or {},
        'manifest': {k: v for k, v in man.items() if k != '_perf'},
    }, ensure_ascii=False), encoding='utf-8')


def run_one(src, mode, out_json, sample_ms=10, raw_prepare=False):
    env = dict(os.environ)
    env['MCAPVIEWER_READER_MODE'] = mode
    runtime_site = ROOT / 'runtime' / 'Lib' / 'site-packages'
    env['PYTHONPATH'] = os.pathsep.join(
        (str(ROOT), str(HERE), str(runtime_site), env.get('PYTHONPATH', '')))
    cache_td = tempfile.TemporaryDirectory(prefix='mcap-raw-prepare-')
    if raw_prepare:
        cmd = [getattr(sys, '_base_executable', sys.executable),
               str(Path(__file__).resolve()), '--raw-child', '--mcap', src,
               '--mode', mode, '--raw-cache-root', cache_td.name,
               '--raw-result-file', out_json, '--out', out_json]
    else:
        cmd = [getattr(sys, '_base_executable', sys.executable),
               str(HERE / 'perf_bench.py'), '--mcap', src, '--label',
               'prepare-' + mode, '--out', out_json]
    try:
        p = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        h = mem._open_process(p.pid)
        samples = []
        try:
            while p.poll() is None:
                rss, peak = mem._rss_pid(h, p.pid)
                if rss is not None:
                    samples.append(rss)
                time.sleep(max(0.001, sample_ms / 1000.0))
        finally:
            mem._close_process(h)
        stderr = (p.stderr.read() if p.stderr else b'').decode('utf-8', errors='replace')
        if p.returncode != 0 or not os.path.isfile(out_json):
            raise RuntimeError('%s prepare failed: %s' % (mode, stderr[-2000:]))
        result = json.loads(Path(out_json).read_text(encoding='utf-8'))
        result['_independent_peak_rss_bytes'] = max(samples, default=None)
        result['_sample_count'] = len(samples)
        Path(out_json).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                  encoding='utf-8')
        return result
    finally:
        cache_td.cleanup()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mcap', required=True)
    ap.add_argument('--mode', choices=('official', 'chunk_streaming'), required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--sample-ms', type=int, default=10)
    ap.add_argument('--raw-prepare', action='store_true',
                    help='仅执行 prepare，不做 perf_bench 的 OpenCV 后处理')
    ap.add_argument('--raw-child', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--raw-cache-root', help=argparse.SUPPRESS)
    ap.add_argument('--raw-result-file', help=argparse.SUPPRESS)
    a = ap.parse_args()
    if not 1 <= a.sample_ms <= 20:
        ap.error('--sample-ms 必须在 1..20 之间')
    if a.raw_child:
        return _raw_child(os.path.abspath(a.mcap), a.mode,
                          a.raw_cache_root, a.raw_result_file) or 0
    r = run_one(os.path.abspath(a.mcap), a.mode, os.path.abspath(a.out),
                a.sample_ms, raw_prepare=a.raw_prepare)
    print(json.dumps({
        'mode': a.mode,
        'wall_ms': r['perf'].get('wall_ms'),
        'reader_ms': r['perf'].get('mcap_iteration_ms'),
        'peak_rss_mb': round((r.get('_independent_peak_rss_bytes') or 0) / 1048576, 1),
        'sample_count': r.get('_sample_count'),
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
