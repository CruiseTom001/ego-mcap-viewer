"""direct_playback.py —— P1.6F-E：Desktop Direct 播放的线程化控制层

职责（F-E 第 16~23、62~65 节）：
  * Direct 的 chunk 读取 / 解压 / H264 解码**绝不在 GUI 主线程**执行；
  * **同一时刻最多 1 个未完成的 frame 请求**；新目标只覆盖 `latest_target`
    （不排队历史请求 → 天然丢弃过期请求）；
  * `generation` 令牌：seek / camera switch / 换文件 / close 时 +1，
    迟到的旧 generation 帧**不渲染**；
  * 提供 frame_requests / completed / dropped_stale / replaced / latency 统计。

本模块不含任何 UI 代码，便于离线（offscreen）测试；Desktop 只负责
把 frameReady 接到画面显示。
"""

from PySide6.QtCore import QMutex, QObject, QThread, QWaitCondition, Signal


class DirectFrameWorker(QThread):
    """后台取帧线程：只做 source.frame_at()，不在 GUI 线程做 I/O 与解码"""

    frameReady = Signal(int, object, float)      # generation, VideoFrame|None, latency_ms

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self.source = source
        self._mutex = QMutex()
        self._cond = QWaitCondition()
        self._pending = None          # (generation, media_time, camera)
        self._running = True
        self._busy = False

    # ---------------------------------------------------------------- 提交
    def submit(self, generation, media_time, camera=None):
        """提交请求；若正忙，只覆盖 latest（不排队）"""
        self._mutex.lock()
        try:
            self._pending = (generation, float(media_time), camera)
            self._cond.wakeAll()
        finally:
            self._mutex.unlock()

    def is_busy(self):
        self._mutex.lock()
        try:
            return self._busy
        finally:
            self._mutex.unlock()

    def take_pending(self):
        self._mutex.lock()
        try:
            item = self._pending
            self._pending = None
            return item
        finally:
            self._mutex.unlock()

    def stop(self):
        self._mutex.lock()
        try:
            self._running = False
            self._cond.wakeAll()
        finally:
            self._mutex.unlock()

    # ---------------------------------------------------------------- 线程体
    def run(self):
        import time as _t
        while True:
            self._mutex.lock()
            while self._running and self._pending is None:
                self._cond.wait(self._mutex, 50)
            if not self._running:
                self._mutex.unlock()
                return
            gen, t, cam = self._pending
            self._pending = None
            self._busy = True
            self._mutex.unlock()
            t0 = _t.perf_counter()
            fr = None
            try:
                if cam is not None and cam != self.source.active_camera():
                    fr = self.source.switch_camera(cam, t)
                else:
                    fr = self.source.frame_at(t)
            except Exception:
                fr = None
            ms = (_t.perf_counter() - t0) * 1000.0
            self._mutex.lock()
            self._busy = False
            self._mutex.unlock()
            try:
                self.frameReady.emit(gen, fr, ms)
            except RuntimeError:
                return                    # 对象已析构


class DirectPlaybackController(QObject):
    """把 Direct source 包装成"单请求 + generation 防回灌"的取帧控制器"""

    frameReady = Signal(object)          # VideoFrame（已通过 generation 校验）
    failure = Signal(str)

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self.source = source
        self.generation = 0
        self.latest_target = None
        self._worker = DirectFrameWorker(source, self)
        self._worker.frameReady.connect(self._on_frame)
        self._worker.start()
        self.stats = dict(frame_requests=0, frame_completed=0,
                          frame_dropped_stale=0, frame_request_replaced=0,
                          max_request_latency_ms=0.0, last_latency_ms=0.0,
                          failures=0)

    # ---------------------------------------------------------------- 请求
    def request(self, media_time, camera=None):
        """GUI tick / seek / switch 统一入口（不阻塞）"""
        self.latest_target = float(media_time)
        self.stats['frame_requests'] += 1
        if self._worker.is_busy():
            # 忙 → 只保留 latest（覆盖旧目标），等本轮完成后自动取最新
            self.stats['frame_request_replaced'] += 1
            self._worker.submit(self.generation, self.latest_target, camera)
        else:
            self._worker.submit(self.generation, self.latest_target, camera)

    def bump_generation(self):
        """seek / camera switch / 换文件 / close：让迟到帧失效"""
        self.generation += 1
        return self.generation

    def outstanding(self):
        return 1 if self._worker.is_busy() else 0

    def max_outstanding(self):
        return 1                      # 由单 pending + busy 标志保证

    # ---------------------------------------------------------------- 回调
    def _on_frame(self, gen, frame, latency_ms):
        self.stats['last_latency_ms'] = round(latency_ms, 1)
        self.stats['max_request_latency_ms'] = max(
            self.stats['max_request_latency_ms'], round(latency_ms, 1))
        if gen != self.generation:
            self.stats['frame_dropped_stale'] += 1
            return                    # 迟到帧：不渲染
        if frame is None:
            self.stats['failures'] += 1
            self.failure.emit('decode-failed')
            return
        self.stats['frame_completed'] += 1
        self.frameReady.emit(frame)

    def stop(self):
        try:
            self._worker.stop()
            self._worker.wait(3000)
        except Exception:
            pass
        try:
            self.source.close()
        except Exception:
            pass

    def backend_name(self):
        try:
            return self.source.stats().get('backend', 'unknown')
        except Exception:
            return 'unknown'


# ====================================================================== 选择策略
def select_backend(mcap_path, cache_dir=None, manifest=None, camera='camera2'):
    """F-E 冻结的 backend 选择策略（第 5 节）：

    ① 已有 videoonly_v3 MP4 cache → 'mp4'
    ② 无 cache 且 Indexed + Direct capability supported → 'direct'
    ③ 其余（no-index / Direct 不支持 / 探测失败）→ 'cache_required'
    """
    import os

    import appcache
    import video_source as VS
    cdir = cache_dir
    man = manifest
    if cdir is None or man is None:
        try:
            fid = appcache.file_id(mcap_path)
            cdir = cdir or appcache.cache_dir(appcache.cache_key(fid))
            man = man or appcache.load_manifest(cdir, source_path=mcap_path)
        except Exception:
            man = None
    if man:
        for cam_meta in (man.get('cameras') or []):
            if not (os.path.isfile(os.path.join(cdir or '', cam_meta.get('file') or ''))):
                man = None
                break
    if man:
        return ('mp4', None)
    cap = VS.direct_capability(mcap_path)
    if cap.supported and camera in (cap.cameras or ()):
        return ('direct', cap.reason)
    return ('cache_required', getattr(cap, 'reason', 'UNKNOWN'))


# ====================================================================== 会话层
class DirectPlaybackSession(QObject):
    """F-E1：Desktop 侧"打开即播"会话（可离线测试，UI 只负责显示）

    负责：
      * 后台打开 source（capability → open → 首帧），**全程不占 GUI 线程**；
      * 首帧就绪后才锚定媒体时钟（避免把打开耗时算进播放时间）；
      * tick(target) → 单请求 + latest 覆盖；seek/switch → generation++；
      * TTF 与主线程 stall 度量；close 幂等。

    信号：
      opened(backend, ttfp_ms)   首帧已就绪（UI 可开始显示）
      frameReady(VideoFrame)     当前 generation 的有效帧
      failed(reason)             打开失败（UI 应 fallback 到既有 cache 流程）
      stopped()
    """

    opened = Signal(str, float)
    frameReady = Signal(object)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, mcap_path, cache_dir=None, manifest=None, parent=None):
        super().__init__(parent)
        self.mcap_path = mcap_path
        self.cache_dir = cache_dir
        self.manifest = manifest
        self.backend = 'NONE'
        self.source = None
        self.controller = None
        self.generation = 0
        self.current_frame = None
        self.rendered_stale = 0
        self.resume_time = None
        self.metrics = dict(backend_select_ms=0.0, source_open_ms=0.0,
                            first_decode_ms=0.0, ttfp_ms=0.0,
                            max_tick_stall_ms=0.0)
        self._open_worker = None

    # ---------------------------------------------------------------- 打开
    def open_async(self, select_backend=True):
        """后台执行：backend 选择 + source open + 首帧（不阻塞 GUI）"""
        from PySide6.QtCore import QThread
        import time as _t

        class _Opener(QThread):
            done = Signal(object, object, float, float, float)

            def __init__(self, outer):
                super().__init__()
                self.outer = outer

            def run(self):
                o = self.outer
                t0 = _t.perf_counter()
                kind, reason = (DP_select(o.mcap_path, o.cache_dir, o.manifest)
                                if True else ('direct', ''))
                t_sel = (_t.perf_counter() - t0) * 1000.0
                if kind != 'direct':
                    self.done.emit(None, reason or kind, t_sel, 0.0, 0.0)
                    return
                try:
                    t1 = _t.perf_counter()
                    src = VSRC.McapDirectVideoSource(o.mcap_path).open()
                    t_open = (_t.perf_counter() - t1) * 1000.0
                    t2 = _t.perf_counter()
                    fr = src.frame_at(0.0)
                    t_dec = (_t.perf_counter() - t2) * 1000.0
                    self.done.emit(src, fr, t_sel, t_open, t_dec)
                except Exception as e:
                    self.done.emit(None, '%s: %s' % (type(e).__name__, e),
                                   t_sel, 0.0, 0.0)

        self._open_worker = _Opener(self)
        self._open_worker.done.connect(self._on_opened)
        self._open_worker.start()

    def _on_opened(self, src, payload, t_sel, t_open, t_dec):
        self.metrics['backend_select_ms'] = round(t_sel, 1)
        self.metrics['source_open_ms'] = round(t_open, 1)
        self.metrics['first_decode_ms'] = round(t_dec, 1)
        if src is None:
            self.backend = 'CACHE_REQUIRED'
            self.metrics['ttfp_ms'] = round(t_sel + t_open + t_dec, 1)
            self.failed.emit(str(payload))
            return
        self.source = src
        self.backend = 'MCAP_DIRECT'
        self.controller = DirectPlaybackController(src, self)
        self.controller.frameReady.connect(self._on_frame)
        self.controller.failure.connect(self._on_failure)
        self.current_frame = payload
        self.metrics['ttfp_ms'] = round(t_sel + t_open + t_dec, 1)
        self.opened.emit(self.backend, self.metrics['ttfp_ms'])
        self.frameReady.emit(payload)

    # ---------------------------------------------------------------- 播放
    def tick(self, target_media_time):
        """GUI tick：提交目标时间（单请求 + latest 覆盖）"""
        import time as _t
        if self.controller is None:
            return
        t0 = _t.perf_counter()
        self.controller.request(target_media_time)
        self.metrics['max_tick_stall_ms'] = max(
            self.metrics['max_tick_stall_ms'],
            round((_t.perf_counter() - t0) * 1000.0, 2))

    def seek(self, media_time):
        self.generation += 1
        if self.controller is not None:
            self.controller.bump_generation()
            self.controller.request(media_time)
        return self.generation

    def switch_camera(self, camera, media_time):
        self.generation += 1
        if self.controller is not None:
            self.controller.bump_generation()
            self.controller.request(media_time, camera=camera)
        return self.generation

    def _on_frame(self, frame):
        if frame is None:
            return
        self.current_frame = frame
        self.frameReady.emit(frame)

    def _on_failure(self, reason):
        self.resume_time = (self.current_frame.media_time
                            if self.current_frame else 0.0)
        self.failed.emit(reason)

    def duration(self):
        try:
            return self.source.duration() if self.source else 0.0
        except Exception:
            return 0.0

    def stats(self):
        st = dict(self.metrics)
        st['backend'] = self.backend
        st['generation'] = self.generation
        st['rendered_stale'] = self.rendered_stale
        if self.controller is not None:
            st.update({('ctl_' + k): v for k, v in self.controller.stats.items()})
        return st

    def close(self):
        self.generation += 1
        if self.controller is not None:
            try:
                self.controller.stop()
            except Exception:
                pass
            self.controller = None
        elif self.source is not None:
            try:
                self.source.close()
            except Exception:
                pass
        if self._open_worker is not None:
            try:
                self._open_worker.wait(3000)
            except Exception:
                pass
            self._open_worker = None
        self.source = None
        self.current_frame = None
        self.backend = 'NONE'
        self.stopped.emit()


def DP_select(mcap_path, cache_dir=None, manifest=None):
    return select_backend(mcap_path, cache_dir=cache_dir, manifest=manifest)


VSRC = None


def _ensure_vsrc():
    global VSRC
    if VSRC is None:
        import video_source as _v
        VSRC = _v
    return VSRC


# 延迟导入，避免循环依赖
import video_source as _vs_mod          # noqa: E402
VSRC = _vs_mod
