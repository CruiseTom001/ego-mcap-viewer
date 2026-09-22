"""Indexed, chunk-at-a-time MCAP reader used by the desktop cache path.

The module deliberately owns the small amount of candidate selection logic it
needs instead of depending on ``mcap.reader._*`` internals.  It only exposes
MCAP records; cache/video/audio/IMU handling remains in ``mcap_reader`` and
``prepare``.
"""

from __future__ import annotations

from mcap.reader import ReadDataStream
from mcap.exceptions import McapError
from mcap.records import Chunk, Message
from mcap.stream_reader import breakup_chunk


def candidate_chunks(summary, topics=None, start_time=None, end_time=None):
    """Return conservative candidate ChunkIndex records in file order."""
    wanted = None if topics is None else set(topics)
    out = []
    for ci in sorted(summary.chunk_indexes,
                     key=lambda x: int(x.chunk_start_offset)):
        if start_time is not None and ci.message_end_time < start_time:
            continue
        if end_time is not None and ci.message_start_time >= end_time:
            continue
        if wanted is None or not ci.message_index_offsets:
            out.append(ci)
            continue
        keep = False
        for channel_id in ci.message_index_offsets:
            channel = summary.channels.get(channel_id)
            if channel is None:
                # Cannot prove exclusion for an unknown channel; be
                # conservative and let record parsing report corruption.
                keep = True
                break
            if channel.topic in wanted:
                keep = True
                break
        if keep:
            out.append(ci)
    return out


class ChunkStreamingIndexedReader:
    """Yield selected messages immediately while retaining one chunk only."""

    def __init__(self, stream, summary, validate_crcs=True):
        self._stream = stream
        self._summary = summary
        self._validate_crcs = bool(validate_crcs)

    def iter_messages(self, topics=None, start_time=None, end_time=None):
        summary = self._summary
        wanted = None if topics is None else set(topics)
        for ci in candidate_chunks(summary, wanted, start_time, end_time):
            self._stream.seek(int(ci.chunk_start_offset) + 1 + 8)
            chunk = Chunk.read(ReadDataStream(self._stream))
            records = breakup_chunk(chunk, validate_crc=self._validate_crcs)
            try:
                for record in records:
                    if not isinstance(record, Message):
                        continue
                    channel = summary.channels.get(record.channel_id)
                    if channel is None:
                        raise McapError(
                            'message references unknown channel %s' % record.channel_id)
                    if wanted is not None and channel.topic not in wanted:
                        continue
                    if start_time is not None and record.log_time < start_time:
                        continue
                    if end_time is not None and record.log_time >= end_time:
                        continue
                    if channel.schema_id == 0:
                        schema = None
                    else:
                        try:
                            schema = summary.schemas[channel.schema_id]
                        except KeyError as exc:
                            raise McapError(
                                'channel references unknown schema %s' %
                                channel.schema_id) from exc
                    yield schema, channel, record
            finally:
                # No Message or payload reference survives this chunk.
                del records
                del chunk


__all__ = ['ChunkStreamingIndexedReader', 'candidate_chunks']
