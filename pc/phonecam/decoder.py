"""H.264 decoding with PyAV, tuned for low delay."""

from __future__ import annotations

import logging
from fractions import Fraction

import av
import av.error
from av.codec.context import Flags

log = logging.getLogger(__name__)

TIME_BASE = Fraction(1, 1_000_000)


class H264Decoder:
    """Feeds CONFIG (SPS/PPS) and access units to an FFmpeg H.264 decoder.

    - `low_delay` flag, slice threading only (frame threading adds one frame of delay per thread).
    - A new decoder is created whenever CONFIG changes (resolution/encoder restart).
    - Until a keyframe arrives after CONFIG or after a decode error, frames are skipped.
    """

    def __init__(self) -> None:
        self._ctx: av.CodecContext | None = None
        self._csd: bytes | None = None
        self._need_key = True
        self._prepend_csd = False
        self.skipped = 0
        self.errors = 0
        # VAAPI hardware decode is added in phase 5; PyAV's bundled FFmpeg lacks it.
        self.name = "software (FFmpeg h264)"

    @property
    def configured(self) -> bool:
        return self._ctx is not None

    def configure(self, csd: bytes) -> None:
        if csd == self._csd and self._ctx is not None:
            return
        log.info("decoder (re)configured, %d bytes of SPS/PPS", len(csd))
        ctx = av.CodecContext.create("h264", "r")
        ctx.flags |= Flags.low_delay
        ctx.thread_type = "SLICE"
        ctx.thread_count = 2
        ctx.open()
        self._ctx = ctx
        self._csd = csd
        self._need_key = True

    def decode(self, au: bytes, pts_us: int, keyframe: bool) -> list[av.VideoFrame]:
        if self._ctx is None or self._csd is None:
            self.skipped += 1
            return []
        if self._need_key:
            if not keyframe:
                self.skipped += 1
                return []
            self._need_key = False
            self._prepend_csd = True
        data = self._csd + au if self._prepend_csd else au
        self._prepend_csd = False
        packet = av.Packet(data)
        packet.pts = pts_us
        packet.dts = pts_us
        packet.time_base = TIME_BASE
        try:
            return list(self._ctx.decode(packet))
        except av.error.FFmpegError as e:
            self.errors += 1
            log.warning("decode error (%s); waiting for next keyframe", e)
            self._need_key = True
            return []

    def reset(self) -> None:
        self._ctx = None
        self._csd = None
        self._need_key = True
