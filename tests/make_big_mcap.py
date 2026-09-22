"""make_big_mcap.py —— 生成用于性能验收的大 MCAP（默认 ~2GB）

把一份真实的小 MCAP 的消息**多轮重放**（时间戳每轮顺延），写成合法的大 MCAP：
  * 保留原始 schema / channel / 消息内容（数据分布与真实文件一致）
  * 可选 `--no-index` 生成"缺索引"版本（截断 summary + 重写 footer），
    用于验证 OPT-03（无索引文件只扫一遍）

用法：
  python tests/make_big_mcap.py --src <真实.mcap> --out <大.mcap> --target-gb 2
  python tests/make_big_mcap.py --src <真实.mcap> --out <大.mcap> --no-index
"""

import os
import sys
import time
import struct
import zlib
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import mcap.reader as MR
from mcap.writer import Writer, CompressionType


def strip_index(src, dst):
    """把带索引的 mcap 复制成"缺索引"版本（数据区完整，只是没有 summary）。

    末尾结构：… 数据区 | summary 区 | footer(29B) | magic(8B)
    全程流式复制，不把整个文件读进内存（2GB+ 也安全）。
    """
    size = os.path.getsize(src)
    with open(src, 'rb') as fh:
        fh.seek(max(0, size - 8192))
        tail_off = fh.tell()
        tail = fh.read()
    idx = tail.rfind(b'\x02' + struct.pack('<Q', 20))
    if idx < 0:
        raise SystemExit('找不到 footer，无法处理：%s' % src)
    summary_start = struct.unpack('<Q', tail[idx + 9:idx + 17])[0]
    if summary_start <= 0:
        raise SystemExit('该文件本来就没有索引（summary_start=0）')
    payload = struct.pack('<QQ', 0, 0)               # summary_start=0 → 无索引
    crc = zlib.crc32(payload) & 0xffffffff
    footer = (bytes([0x02]) + struct.pack('<Q', 20) + payload
              + struct.pack('<I', crc) + b'\x89MCAP0\r\n')
    done = 0
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        while done < summary_start:
            block = fi.read(min(8 << 20, summary_start - done))
            if not block:
                break
            fo.write(block)
            done += len(block)
        fo.write(footer)
    return summary_start


def build(src, out, target_bytes, compression=CompressionType.ZSTD,
          chunk_size=1024 * 1024):
    with open(src, 'rb') as fh:
        reader = MR.make_reader(fh)
        summ = reader.get_summary()
        if summ is None:
            raise SystemExit('源文件没有索引，请换一个带索引的文件作为模板')
        chans = {c.id: c for c in summ.channels.values()}
        schemas = {s.id: s for s in summ.schemas.values()}
        msgs = []
        for _s, ch, msg in reader.iter_messages(log_time_order=True):
            msgs.append((ch.id, msg.log_time, msg.publish_time,
                         msg.sequence, msg.data))
        start = summ.statistics.message_start_time if summ.statistics else 0
        end = summ.statistics.message_end_time if summ.statistics else 0
        span = max(1, end - start)

    print('模板消息 %d 条，单轮时长 %.2f 秒' % (len(msgs), span / 1e9))
    written = 0
    rounds = 0
    t0 = time.time()
    with open(out, 'wb') as fh:
        w = Writer(fh, chunk_size=chunk_size, compression=compression,
                   enable_crcs=True, use_statistics=True, use_summary_offsets=True)
        w.start(profile='', library='make_big_mcap')
        sid_map = {}
        for sid, s in schemas.items():
            sid_map[sid] = w.register_schema(
                name=s.name, encoding=s.encoding, data=s.data)
        cid_map = {}
        for cid, c in chans.items():
            cid_map[cid] = w.register_channel(
                topic=c.topic, message_encoding=c.message_encoding,
                schema_id=sid_map.get(c.schema_id), metadata=dict(c.metadata or {}))
        while written < target_bytes:
            off = rounds * (span + 1_000_000_000)      # 每轮顺延（留 1 秒空隙）
            for cid, lt, pt, seq, data in msgs:
                w.add_message(channel_id=cid_map[cid], log_time=lt + off,
                              data=data, publish_time=pt + off, sequence=seq)
                written += len(data)
            rounds += 1
            if rounds % 5 == 0:
                el = time.time() - t0
                print('  第 %d 轮，累计 payload %.2f GB，用时 %.0f s'
                      % (rounds, written / 1e9, el), flush=True)
        w.finish()
    size = os.path.getsize(out)
    print('已生成 %s：%.2f GB（%d 轮，用时 %.0f s）'
          % (out, size / 1e9, rounds, time.time() - t0))
    return size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--target-gb', type=float, default=2.0)
    ap.add_argument('--no-index', action='store_true',
                    help='从 --src 生成"缺索引"副本（不做多轮放大）')
    ap.add_argument('--compression', default='zstd', choices=('zstd', 'lz4', 'none'))
    a = ap.parse_args()

    if a.no_index:
        n = strip_index(a.src, a.out)
        print('已生成缺索引副本 %s（数据区 %.2f GB）' % (a.out, n / 1e9))
        return 0

    comp = {'zstd': CompressionType.ZSTD, 'lz4': CompressionType.LZ4,
            'none': CompressionType.NONE}[a.compression]
    build(a.src, a.out, int(a.target_gb * 1e9), compression=comp)
    return 0


if __name__ == '__main__':
    sys.exit(main())
