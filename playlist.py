"""playlist.py —— 文件夹扫描与播放列表"""

import os
import re
import time

_NUM = re.compile(r'(\d+)')

#: 可直接播放的视频格式（无需 mcap 缓存封装）
DIRECT_EXTS = ('.mp4',)
#: 需要缓存封装的 MCAP 录像
MCAP_EXTS = ('.mcap',)
ALL_EXTS = MCAP_EXTS + DIRECT_EXTS


def is_direct_format(name):
    """True 表示这个文件能直接播放（不入缓存队列）"""
    return os.path.splitext(name or '')[1].lower() in DIRECT_EXTS


def is_supported(name):
    return os.path.splitext(name or '')[1].lower() in ALL_EXTS


def probe_mp4(path):
    """探测 MP4 的时长/帧率/帧数（失败返回 None，不抛异常）。

    仅用于直读播放与合格率汇总，不做任何写盘。
    """
    try:
        import cv2
    except Exception:
        return None
    cap = None
    try:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return None
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if frames <= 0 or fps <= 0:
            return None
        return dict(duration_s=frames / fps, fps=fps, frames=frames,
                    width=w, height=h)
    except Exception:
        return None
    finally:
        try:
            if cap is not None:
                cap.release()
        except Exception:
            pass


def natural_key(s):
    """自然排序：video2 排在 video10 前面"""
    return [int(t) if t.isdigit() else t.lower() for t in _NUM.split(s)]


def scan_folder(folder, recursive=True, limit=3000):
    """返回文件夹里的 .mcap 与 .mp4 列表（按文件名自然排序）"""
    out = []
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        return out
    if recursive:
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith('.') and d not in ('cache', '__pycache__')]
            for fn in filenames:
                if is_supported(fn):
                    out.append((dirpath, fn))
                    if len(out) >= limit:
                        break
            if len(out) >= limit:
                break
    else:
        for fn in os.listdir(folder):
            fp = os.path.join(folder, fn)
            if os.path.isfile(fp) and is_supported(fn):
                out.append((folder, fn))
    out.sort(key=lambda x: (natural_key(os.path.relpath(x[0], folder)), natural_key(x[1])))

    items = []
    for dirpath, fn in out:
        fp = os.path.join(dirpath, fn)
        try:
            st = os.stat(fp)
            size, mtime = st.st_size, int(st.st_mtime)
        except OSError:
            continue
        rel = os.path.relpath(dirpath, folder)
        items.append(dict(
            path=fp,
            name=fn,
            stem=os.path.splitext(fn)[0],
            dir=dirpath,
            rel_dir='' if rel == '.' else rel,
            size=size,
            mtime=mtime,
            mtime_str=time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime)),
        ))
    return items


def human_size(n):
    if n < 1024:
        return '%d B' % n
    if n < 1048576:
        return '%.1f KB' % (n / 1024)
    if n < 1073741824:
        return '%.1f MB' % (n / 1048576)
    return '%.2f GB' % (n / 1073741824)


def human_time(sec):
    if sec is None:
        return '—'
    m = int(sec // 60)
    s = sec - m * 60
    return '%02d:%06.3f' % (m, s)
