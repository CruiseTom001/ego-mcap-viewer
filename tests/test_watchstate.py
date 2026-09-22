"""观看状态持久化测试（watchstate）

覆盖：读写往返 / 源文件变化=新版本 / 损坏自动备份 / 非法状态过滤 / 目录隔离。
"""

import os
import json
import time
import unittest

import watchstate as WS


class WatchStateCase(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.get('MCAPVIEWER_STATE_DIR')
        import tempfile
        # 每条用例独立的临时状态目录
        self.state_dir = tempfile.mkdtemp(prefix='mcapview-state-')
        os.environ['MCAPVIEWER_STATE_DIR'] = self.state_dir
        self.folder = tempfile.mkdtemp(prefix='mcapview-folder-')

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop('MCAPVIEWER_STATE_DIR', None)
        else:
            os.environ['MCAPVIEWER_STATE_DIR'] = self._old_env
        import shutil
        shutil.rmtree(self.state_dir, ignore_errors=True)
        shutil.rmtree(self.folder, ignore_errors=True)

    def _mcap(self, name='a.mcap'):
        p = os.path.join(self.folder, name)
        with open(p, 'wb') as fh:
            fh.write(b'MCAPDATA' * 10)
        return p

    def test_roundtrip(self):
        p = self._mcap()
        sid = WS.source_id(p)
        data = WS.load_state(self.folder)
        data['items'][sid] = dict(
            path=p, name='a.mcap', size=80, mtime_ns=123,
            state='WATCHED', watched_at_ns=456, watched_reason='natural_end',
            last_position_s=1.5, cleanup_pending=False, error=None)
        WS.save_state(self.folder, data)
        again = WS.load_state(self.folder)
        self.assertIn(sid, again['items'])
        self.assertEqual(again['items'][sid]['state'], 'WATCHED')
        self.assertEqual(again['items'][sid]['watched_reason'], 'natural_end')
        self.assertEqual(again['items'][sid]['last_position_s'], 1.5)

    def test_source_id_changes_when_mtime_changes(self):
        p = self._mcap()
        sid1 = WS.source_id(p)
        st = os.stat(p)
        back = (st.st_atime_ns, st.st_mtime_ns)
        time.sleep(0.02)
        os.utime(p, ns=(back[0], back[1] + 2_000_000))
        sid2 = WS.source_id(p)
        self.assertNotEqual(sid1, sid2, 'mtime 变化必须产生新版本 id')

    def test_corrupt_state_is_backed_up_and_recovered(self):
        p = os.path.join(WS.folder_state_path(self.folder))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write('{ 这不是 JSON !!!')
        data = WS.load_state(self.folder)
        self.assertEqual(data['items'], {}, '损坏状态按空处理，不许崩溃')
        backups = [f for f in os.listdir(self.state_dir) if '.corrupt-' in f]
        self.assertEqual(len(backups), 1, '损坏文件必须备份为 .corrupt-<时间戳>')

    def test_unknown_states_are_filtered(self):
        p = self._mcap()
        sid = WS.source_id(p)
        path = WS.folder_state_path(self.folder)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'items': {sid: {'state': 'PLAYING'}},     # 运行态不入盘
                       'other': {sid: {'state': 'WATCHED'}}}, fh)
        data = WS.load_state(self.folder)
        self.assertNotIn(sid, data['items'], 'CACHING/PLAYING 等运行态必须被忽略')

    def test_folder_hash_is_stable_and_distinct(self):
        a = WS.folder_hash(self.folder)
        b = WS.folder_hash(self.folder.rstrip('\\/') + os.sep)
        self.assertEqual(a, b, '同一文件夹不同写法必须同一哈希')
        import tempfile
        other = tempfile.mkdtemp(prefix='mcapview-folder2-')
        try:
            self.assertNotEqual(a, WS.folder_hash(other))
        finally:
            os.rmdir(other)

    def test_state_dir_env_override(self):
        self.assertEqual(os.path.abspath(WS.state_dir()),
                         os.path.abspath(self.state_dir))


if __name__ == '__main__':
    unittest.main()
