"""Accumulator optimization byte/lifecycle tests."""

import hashlib
import json
import os
import shutil
import tempfile
import unittest

import prepare as PREP


class AccumulatorOptimizationCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='acc-opt-')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_imu_json_is_byte_identical(self):
        acc = PREP._new_imu_accumulator()
        rows = []
        t0 = 1_700_000_000_000_000_000
        for i in range(20):
            ts = t0 + i * 5_000_000
            sample = dict(angular_velocity=[0.1 + i, -2.0, 3.25],
                          linear_acceleration=[4.5, 5.0 + i, 6.75])
            PREP._append_imu(acc, ts, sample)
            rows.append((ts, sample))
        ref = dict(
            topic='/imu',
            t=[(ts - t0) / PREP.NS for ts, _ in rows],
            av=[[s['angular_velocity'][i] for _ts, s in rows] for i in range(3)],
            la=[[s['linear_acceleration'][i] for _ts, s in rows] for i in range(3)])
        expected = json.dumps(ref, separators=(',', ':')).encode('utf-8')
        PREP._finish_imu(self.root, [dict(id=1, topic='/imu', samples=acc)], t0)
        with open(os.path.join(self.root, 'imu.json'), 'rb') as fh:
            got = fh.read()
        self.assertEqual(hashlib.sha256(got).digest(),
                         hashlib.sha256(expected).digest())
        self.assertEqual(got, expected)

    def test_audio_spool_is_removed_after_finalize(self):
        spool = os.path.join(self.root, '.audio.tmp')
        payloads = [b'\x01\x02' * 4, b'\x03\x04' * 3]
        with open(spool, 'wb') as fh:
            chunks = []
            for i, payload in enumerate(payloads):
                off = fh.tell(); fh.write(payload)
                chunks.append((100 + i, off, len(payload)))
        ch = dict(id=1, topic='/audio', config={
            'sample_rate': 16000, 'channels': 1, 'bit_depth': 16,
            'format': 'PCM_16', 'device_name': ''}, chunks=chunks,
            spool_path=spool, spool_fh=None, packet_count=2,
            payload_bytes=sum(map(len, payloads)))
        PREP._finish_audio(self.root, [ch], 0)
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'audio.wav')))
        self.assertFalse(os.path.exists(spool))

    def test_audio_spool_removed_for_unsupported_format(self):
        spool = os.path.join(self.root, '.audio-invalid.tmp')
        with open(spool, 'wb') as fh:
            fh.write(b'1234')
        ch = dict(id=1, topic='/audio', config={
            'sample_rate': 16000, 'channels': 1, 'bit_depth': 24,
            'format': 'PCM_24', 'device_name': ''}, chunks=[],
            spool_path=spool, spool_fh=None, packet_count=0, payload_bytes=4)
        PREP._finish_audio(self.root, [ch], 0)
        self.assertFalse(os.path.exists(spool))


if __name__ == '__main__':
    unittest.main()
