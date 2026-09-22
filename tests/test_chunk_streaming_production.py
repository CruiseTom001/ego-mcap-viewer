"""Production indexed-reader routing and semantic equivalence tests."""

import os
import shutil
import tempfile
import unittest

from mcap.writer import Writer, CompressionType, IndexType

import mcap_reader as MR


class ChunkStreamingProductionCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='mcap-prod-reader-')
        cls.path = os.path.join(cls.root, 'indexed.mcap')
        with open(cls.path, 'wb') as fh:
            w = Writer(fh, chunk_size=256, compression=CompressionType.NONE,
                       index_types=IndexType.ALL, enable_crcs=True,
                       enable_data_crcs=False)
            w.start(profile='test', library='production-reader-test')
            sid = w.register_schema(name='test.Schema', encoding='protobuf', data=b'')
            cid2 = w.register_channel(topic='/camera2/compressed',
                                      message_encoding='protobuf', schema_id=sid)
            cid3 = w.register_channel(topic='/camera3/compressed',
                                      message_encoding='protobuf', schema_id=sid)
            for i in range(20):
                w.add_message(channel_id=cid2, log_time=1_000 + i * 2,
                              publish_time=1_000 + i * 2,
                              sequence=i, data=b'cam2-%02d' % i)
                w.add_message(channel_id=cid3, log_time=1_001 + i * 2,
                              publish_time=1_001 + i * 2,
                              sequence=i, data=b'cam3-%02d' % i)
            w.finish()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def _read(self, mode):
        old = os.environ.get('MCAPVIEWER_READER_MODE')
        os.environ['MCAPVIEWER_READER_MODE'] = mode
        try:
            r = MR.McapReader(self.path)
            return [(cid, ts, data) for cid, ts, _p, _s, data in
                    r.iter_messages(log_time_order=False)]
        finally:
            if old is None:
                os.environ.pop('MCAPVIEWER_READER_MODE', None)
            else:
                os.environ['MCAPVIEWER_READER_MODE'] = old

    def test_indexed_default_route_matches_official(self):
        self.assertTrue(MR.McapReader(self.path).has_index)
        official = self._read('official')
        streaming = self._read('chunk_streaming')
        self.assertEqual(streaming, official)
        self.assertEqual(len(streaming), 40)

    def test_topic_filter_and_file_order(self):
        old = os.environ.get('MCAPVIEWER_READER_MODE')
        os.environ['MCAPVIEWER_READER_MODE'] = 'chunk_streaming'
        try:
            r = MR.McapReader(self.path)
            cam2 = [cid for cid, c in r.channels.items()
                    if c['topic'] == '/camera2/compressed']
            got = [cid for cid, _ts, _p, _s, _d in
                   r.iter_messages(set(cam2), log_time_order=False)]
            self.assertEqual(len(got), 20)
            self.assertEqual(set(got), set(cam2))
        finally:
            if old is None:
                os.environ.pop('MCAPVIEWER_READER_MODE', None)
            else:
                os.environ['MCAPVIEWER_READER_MODE'] = old

    def test_official_debug_mode_remains_available(self):
        self.assertEqual(len(self._read('official')), 40)

    def test_indexed_chunk_crc_failure_is_not_fallback(self):
        bad = os.path.join(self.root, 'indexed-bad-crc.mcap')
        shutil.copy2(self.path, bad)
        old = os.environ.get('MCAPVIEWER_READER_MODE')
        os.environ['MCAPVIEWER_READER_MODE'] = 'chunk_streaming'
        try:
            r = MR.McapReader(bad)
            ci = r._official_summary.chunk_indexes[0]
            # Skip the Chunk record header and fixed Chunk fields; flip a byte
            # inside compressed/uncompressed data while leaving the summary.
            off = int(ci.chunk_start_offset) + 9 + 8 + 8 + 8 + 4 + 4 + 8 + 1
            with open(bad, 'r+b') as fh:
                fh.seek(off)
                value = fh.read(1)
                fh.seek(off)
                fh.write(bytes([value[0] ^ 0xFF]))
            with self.assertRaises(MR.McapError):
                list(r.iter_messages(log_time_order=False))
        finally:
            if old is None:
                os.environ.pop('MCAPVIEWER_READER_MODE', None)
            else:
                os.environ['MCAPVIEWER_READER_MODE'] = old


if __name__ == '__main__':
    unittest.main()
