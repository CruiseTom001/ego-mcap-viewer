"""缓存滚动清理测试（最多保留 3 个 · LRU · 精简缓存 desktop_camera2_camera3_v1）

覆盖四大块（共 25 条）：
  A 台账 .cache_lru.json：touch / list / 字节统计 / 损坏自愈
  B 滚动清理：预算内不动、超额删最旧、keep 保护、staging 不可触碰、
    永不越出缓存根目录、旧版完整缓存自动纳入管理
  C 精简缓存：fid@profile 命名空间、prepare 只封装 camera2/3、
    无主视角时回退全通道、与完整缓存（server 布局）共存
  D 桌面集成：打开文件登记台账、界面缓存字段、状态栏清理提示

所有测试都把 appcache.CACHE_ROOT 指到临时目录，结束恢复，互不污染。
"""

import os
import shutil
import time
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from tests import mcapfix as fx          # noqa: E402
import appcache                          # noqa: E402
import mcap_reader as MR                 # noqa: E402
import prepare as PREP                   # noqa: E402
import desktop as D                      # noqa: E402

from PySide6.QtWidgets import QApplication            # noqa: E402
from PySide6.QtCore import QSettings                   # noqa: E402

BASE = 1_000_000_000_000
PROF = appcache.CACHE_PROFILE


def key_of(fid):
    return appcache.cache_key(fid)


class CacheRootCase(unittest.TestCase):
    """公共基类：把缓存根目录指到临时目录，用后恢复"""

    def setUp(self):
        self._old_root = appcache.CACHE_ROOT
        self._old_external = appcache.CACHE_IS_EXTERNAL
        self.root = tempfile.mkdtemp(prefix='mcapview-lru-')
        appcache.CACHE_ROOT = self.root
        os.makedirs(self.root, exist_ok=True)

    def tearDown(self):
        appcache.CACHE_ROOT = self._old_root
        appcache.CACHE_IS_EXTERNAL = self._old_external
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- 工具 ------------------------------------------------------------
    def mk_entry(self, fid, size=1024, last_used=None, profile=PROF):
        """造一个真实存在的缓存条目目录 + 台账触点，可回拨 last_used"""
        d = appcache.cache_dir(appcache.cache_key(fid, profile))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'blob.bin'), 'wb') as fh:
            fh.write(b'\x01' * size)
        e = appcache.touch_cache(fid, profile=profile)
        self.assertIsNotNone(e, 'touch 应当成功')
        if last_used is not None:
            data = appcache._load_lru()
            data['entries'][appcache.cache_key(fid, profile)]['last_used'] = last_used
            appcache._save_lru(data)
        return d


# ================================================================== A 台账
class LedgerCase(CacheRootCase):
    def test_touch_creates_entry_and_ledger_file(self):
        self.mk_entry('aaaa1111aaaa1111')
        e = appcache.touch_cache('aaaa1111aaaa1111')
        self.assertEqual(e['key'], key_of('aaaa1111aaaa1111'))
        self.assertEqual(e['fid'], 'aaaa1111aaaa1111')
        self.assertEqual(e['profile'], PROF)
        self.assertTrue(os.path.isfile(appcache._lru_path()))

    def test_touch_updates_and_does_not_duplicate(self):
        self.mk_entry('bbbb2222bbbb2222', last_used=100.0)
        e = appcache.touch_cache('bbbb2222bbbb2222')
        self.assertGreater(e['last_used'], 100.0, '重复 touch 应刷新最近使用时间')
        entries = appcache.list_cache_entries()
        self.assertEqual(len(entries), 1, '重复 touch 不得产生重复条目')

    def test_touch_records_bytes(self):
        self.mk_entry('cccc3333cccc3333', size=777)
        e = appcache.touch_cache('cccc3333cccc3333')
        self.assertEqual(e['bytes'], 777)

    def test_touch_records_source_and_name(self):
        src = os.path.join(self.root, 'some video.mcap')
        with open(src, 'wb') as fh:
            fh.write(b'x')
        self.mk_entry('dddd4444dddd4444')
        e = appcache.touch_cache('dddd4444dddd4444', source_path=src)
        self.assertEqual(os.path.normcase(e['source']),
                         os.path.normcase(os.path.abspath(src)))
        self.assertEqual(e['name'], 'some video.mcap')

    def test_touch_missing_dir_is_noop(self):
        self.assertIsNone(appcache.touch_cache('nope0000'))
        self.assertFalse(os.path.isfile(appcache._lru_path()),
                         '无效 touch 不应留下台账文件')

    def test_list_empty_returns_empty(self):
        self.assertEqual(appcache.list_cache_entries(), [])

    def test_list_sorted_by_last_used_desc(self):
        self.mk_entry('bb00000000000001', last_used=100.0)
        self.mk_entry('bb00000000000002', last_used=300.0)
        self.mk_entry('bb00000000000003', last_used=200.0)
        keys = [e['key'] for e in appcache.list_cache_entries()]
        self.assertEqual(keys, [key_of('bb00000000000002'), key_of('bb00000000000003'), key_of('bb00000000000001')])

    def test_corrupt_ledger_recovers(self):
        with open(appcache._lru_path(), 'w', encoding='utf-8') as fh:
            fh.write('{ 这不是 JSON !!!')
        self.assertEqual(appcache.list_cache_entries(), [], '坏台账应按空处理')
        e = self.mk_entry('eeee5555eeee5555')
        self.assertIsNotNone(e, '坏台账之后 touch 必须照常工作')


# ================================================================== B 滚动清理
class EvictCase(CacheRootCase):
    def test_reconcile_drops_missing_dirs(self):
        d = self.mk_entry('ffff6666ffff6666')
        shutil.rmtree(d)
        self.assertEqual(appcache.list_cache_entries(), [],
                         '目录已消失的条目应被台账清理')

    def test_reconcile_ignores_legacy_and_foreign_dirs(self):
        # 旧版（server 布局）完整缓存 / 普通目录：绝不收养、绝不登记、绝不删除
        legacy = os.path.join(self.root, 'deadbeef00112233')
        os.makedirs(legacy)
        with open(os.path.join(legacy, 'camera2_c9.mp4'), 'wb') as fh:
            fh.write(b'\x02' * 2048)
        plain = os.path.join(self.root, 'not-a-cache')
        os.makedirs(plain)
        with open(os.path.join(plain, 'user.dat'), 'wb') as fh:
            fh.write(b'\x03' * 64)
        entries = appcache.list_cache_entries()
        self.assertEqual(entries, [], '命名空间之外的目录一律不收养')
        self.assertTrue(os.path.isdir(legacy), 'legacy 完整缓存不能被删除')
        self.assertTrue(os.path.isfile(os.path.join(plain, 'user.dat')))

    def test_no_eviction_within_budget(self):
        for i in range(appcache.MAX_CACHE_ENTRIES):
            self.mk_entry('aa0000000000000%d' % i, last_used=100.0 + i)
        self.assertEqual(appcache.evict_cache(), [])
        self.assertEqual(len(appcache.list_cache_entries()),
                         appcache.MAX_CACHE_ENTRIES)

    def test_over_budget_evicts_oldest(self):
        for i in range(4):
            self.mk_entry('aa0000000000000%d' % i, last_used=100.0 + i)
        evicted = appcache.evict_cache()
        self.assertEqual(evicted, [key_of('aa00000000000000')], '只清理最旧的 1 个')
        self.assertFalse(os.path.isdir(appcache.cache_dir(key_of('aa00000000000000'))))
        self.assertEqual(len(appcache.list_cache_entries()), 3)

    def test_keep_protects_oldest(self):
        for i in range(5):
            self.mk_entry('bb0000000000000%d' % i, last_used=100.0 + i)
        evicted = appcache.evict_cache(keep=['bb00000000000000'])
        self.assertNotIn(key_of('bb00000000000000'), evicted, 'keep 里的条目永不清')
        self.assertTrue(os.path.isdir(appcache.cache_dir(key_of('bb00000000000000'))))
        self.assertEqual(sorted(evicted),
                         sorted([key_of('bb00000000000001'), key_of('bb00000000000002')]),
                         '应跳过受保护的，按 LRU 删其次旧的两个')

    def test_keep_protects_slim_even_when_full_present(self):
        # server/full、legacy 目录不算入 3 个槽位，也绝不被 evict 删除
        self.mk_entry('d0d0d0d0d0d0d0d0', last_used=100.0)
        legacy = os.path.join(self.root, 'd0d0d0d0d0d0d0d0')   # 手工造 legacy 完整缓存
        os.makedirs(legacy, exist_ok=True)
        with open(os.path.join(legacy, 'camera2_c9.mp4'), 'wb') as fh:
            fh.write(b'\x02' * 512)
        for i in range(4):
            self.mk_entry('cc0000000000000%d' % i, last_used=300.0 + i)
        evicted = appcache.evict_cache(keep=['d0d0d0d0d0d0d0d0'])
        # legacy 完整缓存不纳入台账：精简条目共 5 个，超额 2 → 清 g0/g1
        self.assertEqual(sorted(evicted),
                         sorted([key_of('cc00000000000000'),
                                 key_of('cc00000000000001')]))
        self.assertTrue(os.path.isdir(legacy), 'legacy 完整缓存绝不能被删除')
        self.assertTrue(os.path.isdir(appcache.cache_dir(key_of('d0d0d0d0d0d0d0d0'))))
        self.assertEqual(len(appcache.list_cache_entries()), 3)

    def test_evict_returns_keys_and_updates_ledger(self):
        for i in range(4):
            self.mk_entry('dd0000000000000%d' % i, last_used=100.0 + i)
        # 演算模式：只报告将清理谁，不动磁盘
        plan = appcache.evict_cache(dry_run=True)
        self.assertEqual(plan, [key_of('dd00000000000000')])
        self.assertTrue(os.path.isdir(appcache.cache_dir(key_of('dd00000000000000'))))
        self.assertEqual(len(appcache._load_lru()['entries']), 4)
        # 真正执行
        evicted = appcache.evict_cache()
        self.assertEqual(evicted, [key_of('dd00000000000000')])
        keys = {e['key'] for e in appcache.list_cache_entries()}
        self.assertNotIn(key_of('dd00000000000000'), keys)

    def test_staging_dirs_never_evicted(self):
        for i in range(5):
            self.mk_entry('ee0000000000000%d' % i, last_used=100.0 + i)
        # 正在进行的临时目录：点前缀 / .staging / .old / .tmp 结尾
        # （注意：不能用 s0 自己的 .staging 伴生目录——它随条目一起清理是正确行为）
        temps = ['.staging-abc123', 'keepme@%s.staging' % PROF, 'zzz.old', 'zzz.tmp']
        for name in temps:
            os.makedirs(os.path.join(self.root, name), exist_ok=True)
        appcache.evict_cache()
        for name in temps:
            self.assertTrue(os.path.isdir(os.path.join(self.root, name)),
                            '进行中目录 %s 绝不能被清理' % name)

    def test_never_deletes_outside_cache_root(self):
        # 模拟台账被改坏、条目指向缓存根之外：删除动作必须被根目录守卫拦下
        parent = os.path.dirname(os.path.abspath(self.root))
        victim = os.path.join(parent, 'mcapview-lru-victim')
        os.makedirs(victim, exist_ok=True)
        try:
            target = os.path.join(victim, 'important.dat')
            with open(target, 'wb') as fh:
                fh.write('重要数据'.encode('utf-8'))
            data = appcache._load_lru()
            data['entries']['../mcapview-lru-victim'] = dict(
                key='../mcapview-lru-victim', fid='victim', profile='x',
                last_used=1.0, bytes=8, name='victim')
            appcache._save_lru(data)
            for i in range(4):
                self.mk_entry('fa0000000000000%d' % i, last_used=100.0 + i)
            appcache.evict_cache()
            self.assertTrue(os.path.isfile(target),
                            '缓存根之外的文件绝不能被删除')
        finally:
            shutil.rmtree(victim, ignore_errors=True)


# ================================================================== C 精简缓存
class SlimProfileCase(CacheRootCase):
    def test_cache_key_semantics(self):
        self.assertEqual(appcache.cache_key('abc'), 'abc@' + PROF)
        self.assertEqual(appcache.cache_key('abc@' + PROF), 'abc@' + PROF,
                         '已带后缀的键必须幂等')
        self.assertEqual(appcache.cache_key('abc', profile=None), 'abc')
        self.assertEqual(appcache.profile_of('abc@' + PROF), PROF)
        self.assertEqual(appcache.profile_of('abc'), 'legacy-full')
        self.assertEqual(appcache.cache_key(''), '')

    def test_profile_keeps_topic_variants(self):
        keep = appcache.profile_keeps_topic
        self.assertTrue(keep('/robot0/sensor/camera2/compressed'))
        self.assertTrue(keep('/robot0/sensor/camera3/compressed'))
        self.assertTrue(keep('cam-3'))
        self.assertFalse(keep('/robot0/sensor/camera1/compressed'))
        self.assertFalse(keep('camera13'), 'camera13 不是 camera3')
        self.assertFalse(keep('camera_info'))
        self.assertFalse(keep(''))
        self.assertFalse(keep(None))
        # 其他配置名（比如将来的服务端精简配置）不做过滤
        self.assertTrue(keep('camera1', profile='other_profile'))

    def _build_mcap(self, name, channels):
        mcap = os.path.join(self.root, name + '.mcap')
        recs = [fx.header(), fx.schema(1, 'foxglove.CompressedImage')]
        png = fx.make_png(64, 48)
        for cid, topic in channels:
            recs.append(fx.channel(cid, 1, topic))
        n = 0
        for cid, _topic in channels:
            for i in range(3):
                ts = BASE + n * 33_000_000
                recs.append(fx.message(cid, i, ts, ts,
                                       fx.compressed_image(png, 'png', 'cam')))
                n += 1
        fx.assemble(mcap, recs)
        return mcap

    def test_prepare_slim_keeps_only_camera2_3(self):
        mcap = self._build_mcap('slim4', [
            (1, '/robot0/sensor/camera1/compressed'),
            (2, '/robot0/sensor/camera2/compressed'),
            (3, '/robot0/sensor/camera3/compressed'),
            (4, '/robot0/sensor/camera4/compressed'),
        ])
        outdir = appcache.cache_dir(appcache.cache_key('fidslim1'))
        man = PREP.prepare(mcap, outdir,
                           camera_pred=appcache.profile_keeps_topic,
                           profile=PROF)
        keys = sorted(c['key'] for c in man['cameras'] if c.get('playable'))
        self.assertEqual(keys, ['camera2', 'camera3'],
                         '精简缓存只应封装视角 2/3')
        self.assertEqual(man.get('cache_profile'), PROF)
        for name in os.listdir(outdir):
            self.assertNotIn('camera1', name)
            self.assertNotIn('camera4', name)

    def test_prepare_slim_rejects_when_no_primary(self):
        # 十一、无 camera2/3 时明确报错，不做「回退缓存全部通道」的假回退
        mcap = self._build_mcap('slimfb', [
            (1, '/robot0/sensor/camera5/compressed'),
            (2, '/robot0/sensor/camera7/compressed'),
        ])
        outdir = appcache.cache_dir(appcache.cache_key('fidslim2'))
        with self.assertRaises(MR.McapError):
            PREP.prepare(mcap, outdir,
                         camera_pred=appcache.profile_keeps_topic,
                         profile=PROF)
        self.assertFalse(os.path.isdir(outdir), '不应留下任何桌面缓存')
        self.assertFalse(os.path.isdir(outdir + '.staging'))

    def test_slim_and_full_coexist(self):
        mcap = self._build_mcap('both', [
            (1, '/robot0/sensor/camera1/compressed'),
            (2, '/robot0/sensor/camera2/compressed'),
        ])
        full_dir = appcache.cache_dir('fidboth')                # server 布局
        slim_dir = appcache.cache_dir(appcache.cache_key('fidboth'))
        man_full = PREP.prepare(mcap, full_dir)
        man_slim = PREP.prepare(mcap, slim_dir,
                                camera_pred=appcache.profile_keeps_topic,
                                profile=PROF)
        self.assertNotEqual(os.path.normcase(full_dir),
                            os.path.normcase(slim_dir))
        self.assertTrue(os.path.isdir(full_dir))
        self.assertTrue(os.path.isdir(slim_dir))
        self.assertIsNotNone(PREP.load_manifest(full_dir, source_path=mcap))
        self.assertIsNotNone(PREP.load_manifest(slim_dir, source_path=mcap))
        self.assertEqual(len([c for c in man_full['cameras']
                              if c.get('playable')]), 2)
        self.assertEqual(len([c for c in man_slim['cameras']
                              if c.get('playable')]), 1)


# ================================================================== D 桌面集成
class DesktopCase(CacheRootCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        QSettings('MCAPViewer', 'Desktop').clear()
        cls.dir = tempfile.mkdtemp(prefix='mcapview-lru-win-')
        cls.wins = []

    @classmethod
    def tearDownClass(cls):
        for w in cls.wins:
            try:
                w.close()
            except Exception:
                pass
        cls.app.processEvents()
        for w in cls.wins:
            try:
                w.deleteLater()
            except Exception:
                pass
        cls.app.processEvents()
        QSettings('MCAPViewer', 'Desktop').clear()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _build_mcap(self, name):
        mcap = os.path.join(self.dir, name + '.mcap')
        recs = [fx.header(), fx.schema(1, 'foxglove.CompressedImage')]
        recs.append(fx.channel(1, 1, '/robot0/sensor/camera2/compressed'))
        png = fx.make_png(64, 48)
        for i in range(3):
            ts = BASE + i * 33_000_000
            recs.append(fx.message(1, i, ts, ts,
                                   fx.compressed_image(png, 'png', 'cam')))
        fx.assemble(mcap, recs)
        return mcap

    def _slim_prepare(self, mcap):
        fid = appcache.file_id(mcap)
        outdir = D.cache_outdir(fid)
        man = PREP.prepare(mcap, outdir,
                           camera_pred=appcache.profile_keeps_topic,
                           profile=PROF)
        return fid, man

    def _window(self, mcap, man):
        win = D.MainWindow()
        self.wins.append(win)
        win.chk_auto.setChecked(False)      # 关掉后台预热，避免测试被定时器干扰
        win.folder = os.path.dirname(mcap)
        win.index = 0
        win.fid = appcache.file_id(mcap)
        win.items = [dict(path=mcap, name=os.path.basename(mcap),
                          size=os.path.getsize(mcap), mtime=int(time.time()),
                          mtime_str='', stem=os.path.basename(mcap),
                          dir=win.folder, rel_dir='')]
        win._apply(man, cached=True)
        return win

    def test_window_has_cache_info_fields(self):
        mcap = self._build_mcap('fields')
        fid, man = self._slim_prepare(mcap)
        win = self._window(mcap, man)
        for field in ('时长', '当前状态', '缓存大小', '视角'):
            self.assertIn(field, win.finfo, '「当前文件」栏缺少字段：%s' % field)
            self.assertNotEqual(win.finfo[field].text(), '—')
        self.assertTrue(win._details, '详细信息数据应当就绪')
        keys = [k for k, _v in win._details]
        self.assertIn('缓存策略', keys)
        self.assertIn('当前缓存', keys)
        self.assertIn('源路径', keys)
        win.close()

    def test_apply_fills_cache_fields_without_touching_cache(self):
        # 打开已缓存文件：界面字段齐全；缓存目录保持原样（登记由 QueueManager 负责）
        mcap = self._build_mcap('touch')
        fid, man = self._slim_prepare(mcap)
        win = self._window(mcap, man)
        for field in ('时长', '当前状态', '缓存大小', '视角'):
            self.assertIn(field, win.finfo)
        self.assertTrue(os.path.isdir(D.cache_outdir(fid)),
                        '打开文件不应删除当前缓存')
        self.assertTrue(win._details, '详细信息应当就绪')
        win.close()

    def test_discard_cache_reports_result(self):
        # discard 必须返回结构化结果并确认目录真的消失
        d = self.mk_entry('de1ede1ede1ede1e', size=512)
        res = appcache.discard_cache(appcache.cache_key('de1ede1ede1ede1e'))
        self.assertTrue(res['success'])
        self.assertGreaterEqual(res['freed_bytes'], 512)
        self.assertIsNone(res['error'])
        self.assertFalse(os.path.isdir(d), '删除后目录必须确实消失')
        # 缓存根之外的目标被守卫拒绝，且返回失败而不是谎报成功
        parent = os.path.dirname(os.path.abspath(self.root))
        outside = os.path.join(parent, 'mcapview-outside-probe')
        os.makedirs(outside, exist_ok=True)
        try:
            res = appcache.discard_cache('../mcapview-outside-probe')
            self.assertTrue(os.path.isdir(outside),
                            '缓存根之外的目录绝不能被删除（无论返回值如何）')
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_evict_failure_keeps_ledger_entry(self):
        # 删除失败时条目必须保留在台账，绝不谎报已清理
        for i in range(4):
            self.mk_entry('dd0000000000000%d' % i, last_used=100.0 + i)
        real_discard = appcache.discard_cache

        def broken(key):
            return dict(success=False, freed_bytes=0, error='模拟占用')

        appcache.discard_cache = broken
        try:
            evicted = appcache.evict_cache()
            self.assertEqual(evicted, [], '删除失败不得计入已清理')
            self.assertEqual(len(appcache._load_lru()['entries']), 4,
                             '失败的条目必须留在台账')
            for i in range(4):
                self.assertTrue(os.path.isdir(
                    appcache.cache_dir(key_of('dd0000000000000%d' % i))))
        finally:
            appcache.discard_cache = real_discard

    def test_corrupt_lru_file_does_not_break_start(self):
        # 损坏的 LRU 台账不能影响任何启动 / 打开 / 清理流程
        with open(appcache._lru_path(), 'w', encoding='utf-8') as fh:
            fh.write('??? 坏掉的台账 ???')
        self.mk_entry('0a10a10a10a10a10')
        self.assertEqual(len(appcache.list_cache_entries()), 1)
        self.assertEqual(appcache.evict_cache(), [])


if __name__ == '__main__':
    unittest.main()
