"""queue_manager.py —— 三队列缓存控制器（未看 / 已缓存 / 已看完）

这是缓存管理的唯一控制器，替代旧 LRU 的淘汰决策。用户界面只显示三个队列；
CACHING / ERROR / CLEANUP_PENDING 作为行内徽标，不单独成队。

状态机
    UNWATCHED → CACHING → CACHED → PLAYING → WATCHED
    WATCHED →(重新缓存)→ UNWATCHED → CACHING → CACHED → …

职责
    * 扫描对账（watchstate 持久化 + 磁盘缓存互相校正）
    * 已缓存槽位最多 MAX_CACHED_ITEMS=3（只算桌面精简 profile，
      当前播放计入槽位；server/full/legacy 缓存不计数、绝不删除）
    * 全程只运行一个缓存 Worker（前台点名可取消并插队）
    * 看完 → 停流 → 删桌面缓存 → 失败进 CLEANUP_PENDING 定时重试
    * 删除带跨进程锁（appcache.exclusive_cache_lock），
      PREP.running_jobs() 里的任务绝不删

界面通过信号刷新；MainWindow 注入 on_status / before_delete 两个回调。
"""

import os
import time

from PySide6.QtCore import QObject, QThread, QTimer, Signal

import appcache
import prepare as PREP
import watchstate
import playlist as PL

#: 用户可见队列容量（当前播放计入）
MAX_CACHED_ITEMS = 3

#: 删除失败后的重试间隔（测试可改小）
RETRY_DELAY_MS = 3000

#: 跨进程锁等待秒数（测试可改小）
LOCK_TIMEOUT = 10.0

# ---------------------------------------------------------------- 内部状态
UNWATCHED = 'UNWATCHED'
CACHING = 'CACHING'
CACHED = 'CACHED'
PLAYING = 'PLAYING'
WATCHED = 'WATCHED'
ERROR = 'ERROR'
#: CLEANUP_PENDING 不是独立状态：WATCHED + cleanup_pending 标志
CLEANUP_PENDING = 'CLEANUP_PENDING'

_USER_STATES = ('未看', '已缓存', '已看完')


class CacheWorker(QThread):
    """单个后台缓存任务；同一时刻全局最多一个在跑。

    done(对象, 是否成功) 在 finally 里必发 —— QueueManager 靠它清除引用，
    这是唯一的清除点，避免 isRunning() 竞态导致双 Worker 并发。
    """

    progressed = Signal(str, float, str)      # sid, 0..1, 消息
    finished_ok = Signal(str, dict)           # sid, manifest
    failed = Signal(str, str)                 # sid, traceback
    done = Signal(object, bool)               # worker 对象, 是否成功

    def __init__(self, sid, path, outdir, parent=None):
        super().__init__(parent)
        self.sid = sid
        self.path = path
        self.outdir = outdir
        import threading
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()
        self.requestInterruption()

    def run(self):
        ok = False
        try:
            man = PREP.prepare(
                self.path, self.outdir,
                progress=lambda p, m: self.progressed.emit(self.sid, p, m),
                cancel_event=self._cancel,
                camera_pred=appcache.profile_keeps_topic,
                profile=appcache.CACHE_PROFILE)
            if self._cancel.is_set():
                return
            self.finished_ok.emit(self.sid, man)
            ok = True
        except BaseException:
            if not self._cancel.is_set():
                import traceback
                self.failed.emit(self.sid, traceback.format_exc())
        finally:
            self.done.emit(self, ok)


class CacheQueueManager(QObject):
    queuesChanged = Signal()
    itemProgress = Signal(str, float, str)
    itemError = Signal(str, str)
    cacheDeleted = Signal(str, int)           # sid, 释放字节数
    currentReady = Signal(str)                # sid 缓存就绪（可播放）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.folder = ''
        self.items = {}          # sid -> 条目 dict
        self.order = []          # 自然顺序 sid（含已看完）
        self.current_sid = None  # 正在播放（PLAYING，计入槽位）
        self.requested_sid = None  # 用户点名（优先缓存/腾槽）
        self.worker = None
        self._no_auto = set()    # 用户取消过缓存的 sid：不自动重试，须手动再点
        self._paused = False     # 用户点过「清空」后暂停自动补槽，手动点名恢复
        self.on_status = None      # fn(msg, ms)
        self.before_delete = None  # fn(sid) -> None（主窗口停流并确认句柄释放）

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(300)
        self._save_timer.timeout.connect(self._save_now)

    # ---------------------------------------------------------------- 基础
    def _say(self, msg, ms=6000):
        if self.on_status:
            try:
                self.on_status(msg, ms)
            except Exception:
                pass

    def _outdir(self, sid):
        return appcache.cache_dir(appcache.cache_key(sid))

    def _cache_valid(self, sid, path):
        return PREP.load_manifest(self._outdir(sid), source_path=path) is not None

    def _cache_dir_exists(self, sid):
        return os.path.isdir(self._outdir(sid))

    def sid_of(self, path):
        return watchstate.source_id(path)

    # ---------------------------------------------------------------- 装载
    def load_folder(self, folder, paths):
        """扫描结果进入三队列并对账。paths：按自然顺序的绝对路径列表"""
        # 退休桌面 profile（如 noaudio 升级前的 v1）的 stale 清理：
        # 走跨进程锁 + in-use 保护，正在使用的目录跳过、失败留下次重试
        try:
            appcache.purge_retired_desktop_caches(PREP.running_jobs())
        except Exception:
            pass
        self.folder = folder
        self._paused = False             # 换文件夹恢复自动补槽
        state = watchstate.load_state(folder)
        old = state.get('items') or {}
        self.items, self.order = {}, []
        for p in paths:
            try:
                sid = watchstate.source_id(p)
            except OSError:
                continue            # 文件不可访问：跳过，不动原始文件
            it = watchstate.new_item(p)
            it['sid'] = sid
            # 直读格式（MP4 等）：不需要缓存封装，直接读原文件播放
            it['direct'] = PL.is_direct_format(it['name'])
            rec = old.get(sid)
            if rec:
                # 继承持久化状态；源文件变化 → sid 不同 → 天然是新版本
                for k in ('state', 'watched_at_ns', 'watched_reason',
                          'last_position_s', 'cleanup_pending', 'error',
                          'duration_s', 'bad_segments', 'bad_pending'):
                    if k in rec:
                        it[k] = rec[k]
                # 入盘状态只可能是三种；其余（上次崩溃残留的运行态）按未看处理
                if it['state'] not in watchstate.PERSISTED_STATES:
                    it['state'] = UNWATCHED
                if it['state'] == WATCHED and not it.get('cleanup_pending'):
                    it['cleanup_pending'] = False
            self.items[sid] = it
            self.order.append(sid)
        self.current_sid = None
        self.requested_sid = None
        self._no_auto = set()
        self._reconcile()
        self._save_soon()
        self.queuesChanged.emit()
        self.ensure_slots()

    def _reconcile(self):
        """七、启动对账：持久化状态与磁盘缓存互相校正"""
        for sid in self.order:
            it = self.items[sid]
            if it['state'] == WATCHED or it.get('cleanup_pending'):
                it['state'] = WATCHED
                if self._cache_dir_exists(sid):
                    # 残留缓存：安排安全删除（删除失败则保持 cleanup_pending）
                    self._finish_delete(sid)
                continue
            if it.get('direct'):
                # 直读文件：没有缓存概念，只要求源文件还在
                it['state'] = CACHED if os.path.isfile(it['path']) else UNWATCHED
                continue
            if self._cache_valid(sid, it['path']):
                it['state'] = CACHED
            else:
                it['state'] = UNWATCHED
        # 上次异常退出留下的 CACHING：磁盘上只可能有 staging 残留，
        # 对账时条目已被收敛为 UNWATCHED/CACHED；staging 由 discard 统一清理。
        for sid, it in self.items.items():
            if it['state'] == CACHING:
                it['state'] = UNWATCHED

    # ---------------------------------------------------------------- 查询
    def queues(self):
        """{'unwatched': [item], 'cached': [item], 'watched': [item]}（自然顺序）"""
        g = {'unwatched': [], 'cached': [], 'watched': []}
        for sid in self.order:
            it = self.items[sid]
            st = it['state']
            if st in (CACHED, PLAYING):
                g['cached'].append(it)
            elif st == WATCHED or it.get('cleanup_pending'):
                g['watched'].append(it)
            else:
                g['unwatched'].append(it)     # UNWATCHED / CACHING / ERROR
        return g

    def cached_count(self):
        """占用缓存槽位的数量（直读的 MP4 不占槽位、不参与淘汰）"""
        return len([it for it in self.queues()['cached'] if not it.get('direct')])

    def cached_sids(self):
        return [it['sid'] for it in self.queues()['cached']
                if not it.get('direct')]

    def cached_sids_in_order(self):
        return [it['sid'] for it in self.queues()['cached']
                if not it.get('direct')]

    def is_direct(self, sid):
        """True = 直读格式（MP4 等），不需要缓存封装"""
        it = self.items.get(sid)
        return bool(it and it.get('direct'))

    def next_cached_sid(self, after=None):
        """自动连播：已缓存队列里按自然顺序的下一个（可不含 after）"""
        for it in self.queues()['cached']:
            if it['sid'] != after:
                return it['sid']
        return None

    def next_cached_sid_after(self, sid):
        """自然顺序里排在 sid 之后的第一个已缓存项"""
        try:
            i = self.order.index(sid)
        except ValueError:
            return self.next_cached_sid()
        for j in range(i + 1, len(self.order)):
            if self.items[self.order[j]]['state'] in (CACHED, PLAYING):
                return self.order[j]
        return None

    def previous_cached_sid_before(self, sid):
        """自然顺序里排在 sid 之前的第一个已缓存项"""
        try:
            i = self.order.index(sid)
        except ValueError:
            return None
        for j in range(i - 1, -1, -1):
            if self.items[self.order[j]]['state'] in (CACHED, PLAYING):
                return self.order[j]
        return None

    def next_playable_sid(self, current_sid, direction):
        """上一个/下一个导航目标：跳过已看完；优先已缓存；否则方向上最近的未看。

        返回 None 表示这个方向没有可去的目标。
        """
        if current_sid in self.order:
            i = self.order.index(current_sid)
        else:
            i = -1 if direction > 0 else len(self.order)
        first_uncached = None
        rng = (range(i + direction, len(self.order), direction) if direction > 0
               else range(i + direction, -1, -1))
        for j in rng:
            sid = self.order[j]
            st = self.items[sid]['state']
            if st in (CACHED, PLAYING):
                return sid
            if first_uncached is None and st in (UNWATCHED, CACHING, ERROR):
                first_uncached = sid
        return first_uncached

    def has_unwatched(self):
        return bool(self.queues()['unwatched'])

    def cache_usage_bytes(self):
        total = 0
        for sid in self.cached_sids():
            total += appcache.cache_size(sid)
        return total

    # ---------------------------------------------------------------- 状态迁移
    def set_current(self, sid):
        """标记正在播放（PLAYING 计入已缓存槽位）"""
        if self.current_sid and self.current_sid in self.items \
                and self.items[self.current_sid]['state'] == PLAYING:
            self.items[self.current_sid]['state'] = CACHED
        self.current_sid = sid
        if sid and self.items.get(sid, {}).get('state') in (CACHED, PLAYING):
            self.items[sid]['state'] = PLAYING
        self._save_soon()
        self.queuesChanged.emit()

    def update_position(self, sid, t_s):
        it = self.items.get(sid)
        if it is not None:
            it['last_position_s'] = float(t_s)

    def mark_watched(self, sid, reason, duration_s=None):
        """三、看完（natural_end / manual）。重复调用幂等。

        duration_s：视频总时长（秒），供定位合格率汇总使用。
        没播放过就直接标记看完（duration 缺失）时，这里会自己补：
        直读文件用 probe，MCAP 在删缓存之前读一次 manifest。
        """
        it = self.items.get(sid)
        if not it or it['state'] in (WATCHED, CLEANUP_PENDING) \
                or it.get('cleanup_pending'):
            return
        if duration_s is not None:
            it['duration_s'] = float(duration_s or 0.0)
        if float(it.get('duration_s') or 0.0) <= 0:
            self._fill_duration(it)          # 必须在删缓存之前
        if self.current_sid == sid:
            self.current_sid = None
        it['state'] = WATCHED
        it['watched_at_ns'] = time.time_ns()
        it['watched_reason'] = reason
        it['progress'] = 0.0
        it['progress_msg'] = ''
        self.requested_sid = None if self.requested_sid == sid else self.requested_sid
        self._save_soon()
        self.queuesChanged.emit()
        if self._cache_dir_exists(sid):
            self._finish_delete(sid)
        self.ensure_slots()

    def _fill_duration(self, it):
        """补一个视频的真实时长（没播放过就标看完时用；失败保持 0，不抛异常）"""
        try:
            if it.get('direct'):
                info = PL.probe_mp4(it['path'])
                if info:
                    it['duration_s'] = float(info['duration_s'])
                    return True
                return False
            man = PREP.load_manifest(self._outdir(it['sid']),
                                     source_path=it['path'])
            if man:
                it['duration_s'] = float(man.get('duration_s') or 0.0)
                return True
        except Exception:
            pass
        return False

    def _safe_delete(self, sid):
        """带跨进程锁与运行任务保护的删除；返回 appcache.discard_cache 结果"""
        target = os.path.abspath(self._outdir(sid))
        try:
            jobs = {os.path.abspath(j) for j in PREP.running_jobs()}
        except Exception:
            jobs = set()
        if target in jobs:
            return dict(success=False, freed_bytes=0, error='缓存任务仍在进行')
        if self.current_sid == sid:
            return dict(success=False, freed_bytes=0, error='正在播放，禁止删除')
        if self.before_delete:
            try:
                self.before_delete(sid)
            except Exception:
                pass
        try:
            with appcache.exclusive_cache_lock(timeout=LOCK_TIMEOUT):
                return appcache.discard_cache(appcache.cache_key(sid))
        except Exception as e:
            return dict(success=False, freed_bytes=0, error=str(e))

    def _finish_delete(self, sid):
        """看完后的缓存删除；失败进 CLEANUP_PENDING 并定时重试，不谎报成功"""
        it = self.items.get(sid)
        if it is None:
            return
        res = self._safe_delete(sid)
        if res.get('success'):
            it['cleanup_pending'] = False
            it['error'] = None
            self.cacheDeleted.emit(sid, int(res.get('freed_bytes') or 0))
        else:
            it['cleanup_pending'] = True
            it['error'] = res.get('error')
            self._say('缓存仍被占用，将稍后重试释放：%s' % it['name'])
            QTimer.singleShot(RETRY_DELAY_MS,
                              lambda: self._retry_cleanup(sid))
        self._save_soon()
        self.queuesChanged.emit()

    def _retry_cleanup(self, sid):
        it = self.items.get(sid)
        if it is None or not it.get('cleanup_pending'):
            return
        if self._cache_dir_exists(sid):
            self._finish_delete(sid)
        else:
            it['cleanup_pending'] = False
            it['error'] = None
            self._save_soon()
            self.queuesChanged.emit()

    # ---------------------------------------------------------------- 缓存调度
    def ensure_slots(self):
        """已缓存不足 3 个时，按自然顺序（或用户点名优先）补缓存；单 Worker。

        引用非空即视为有任务在跑（哪怕线程还没真正启动）——
        引用只在 _on_worker_done（done 信号 finally 必发）里清除，杜绝并发双跑。

        ``_paused``：用户点过「清空队列(与缓存)」后暂停自动补槽——清空就要真的
        清空，不能立刻又把前几个缓存回来；用户手动双击/点「缓存」时自动恢复。
        """
        if self._paused:
            return
        if self.worker is not None:
            return
        if self.cached_count() >= MAX_CACHED_ITEMS:
            return
        target = None
        req = self.items.get(self.requested_sid)
        if req is not None and req['state'] in (UNWATCHED, ERROR):
            target = self.requested_sid      # 用户显式点名（含重试缓存）
        else:
            # 自动补槽只挑未看；失败的（ERROR）必须等用户点「重试缓存」，
            # 否则坏文件会在这里无限热重试。用户取消过的 sid 也不自动重试。
            # 直读格式（MP4）不需要缓存，永远不参与补槽。
            for sid in self.order:
                it = self.items[sid]
                if it.get('direct'):
                    continue
                if it['state'] == UNWATCHED and sid not in self._no_auto:
                    target = sid
                    break
        if target is not None:
            self._start_cache(target)

    def request_cache(self, sid):
        """未看项的「缓存」按钮：排队并尽量立即开始（直读文件无需缓存）"""
        it = self.items.get(sid)
        if not it or it.get('direct') \
                or it['state'] in (CACHING, CACHED, PLAYING, WATCHED):
            return
        self._paused = False             # 用户手动点名：恢复自动补槽
        self._no_auto.discard(sid)
        self.requested_sid = sid
        self.ensure_slots()

    def cancel_cache(self, sid):
        """用户取消缓存任务：回到未看，且不自动重试（须手动再点）"""
        if self.requested_sid == sid:
            self.requested_sid = None
        self._no_auto.add(sid)
        it = self.items.get(sid)
        if it is None:
            return
        if it['state'] == CACHING and self.worker is not None \
                and self.worker.sid == sid:
            self.worker.cancel()      # done 信号里统一收尾：CACHING→未看
        elif it['state'] == CACHING:
            it['state'] = UNWATCHED
        self._say('已取消缓存：%s' % it['name'])
        self.queuesChanged.emit()

    def request_play(self, sid):
        """用户点名播放未缓存项：必要时腾出最旧槽位（绝不动当前播放）"""
        it = self.items.get(sid)
        if not it:
            return False
        if it.get('direct'):
            return True                  # 直读文件：无需缓存，直接可播
        if it['state'] in (CACHED, PLAYING) and self._cache_valid(sid, it['path']):
            return True
        if it['state'] == WATCHED:
            self._say('该文件已看完，缓存已删除；请点「重新缓存」')
            return False
        self._paused = False             # 用户手动点名：恢复自动补槽
        self.requested_sid = sid
        slots = self.cached_sids()
        if len(slots) >= MAX_CACHED_ITEMS:
            victim = self._oldest_cached(exclude={sid, self.current_sid})
            if victim is None:
                self._say('3 个缓存槽位都被占用且无法腾出，请稍后再试')
                return False
            if not self._evict_to_unwatched(victim):
                return False
        self.ensure_slots()
        return None                 # None = 已受理，等 currentReady

    def _oldest_cached(self, exclude=()):
        """最早进入已缓存的条目（cached_at 最小），排除指定 sid 与直读项"""
        best, best_t = None, None
        for it in self.queues()['cached']:
            sid = it['sid']
            if sid in exclude or it.get('direct'):
                continue
            t = it.get('cached_at') or 0
            if best is None or t < best_t:
                best, best_t = sid, t
        return best

    def _evict_to_unwatched(self, sid):
        """腾槽位：删除桌面缓存并移回未看；失败返回 False（槽位保持不变）"""
        it = self.items.get(sid)
        if it is None:
            return False
        res = self._safe_delete(sid)
        if not res.get('success'):
            it['error'] = res.get('error')
            self._say('腾出槽位失败：%s' % res.get('error'))
            self.queuesChanged.emit()
            return False
        it['state'] = UNWATCHED
        it['progress'] = 0.0
        it['progress_msg'] = ''
        it['cached_at'] = 0
        it['error'] = None
        self._save_soon()
        self.queuesChanged.emit()
        return True

    def _start_cache(self, sid):
        it = self.items[sid]
        it['state'] = CACHING
        it['progress'] = 0.01
        it['progress_msg'] = '排队中'
        it['error'] = None
        self.worker = CacheWorker(sid, it['path'], self._outdir(sid), self)
        self.worker.progressed.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_cached_ok)
        self.worker.failed.connect(self._on_cache_fail)
        self.worker.done.connect(self._on_worker_done)
        self.worker.start()
        self.queuesChanged.emit()
        self._say('正在缓存 %s …' % it['name'])

    def _on_progress(self, sid, p, msg):
        it = self.items.get(sid)
        if it is not None:
            it['progress'] = float(p)
            it['progress_msg'] = msg
        self.itemProgress.emit(sid, float(p), msg)

    def _on_cached_ok(self, sid, _man):
        it = self.items.get(sid)
        if it is not None:
            it['state'] = CACHED
            it['progress'] = 1.0
            it['progress_msg'] = ''
            it['cached_at'] = time.time_ns()
            it['error'] = None
        self._save_soon()
        self.queuesChanged.emit()
        self.currentReady.emit(sid)
        self.ensure_slots()

    def _on_cache_fail(self, sid, tb):
        it = self.items.get(sid)
        if it is not None:
            it['state'] = ERROR
            it['progress'] = 0.0
            first = tb.strip().splitlines()[-1] if tb.strip() else '缓存失败'
            it['error'] = first[:300]
        # 点名缓存失败：清除点名。否则 ensure_slots 会立刻再次拾取 ERROR 项，
        # 形成对坏文件的无限热重试；规范要求失败后停在「未看+重试按钮」。
        if self.requested_sid == sid:
            self.requested_sid = None
        self.itemError.emit(sid, it['error'] if it else tb)
        self._say('缓存失败：%s' % (it['error'] if it else ''), 10000)
        self.queuesChanged.emit()

    def _on_worker_done(self, worker, ok):
        """Worker 收尾（含取消）：清除引用；CACHING 残留状态收回未看，再继续补槽"""
        if self.worker is worker:
            self.worker = None
        it = self.items.get(worker.sid)
        if it is not None and it['state'] == CACHING and not ok:
            it['state'] = UNWATCHED
            it['progress'] = 0.0
            it['progress_msg'] = ''
        self.queuesChanged.emit()
        self.ensure_slots()

    def request_recache(self, sid):
        """四、重新缓存：已看完 → 未看，并尽量立即开始"""
        it = self.items.get(sid)
        if not it or it['state'] != WATCHED:
            return
        it['state'] = UNWATCHED
        it['watched_at_ns'] = 0
        it['watched_reason'] = ''
        it['last_position_s'] = 0.0
        it['cleanup_pending'] = False
        it['error'] = None
        self._save_soon()
        self.queuesChanged.emit()
        self.request_cache(sid)

    # ---------------------------------------------------------------- 不合格标记
    def update_markers(self, sid, segments=None, pending='__keep__'):
        """保存人工标注的不合格片段（两下 X 的状态机由 markers.py 驱动）"""
        it = self.items.get(sid)
        if it is None:
            return
        if segments is not None:
            it['bad_segments'] = [[float(a), float(b)] for a, b in segments]
        if pending != '__keep__':
            it['bad_pending'] = None if pending is None else float(pending)
        self._save_soon()

    def markers_of(self, sid):
        it = self.items.get(sid) or {}
        return (it.get('bad_segments') or []), it.get('bad_pending')

    # ---------------------------------------------------------------- 文件夹汇总
    def folder_all_watched(self):
        """该文件夹的每个视频都已看完（没有任何未看/缓存中/失败项）"""
        if not self.order:
            return False
        return all(self.items[sid]['state'] == WATCHED for sid in self.order)

    def summary_items(self):
        """供定位合格率报告使用：每个视频的名称/路径/时长/不合格片段。

        直读的 MP4 没播过就没记时长，这里补一次探测（只探测缺时长的项）。
        """
        out = []
        changed = False
        for sid in self.order:
            it = self.items[sid]
            dur = float(it.get('duration_s') or 0.0)
            if dur <= 0 and it.get('direct'):
                info = PL.probe_mp4(it['path'])
                if info:
                    dur = float(info['duration_s'])
                    it['duration_s'] = dur
                    changed = True
            out.append(dict(name=it['name'], path=it['path'],
                            duration_s=dur,
                            segments=it.get('bad_segments') or []))
        if changed:
            self._save_soon()
        return out

    # ---------------------------------------------------------------- 清空
    def clear_all(self):
        """清空三个队列，并删除当前文件夹里所有 MCAP 的桌面缓存。

        **契约（用户明确）**：
          * 不管从哪个入口点清空，都是「三个队列全部清空 + 当前文件夹所有缓存全删」，
            正在播放的那一项也不是例外；清空后**任何队列里都不再显示条目**
            （不会退回「未看」队列堆着），需要重新开始就点「重新扫描文件夹」；
          * 已经写进状态文件的观看记录会被重置为未看，但**人工标注（不合格片段）
            与时长的历史记录保留**，不随清空丢失；
          * 删不掉的项（被别的程序占用句柄等）会保留原状态并如实回报，
            绝不出现「界面显示未看、磁盘上缓存还在」的假清空。

        调用方（MainWindow）会先停播放并 set_current(None)，因此正常路径下
        全部项目都能删除成功。返回 {'deleted', 'failed', 'kept'}。
        """
        kept, deleted, failed = [], [], []
        self._paused = True              # 清空后不自动补槽
        # 正在播放的那一项也要清：先让主窗口停流（before_delete 钩子），
        # 释放文件句柄后再删，避免 Windows 上因占用而删不掉
        if self.current_sid:
            if self.before_delete:
                try:
                    self.before_delete(self.current_sid)
                except Exception:
                    pass
            self.current_sid = None
        w = self.worker
        if w is not None:
            try:
                w.cancel()
            except Exception:
                pass
            # 等线程真正退出：否则它可能在清空之后才发布 manifest，
            # 让刚清掉的项又变回「已缓存」
            try:
                if w.isRunning():
                    w.wait(10000)
            except Exception:
                pass
        for sid in list(self.order):
            if not self._cache_dir_exists(sid):
                continue                 # 没有缓存目录（含 MP4 直读）＝已经干净
            res = self._safe_delete(sid)
            if not res.get('success'):
                # 删不掉：保留原状态，避免"假清空"
                failed.append((sid, res.get('error')))
                kept.append(sid)
                continue
            deleted.append(sid)
        # 状态落盘：能清的项记为未看（观看进度清零），标注/时长字段保持不动
        for sid in self.order:
            it = self.items[sid]
            if sid in kept:
                continue
            it['state'] = UNWATCHED
            it['watched_at_ns'] = 0
            it['watched_reason'] = ''
            it['last_position_s'] = 0.0
            it['cleanup_pending'] = False
            it['error'] = None
            it['progress'] = 0.0
            it['progress_msg'] = ''
            it['cached_at'] = 0
        self.requested_sid = None
        self._no_auto = set()
        self.flush()
        # 队列本身也清空：三个队列都变成空的，不再显示任何条目
        if kept:
            self.items = {sid: self.items[sid] for sid in kept}
            self.order = list(kept)
        else:
            self.items, self.order = {}, []
        self.queuesChanged.emit()
        # 注意：这里不调 ensure_slots()——清空后保持全空，等用户重新扫描/点名
        return dict(deleted=deleted, failed=failed, kept=kept)

    # ---------------------------------------------------------------- 持久化
    def _serialize(self):
        items = {}
        for sid, it in self.items.items():
            if it['state'] in (CACHED, PLAYING):
                st = 'CACHED'
            elif it['state'] == WATCHED or it.get('cleanup_pending'):
                st = 'WATCHED'
            else:
                st = 'UNWATCHED'
            items[sid] = dict(
                path=it['path'], name=it['name'], size=it['size'],
                mtime_ns=it['mtime_ns'], state=st,
                watched_at_ns=int(it.get('watched_at_ns') or 0),
                watched_reason=it.get('watched_reason') or '',
                last_position_s=float(it.get('last_position_s') or 0.0),
                cleanup_pending=bool(it.get('cleanup_pending')),
                error=it.get('error'),
                duration_s=float(it.get('duration_s') or 0.0),
                bad_segments=[list(s) for s in (it.get('bad_segments') or [])],
                bad_pending=(None if it.get('bad_pending') is None
                             else float(it['bad_pending'])))
        return items

    def _save_soon(self):
        self._save_timer.start()

    def _save_now(self):
        if not self.folder:
            return
        try:
            self.state_data = dict(items=self._serialize())
            watchstate.save_state(self.folder, self.state_data)
        except OSError:
            pass

    def flush(self):
        self._save_now()

    # ---------------------------------------------------------------- 关闭
    def shutdown(self):
        """关窗时：取消缓存任务并入队保存（线程由主窗口统一 wait）"""
        if self.worker is not None:
            try:
                self.worker.cancel()
            except Exception:
                pass
        self._save_now()
