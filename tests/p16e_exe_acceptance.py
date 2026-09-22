"""p16e_exe_acceptance.py —— P1.6E 正式 EXE 真实世界验收（自动化部分）

自动化覆盖：启动 smoke / 空目录 / 真实 MCAP 缓存与产物 / 无音频合同 /
旧 v1 退休清理 / 3 槽位 / no-index single-pass / RSS 趋势。
需要人工确认的项（播放、拖时间轴、取消按键等）在结果里标 manual。

用法：
  python tests/p16e_exe_acceptance.py --exe <MCAP视频查看器.exe>
"""

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import appcache                                    # noqa: E402


def exe_rss_mb(_pid=None):
    """EXE 全部进程的峰值 RSS（MB）；PyInstaller onefile 有父子两个进程，
    取 tasklist 枚举到的最大值。失败返回 None。"""
    try:
        r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq MCAPVIEWER.exe',
                            '/FO', 'CSV', '/NH'],
                           capture_output=True, text=True, errors='replace', timeout=10)
        best = None
        for line in (r.stdout or '').splitlines():
            parts = [x.strip('"') for x in line.split('","')]
            if len(parts) >= 5 and 'MCAPVIEWER' in parts[0].upper():
                k = parts[-1].upper().replace(' K', '')
                if k.endswith('B') and not k.endswith('KB'):
                    mb = float(k[:-1]) / 1024.0
                else:
                    mb = float(k[:-2])
                best = mb if best is None else max(best, mb)
        return best
    except Exception:
        return None


def kill(p):
    try:
        p.terminate()
        p.wait(timeout=10)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def wait_for(cond, timeout, interval=1.0, sampler=None, samples=None):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        if sampler is not None and samples is not None:
            v = sampler()
            if v is not None:
                samples.append(round(time.time() - t0, 1))
                samples.append(round(v, 1))
        time.sleep(interval)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe', required=True)
    ap.add_argument('--timeout', type=int, default=240)
    a = ap.parse_args()
    exe = os.path.abspath(a.exe)
    tmp = os.path.join(os.path.dirname(ROOT), 'tmp', 'p16e')
    os.makedirs(tmp, exist_ok=True)
    real_dir = os.path.join(tmp, 'real')
    empty_dir = os.path.join(tmp, 'empty')
    four_dir = os.path.join(tmp, 'four')
    noidx_dir = os.path.join(tmp, 'noidx')
    for d in (real_dir, empty_dir, four_dir, noidx_dir):
        os.makedirs(d, exist_ok=True)
    R = {'results': [], 'rss': {}}

    def ok(name, passed, note=''):
        R['results'].append(dict(item=name, passed=bool(passed), note=note))
        print('%-46s %s %s' % (name, 'PASS' if passed else 'FAIL', note))

    real_src = [r'D:\视频查看软件\DAS-Ego_20260911203440_none_none_689985_65416a8a.mcap',
                r'D:\wendang\xwechat_files\wxid_oz7zj4zmnwgz12_a0ce\msg\file'
                r'\2026-09\DAS-Ego_20260911154513_none_none_689985_371aafac.mcap']
    # 清掉上一轮可能残留的 EXE 进程（避免文件占用）
    subprocess.run(['taskkill', '/F', '/IM', 'MCAPVIEWER.exe'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    # ---- 准备真实文件夹（2 个真实生产 MCAP）----
    for srcf in real_src:
        dstf = os.path.join(real_dir, os.path.basename(srcf))
        try:
            shutil.copy2(srcf, dstf)
        except PermissionError:
            if os.path.isfile(dstf) and os.path.getsize(dstf) == os.path.getsize(srcf):
                pass                                    # 已存在且完整，跳过
            else:
                raise
    real_files = sorted(os.listdir(real_dir))
    print('真实文件夹:', real_files)

    # ---- 场景 1：空目录 smoke ----
    cache1 = os.path.join(tmp, 'cache_empty')
    os.makedirs(cache1, exist_ok=True)
    env = dict(os.environ, MCAPVIEWER_CACHE=cache1)
    p = subprocess.Popen([exe, empty_dir], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(8)
    ok('启动 smoke（空目录）', p.poll() is None,
       '进程存活 %.0f s' % 8 if p.poll() is None else '提前退出')
    rss = exe_rss_mb(p.pid)
    ok('空目录 RSS 基线', rss is not None and rss < 400, '%.1f MB' % (rss or -1))
    ok('空目录不生成缓存', len(os.listdir(cache1)) <= 2,
       'cache 内容: %d 项' % len(os.listdir(cache1)))
    kill(p)
    time.sleep(2)

    # ---- 场景 2：真实文件夹（含旧 v1 退休清理）----
    cache2 = os.path.join(tmp, 'cache_real')
    shutil.rmtree(cache2, ignore_errors=True)
    os.makedirs(cache2, exist_ok=True)
    state2 = os.path.join(tmp, 'state_real')
    shutil.rmtree(state2, ignore_errors=True)
    env = dict(os.environ, MCAPVIEWER_CACHE=cache2,
               MCAPVIEWER_STATE_DIR=state2)
    # 构造旧 v1 目录（用第一个真实文件的 fid）
    import hashlib
    f0 = os.path.join(real_dir, real_files[0])
    st = os.stat(f0)
    fid = hashlib.sha1(('%s|%d|%d' % (os.path.abspath(f0), st.st_size,
                                      st.st_mtime_ns)).encode()).hexdigest()[:16]
    v1 = os.path.join(cache2, '%s@desktop_camera2_camera3_v1' % fid)
    os.makedirs(v1, exist_ok=True)
    with open(os.path.join(v1, 'old.bin'), 'wb') as fh:
        fh.write(b'x' * 2048)
    p = subprocess.Popen([exe, real_dir], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    samples = []
    done = wait_for(
        lambda: (lambda dirs: all(any(d.startswith(f[:24]) for d in dirs)
                                  for f in real_files))(os.listdir(cache2)),
        a.timeout, 2.0, lambda: exe_rss_mb(p.pid), samples)
    total_wait = time.time() and None
    ok('真实 MCAP 自动预热完成', done, '用时上限 %ds' % a.timeout)
    ok('旧 v1 退休清理（EXE 启动/打开文件夹时）',
       not os.path.isdir(v1), 'v1 目录应被安全清理')
    if samples:
        vals = [samples[i + 1] for i in range(0, len(samples) - 1, 2)]
        R['rss'][os.path.basename(real_dir)] = vals
        ok('缓存期间 RSS 有界', max(vals) < 700,
           'RSS 采样 %s（前→后 %.0f→%.0f MB）' % (len(vals), vals[0], vals[-1]))
    kill(p)
    time.sleep(2)
    # 产物检查
    prods_ok, no_audio, no_spool = True, True, True
    audio_wav = 0
    for f in real_files:
        st = os.stat(os.path.join(real_dir, f))
        fid = hashlib.sha1(('%s|%d|%d' % (os.path.abspath(
            os.path.join(real_dir, f)), st.st_size, st.st_mtime_ns))
            .encode()).hexdigest()[:16]
        cdir = os.path.join(cache2, '%s@%s' % (fid, appcache.CACHE_PROFILE))
        if not os.path.isdir(cdir):
            prods_ok = False
            continue
        names = os.listdir(cdir)
        if not any(n.startswith('manifest') for n in names):
            prods_ok = False
        if not any('imu.json' == n for n in names):
            prods_ok = False
        if not any('camera2' in n and n.endswith('.mp4') for n in names):
            prods_ok = False
        if 'audio.wav' in names:
            no_audio = False
            audio_wav += 1
        if any(n.startswith('.audio_packets_') for n in names):
            no_spool = False
    ok('真实缓存产物齐全（manifest/camera2 mp4/imu.json）', prods_ok)
    ok('audio.wav 不存在（无音频合同）', no_audio,
       '%d 个目录含 audio.wav' % audio_wav)
    ok('音频 spool 临时文件不存在', no_spool)
    # manifest 里 audio 为 null
    mnull = True
    for f in real_files:
        st = os.stat(os.path.join(real_dir, f))
        fid = hashlib.sha1(('%s|%d|%d' % (os.path.abspath(
            os.path.join(real_dir, f)), st.st_size, st.st_mtime_ns))
            .encode()).hexdigest()[:16]
        mf = os.path.join(cache2, '%s@%s' % (fid, appcache.CACHE_PROFILE),
                          'manifest.json')
        try:
            with open(mf, encoding='utf-8') as fh:
                man = json.load(fh)
            if man.get('audio') is not None:
                mnull = False
            if man.get('cache_profile') != appcache.CACHE_PROFILE:
                mnull = False
        except Exception:
            mnull = False
    ok('manifest audio=null 且 profile=noaudio_v2', mnull)
    # 缓存体积
    sizes = [appcache._dir_size(os.path.join(
        cache2, '%s@%s' % (fid, appcache.CACHE_PROFILE)))
        for f in real_files
        for fid in [hashlib.sha1(('%s|%d|%d' % (os.path.abspath(
            os.path.join(real_dir, f)), os.stat(os.path.join(real_dir, f)).st_size,
            os.stat(os.path.join(real_dir, f)).st_mtime_ns)).encode())
            .hexdigest()[:16]]]
    if sizes and os.path.isfile(real_files and os.path.join(real_dir, real_files[0])):
        src_total = sum(os.path.getsize(os.path.join(real_dir, f))
                        for f in real_files)
        ok('缓存体积合理（<源体积）', sum(sizes) < src_total,
           'cache %.1f MB / source %.1f MB'
           % (sum(sizes) / 1048576, src_total / 1048576))

    # ---- 场景 3：3 槽位 ----
    cache3 = os.path.join(tmp, 'cache_four')
    shutil.rmtree(cache3, ignore_errors=True)
    os.makedirs(cache3, exist_ok=True)
    small = real_files[1] if os.path.getsize(os.path.join(
        real_dir, real_files[1])) < os.path.getsize(os.path.join(
            real_dir, real_files[0])) else real_files[0]
    for i in range(4):
        for attempt in range(3):
            try:
                shutil.copy2(os.path.join(real_dir, small),
                             os.path.join(four_dir, 'v%d_%s' % (i, small)))
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(3)
    state3 = os.path.join(tmp, 'state_four')
    shutil.rmtree(state3, ignore_errors=True)
    env = dict(os.environ, MCAPVIEWER_CACHE=cache3,
               MCAPVIEWER_STATE_DIR=state3)
    p = subprocess.Popen([exe, four_dir], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    done = wait_for(lambda: len([d for d in os.listdir(cache3)
                                 if d.endswith('@' + appcache.CACHE_PROFILE)]) >= 3,
                    a.timeout, 2.0)
    ok('自动预热（3 槽位）', done)
    time.sleep(3)
    n_cached = len([d for d in os.listdir(cache3)
                    if d.endswith('@' + appcache.CACHE_PROFILE)])
    ok('最多 3 个桌面缓存', n_cached == 3, '实际 %d 个' % n_cached)
    kill(p)
    time.sleep(2)

    # ---- 场景 4：no-index single-pass（2GB synthetic，唯一 2GB 样本）----
    noidx_src = os.path.join(os.path.dirname(ROOT), 'tmp', 'big_2gb_noidx.mcap')
    if os.path.isfile(noidx_src):
        shutil.copy2(noidx_src, noidx_dir)
        cache4 = os.path.join(tmp, 'cache_noidx')
        shutil.rmtree(cache4, ignore_errors=True)
        os.makedirs(cache4, exist_ok=True)
        state4 = os.path.join(tmp, 'state_noidx')
        shutil.rmtree(state4, ignore_errors=True)
        env = dict(os.environ, MCAPVIEWER_CACHE=cache4,
                   MCAPVIEWER_STATE_DIR=state4)
        t0 = time.time()
        p = subprocess.Popen([exe, noidx_dir], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        done = wait_for(lambda: len([d for d in os.listdir(cache4)
                                     if appcache.CACHE_PROFILE in d]) >= 1,
                        max(a.timeout, 180), 2.0)
        elapsed = time.time() - t0
        ok('no-index 2GB 缓存完成（single-pass）', done,
           '用时 %.1f s' % elapsed)
        ok('no-index 缓存时间合理（<60s）', elapsed < 60, '%.1f s' % elapsed)
        kill(p)
        time.sleep(2)
        cdir = [os.path.join(cache4, d) for d in os.listdir(cache4)
                if appcache.CACHE_PROFILE in d]
        if cdir:
            names = os.listdir(cdir[0])
            ok('no-index 产物齐全且无音频',
               any('imu.json' == n for n in names)
               and 'audio.wav' not in names)
            perf_log = os.path.join(cache4, 'cache-perf.log')
            lazy = False
            try:
                with open(perf_log, encoding='utf-8') as fh:
                    for line in fh:
                        if line.strip() and json.loads(line).get('lazy_single_pass'):
                            lazy = True
            except Exception:
                pass
            ok('cache-perf 记录 lazy_single_pass=True', lazy)
    else:
        ok('no-index 2GB 缓存完成', False, '样本不存在')

    R['exe'] = exe
    R['exe_size'] = os.path.getsize(exe)
    out = os.path.join(os.path.dirname(ROOT), 'tmp', 'p16e_acceptance.json')
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(R, fh, ensure_ascii=False, indent=1)
    npass = sum(1 for r in R['results'] if r['passed'])
    print('\n验收结果: %d / %d PASS  → %s'
          % (npass, len(R['results']), out))
    return 0 if npass == len(R['results']) else 1


if __name__ == '__main__':
    sys.exit(main())
