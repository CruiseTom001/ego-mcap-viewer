"""h264_compat_audit.py —— P1.6F-A：H264 直接播放兼容性审计（只读 MCAP，不改源文件）

对每个 video 通道（camera2/camera3）检查：
  topic / codec / message_count / payload_bytes / 时间戳单调 /
  NAL 封装（AnnexB / AVCC）/ SPS / PPS / IDR 数量与间隔 / B 帧 /
  一消息一帧 / 首个可解码时间戳

并用「首个 IDR(+SPS/PPS) → 临时 mux 成 MP4 → OpenCV 打开」做**真解码自包含验证**。

输出 JSON（tmp/h264_compat_audit.json）+ 控制台表格。
"""

import io
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(os.path.dirname(ROOT), 'tmp')
sys.path.insert(0, ROOT)

import mcap_reader as MR                              # noqa: E402
import h264mp4 as H                                   # noqa: E402

try:
    import cv2
    HAS_CV = True
except Exception:
    HAS_CV = False

NONIDR, IDR, SEI, SPS, PPS, AUD = 1, 5, 6, 7, 8, 9


def _ue(bits, i):
    zeros = 0
    while i < len(bits) and bits[i] == '0':
        zeros += 1
        i += 1
    if i >= len(bits):
        return None, i
    v = int(bits[i:i + zeros + 1], 2) - 1
    return v, i + zeros + 1


def slice_type(nalu):
    """slice_type（0=P,1=B,2=I,3=SP,4=SI；≥5 表示同一类型）。失败返回 None。"""
    try:
        rbsp = H.unescape_rbsp(nalu[1:])
        bits = ''.join(format(b, '08b') for b in rbsp[:4])
        _, i = _ue(bits, 0)                  # first_mb_in_slice
        v, _ = _ue(bits, i)                  # slice_type
        return None if v is None else v % 5
    except Exception:
        return None


def audit_camera(path, ch, want_ids):
    """审计单个 video 通道"""
    r = dict(topic=ch['topic'], channel_id=ch['id'], codec=None,
             message_count=0, payload_bytes=0, timestamp_order_ok=True,
             nal_format='unknown', avcc_messages=0, annexb_messages=0,
             sps_count=0, pps_count=0, idr_count=0, idr_frame_positions=[],
             idr_time_gaps_s=[], b_frames=0, one_message_one_frame=True,
             slice_counts={}, first_ts_ns=None, last_ts_ns=None,
             first_decodable_ts_ns=None, multi_slice_messages=0,
             no_slice_messages=0)
    last_ts = None
    idx = 0
    idr_idxs = []
    idr_ts = []
    frames_for_mux = []          # 用于自包含验证（首个 IDR 之后若干帧）
    started = False
    param_before_idr = []
    recent_params = []
    reader = MR.McapReader(path)
    for cid, lg, _p, _s, data in reader.iter_messages(
            set(want_ids), log_time_order=False, pushdown=True):
        try:
            fmt, payload, _extra = MR.decode_video(ch['schema'], ch['message_encoding'], data)
        except Exception:
            continue
        if not payload:
            continue
        if r['codec'] is None and fmt:
            r['codec'] = fmt
        r['message_count'] += 1
        r['payload_bytes'] += len(payload)
        if r['first_ts_ns'] is None:
            r['first_ts_ns'] = lg
        if last_ts is not None and lg < last_ts:
            r['timestamp_order_ok'] = False
        last_ts = lg
        r['last_ts_ns'] = lg
        # ---- 封装格式 ----
        if payload[:4] == b'\x00\x00\x00\x01' or payload[:3] == b'\x00\x00\x01':
            r['annexb_messages'] += 1
        else:
            r['avcc_messages'] += 1
        # ---- NAL 解析 ----
        try:
            nalus = H.split_nalus(payload)
        except Exception:
            nalus = []
        types = [H.nal_type(n) for n in nalus]
        for t in types:
            r['slice_counts'][str(t)] = r['slice_counts'].get(str(t), 0) + 1
        sl = [t for t in types if t in (NONIDR, IDR)]
        if len(sl) == 1:
            pass
        elif len(sl) == 0:
            r['no_slice_messages'] += 1
            r['one_message_one_frame'] = False
        else:
            r['multi_slice_messages'] += 1
            r['one_message_one_frame'] = False
        if SPS in types:
            r['sps_count'] += 1
            recent_params = [n for n in nalus if H.nal_type(n) in (SPS, PPS)]
        if PPS in types:
            r['pps_count'] += 1
        # ---- B 帧 ----
        for n, t in zip(nalus, types):
            if t in (NONIDR, IDR):
                st = slice_type(n)
                if st == 1:
                    r['b_frames'] += 1
        # ---- IDR / 自包含 ----
        if IDR in types:
            r['idr_count'] += 1
            idr_idxs.append(idx)
            idr_ts.append(lg)
            if not started:
                param_before_idr = list(recent_params)
                started = True
                r['first_decodable_ts_ns'] = lg
        if started and len(frames_for_mux) < 40:
            frames_for_mux.append((lg, payload))
        idx += 1
    if len(idr_idxs) > 1:
        d = [idr_idxs[i + 1] - idr_idxs[i] for i in range(len(idr_idxs) - 1)]
        r['idr_frame_positions'] = d[:20]
        r['idr_interval_frames_median'] = sorted(d)[len(d) // 2]
    if len(idr_ts) > 1:
        g = [(idr_ts[i + 1] - idr_ts[i]) / 1e9 for i in range(len(idr_ts) - 1)]
        r['idr_time_gaps_s'] = [round(x, 3) for x in g[:20]]
        r['idr_interval_s_median'] = round(sorted(g)[len(g) // 2], 3)
    total = r['annexb_messages'] + r['avcc_messages']
    r['nal_format'] = ('annexb' if r['annexb_messages'] == total and total else
                       'avcc' if r['avcc_messages'] == total and total else
                       'mixed' if total else 'unknown')
    # ---- 自包含解码验证（用首个 IDR 起的一段帧 mux 成临时 MP4）----
    r['self_contained_decode'] = None
    r['self_contained_frames'] = None
    if started and frames_for_mux and HAS_CV:
        tmpd = tempfile.mkdtemp(prefix='h264audit-')
        try:
            mp4 = os.path.join(tmpd, 'probe.mp4')
            start = frames_for_mux[0][0]
            H.mux_frames([(ts, pl) for ts, pl in frames_for_mux], mp4,
                         start_offset_ns=start, time_base_ns=0)
            cap = cv2.VideoCapture(mp4)
            n = 0
            while True:
                ok, _fr = cap.read()
                if not ok:
                    break
                n += 1
            cap.release()
            r['self_contained_frames'] = n
            r['self_contained_decode'] = bool(n >= 1)
        except Exception as e:
            r['self_contained_decode'] = False
            r['self_contained_error'] = '%s: %s' % (type(e).__name__, e)
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)
    # ---- 兼容性判定（P1.6F 第 16/17 节）----
    if r['idr_count'] == 0 or r['sps_count'] == 0:
        r['decision'] = 'DIRECT_BLOCKED_CODEC_STRUCTURE'
    elif r['b_frames'] > 0:
        r['decision'] = 'DIRECT_BLOCKED_B_FRAMES'
    elif not r['one_message_one_frame']:
        r['decision'] = 'DIRECT_BLOCKED_MESSAGE_FRAGMENTATION'
    elif r['nal_format'] == 'avcc' or r['nal_format'] == 'mixed':
        r['decision'] = 'DIRECT_NEEDS_AVCC_ADAPTER'
    elif r['self_contained_decode'] is False:
        r['decision'] = 'DIRECT_BLOCKED_CODEC_STRUCTURE'
    else:
        r['decision'] = 'DIRECT_COMPATIBLE'
    return r


def main():
    samples = [
        (r'D:\视频查看软件\DAS-Ego_20260911203440_none_none_689985_65416a8a.mcap', 'Real 41.8MB'),
        (r'D:\wendang\xwechat_files\wxid_oz7zj4zmnwgz12_a0ce\msg\file\2026-09'
         r'\DAS-Ego_20260911154513_none_none_689985_371aafac.mcap', 'Real 216MB'),
        (os.path.join(TMP, 'big_2gb.mcap'), 'Synthetic 2GB（真实数据重放，非真实大文件）'),
    ]
    out = {'cv2_available': HAS_CV, 'samples': []}
    for path, label in samples:
        if not os.path.isfile(path):
            out['samples'].append(dict(label=label, path=path, error='missing'))
            continue
        t0 = time.perf_counter()
        r = MR.McapReader(path)
        summ = r.summary()
        vids = [c for c in summ['channels'] if c['kind'] == 'video']
        cams = [c for c in vids if MR.classify_video_format('h264') == 'h264']
        rec = dict(label=label, path=path, size_mb=round(os.path.getsize(path) / 1048576, 1),
                   has_index=bool(summ.get('has_index')),
                   chunk_count=summ.get('chunk_count') or 0,
                   video_channels=[c['topic'] for c in vids], cameras=[])
        for c in vids:
            a = audit_camera(path, c, [cc['id'] for cc in vids])
            a.pop('channel_id', None)
            rec['cameras'].append(a)
        # 文件级决策：两路都 compatible/needs_adapter 才算 eligible
        ok = all(a['decision'] in ('DIRECT_COMPATIBLE', 'DIRECT_NEEDS_AVCC_ADAPTER')
                 for a in rec['cameras'])
        rec['file_decision'] = ('DIRECT_FILE_ELIGIBLE' if ok else
                                'DIRECT_FILE_INELIGIBLE_CACHE_FALLBACK')
        rec['audit_seconds'] = round(time.perf_counter() - t0, 2)
        out['samples'].append(rec)
        print('=' * 92)
        print('%s（%.1f MB, indexed=%s, chunks=%s）审计 %.1fs → %s'
              % (label, rec['size_mb'], rec['has_index'], rec['chunk_count'],
                 rec['audit_seconds'], rec['file_decision']))
        for a in rec['cameras']:
            print('  %-42s %s' % (a['topic'], a['decision']))
            print('    codec=%s  messages=%d  bytes=%.1fMB  format=%s  order_ok=%s'
                  % (a['codec'], a['message_count'], a['payload_bytes'] / 1048576,
                     a['nal_format'], a['timestamp_order_ok']))
            print('    SPS=%d PPS=%d IDR=%d IDR间隔=%s帧/%ss  B帧=%d  一消息一帧=%s'
                  % (a['sps_count'], a['pps_count'], a['idr_count'],
                     a.get('idr_interval_frames_median'), a.get('idr_interval_s_median'),
                     a['b_frames'], a['one_message_one_frame']))
            print('    自包含解码=%s（%s 帧） 首可解码 ts=%s'
                  % (a['self_contained_decode'], a['self_contained_frames'],
                     a['first_decodable_ts_ns']))
    p = os.path.join(TMP, 'h264_compat_audit.json')
    with open(p, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    elig = [s for s in out['samples'] if s.get('file_decision') == 'DIRECT_FILE_ELIGIBLE']
    tot = [s for s in out['samples'] if 'file_decision' in s]
    print()
    print('样本合格率: %d / %d' % (len(elig), len(tot)))
    print('汇总已写入', p)
    return 0


if __name__ == '__main__':
    sys.exit(main())
