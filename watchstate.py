"""watchstate.py —— 三队列观看状态持久化

「已看完」等观看状态绝不能存在视频缓存目录里（看完后缓存会被删除），
统一存放到：

    %LOCALAPPDATA%\\MCAPViewer\\state\\<folder_hash>.json

绝不写进原始 MCAP 所在目录，避免影响采集文件。

关键规则
    * source_id = sha1(规范绝对路径 | size | mtime_ns)[:16]（与 appcache.file_id
      同源）。源文件任何变化都会得到新 id —— 自动视为新文件版本，不继承旧 WATCHED。
    * 写入一律走 临时文件 + os.replace 原子替换（appcache.write_json_atomic）。
    * 状态文件损坏：备份为 *.corrupt-<时间戳>，按空状态继续，绝不让程序崩溃。
    * 可用 MCAPVIEWER_STATE_DIR 环境变量重定向（测试隔离用）。
"""

import os
import sys
import json
import time
import tempfile
import hashlib

import appcache

HERE = os.path.dirname(os.path.abspath(__file__))
# PyInstaller one-file 的 _MEI 临时目录绝不能用来存放状态
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable))
           if getattr(sys, 'frozen', False) else HERE)

STATE_VERSION = 1

#: 持久化允许的队列状态（运行态 CACHING/PLAYING/ERROR 等不入盘，由对账收敛）
PERSISTED_STATES = ('UNWATCHED', 'CACHED', 'WATCHED')


def _writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, '.write-probe-%d' % os.getpid())
        with open(probe, 'wb') as fh:
            fh.write(b'1')
        os.remove(probe)
        return True
    except Exception:
        return False


def state_dir():
    """状态目录候选顺序：环境变量 → 用户数据目录 → 程序目录 → 临时目录

    用户数据目录跨平台（Windows %LOCALAPPDATA%，macOS ~/Library/Application Support）。
    """
    candidates = []
    env = os.environ.get('MCAPVIEWER_STATE_DIR')
    if env:
        candidates.append(os.path.abspath(env))
    candidates.append(os.path.join(appcache.user_data_dir(), 'state'))
    candidates.append(os.path.join(APP_DIR, 'state'))
    candidates.append(os.path.join(tempfile.gettempdir(), 'MCAPViewer', 'state'))
    for c in candidates:
        if _writable(c):
            return c
    return candidates[-1]


def folder_hash(folder):
    """文件夹路径 → 16 位稳定哈希（大小写/斜杠方向不敏感）"""
    norm = os.path.normcase(os.path.abspath(folder))
    return hashlib.sha1(norm.encode('utf-8')).hexdigest()[:16]


def folder_state_path(folder):
    return os.path.join(state_dir(), folder_hash(folder) + '.json')


def source_id(path):
    """条目唯一 id：路径 + 大小 + mtime_ns。源文件变化 = 新版本 = 新 id。"""
    return appcache.file_id(path)


def new_item(path):
    """一个条目的完整字段模板（未看）"""
    st = os.stat(path)
    return dict(
        path=os.path.abspath(path), name=os.path.basename(path),
        size=st.st_size, mtime_ns=st.st_mtime_ns,
        state='UNWATCHED',
        watched_at_ns=0, watched_reason='',
        last_position_s=0.0, cleanup_pending=False, error=None,
        duration_s=0.0,          # 视频时长（供定位合格率汇总）
        bad_segments=[],         # 人工标注的不合格片段 [[start_s, end_s], ...]
        bad_pending=None)        # 已按下第一下 X、等待第二下闭合的起点（秒）


def load_state(folder):
    """读取文件夹的持久化状态；损坏自动备份为 .corrupt-<ns> 并按空状态继续"""
    path = folder_state_path(folder)
    data = dict(version=STATE_VERSION, folder=os.path.abspath(folder), items={})
    try:
        with open(path, encoding='utf-8') as fh:
            raw = json.load(fh)
        if isinstance(raw, dict) and isinstance(raw.get('items'), dict):
            data['folder'] = raw.get('folder') or data['folder']
            for k, v in raw['items'].items():
                if isinstance(v, dict):
                    st = v.get('state')
                    if st in PERSISTED_STATES:
                        data['items'][str(k)] = dict(v)
    except FileNotFoundError:
        pass
    except Exception:
        try:
            backup = '%s.corrupt-%d' % (path, time.time_ns())
            os.replace(path, backup)
            data['corrupt_backup'] = backup
        except OSError:
            pass
    return data


def save_state(folder, data):
    """原子保存；失败只抛 OSError，由调用方决定是否提示（绝不让界面崩溃）"""
    data['version'] = STATE_VERSION
    data['folder'] = os.path.abspath(folder)
    os.makedirs(state_dir(), exist_ok=True)
    appcache.write_json_atomic(folder_state_path(folder), data)
    return folder_state_path(folder)
