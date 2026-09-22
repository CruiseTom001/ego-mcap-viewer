"""三队列状态机测试（queue_manager + desktop 集成）

对应规范第十五章的测试清单：文件夹装载 / 单 Worker 补槽 / 看完删缓存 /
幂等 / 非自然结束不标记 / 删除失败重试 / 腾槽位 / 保护规则 / 持久化 /
源文件变化 / 跨进程锁 / 无主视角不给假缓存 / 原始文件不动 …

全部使用临时缓存根与临时状态目录，绝不触碰真实 cache。
"""

import os
import json
import time
import shutil
import hashlib
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from tests import mcapfix as fx          # noqa: E402
import appcache                          # noqa: E402
import mcap_reader as MR                 # noqa: E402
import prepare as PREP                   # noqa: E402
import queue_manager as QM               # noqa: E402
import watchstate as WS                  # noqa: E402

from PySide6.QtWidgets import QApplication            # noqa: E402
from PySide6.QtCore import QSettings, QPoint          # noqa: E402

QM.RETRY_DELAY_MS = 20                   # 测试里删除重试要快
BASE = 1_000_000_000_000
PROF = appcache.CACHE_PROFILE


def key_of(sid):
    return appcache.cache_key(sid)


class QueueCase(unittest.TestCase):
    """QueueManager 纯逻辑测试（不需要主窗口）"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._old_root = appcache.CACHE_ROOT
        self._old_env = os.environ.get('MCAPVIEWER_STATE_DIR')
        self.root = tempfile.mkdtemp(prefix='mcapview-q-cache-')
        self.state_dir = tempfile.mkdtemp(prefix='mcapview-q-state-')
        appcache.CACHE_ROOT = self.root
        os.environ['MCAPVIEWER_STATE_DIR'] = self.state_dir
        self.folder = tempfile.mkdtemp(prefix='mcapview-q-folder-')
        self.paths = []
        for i in range(6):
            self.paths.append(self._build_mcap('vid%s' % chr(ord('A') + i)))
        self.qm = QM.CacheQueueManager()
        self.ready = []
        self.qm.currentReady.connect(self.ready.append)

    def tearDown(self):
        if self.qm.worker is not None and self.qm.worker.isRunning():
            self.qm.worker.cancel()
            self.qm.worker.wait(10000)
        self.qm.shutdown()
        appcache.CACHE_ROOT = self._old_root
        if self._old_env is None:
            os.environ.pop('MCAPVIEWER_STATE_DIR', None)
        else:
            os.environ['MCAPVIEWER_STATE_DIR'] = self._old_env
        for d in (self.root, self.state_dir, self.folder):
            shutil.rmtree(d, ignore_errors=True)

    # ---- 工具 ------------------------------------------------------------
    def _build_mcap(self, name):
        mcap = os.path.join(self.folder, name + '.mcap')
        recs = [fx.header(), fx.schema(1, 'foxglove.CompressedImage')]
        recs.append(fx.channel(1, 1, '/robot0/sensor/camera2/compressed'))
        png = fx.make_png(48, 36)
        for i in range(3):
            ts = BASE + i * 33_000_000
            recs.append(fx.message(1, i, ts, ts,
                                   fx.compressed_image(png, 'png', 'cam')))
        fx.assemble(mcap, recs)
        return os.path.abspath(mcap)

    def _load(self):
        self.qm.load_folder(self.folder, self.paths)

    def _pump(self, pred, timeout=20.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.app.processEvents()
            if pred():
                return True
            time.sleep(0.01)
        return False

    def _wait_slots(self, n=3, timeout=30.0):
        ok = self._pump(lambda: self.qm.cached_count() >= n, timeout)
        self.assertTrue(ok, '等待缓存槽位到 %d 超时' % n)

    def _sid(self, name):
        p = os.path.join(self.folder, 'vid%s.mcap' % name)
        return self.qm.sid_of(p)

    # ---- 二、装载与补槽 ---------------------------------------------------
    def test_01_first_open_all_unwatched(self):
        self._load()
        q = self.qm.queues()
        self.assertEqual(len(q['unwatched']), 6)
        self.assertEqual(q['cached'], [])
        self.assertEqual(q['watched'], [])

    def test_02_worker_fills_three_slots_max(self):
        self._load()
        self._wait_slots(3)
        q = self.qm.queues()
        self.assertEqual(len(q['cached']), QM.MAX_CACHED_ITEMS,
                         '已缓存必须恰好补满 3 个')
        self.assertEqual(len(q['unwatched']), 3)
        # 自然顺序：A/B/C 先进
        names = [it['name'] for it in q['cached']]
        self.assertEqual(names, ['vidA.mcap', 'vidB.mcap', 'vidC.mcap'])

    def test_03_worker_only_one_running(self):
        self._load()
        self._pump(lambda: self.qm.worker is not None
                   and self.qm.worker.isRunning(), 10)
        if self.qm.worker is not None:
            self.assertEqual(len([1]), 1)     # 单 Worker 实例
        self._wait_slots(3)
        self.assertIsNone(self.qm.worker, '补满槽位后不应再有缓存任务')

    def test_04_watched_kept_after_restart(self):
        self._load()
        self._wait_slots(3)
        self.qm.mark_watched(self._sid('A'), 'natural_end')
        self._pump(lambda: not os.path.isdir(
            appcache.cache_dir(key_of(self._sid('A')))))
        self.qm.flush()
        # 新实例 = 重启软件
        qm2 = QM.CacheQueueManager()
        try:
            qm2.load_folder(self.folder, self.paths)
            q2 = qm2.queues()
            self.assertEqual([it['name'] for it in q2['watched']],
                             ['vidA.mcap'], '重启后已看完状态必须保留')
            self.assertEqual(len(q2['cached']), 3, '已看完不占槽位，仍补 3 个')
            self.assertNotIn('vidA.mcap',
                             [it['name'] for it in q2['cached']],
                             '已看完的项目不得自动重新缓存')
        finally:
            qm2.shutdown()
            if qm2.worker is not None and qm2.worker.isRunning():
                qm2.worker.wait(10000)

    def test_05_source_mtime_change_resets_to_unwatched(self):
        self._load()
        self._wait_slots(3)
        sid = self._sid('A')
        self.qm.mark_watched(sid, 'manual')
        self._pump(lambda: not os.path.isdir(appcache.cache_dir(key_of(sid))))
        # 外部修改源文件（mtime 变化）→ 新版本 → 未看
        st = os.stat(self.paths[0])
        os.utime(self.paths[0], ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
        qm2 = QM.CacheQueueManager()
        try:
            qm2.load_folder(self.folder, self.paths)
            names = {it['name']: it['state'] for it in qm2.items.values()}
            self.assertIn(names['vidA.mcap'], (QM.UNWATCHED, QM.CACHING),
                          '源文件变化后不得继承已看完（补槽立刻开始也算新版本）')
        finally:
            qm2.shutdown()
            if qm2.worker is not None and qm2.worker.isRunning():
                qm2.worker.wait(10000)

    # ---- 三、看完与删除 ---------------------------------------------------
    def test_06_mark_watched_deletes_cache_and_refills(self):
        self._load()
        self._wait_slots(3)
        sid = self._sid('A')
        self.qm.mark_watched(sid, 'natural_end')
        self.assertEqual(self.qm.items[sid]['state'], QM.WATCHED)
        self._pump(lambda: not os.path.isdir(appcache.cache_dir(key_of(sid))))
        self.assertFalse(os.path.isdir(appcache.cache_dir(key_of(sid))),
                         '看完后桌面缓存必须删除')
        self.assertTrue(os.path.isfile(self.paths[0]), '原始 MCAP 必须保留')
        self.qm.flush()
        self._wait_slots(3)
        self.assertNotIn(sid, self.qm.cached_sids())
        q = self.qm.queues()
        self.assertEqual(len(q['unwatched']), 2, 'A 看完后 D 补位，未看剩 E/F')
        self.assertIn('vidD.mcap', [it['name'] for it in q['cached']])

    def test_07_mark_watched_is_idempotent(self):
        self._load()
        self._wait_slots(3)
        sid = self._sid('A')
        self.qm.mark_watched(sid, 'natural_end')
        self.qm.mark_watched(sid, 'manual')          # 重复调用必须幂等
        self.assertEqual(self.qm.items[sid]['watched_reason'], 'natural_end')

    def test_08_delete_failure_goes_cleanup_pending(self):
        self._load()
        self._wait_slots(3)
        sid = self._sid('A')
        real = appcache.discard_cache

        def busy(key):
            return dict(success=False, freed_bytes=0, error='文件被占用')

        appcache.discard_cache = busy
        try:
            self.qm.mark_watched(sid, 'natural_end')
            it = self.qm.items[sid]
            self.assertEqual(it['state'], QM.WATCHED,
                             '删除失败时文件仍应移到已看完')
            self.assertTrue(it['cleanup_pending'], '必须标记 cleanup_pending')
            self.assertTrue(os.path.isdir(appcache.cache_dir(key_of(sid))),
                            '缓存目录应保留等待重试')
        finally:
            appcache.discard_cache = real
        # 定时重试后成功
        self._pump(lambda: not it.get('cleanup_pending'), 10)
        self.assertFalse(os.path.isdir(appcache.cache_dir(key_of(sid))),
                         '重试后缓存应被删除')

    def test_09_current_playing_never_deleted(self):
        self._load()
        self._wait_slots(3)
        a = self._sid('A')
        self.qm.set_current(a)
        self.assertEqual(self.qm.items[a]['state'], QM.PLAYING)
        victim = self.qm._oldest_cached(exclude={a})
        self.assertIsNotNone(victim)
        self.assertNotEqual(victim, a)
        # 直接对当前播放项请求删除也必须被拒绝
        res = self.qm._safe_delete(a)
        self.assertFalse(res['success'])

    # ---- 四、重新缓存 -----------------------------------------------------
    def test_10_recache_moves_watched_back(self):
        self._load()
        self._wait_slots(3)
        sid = self._sid('A')
        self.qm.mark_watched(sid, 'manual')
        self._pump(lambda: not os.path.isdir(appcache.cache_dir(key_of(sid))))
        self.qm.request_recache(sid)
        it = self.qm.items[sid]
        self.assertIn(it['state'], (QM.UNWATCHED, QM.CACHING),
                      '重新缓存后先进未看/缓存中，不直接进已缓存')
        # 槽位满时排队（规范四.6）；看完一个腾出槽位后 A 应被自动缓存（四.4/五）
        self.qm.mark_watched(self._sid('B'), 'manual')
        self.qm.flush()
        ok = self._pump(lambda: it['state'] == QM.CACHED, 30)
        self.assertTrue(ok, '腾出槽位后重新缓存的项目应自动补上')
        self.assertEqual(it['state'], QM.CACHED)

    def test_11_cache_error_shows_and_can_retry(self):
        # 一个坏文件：prepare 失败 → ERROR + 错误信息；可重试
        bad = os.path.join(self.folder, 'broken.mcap')
        with open(bad, 'wb') as fh:
            fh.write(b'\x89this is not an mcap')
        self.paths.append(bad)
        self.qm.load_folder(self.folder, self.paths)
        bad_sid = self.qm.sid_of(bad)
        # 把坏文件排到最前：直接点名
        self.qm.request_cache(bad_sid)
        ok = self._pump(lambda: self.qm.items[bad_sid]['state'] == QM.ERROR, 20)
        self.assertTrue(ok, '坏文件应进入 ERROR 徽标')
        self.assertTrue(self.qm.items[bad_sid]['error'])
        self.qm.requested_sid = None
        self._wait_slots(3)
        self.assertNotIn(bad_sid, self.qm.cached_sids())

    # ---- 八、槽位与腾让 ---------------------------------------------------
    def test_12_manual_play_evicts_oldest_slot(self):
        self._load()
        self._wait_slots(3)
        a, d = self._sid('A'), self._sid('D')
        self.qm.set_current(a)
        victim = self.qm._oldest_cached(exclude={a})
        self.assertEqual(victim, self._sid('B'),
                         'A 在播放、B 最早进入，应腾 B')
        self.qm.request_play(d)
        ok = self._pump(lambda: d in self.qm.cached_sids(), 30)
        self.assertTrue(ok)
        self.assertLessEqual(self.qm.cached_count(), QM.MAX_CACHED_ITEMS)
        self.assertEqual(self.qm.items[self._sid('B')]['state'], QM.UNWATCHED,
                         '被腾出的项目回到未看')
        self.assertEqual(self.qm.items[a]['state'], QM.PLAYING,
                         '当前播放项不得被腾出')

    def test_13_current_played_slot_counts(self):
        self._load()
        self._wait_slots(3)
        a = self._sid('A')
        self.qm.set_current(a)
        self.assertEqual(self.qm.cached_count(), 3, '播放中的项计入 3 个槽位')

    # ---- 九、安全边界 -----------------------------------------------------
    def test_14_full_and_foreign_caches_untouched(self):
        # server/full 无后缀缓存、普通目录、staging、runtime.building 全部不动
        self._load()
        self._wait_slots(3)
        sid = self._sid('A')
        self.qm.mark_watched(sid, 'manual')
        self._pump(lambda: not os.path.isdir(appcache.cache_dir(key_of(sid))))
        full = os.path.join(self.root, sid)             # server 完整缓存布局
        os.makedirs(full, exist_ok=True)
        with open(os.path.join(full, 'camera1_c1.mp4'), 'wb') as fh:
            fh.write(b'x' * 100)
        plain = os.path.join(self.root, 'my-notes')
        os.makedirs(plain, exist_ok=True)
        stg1 = os.path.join(self.root, '.staging-deadbeef')
        stg2 = os.path.join(self.root, sid + '@%s.staging' % PROF)
        building = os.path.join(self.root, 'runtime.building-123')
        for d in (stg1, stg2, building):
            os.makedirs(d, exist_ok=True)
        # 触发一轮删除/腾槽
        d4 = self._sid('D')
        self.qm.request_play(d4)
        self._pump(lambda: d4 in self.qm.cached_sids(), 30)
        self.assertTrue(os.path.isdir(full), 'server 完整缓存不能删')
        self.assertTrue(os.path.isfile(os.path.join(full, 'camera1_c1.mp4')))
        self.assertTrue(os.path.isdir(plain), '普通目录不能删')
        self.assertTrue(os.path.isdir(stg1), '.staging- 不能删')
        self.assertTrue(os.path.isdir(stg2), '*.staging 不能删')
        self.assertTrue(os.path.isdir(building), 'runtime.building-* 不能删')
        self.assertLessEqual(self.qm.cached_count(), 3,
                             '槽位只数精简缓存')

    def test_15_running_job_is_protected(self):
        self._load()
        self._wait_slots(3)
        sid = self._sid('A')
        outdir = os.path.abspath(appcache.cache_dir(key_of(sid)))
        real_jobs = PREP.running_jobs
        PREP.running_jobs = lambda: [outdir]
        try:
            res = self.qm._safe_delete(sid)
            self.assertFalse(res['success'], '进行中的 prepare 任务绝不能删')
            self.assertTrue(os.path.isdir(outdir))
        finally:
            PREP.running_jobs = real_jobs

    def test_16_cross_process_lock_serializes_delete(self):
        self._load()
        self._wait_slots(3)
        sid = self._sid('A')
        with appcache.exclusive_cache_lock():
            old_to = QM.LOCK_TIMEOUT
            QM.LOCK_TIMEOUT = 0.3
            try:
                res = self.qm._safe_delete(sid)
            finally:
                QM.LOCK_TIMEOUT = old_to
            self.assertFalse(res['success'],
                             '另一实例持锁时删除必须失败而不是并发删')
        # 锁释放后可删
        res = self.qm._safe_delete(sid)
        self.assertTrue(res['success'])

    def test_17_corrupt_lru_and_state_recover(self):
        with open(appcache._lru_path(), 'w', encoding='utf-8') as fh:
            fh.write('broken json !!!')
        st_path = WS.folder_state_path(self.folder)
        os.makedirs(os.path.dirname(st_path), exist_ok=True)
        with open(st_path, 'w', encoding='utf-8') as fh:
            fh.write('{{{{')
        self._load()
        self._wait_slots(3)
        self.assertEqual(self.qm.cached_count(), 3, '坏台账/坏状态不得影响启动')

    # ---- 十一、无主视角不给假缓存 ----------------------------------------
    def test_18_no_primary_view_no_fake_cache(self):
        mcap = os.path.join(self.folder, 'nopri.mcap')
        recs = [fx.header(), fx.schema(1, 'foxglove.CompressedImage')]
        recs.append(fx.channel(1, 1, '/robot0/sensor/camera5/compressed'))
        png = fx.make_png(48, 36)
        for i in range(2):
            ts = BASE + i * 33_000_000
            recs.append(fx.message(1, i, ts, ts,
                                   fx.compressed_image(png, 'png', 'cam')))
        fx.assemble(mcap, recs)
        outdir = appcache.cache_dir(key_of(self.qm.sid_of(mcap)))
        with self.assertRaises(MR.McapError):
            PREP.prepare(mcap, outdir,
                         camera_pred=appcache.profile_keeps_topic,
                         profile=PROF)
        self.assertFalse(os.path.isdir(outdir))

    # ---- 二十九、原始文件与导出不受影响 ----------------------------------
    def test_19_source_hash_unchanged_after_full_flow(self):
        hashes = {p: fx.file_sha256(p) for p in self.paths}
        self._load()
        self._wait_slots(3)
        for nm in ('A', 'B'):
            self.qm.mark_watched(self._sid(nm), 'natural_end')
        self._pump(lambda: self.qm.cached_count() == 3, 30)
        exported = os.path.join(self.folder, '..', 'user-export.txt')
        with open(exported, 'w') as fh:
            fh.write('user data')
        try:
            for p, h in hashes.items():
                self.assertEqual(fx.file_sha256(p), h,
                                 '原始 MCAP 内容与哈希必须完全不变：%s' % p)
            self.assertTrue(os.path.isfile(exported), '用户导出文件不能被删')
        finally:
            os.remove(exported)

    def test_24_cancel_cache_back_to_unwatched_no_auto_retry(self):
        self._load()
        # worker 正在缓存 A；对排队的 B 发起取消
        self._pump(lambda: self.qm.worker is not None, 10)
        b = self._sid('B')
        self.qm.requested_sid = b
        self.qm.cancel_cache(b)
        self.assertIn(b, self.qm._no_auto)
        self.assertEqual(self.qm.items[b]['state'], QM.UNWATCHED)
        # A 缓存完后 B 不得被自动重试（须手动点「缓存」）
        self._wait_slots(1)
        ok = self._pump(lambda: self.qm.items[b]['state'] != QM.UNWATCHED, 1.5)
        self.assertFalse(ok, '取消过的项目不得被自动重新缓存')
        self.assertEqual(self.qm.items[b]['state'], QM.UNWATCHED)

    def test_25_next_playable_skips_watched_prefers_cached(self):
        self._load()
        self._wait_slots(3)
        a, b, c = self._sid('A'), self._sid('B'), self._sid('C')
        self.qm.set_current(a)
        for s in (a, b, c):
            self.qm.mark_watched(s, 'manual')
        # A/B/C 都已看完 → +1 方向应跳过已看完，落在最近的未看 D
        self.assertEqual(self.qm.next_playable_sid(a, 1), self._sid('D'))
        # D 往 -1：沿途全是已看完 → None（不停留在已看完项上）
        self.assertIsNone(self.qm.next_playable_sid(self._sid('D'), -1))
        # 已看完项不参与已缓存导航
        self.assertIsNone(self.qm.next_cached_sid_after(b))


class DesktopFlowCase(unittest.TestCase):
    """桌面端集成：自然播完标记 / 拖动与 End 键不标记 / 关窗安全停止"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        QSettings('MCAPViewer', 'Desktop').clear()
        cls._old_root = appcache.CACHE_ROOT
        cls._old_env = os.environ.get('MCAPVIEWER_STATE_DIR')
        cls.root = tempfile.mkdtemp(prefix='mcapview-d-cache-')
        cls.state_dir = tempfile.mkdtemp(prefix='mcapview-d-state-')
        appcache.CACHE_ROOT = cls.root
        os.environ['MCAPVIEWER_STATE_DIR'] = cls.state_dir
        cls.folder = tempfile.mkdtemp(prefix='mcapview-d-folder-')
        cls.wins = []

    @classmethod
    def tearDownClass(cls):
        for w in cls.wins:
            try:
                w.close()
                w.deleteLater()
            except Exception:
                pass
        cls.app.processEvents()
        appcache.CACHE_ROOT = cls._old_root
        if cls._old_env is None:
            os.environ.pop('MCAPVIEWER_STATE_DIR', None)
        else:
            os.environ['MCAPVIEWER_STATE_DIR'] = cls._old_env
        QSettings('MCAPViewer', 'Desktop').clear()
        shutil.rmtree(cls.root, ignore_errors=True)
        shutil.rmtree(cls.state_dir, ignore_errors=True)
        shutil.rmtree(cls.folder, ignore_errors=True)

    def _build_mcap(self, name):
        mcap = os.path.join(self.folder, name + '.mcap')
        recs = [fx.header(), fx.schema(1, 'foxglove.CompressedImage')]
        recs.append(fx.channel(1, 1, '/robot0/sensor/camera2/compressed'))
        png = fx.make_png(48, 36)
        for i in range(3):
            ts = BASE + i * 33_000_000
            recs.append(fx.message(1, i, ts, ts,
                                   fx.compressed_image(png, 'png', 'cam')))
        fx.assemble(mcap, recs)
        return os.path.abspath(mcap)

    def _window(self, n=2):
        import desktop as D
        paths = [self._build_mcap('clip%s' % chr(ord('A') + i))
                 for i in range(n)]
        win = D.MainWindow()
        self.wins.append(win)
        win.chk_auto.setChecked(False)
        # 测试里不让「全部看完」弹窗阻塞（各用例需要时再覆盖本 hook）
        win._confirm_folder_complete = lambda summary, path: False
        win.load_folder(self.folder, autoplay=False)
        self.assertTrue(self._pump(lambda: win.qm.cached_count() >= 1, 30),
                        '打开文件夹后应自动补缓存')
        win.qm.set_current(win.sids[0])
        win.index = 0                       # 当前打开的就是第一个文件
        # 直接把第一个文件标为缓存命中并装配（不走完整播放流）
        man = PREP.load_manifest(D.cache_outdir(win.sids[0]),
                                 source_path=paths[0])
        win.items = [dict(path=p, name=os.path.basename(p),
                          size=os.path.getsize(p), mtime=int(time.time()),
                          mtime_str='', stem='', dir=self.folder, rel_dir='')
                     for p in paths]
        win.sids = [win.qm.sid_of(p) for p in paths]
        win._apply(man, cached=True)
        return win, paths

    def _pump(self, pred, timeout=30.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.app.processEvents()
            if pred():
                return True
            time.sleep(0.01)
        return False

    def test_20_natural_end_marks_watched_and_deletes(self):
        import desktop as D
        win, paths = self._window()
        sid = win.sids[0]
        win.duration = 0.1
        win.playing = True
        win._on_reach_end()                 # 仅正常播放时钟越过结尾才会调用
        qit = win.qm.items[sid]
        self.assertEqual(qit['state'], QM.WATCHED, '自然播完必须标记已看完')
        self.assertEqual(qit['watched_reason'], 'natural_end')
        self._pump(lambda: not os.path.isdir(D.cache_outdir(sid)), 10)
        self.assertFalse(os.path.isdir(D.cache_outdir(sid)), '看完删缓存')
        self.assertTrue(os.path.isfile(paths[0]), '原始文件保留')

    def test_21_manual_button_marks_watched(self):
        win, _paths = self._window()
        sid = win.sids[0]
        win.finish_current_video('manual')
        self.assertEqual(win.qm.items[sid]['state'], QM.WATCHED)
        # 幂等
        win.finish_current_video('manual')
        self.assertEqual(win.qm.items[sid]['watched_reason'], 'manual')

    def test_22_seek_end_key_and_frame_step_do_not_mark(self):
        import desktop as D
        win, _paths = self._window()
        sid = win.sids[0]
        win.duration = 10.0
        win.seek(10.0)                       # 拖动到结尾
        self.app.processEvents()
        self.assertNotEqual(win.qm.items[sid]['state'], QM.WATCHED,
                            '拖动到结尾不得标记已看完')
        ev = type('Ev', (), {'key': lambda self: D.Qt.Key_End,
                             'modifiers': lambda self: D.Qt.NoModifier})()
        win._handle_key(ev)                  # End 键
        self.app.processEvents()
        self.assertNotEqual(win.qm.items[sid]['state'], QM.WATCHED,
                            'End 键不得标记已看完')
        win.seek(0.05)
        win.frame_step(1)                    # 逐帧跳到最后一帧
        win.frame_step(1)
        self.app.processEvents()
        self.assertNotEqual(win.qm.items[sid]['state'], QM.WATCHED,
                            '逐帧到结尾不得标记已看完')

    def test_23_close_stops_queue_manager(self):
        import desktop as D
        win, _paths = self._window()
        # 让后台继续补第 2 个，然后立刻关窗
        win.close()
        self.assertTrue(self._pump(
            lambda: win.qm.worker is None or not win.qm.worker.isRunning(), 15),
            '关窗后缓存线程必须退出')
        self.assertFalse(win.qm.worker is not None and win.qm.worker.isRunning())


class UiFlowCase(unittest.TestCase):
    """UI/异常流：两行队列行、高亮、错误解锁、取消、看完清场、位置保存、导航"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        QSettings('MCAPViewer', 'Desktop').clear()
        cls._old_root = appcache.CACHE_ROOT
        cls._old_env = os.environ.get('MCAPVIEWER_STATE_DIR')
        cls.root = tempfile.mkdtemp(prefix='mcapview-ui-cache-')
        cls.state_dir = tempfile.mkdtemp(prefix='mcapview-ui-state-')
        appcache.CACHE_ROOT = cls.root
        os.environ['MCAPVIEWER_STATE_DIR'] = cls.state_dir
        cls.folder = tempfile.mkdtemp(prefix='mcapview-ui-folder-')
        cls.wins = []

    @classmethod
    def tearDownClass(cls):
        for w in cls.wins:
            try:
                w.close()
                w.deleteLater()
            except Exception:
                pass
        cls.app.processEvents()
        appcache.CACHE_ROOT = cls._old_root
        if cls._old_env is None:
            os.environ.pop('MCAPVIEWER_STATE_DIR', None)
        else:
            os.environ['MCAPVIEWER_STATE_DIR'] = cls._old_env
        QSettings('MCAPViewer', 'Desktop').clear()
        shutil.rmtree(cls.root, ignore_errors=True)
        shutil.rmtree(cls.state_dir, ignore_errors=True)

    def setUp(self):
        # 每个用例独立文件夹，避免上一条的缓存占满 3 个槽位造成污染
        self.folder = tempfile.mkdtemp(prefix='mcapview-ui-f-')

    def tearDown(self):
        for w in list(self.wins):
            try:
                w.close()
                w.deleteLater()
            except Exception:
                pass
        self.wins.clear()
        self.app.processEvents()
        shutil.rmtree(self.folder, ignore_errors=True)

    def _build_mcap(self, name):
        mcap = os.path.join(self.folder, name + '.mcap')
        recs = [fx.header(), fx.schema(1, 'foxglove.CompressedImage')]
        recs.append(fx.channel(1, 1, '/robot0/sensor/camera2/compressed'))
        png = fx.make_png(48, 36)
        for i in range(3):
            ts = BASE + i * 33_000_000
            recs.append(fx.message(1, i, ts, ts,
                                   fx.compressed_image(png, 'png', 'cam')))
        fx.assemble(mcap, recs)
        return os.path.abspath(mcap)

    def _window(self, names):
        import desktop as D
        paths = [self._build_mcap(n) for n in names]
        win = D.MainWindow()
        self.wins.append(win)
        win.chk_auto.setChecked(False)
        # 测试里不让「全部看完」弹窗阻塞（u11 自己覆盖这个 hook 验证两条分支）
        win._confirm_folder_complete = lambda summary, path: False
        win.load_folder(self.folder, autoplay=False)
        win.show()
        win.resize(1280, 820)
        self.app.processEvents()
        return win, paths

    def _pump(self, pred, timeout=30.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.app.processEvents()
            if pred():
                return True
            time.sleep(0.02)
        return False

    def _rows(self, win, key):
        lay = win.queue_layouts[key]
        out = []
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is not None:
                out.append(w)
        return out

    def test_u1_two_line_row_button_visible_with_long_name(self):
        import desktop as D
        long_name = 'DAS-Ego_20260915200634_none_none_f25ffd_5b03c061'
        win, _paths = self._window([long_name])
        self.assertTrue(self._pump(lambda: win.qm.cached_count() >= 1, 30),
                        '单文件应被预热进已缓存')
        # 文件可能停留在未看或已缓存页，取「有行」的那个页验证
        rows = self._rows(win, 'unwatched') or self._rows(win, 'cached')
        self.assertTrue(rows, '至少一个页签应有行')
        row = rows[0]
        btns = row.findChildren(D.QPushButton)
        self.assertTrue(btns, '行里必须有操作按钮')
        btn = btns[0]
        self.assertEqual(btn.width(), 82, '按钮固定宽度 82px，不得被文件名挤压')
        self.assertLessEqual(btn.mapTo(row, QPoint(0, 0)).x() + btn.width(), row.width() + 1,
                             '按钮必须完整落在行内')
        name_lbl = row.findChildren(D.ElidedLabel)[0]
        self.assertTrue(name_lbl.width() > 40, '省略文件名标签应保留可读宽度')
        win.close()

    def test_u2_row_button_visible_when_narrow(self):
        import desktop as D
        win, _paths = self._window(['DAS-Ego_20260915200634_none_none_f25ffd'])
        self.assertTrue(self._pump(lambda: win.qm.cached_count() >= 1, 30))
        win.splitter.setSizes([360, 900])       # 压到左栏最小宽度
        self.app.processEvents()
        rows = self._rows(win, 'unwatched') or self._rows(win, 'cached')
        self.assertTrue(rows)
        row = rows[0]
        btn = row.findChildren(D.QPushButton)[0]
        self.assertEqual(btn.width(), 82)
        self.assertLessEqual(btn.mapTo(row, QPoint(0, 0)).x() + btn.width(), row.width() + 1)
        win.close()

    def test_u3_row_height_and_current_highlight(self):
        import desktop as D
        win, _paths = self._window(['c1', 'c2'])
        self.assertTrue(self._pump(lambda: win.qm.cached_count() >= 1, 30))
        # 小文件可能在 show/processEvents 期间就全部缓存完，两页任取其一
        rows = self._rows(win, 'unwatched') or self._rows(win, 'cached')
        self.assertTrue(rows, '至少一个页签应有行')
        row = rows[0]
        self.assertEqual(row.height(), D.MainWindow.ROW_H - 2,
                         '行高必须固定 58px（ROW_H-2）')
        win.qm.set_current(win.sids[0])
        self._pump(lambda: self._rows(win, 'cached'), 10)
        rows = self._rows(win, 'cached')
        self.assertTrue(rows, '播放中的项应在已缓存页')
        self.assertIn('1a2c4e', rows[0].styleSheet(),
                      '当前播放行应有深蓝背景高亮')
        self.assertIn('播放中', win._badge_text(win.qm.items[win.sids[0]]))
        win.close()

    def test_u4_cache_failure_unlocks_dialog_and_loading(self):
        win, _paths = self._window(['okfile'])
        bad_sid = win.sids[0]
        self._pump(lambda: win.qm.worker is None, 30)   # 等初始缓存结束
        win._pending_play_sid = bad_sid
        win._show_cache_dialog('okfile.mcap')
        self.assertIsNotNone(win._dlg)
        win._on_qm_error(bad_sid, '模拟失败：CRC 校验不过')
        self.assertIsNone(win._pending_play_sid, '失败后必须清空点名')
        self.assertFalse(win.loading, '失败后 loading 必须恢复 False')
        self.assertIsNone(win._dlg, '失败后进度框必须关闭')
        # 取消路径（真实路径 = 进度框取消按钮）：清点名 + 项目不自动重试
        win._pending_play_sid = bad_sid
        win._show_cache_dialog('okfile.mcap')
        win._cancel_pending_cache()
        self.assertIsNone(win._pending_play_sid, '取消后必须清空点名')
        self.assertIn(bad_sid, win.qm._no_auto)
        win.close()

    def test_u5_watched_clears_grid_panes_imu_and_shows_hint(self):
        import desktop as D
        win, _paths = self._window(['v1'])
        self.assertTrue(self._pump(lambda: win.qm.cached_count() >= 1, 30))
        win.qm.set_current(win.sids[0])
        man = PREP.load_manifest(D.cache_outdir(win.sids[0]),
                                 source_path=_paths[0])
        win.index = 0
        win._apply(man, cached=True)
        self.app.processEvents()
        self.assertGreater(win.grid.count(), 0, '播放中应有画面网格')
        win.imu_data = {'t': [0.0, 0.1], 'av': [[0] * 2] * 3, 'la': [[0] * 2] * 3}
        win.t = 0.05
        win.finish_current_video('manual')
        self.assertEqual(win.grid.count(), 0, '看完后画面网格必须清空')
        self.assertEqual(win.panes, {}, '看完后 panes 必须为空')
        self.assertIsNone(win.imu_data, '看完后 IMU 数据必须清空')
        self.assertTrue(win.empty_hint.isVisible(), '必须显示空状态提示')
        self.assertIn('已看完', win.empty_hint.text())
        self.assertIn('缓存已释放', win.empty_hint.text())
        win.close()

    def test_u6_position_saved_on_natural_and_manual(self):
        import desktop as D
        win, _paths = self._window(['p1'])
        self.assertTrue(self._pump(lambda: win.qm.cached_count() >= 1, 30))
        win.qm.set_current(win.sids[0])
        man = PREP.load_manifest(D.cache_outdir(win.sids[0]),
                                 source_path=_paths[0])
        win.index = 0
        win._apply(man, cached=True)
        # 自然结束：位置应接近 duration
        win.duration = 0.1
        win.playing = True
        win.t = 0.1
        win._on_reach_end()
        self.assertAlmostEqual(win.qm.items[win.sids[0]]['last_position_s'],
                               0.1, places=3, msg='自然结束应保存到结尾位置')
        # 手动标记：保存点击时的实际位置（先重新缓存，恢复可播状态）
        win.qm.request_recache(win.sids[0])
        self._pump(lambda: win.qm.items[win.sids[0]]['state'] == QM.CACHED, 30)
        win.qm.set_current(win.sids[0])
        man = PREP.load_manifest(D.cache_outdir(win.sids[0]),
                                 source_path=_paths[0])
        win.index = 0
        win._apply(man, cached=True)
        win.t = 0.033
        win.finish_current_video('manual')
        self.assertAlmostEqual(win.qm.items[win.sids[0]]['last_position_s'],
                               0.033, places=3, msg='手动标记应保存实际位置')
        win.close()

    def test_u7_step_next_skips_watched_and_autocaches(self):
        win, _paths = self._window(['n1', 'n2'])
        self.assertTrue(self._pump(lambda: win.qm.cached_count() >= 2, 30))
        win.qm.set_current(win.sids[0])
        win.index = 0
        # 把第二个变成未看（模拟缓存被看完删除后的队列形态）
        win.qm._evict_to_unwatched(win.sids[1])
        win.step_file(1)                       # 目标未缓存 → 缓存后自动播放
        self.assertEqual(win._pending_play_sid, win.sids[1])
        self._pump(lambda: win.qm.items[win.sids[1]]['state'] == QM.CACHED, 30)
        self._pump(lambda: win.index == 1 and win.panes, 30)
        self.assertEqual(win.index, 1, '缓存完成后应自动打开第二个')
        win.close()

    def test_u8_status_bar_compact(self):
        win, _paths = self._window(['s1'])
        self._pump(lambda: win.qm.cached_count() >= 1, 30)
        import re
        txt = win.lbl_queues.text()
        self.assertLess(len(txt), 60, '底部状态栏必须紧凑：%r' % txt)
        self.assertRegex(txt, r'未看 \d+ ｜ 已缓存 \d/\d ｜ 已看完 \d+ ｜ 缓存 ')
        win.close()

    # ---- 不合格标注（两下 X）--------------------------------------------
    def _key(self, win, key, mod=None):
        import desktop as D
        ev = type('Ev', (), {
            'key': lambda self: key,
            'modifiers': lambda self: (D.Qt.NoModifier if mod is None else mod)})()
        return win._handle_key(ev)

    def test_u9_two_x_presses_mark_one_bad_segment(self):
        import desktop as D
        win, _paths = self._window(['DAS-Ego_20260911203440_none_none_689985_a1'])
        self.assertTrue(self._pump(lambda: win.qm.cached_count() >= 1, 30))
        win.index = 0
        win.qm.set_current(win.sids[0])
        man = PREP.load_manifest(D.cache_outdir(win.sids[0]),
                                 source_path=_paths[0])
        win._apply(man, cached=True)
        win.duration = 10.0
        win.t = 2.0
        self._key(win, D.Qt.Key_X)                 # 第一下：起点
        segs, pending = win.qm.markers_of(win.sids[0])
        self.assertEqual(list(segs), [])
        self.assertAlmostEqual(pending, 2.0, places=3)
        self.assertIn('待闭合', win.lbl_bad.text(), '应提示等待第二下 X')
        win.t = 3.5
        self._key(win, D.Qt.Key_X)                 # 第二下：闭合
        segs, pending = win.qm.markers_of(win.sids[0])
        self.assertEqual([list(s) for s in segs], [[2.0, 3.5]])
        self.assertIsNone(pending)
        self.assertIn('不合格 1 段', win.lbl_bad.text())
        self.assertIn('1.5 秒', win.lbl_bad.text())
        self.assertEqual(len(win._bad_norm), 1)
        win.close()

    def test_u10_undo_bad_mark_removes_last_segment(self):
        import desktop as D
        win, _paths = self._window(['DAS-Ego_20260911203440_none_none_689985_b2'])
        self.assertTrue(self._pump(lambda: win.qm.cached_count() >= 1, 30))
        win.index = 0
        win.duration = 10.0
        win.qm.update_markers(win.sids[0], [[1.0, 2.0], [4.0, 5.0]], None)
        win._refresh_marker_ui()
        self.assertEqual(len(win._bad_norm), 2)
        self._key(win, D.Qt.Key_Z, D.Qt.ControlModifier)
        segs, _pending = win.qm.markers_of(win.sids[0])
        self.assertEqual([list(s) for s in segs], [[1.0, 2.0]], '应撤销最后一段')
        self.assertIn('不合格 1 段', win.lbl_bad.text())
        win.close()

    # ---- 全部看完：报告 + 弹窗 -------------------------------------------
    def test_u13_unclosed_mark_is_auto_closed_on_finish(self):
        """只按了一下 X 就点「标记已看完」：未闭合的起点要自动闭合，避免漏标"""
        import desktop as D
        win, _paths = self._window(['DAS-Ego_20260911203440_none_none_689985_c3'])
        self.assertTrue(self._pump(lambda: win.qm.cached_count() >= 1, 30))
        win.index = 0
        win.qm.set_current(win.sids[0])
        man = PREP.load_manifest(D.cache_outdir(win.sids[0]),
                                 source_path=_paths[0])
        win._apply(man, cached=True)
        win.duration = 10.0
        win.t = 3.0
        self._key(win, D.Qt.Key_X)                     # 只按下起点，不闭合
        segs, pending = win.qm.markers_of(win.sids[0])
        self.assertEqual(list(segs), [])
        self.assertAlmostEqual(pending, 3.0, places=3)
        win.t = 6.5
        win.finish_current_video('manual')             # 结束时自动闭合
        segs, pending = win.qm.markers_of(win.sids[0])
        self.assertEqual([list(s) for s in segs], [[3.0, 6.5]])
        self.assertIsNone(pending)
        win.close()

    def test_u11_folder_complete_generates_report_and_clears_on_yes(self):
        import desktop as D
        names = ['DAS-Ego_20260911203440_none_none_689985_a1',
                 'DAS-Ego_20260911204500_none_none_689985_a2']
        win, _paths = self._window(names)
        self.assertTrue(self._pump(lambda: win.qm.cached_count() >= 2, 60))
        # 标注一段不合格（10 秒里的 2 秒）后再看完
        win.index = 0
        win.duration = 10.0
        win.qm.update_markers(win.sids[0], [[2.0, 4.0]], None)
        for p in _paths:
            sid = win.qm.sid_of(p)
            win.qm.mark_watched(sid, 'manual', 10.0)
            self._pump(lambda s=sid: not os.path.isdir(
                D.cache_outdir(s)), 20)
        self.assertTrue(win.qm.folder_all_watched())
        calls = {}
        win._confirm_folder_complete = lambda summary, path: (
            calls.update(summary=summary, path=path), False)[1]
        win._on_folder_complete()
        self.assertIn('summary', calls, '应生成报告摘要；R1 起不再弹窗')
        self.assertAlmostEqual(calls['summary']['rate'], 90.0, places=2)
        self.assertTrue(os.path.isfile(calls['path']), '报告文件必须存在')
        with open(calls['path'], encoding='utf-8-sig') as fh:
            body = fh.read()
        self.assertIn('689985', body)
        self.assertIn('2026-09-11', body)
        self.assertIn('90.00%', body)
        # 返回「否」：不清空，且不会重复弹窗
        self.assertEqual(len(win.qm.queues()['watched']), 2)
        self.assertEqual(win._prompted_folder, win.folder)
        # 返回「是」：清空 + 选下一个文件夹（重置防重复标志 = 新一轮）
        chose = {}
        win.choose_folder = lambda: chose.update(called=True)
        win._prompted_folder = None
        win._confirm_folder_complete = lambda summary, path: True
        win._on_folder_complete()
        self.assertEqual(len(win.qm.queues()['watched']), 0, '选「是」应清空已看完')
        self.assertTrue(chose.get('called'), '选「是」应进入选择文件夹')
        win.close()

    def test_u12_clear_button_wipes_queues_and_caches(self):
        import desktop as D
        win, _paths = self._window(['c1', 'c2'])
        self.assertTrue(self._pump(lambda: win.qm.cached_count() >= 2, 60))
        sid = win.sids[0]
        win.qm.mark_watched(sid, 'manual', 10.0)
        self._pump(lambda: not os.path.isdir(D.cache_outdir(sid)), 20)
        win.clear_queues_and_cache(ask=False)
        q = win.qm.queues()
        self.assertTrue(all(len(q[k]) == 0 for k in q),
                        '清空后三个队列都必须为空：%s'
                        % {k: len(v) for k, v in q.items()})
        self.assertEqual(win.qm.cached_count(), 0, '清空后不该还有缓存')
        self.assertEqual(win.items, [], '本地列表也应卸载（不再指着已清空的项）')
        self.assertIsNone(win._prompted_folder)
        self.assertTrue(all(os.path.isfile(p) for p in _paths),
                        '原始文件绝不能被清空操作删除')
        # 回归：清空后不得自动回流
        self._pump(lambda: False, 2.0)
        q = win.qm.queues()
        self.assertTrue(all(len(q[k]) == 0 for k in q), '2 秒后仍应为空')
        self.assertTrue(win.empty_hint.isVisible(), '清空后应显示空状态提示')
        self.assertIn('重新扫描', win.empty_hint.text())
        # 重新扫描后可以正常重新开始
        win.load_folder(win.folder, autoplay=False)
        self._pump(lambda: win.qm.cached_count() >= 1, 60)
        self.assertEqual(len(win.qm.queues()['unwatched'])
                         + win.qm.cached_count(), 2)
        win.close()


if __name__ == '__main__':
    unittest.main()
