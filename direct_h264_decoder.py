"""direct_h264_decoder.py —— P1.6F-C：内存内 H264 Access Unit 直解原型

接口（合同第 16 节）：
    DirectH264Decoder.open() / reset() / decode_access_unit(payload) / close()

backend：PyAV / libavcodec in-process 软解（不生成 MP4、不起子进程）。
payload 为 Annex-B（含 SPS/PPS/IDR 的自包含 AU 可直接解）。

注意：本模块是 F-C 原型，**尚未接入 Desktop 播放器**。
"""

import av


class DecodedFrame(object):
    __slots__ = ('image', 'width', 'height', 'pix_fmt')

    def __init__(self, image, width, height, pix_fmt):
        self.image = image          # numpy BGR24
        self.width = width
        self.height = height
        self.pix_fmt = pix_fmt


class DirectH264Decoder(object):
    """内存内 Annex-B 直解（软件解码）"""

    def __init__(self, threads=1):
        self.threads = int(threads)
        self._codec = None
        self.opened = 0
        self.decoded_frames = 0
        self.failed_payloads = 0

    # ---------------------------------------------------------------- 生命周期
    def open(self):
        self._codec = self._make()
        self.opened += 1
        return self

    def _make(self):
        ctx = av.CodecContext.create('h264', 'r')
        try:
            ctx.thread_count = self.threads
        except Exception:
            pass
        ctx.open()
        return ctx

    def reset(self):
        """seek / 切换 camera 时清空解码状态（保留 backend 对象）"""
        if self._codec is not None:
            try:
                self._codec.flush_buffers()
            except Exception:
                pass
        return self

    def close(self):
        if self._codec is not None:
            try:
                self._codec.close()
            except Exception:
                pass
        self._codec = None

    # ---------------------------------------------------------------- 解码
    def decode_access_unit(self, payload, want_bgr=True):
        """解码一个 Access Unit；返回 [DecodedFrame]（可能为空=需更多数据）"""
        if self._codec is None:
            self.open()
        out = []
        try:
            frames = self._codec.decode(av.packet.Packet(bytes(payload)))
        except Exception:
            self.failed_payloads += 1
            return out
        for f in frames:
            try:
                img = f.to_ndarray(format='bgr24') if want_bgr else None
                out.append(DecodedFrame(img, f.width, f.height,
                                        getattr(f.format, 'name', '')))
            except Exception:
                self.failed_payloads += 1
        self.decoded_frames += len(out)
        return out

    def stats(self):
        return dict(opened=self.opened, decoded=self.decoded_frames,
                    failed=self.failed_payloads)
