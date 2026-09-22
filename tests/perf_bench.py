"""perf_bench.py —— 缓存性能与一致性基准工具（Before/After 对比）

用法：
  # 跑一次基准，输出 JSON（含分阶段耗时 + 一致性指纹）
  python tests/perf_bench.py --mcap <file.mcap> --label before --out before.json

  # 对比两次结果（耗时表 + 一致性检查）
  python tests/perf_bench.py --compare before.json after.json

采集内容：
  * prepare() 的分阶段耗时（reader/summary/iteration/mux/audio/imu/manifest/publish/total）
  * 消息与帧计数（seen / selected / 每路帧数 / 字节数）
  * 一致性指纹：时长、time_base、每路 frames/dropped/fps/width/height/mp4 字节、
    times 文件的帧数与 SHA256、IMU 样本数 + SHA256、音频字节 + SHA256、
    manifest 校验（appcache.load_manifest）、MP4 用 OpenCV 实际打开验证
"""

import os
import sys
import json
import time
import shutil
import hashlib
import tempfile
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

CACHE_ENV = 'MCAPVIEWER_CACHE'


def _sha256(path):
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for blk in iter(lambda: fh.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def _count_times(path):
    if not os.path.isfile(path):
        return None
    return os.path.getsize(path) // 8          # float64 数组


def _mp4_playable(path):
    try:
        import cv2
        cap = cv2.VideoCapture(path)
        ok = cap.isOpened()
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) if ok else 0
        cap.release()
        return bool(ok and frames > 0), frames
    except Exception as e:
        return False, 'error: %s' % e


def run_bench(mcap, label='run', cache_root=None, slim=True, keep_cache=False, keep_audio=None, keep_imu=None):
    root = cache_root or tempfile.mkdtemp(prefix='perf-bench-%s-' % label)
    os.environ[CACHE_ENV] = root
    import appcache
    appcache.CACHE_ROOT = root
    import prepare as PREP

    fid = appcache.file_id(mcap)
    outdir = os.path.join(root, appcache.cache_key(fid))
    pred = (lambda t: ('camera2' in (t or '') or 'camera3' in (t or ''))) if slim else None

    t0 = time.time()
    man = PREP.prepare(mcap, outdir, camera_pred=pred,
                       profile=(appcache.CACHE_PROFILE if slim else 'full'),
                       keep_audio=keep_audio, keep_imu=keep_imu)
    wall_ms = round((time.time() - t0) * 1000.0, 1)

    perf = dict(man.get('_perf') or {})
    perf['wall_ms'] = wall_ms
    perf['label'] = label
    perf['mcap'] = os.path.basename(mcap)
    perf['mcap_bytes'] = os.path.getsize(mcap)

    # ---- 一致性指纹 ----
    fp = dict(duration_s=man.get('duration_s'), time_base_ns=man.get('time_base_ns'),
              start_time_ns=man.get('start_time_ns'), end_time_ns=man.get('end_time_ns'),
              camera_count=len(man.get('cameras') or []))
    cams = []
    for cam in man.get('cameras') or []:
        base = os.path.join(outdir, cam.get('key', '') + '_c%d' % cam.get('id', 0))
        item = dict(key=cam.get('key'), id=cam.get('id'), topic=cam.get('topic'),
                    playable=cam.get('playable'), frames=cam.get('frames'),
                    frames_raw=cam.get('frames_raw'), dropped=cam.get('dropped'),
                    width=cam.get('width'), height=cam.get('height'),
                    fps=round(float(cam.get('fps') or 0.0), 4),
                    duration_s=round(float(cam.get('duration_s') or 0.0), 4),
                    start_offset_s=cam.get('start_offset_s'),
                    mp4_bytes=cam.get('mp4_bytes'),
                    times_count=_count_times(base + '.times'),
                    times_sha256=_sha256(base + '.times'))
        ok, n = _mp4_playable(base + '.mp4')
        item['mp4_openable'] = ok
        item['mp4_frames_opencv'] = n
        cams.append(item)
    fp['cameras'] = cams

    imu = man.get('imu') or {}
    imu_path = os.path.join(outdir, 'imu.json')
    fp['imu'] = dict(count=imu.get('count'), file_bytes=(
        os.path.getsize(imu_path) if os.path.isfile(imu_path) else None),
        sha256=_sha256(imu_path))
    audio = man.get('audio') or {}
    audio_path = os.path.join(outdir, audio.get('file') or 'audio.wav')
    fp['audio'] = dict(chunks=audio.get('chunks'), sample_rate=audio.get('sample_rate'),
                       channels=audio.get('channels'),
                       file_bytes=(os.path.getsize(audio_path)
                                   if os.path.isfile(audio_path) else None),
                       sha256=_sha256(audio_path))
    reloaded = appcache.load_manifest(outdir, source_path=mcap)
    fp['manifest_valid'] = reloaded is not None
    fp['on_disk_bytes'] = appcache.cache_size(fid, appcache.CACHE_PROFILE if slim else '')

    result = dict(perf=perf, fingerprint=fp)
    if not keep_cache:
        shutil.rmtree(root, ignore_errors=True)
    return result


def compare(a_path, b_path):
    with open(a_path, encoding='utf-8') as fh:
        A = json.load(fh)
    with open(b_path, encoding='utf-8') as fh:
        B = json.load(fh)
    pa, pb = A['perf'], B['perf']
    fa, fb = A['fingerprint'], B['fingerprint']

    print('=' * 74)
    print('性能对比  (%s → %s)' % (pa.get('label'), pb.get('label')))
    print('=' * 74)
    print('%-22s %14s %14s %10s' % ('指标', 'Before', 'After', '变化'))
    rows = [
        ('MCAP 大小 (MB)', pa['mcap_bytes'] / 1048576, pb['mcap_bytes'] / 1048576, ''),
        ('总缓存时间 (ms)', pa['wall_ms'], pb['wall_ms'], 'pct'),
        ('reader 构造 (ms)', pa.get('reader_create_ms'), pb.get('reader_create_ms'), 'pct'),
        ('summary 读取 (ms)', pa.get('summary_read_ms'), pb.get('summary_read_ms'), ''),
        ('MCAP 遍历 (ms)', pa.get('mcap_iteration_ms'), pb.get('mcap_iteration_ms'), 'pct'),
        ('视频封装 (ms)', pa.get('video_finalize_ms'), pb.get('video_finalize_ms'), 'pct'),
        ('  其中 mux (ms)', sum((pa.get('camera_mux_ms') or {}).values()),
         sum((pb.get('camera_mux_ms') or {}).values()), 'pct'),
        ('音频处理 (ms)', pa.get('audio_process_ms'), pb.get('audio_process_ms'), ''),
        ('IMU 处理 (ms)', pa.get('imu_process_ms'), pb.get('imu_process_ms'), ''),
        ('manifest 写 (ms)', pa.get('manifest_write_ms'), pb.get('manifest_write_ms'), ''),
        ('发布 (ms)', pa.get('publish_ms'), pb.get('publish_ms'), ''),
        ('遍历消息数', pa.get('total_messages_seen'), pb.get('total_messages_seen'), ''),
        ('参与处理消息数', pa.get('selected_messages'), pb.get('selected_messages'), ''),
        ('IMU 消息数', pa.get('imu_messages'), pb.get('imu_messages'), ''),
        ('音频消息数', pa.get('audio_messages'), pb.get('audio_messages'), ''),
        ('has_index', pa.get('has_index'), pb.get('has_index'), ''),
    ]
    for name, va, vb, mode in rows:
        if va is None and vb is None:
            continue
        chg = ''
        try:
            if mode == 'pct' and va:
                chg = '%+.1f%%' % ((vb - va) / va * 100.0)
        except Exception:
            chg = ''
        def fmt(v):
            if isinstance(v, float):
                return ('%.1f' % v)
            return str(v)
        print('%-22s %14s %14s %10s' % (name, fmt(va), fmt(vb), chg))

    sa = pa.get('source_file_size') or 0
    sb = pb.get('source_file_size') or sa
    if sa:
        print('\n吞吐：Before %.1f MB/s   After %.1f MB/s'
              % ((sa / 1048576) / max(0.001, pa['wall_ms'] / 1000.0),
                 (sb / 1048576) / max(0.001, pb['wall_ms'] / 1000.0)))

    print('\n' + '=' * 74)
    print('一致性检查（必须全部 OK）')
    print('=' * 74)
    checks = []

    def chk(name, va, vb, tol=None):
        if isinstance(va, float) or isinstance(vb, float):
            if tol is not None and va is not None and vb is not None:
                same = abs(va - vb) <= tol
            else:
                same = va == vb
        else:
            same = va == vb
        checks.append(same)
        print('%-28s %-26s %-26s %s'
              % (name, va, vb, 'OK' if same else '❌ 不一致'))

    chk('总时长 (s)', fa.get('duration_s'), fb.get('duration_s'), tol=1e-6)
    chk('time_base_ns', fa.get('time_base_ns'), fb.get('time_base_ns'))
    chk('视频通道数', fa.get('camera_count'), fb.get('camera_count'))
    ca = {c.get('key'): c for c in fa.get('cameras') or []}
    cb = {c.get('key'): c for c in fb.get('cameras') or []}
    chk('通道集合', sorted(ca), sorted(cb))
    for key in sorted(set(ca) & set(cb)):
        x, y = ca[key], cb[key]
        chk('%s 帧数' % key, x.get('frames'), y.get('frames'))
        chk('%s 尺寸' % key, (x.get('width'), x.get('height')),
            (y.get('width'), y.get('height')))
        chk('%s fps' % key, x.get('fps'), y.get('fps'), tol=0.01)
        chk('%s times 帧数' % key, x.get('times_count'), y.get('times_count'))
        chk('%s times 内容' % key, x.get('times_sha256'), y.get('times_sha256'))
        chk('%s 丢弃帧' % key, x.get('dropped'), y.get('dropped'))
        chk('%s MP4 可打开' % key, x.get('mp4_openable'), y.get('mp4_openable'))
        chk('%s MP4 帧数' % key, x.get('mp4_frames_opencv'), y.get('mp4_frames_opencv'))
    a_imu = fa.get('imu') or {}
    b_imu = fb.get('imu') or {}
    if b_imu.get('sha256') is None and a_imu.get('sha256'):
        # P1.6D-R2B：桌面精简缓存按合同不再包含 IMU → 缺失是预期变更
        checks.append(True)
        print('%-28s %-26s %-26s %s' % ('IMU 产物', 'imu.json 存在',
                                        '无 IMU（新合同）', 'OK（预期变更）'))
    else:
        chk('IMU 样本数', a_imu.get('count'), b_imu.get('count'))
        chk('IMU 内容', a_imu.get('sha256'), b_imu.get('sha256'))
    a_audio = fa.get('audio') or {}
    b_audio = fb.get('audio') or {}
    if b_audio.get('sha256') is None and a_audio.get('sha256'):
        # P1.6D-R2A：桌面精简缓存按合同不再包含音频 → 缺失是预期变更
        checks.append(True)
        print('%-28s %-26s %-26s %s' % ('音频产物', 'audio.wav 存在',
                                        '无音频（新合同）', 'OK（预期变更）'))
    else:
        chk('音频块数', a_audio.get('chunks'), b_audio.get('chunks'))
        chk('音频内容', a_audio.get('sha256'), b_audio.get('sha256'))
    chk('manifest 校验', fa.get('manifest_valid'), fb.get('manifest_valid'))
    print('\n一致性结果：%d / %d 通过' % (sum(1 for c in checks if c), len(checks)))
    return 0 if all(checks) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mcap')
    ap.add_argument('--label', default='run')
    ap.add_argument('--out')
    ap.add_argument('--cache-root')
    ap.add_argument('--full', action='store_true', help='完整缓存（默认桌面精简）')
    ap.add_argument('--keep-cache', action='store_true')
    ap.add_argument('--keep-audio', action='store_true',
                    help='A/B 对照：显式保留音频（默认按 profile 合同决定）')
    ap.add_argument('--keep-imu', action='store_true',
                    help='A/B 对照：显式保留 IMU（默认按 profile 合同决定）')
    ap.add_argument('--compare', nargs=2)
    a = ap.parse_args()

    if a.compare:
        return compare(a.compare[0], a.compare[1])
    if not a.mcap or not a.out:
        ap.error('需要 --mcap 与 --out')
    res = run_bench(a.mcap, a.label, a.cache_root, slim=not a.full,
                    keep_cache=a.keep_cache,
                    keep_audio=(True if a.keep_audio else None),
                    keep_imu=(True if a.keep_imu else None))
    with open(a.out, 'w', encoding='utf-8') as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    p = res['perf']
    print('label=%s  mcap=%.1f MB  total=%s ms  遍历=%s ms  封装=%s ms'
          % (a.label, p['mcap_bytes'] / 1048576, p.get('wall_ms'),
             p.get('mcap_iteration_ms'), p.get('video_finalize_ms')))
    print('已写入', a.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
