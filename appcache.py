"""appcache.py —— 缓存路径、缓存版本与缓存完整性校验

设计要点
  * file_id 使用 st_mtime_ns（不截断到秒），同一秒内的多次修改也能区分。
  * CACHE_SCHEMA_VERSION 变化时，旧缓存整体判为无效并重建，而不是去删用户缓存。
  * 缓存先写在 staging 目录，全部成功后再整理发布；manifest 最后用
    临时文件 + os.replace 原子写入，避免留下「半截」缓存。
  * 缓存根目录按「程序目录 → %LOCALAPPDATA%\\MCAPViewer\\cache → 临时目录」
    的顺序自动选择：程序放在只读位置（Program Files、只读介质、网络盘）时也能跑。
"""

import os
import sys
import re
import json
import time
import shutil
import tempfile
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
# PyInstaller one-file 会把模块解压到临时 _MEI 目录；缓存绝不能写在那里，
# 否则程序退出后会被一起删除。冻结版优先使用 exe 所在目录。
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable))
           if getattr(sys, 'frozen', False) else HERE)

#: 缓存结构版本。任何会影响缓存内容 / 字段含义的改动都要 +1。
CACHE_SCHEMA_VERSION = 2

#: 缓存根目录的候选顺序（第一个可写的就是它）
CACHE_ROOT_CANDIDATES = []


def _writable(path):
    """目录能创建且能真正写入文件"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, '.write-probe-%d' % os.getpid())
        with open(probe, 'wb') as fh:
            fh.write(b'1')
        os.remove(probe)
        return True
    except Exception:
        return False


def user_data_dir(app='MCAPViewer', platform=None, home=None, environ=None):
    """跨平台用户数据目录（参数可注入，便于单元测试）：

    Windows  %LOCALAPPDATA%\\MCAPViewer
    macOS    ~/Library/Application Support/MCAPViewer
    Linux    $XDG_DATA_HOME/MCAPViewer 或 ~/.local/share/MCAPViewer
    """
    platform = sys.platform if platform is None else platform
    home = os.path.expanduser('~') if home is None else home
    environ = os.environ if environ is None else environ
    if platform == 'darwin':
        base = os.path.join(home, 'Library', 'Application Support')
    elif platform.startswith('win'):
        base = environ.get('LOCALAPPDATA') or os.path.join(
            home, 'AppData', 'Local')
    else:
        # Linux / 其它 POSIX
        base = environ.get('XDG_DATA_HOME') or os.path.join(
            home, '.local', 'share')
    return os.path.join(base, app)


def _pick_cache_root():
    candidates = []
    env = os.environ.get('MCAPVIEWER_CACHE')
    if env:
        candidates.append(os.path.abspath(env))
    candidates.append(os.path.join(APP_DIR, 'cache'))
    candidates.append(os.path.join(user_data_dir(), 'cache'))
    candidates.append(os.path.join(tempfile.gettempdir(), 'MCAPViewer', 'cache'))
    for c in candidates:
        if _writable(c):
            CACHE_ROOT_CANDIDATES.append(c)
            return c
    # 理论上到不了这里（临时目录一般可写）
    CACHE_ROOT_CANDIDATES.append(candidates[-1])
    return candidates[-1]


CACHE_ROOT = _pick_cache_root()

#: 缓存是否落到了程序目录之外（用于在界面/日志里提示用户）
CACHE_IS_EXTERNAL = os.path.normcase(CACHE_ROOT) != os.path.normcase(
    os.path.join(APP_DIR, 'cache'))

MANIFEST_NAME = 'manifest.json'


def file_id(path):
    """路径 + 大小 + mtime_ns 的摘要。任一项变化 id 就变化，缓存自然失效。"""
    try:
        st = os.stat(path)
        raw = '%s|%d|%d' % (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    except OSError:
        raw = os.path.abspath(path)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def cache_dir(fid):
    return os.path.join(CACHE_ROOT, fid)


def staging_dir(fid):
    return os.path.join(CACHE_ROOT, '.staging-%s' % fid)


def ensure_cache_root():
    os.makedirs(CACHE_ROOT, exist_ok=True)
    return CACHE_ROOT


def source_signature(path):
    st = os.stat(path)
    return dict(path=os.path.abspath(path), size=st.st_size, mtime_ns=st.st_mtime_ns)


def same_path(a, b):
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def write_json_atomic(path, obj):
    """先写临时文件再 os.replace，保证读到的 manifest 一定是完整的"""
    tmp = '%s.tmp.%d' % (path, os.getpid())
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, ensure_ascii=False)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def _nonempty_file(path):
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def required_files(man):
    """列出 manifest 声明的所有必须存在的缓存文件（相对缓存目录）"""
    names = []
    for cam in man.get('cameras') or []:
        if not cam.get('playable'):
            continue
        if cam.get('kind') == 'mp4' and cam.get('file'):
            names.append(cam['file'])
        elif cam.get('kind') == 'images' and cam.get('dir'):
            for fn in cam.get('frames_list') or []:
                names.append(os.path.join(cam['dir'], fn))
        if cam.get('source_raw'):
            names.append(cam['source_raw'])
        if cam.get('times_file'):
            names.append(cam['times_file'])
    a = man.get('audio')
    if a and a.get('file'):
        names.append(a['file'])
    if man.get('imu') and man['imu'].get('file'):
        names.append(man['imu']['file'])
    for name in man.get('extra_files') or []:
        names.append(name)
    return names


def validate_manifest(man, source_path, check_files=True):
    """校验缓存是否可用于 source_path。返回 (ok, reason)。

    校验项：schema 版本 / 来源绝对路径 / 文件大小 / mtime_ns / 所有声明文件存在且非空。
    任意一项不通过就判为无效，调用方负责安全重建。
    """
    if not isinstance(man, dict):
        return False, '缓存清单缺失或格式错误'
    ver = man.get('cache_schema_version')
    if ver != CACHE_SCHEMA_VERSION:
        return False, '缓存结构版本不匹配（缓存 %s，当前 %s）' % (ver, CACHE_SCHEMA_VERSION)
    if not man.get('cameras'):
        return False, '缓存里没有相机通道'
    # source 保留为字符串以兼容未改动的 server.py；详细签名单独存放。
    # 同时兼容短暂存在过的 v2 草稿（source 本身是 dict）。
    src = man.get('source_signature') or man.get('source') or {}
    if isinstance(src, str):
        src = dict(path=src,
                   size=(man.get('summary') or {}).get('size'),
                   mtime_ns=man.get('source_mtime_ns'))
    if not same_path(src.get('path'), source_path):
        return False, '缓存来源路径不一致'
    try:
        st = os.stat(source_path)
    except OSError:
        return False, '源文件已不存在'
    if src.get('size') != st.st_size:
        return False, '源文件大小已变化'
    if src.get('mtime_ns') != st.st_mtime_ns:
        return False, '源文件修改时间已变化'
    if not check_files:
        return True, ''
    root = man.get('_dir')
    if not root:
        return False, '缓存目录未知'
    for name in required_files(man):
        if not _nonempty_file(os.path.join(root, name)):
            return False, '缓存文件缺失或为空：%s' % name
    return True, ''


def load_manifest(cache_root_dir, source_path=None, check_files=True):
    """读取并校验 manifest；不通过返回 None"""
    path = os.path.join(cache_root_dir, MANIFEST_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as fh:
            man = json.load(fh)
    except Exception:
        return None
    man['_dir'] = cache_root_dir
    if source_path is not None:
        ok, _reason = validate_manifest(man, source_path, check_files=check_files)
        if not ok:
            return None
    return man


def discard_cache(fid):
    """安全丢弃某个缓存条目；返回 {'success': bool, 'freed_bytes': int, 'error': str|None}

    只删缓存根目录内的内容（commonpath 守卫）；删除后会确认目录确实消失，
    失败绝不谎报成功。
    """
    root = os.path.abspath(CACHE_ROOT)
    freed = 0
    for d in (cache_dir(fid), staging_dir(fid), cache_dir(fid) + '.staging'):
        a = os.path.abspath(d)
        try:
            inside = os.path.commonpath((root, a)) == root and a != root
        except ValueError:
            inside = False
        if not inside or not os.path.isdir(a):
            continue
        size = _dir_size(a)
        try:
            shutil.rmtree(a)
        except OSError as e:
            return dict(success=False, freed_bytes=0,
                        error='删除失败 %s: %s' % (a, e))
        if os.path.isdir(a):
            return dict(success=False, freed_bytes=0,
                        error='删除后目录仍存在：%s' % a)
        freed += size
    return dict(success=True, freed_bytes=freed, error=None)


# ================================================================== 精简缓存与 LRU 滚动清理
#: 桌面端最多保留的「视频缓存条目」数量；超出后按最久未使用（LRU）自动清理。
MAX_CACHE_ENTRIES = 3

#: 精简缓存配置名。桌面端只把界面实际显示的 camera2 / camera3 落盘成缓存，
#: 与 server.py 的完整缓存（全部相机通道）通过「fid@配置名」目录后缀分离：
#: 互不冲突、互不覆盖；旧版完整缓存作为 legacy 条目纳入同一个 LRU 台账，
#: 随滚动清理自然淘汰。台账与目录永远只会在缓存根目录内增删。
#:
#: v2（P1.6D-R2A）：桌面精简缓存不再包含音频（业务不需要声音）。
#: v3（P1.6D-R2B）：进一步不再包含 IMU（Desktop 只用于看视频）——
#: 不再选择 imu topic、不再 decode/collect、不生成 imu.json。
#: 因最终产物集合变化，profile key 必须与旧版分离，避免新旧缓存互相误用。
CACHE_PROFILE = 'desktop_camera2_camera3_videoonly_v3'

#: 产物里不含音频的配置名：只有这些 profile 允许跳过音频链路。
AUDIO_FREE_PROFILES = (CACHE_PROFILE,)

#: 产物里不含 IMU 的配置名：只有这些 profile 允许跳过 IMU 链路。
IMU_FREE_PROFILES = (CACHE_PROFILE,)

#: 当前活跃的桌面 profile（可复用、可登记、占桌面缓存槽位）
ACTIVE_DESKTOP_PROFILES = (CACHE_PROFILE,)

#: 已退休的桌面 profile（历史版本）。
#: 退休缓存：不得复用、不登记、不算 active 槽位；但允许进入安全 stale 清理
#: （程序启动 / 打开文件夹时的对账），避免升级后旧缓存永久占磁盘。
RETIRED_DESKTOP_PROFILES = ('desktop_camera2_camera3_v1',
                            'desktop_camera2_camera3_noaudio_v2')

#: LRU 台账文件（位于缓存根目录）：记录每个缓存条目的最近使用时间与磁盘占用。
LRU_FILE_NAME = '.cache_lru.json'
LRU_SCHEMA_VERSION = 1

_SEP = '@'


def cache_key(fid, profile=CACHE_PROFILE):
    """缓存条目的目录键：fid@配置名；profile 为空返回原始 fid（完整缓存布局）。"""
    fid = (fid or '').strip()
    if not fid or not profile:
        return fid
    if fid.endswith(_SEP + profile):
        return fid
    return fid + _SEP + profile


def profile_of(key):
    """从条目键取出配置名；没有后缀（旧版完整缓存）返回 'legacy-full'"""
    i = (key or '').rfind(_SEP)
    return key[i + 1:] if i >= 0 else 'legacy-full'


def profile_keeps_imu(profile=CACHE_PROFILE):
    """该缓存配置是否保留 IMU 产物（imu.json）。

    桌面精简缓存从 P1.6D-R2B 起不再包含 IMU（Desktop 只用于看视频）：
    IMU 不在订阅的 topic 里，也不会被 decode/collect/写盘。
    完整缓存（full/None）等其它配置保持原有 IMU 行为。
    """
    if profile in (None, ''):
        return True                    # 未声明 profile 视为完整缓存
    return profile not in IMU_FREE_PROFILES


def profile_keeps_audio(profile=CACHE_PROFILE):
    """该缓存配置是否保留音频产物（audio.wav）。

    桌面精简缓存（``CACHE_PROFILE``）从 P1.6D-R2A 起不再包含音频：
    音频既不在订阅的 topic 里，也不会被 decode/collect/写盘。
    server.py 的完整缓存（profile='full' / None）等其它配置保持原有音频行为，
    便于将来仍需要声音的场景（也保留了通用 Audio 代码路径）。
    """
    if profile in (None, ''):
        return True                    # 未声明 profile 视为完整缓存
    return profile not in AUDIO_FREE_PROFILES


def profile_keeps_topic(topic, profile=CACHE_PROFILE):
    """精简缓存的通道筛选：只保留 camera2 / camera3（桌面端仅显示这两路）。

    其他配置名一律全部保留。匹配规则与 desktop.is_primary_view 保持一致，
    放在 appcache 是为了避免桌面端为传一个判定函数而把 Qt 拖进 server/测试。
    """
    if profile != CACHE_PROFILE:
        return True
    text = (topic or '').lower()
    return re.search(r'(?:camera|cam)[_-]?(?:2|3)(?!\d)', text) is not None


def _dir_size(path):
    """目录总字节数（读不到的文件按 0 计）"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _lru_path():
    return os.path.join(CACHE_ROOT, LRU_FILE_NAME)


def _load_lru():
    """读取台账；缺失 / 损坏一律按空台账处理（自愈，不让清理机制卡死程序）"""
    data = dict(schema_version=LRU_SCHEMA_VERSION, entries={})
    try:
        with open(_lru_path(), encoding='utf-8') as fh:
            raw = json.load(fh)
        if isinstance(raw, dict) and isinstance(raw.get('entries'), dict):
            data['entries'] = {str(k): dict(v)
                               for k, v in raw['entries'].items()
                               if isinstance(v, dict)}
    except Exception:
        pass
    return data


def _save_lru(data):
    try:
        write_json_atomic(_lru_path(), data)
    except OSError:
        pass


#: 台账只管理桌面精简命名空间：严格匹配「16位hex@<CACHE_PROFILE>」。
#: server/full 缓存、legacy 无后缀缓存、普通文件夹、.staging/.old/.tmp、
#: runtime.building-* 等一律不登记、不收养、不删除。
#: 已退休的桌面 profile（如 v1）同样不登记、不算槽位，但由
#: purge_retired_desktop_caches() 在启动/打开文件夹时做安全 stale 清理。
import re as _re
_STRICT_ENTRY_RE = _re.compile(r'^[0-9a-f]{16}@' + _re.escape(CACHE_PROFILE) + r'$')

#: 已退休桌面条目的目录名匹配（16位hex@退休profile）
_RETIRED_ENTRY_RE = _re.compile(
    r'^[0-9a-f]{16}@(' + '|'.join(_re.escape(p)
                                  for p in RETIRED_DESKTOP_PROFILES) + r')$')


def retired_desktop_entries():
    """列出磁盘上「已退休桌面 profile」的缓存目录键（只列，不删）。

    只返回目录名精确匹配退休键的条目；.staging / .old / 临时目录等
    一律不单列（属于各自条目，随条目一并处理）。
    """
    out = []
    try:
        for name in os.listdir(CACHE_ROOT):
            if _RETIRED_ENTRY_RE.match(name) and os.path.isdir(
                    os.path.join(CACHE_ROOT, name)):
                out.append(name)
    except OSError:
        pass
    return out


def purge_retired_desktop_caches(running_jobs=()):
    """安全清理「已退休桌面 profile」的缓存（P1.6D-R2A 升级后的陈旧数据）。

    安全机制（满足"禁止直接 rmtree"的要求）：
      * 只删精确匹配退休键的目录（.staging/.old 等不单独碰）；
      * 全程持跨进程锁（exclusive_cache_lock）；
      * running_jobs 里正在使用的目录一律跳过（in-use protection）；
      * 删除后验证目录确实消失，失败保留、下次启动时重试；
      * 一切限制在缓存根目录内（commonpath 守卫）。
    running_jobs：正在运行的 prepare 任务键列表（调用方传 PREP.running_jobs()）。

    返回 dict(deleted=[(key, freed_bytes)], skipped=[key], failed=[(key, error)])
    """
    out = dict(deleted=[], skipped=[], failed=[])
    root = os.path.abspath(CACHE_ROOT)
    jobs = set(str(j) for j in (running_jobs or ()))
    for key in retired_desktop_entries():
        if key in jobs or any(j.startswith(key) for j in jobs):
            out['skipped'].append(key)
            continue
        target = os.path.abspath(os.path.join(root, key))
        try:
            inside = os.path.commonpath((root, target)) == root and target != root
        except ValueError:
            inside = False
        if not inside:
            out['skipped'].append(key)
            continue
        size = _dir_size(target)
        try:
            with exclusive_cache_lock(timeout=10.0):
                shutil.rmtree(target, ignore_errors=False)
        except (OSError, TimeoutError) as e:
            out['failed'].append((key, str(e)))
            continue
        if os.path.isdir(target):
            out['failed'].append((key, '删除后目录仍存在'))
            continue
        stale_stage = target + '.staging'
        if os.path.isdir(stale_stage):
            shutil.rmtree(stale_stage, ignore_errors=True)
        out['deleted'].append((key, size))
    return out


def _reconcile_lru(data):
    """台账与磁盘对齐（自愈，且只管理严格匹配的精简条目）：

    * 台账里的非精简键（历史版本可能收养过 legacy 目录）一律忘记，不删除；
    * 目录已消失的条目 → 从台账移除；
    * 磁盘上严格匹配命名空间、且目录内有 cache_profile 匹配的 manifest 的
      未登记目录 → 纳入管理（目录 mtime 作为最近使用时间）。
    """
    entries = data['entries']
    for key in list(entries.keys()):
        if not _STRICT_ENTRY_RE.match(key) \
                or not os.path.isdir(os.path.abspath(cache_dir(key))):
            entries.pop(key, None)
    try:
        names = os.listdir(CACHE_ROOT)
    except OSError:
        return
    for name in names:
        if name in entries or not _STRICT_ENTRY_RE.match(name):
            continue
        full = os.path.join(CACHE_ROOT, name)
        if not os.path.isdir(full):
            continue
        man = load_manifest(full, check_files=False)
        if not man or man.get('cache_profile') != CACHE_PROFILE:
            continue
        try:
            mt = os.path.getmtime(full)
        except OSError:
            mt = time.time()
        entries[name] = dict(key=name, fid=name.split(_SEP, 1)[0],
                             profile=CACHE_PROFILE, bytes=_dir_size(full),
                             last_used=mt, name=name, adopted=True)


def touch_cache(fid, source_path=None, name=None, profile=CACHE_PROFILE):
    """登记 / 刷新一个缓存条目的 LRU 触点。仅限桌面精简命名空间。

    条目目录不存在、或不匹配严格命名规则时不做任何事。返回条目 dict（副本）。
    """
    if not fid:
        return None
    key = cache_key(fid, profile)
    if not _STRICT_ENTRY_RE.match(key):
        return None
    root = os.path.abspath(cache_dir(key))
    if not os.path.isdir(root):
        return None
    data = _load_lru()
    e = dict(data['entries'].get(key) or {})
    e['key'] = key
    e['fid'] = fid
    e['profile'] = profile or 'legacy-full'
    e['last_used'] = time.time()
    e['bytes'] = _dir_size(root)
    if source_path:
        e['source'] = os.path.abspath(source_path)
        e['name'] = os.path.basename(source_path) or e.get('name') or key
    e.setdefault('name', key)
    data['entries'][key] = e
    _reconcile_lru(data)
    _save_lru(data)
    return dict(e)


def list_cache_entries():
    """返回当前台账里的全部缓存条目（先与磁盘对齐，按最近使用降序）"""
    data = _load_lru()
    _reconcile_lru(data)
    _save_lru(data)
    out = [dict(e) for e in sorted(data['entries'].values(),
                                   key=lambda x: x.get('last_used', 0.0),
                                   reverse=True)]
    return out


def cache_size(fid, profile=CACHE_PROFILE):
    """某个缓存条目的磁盘占用（字节）；条目不存在返回 0"""
    root = cache_dir(cache_key(fid, profile))
    return _dir_size(root) if os.path.isdir(root) else 0


def evict_cache(keep=(), dry_run=False):
    """按 LRU 滚动清理缓存，最多保留 MAX_CACHE_ENTRIES 个条目。

    keep: 绝不允许清理的 file_id 或条目键（当前打开 / 前台解析 / 后台预热）。
          传入 fid 会同时展开成「精简条目」和「完整条目」两个键，防止同名
          旧版完整缓存把正在用的文件删掉。
    dry_run: True 时只演算不删盘，返回将要清理的键。

    返回被清理（或将被清理）的条目键列表。
    禁止项（由实现保证，调用方无法绕过）：
      * keep 里的条目永不清理；
      * .staging / .old / .tmp 等进行中目录永不登记、永不清理；
      * 任何删除都限制在缓存根目录内（discard_cache 的 commonpath 守卫）。
    """
    keep_keys = set()
    for k in keep or ():
        if not k:
            continue
        keep_keys.add(k)
        keep_keys.add(cache_key(k))                 # 精简条目
        base = k.split(_SEP, 1)[0] if _SEP in k else k
        keep_keys.add(base)                         # 完整 / legacy 条目
    data = _load_lru()
    _reconcile_lru(data)
    entries = data['entries']
    excess = len(entries) - max(0, int(MAX_CACHE_ENTRIES))
    if excess <= 0:
        return []
    evicted = []
    for e in sorted(entries.values(), key=lambda x: x.get('last_used', 0.0)):
        if excess <= 0:
            break
        key = e.get('key') or ''
        if not key or key in keep_keys or e.get('fid') in keep_keys:
            continue
        if not dry_run:
            res = discard_cache(key)
            if not res.get('success'):
                continue          # 删除失败：条目保留在台账，改试下一个候选
            entries.pop(key, None)
        evicted.append(key)
        excess -= 1
    if evicted and not dry_run:
        _save_lru(data)
    return evicted


# ================================================================== 跨进程锁
try:
    import msvcrt                        # Windows
except ImportError:
    msvcrt = None
try:
    import fcntl                         # macOS / Linux
except ImportError:
    fcntl = None

import contextlib


@contextlib.contextmanager
def exclusive_cache_lock(timeout=10.0):
    """跨进程互斥锁：防止两个程序实例同时删除 / 修改同一份缓存。

    Windows 用标准库 msvcrt 的文件锁，macOS/Linux 用 fcntl.flock，
    锁文件都是缓存根下的 .cache.lock，不引入任何第三方依赖；
    都不可用时退化为直接放行。
    拿不到锁超时抛 TimeoutError，调用方按删除失败处理并稍后重试。
    """
    path = os.path.join(CACHE_ROOT, '.cache.lock')
    fh = open(path, 'a+b')
    locked = False
    try:
        if msvcrt is not None:
            deadline = time.time() + timeout
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.time() >= deadline:
                        raise TimeoutError('缓存锁等待超时（另一程序实例占用中）')
                    time.sleep(0.05)
        elif fcntl is not None:
            deadline = time.time() + timeout
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    if time.time() >= deadline:
                        raise TimeoutError('缓存锁等待超时（另一程序实例占用中）')
                    time.sleep(0.05)
        yield
    finally:
        if locked:
            try:
                if msvcrt is not None:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()
