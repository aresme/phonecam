"""H.264 decoding with PyAV, tuned for low delay."""

from __future__ import annotations

import logging
from fractions import Fraction

import av
import av.error
from av.codec.context import Flags

log = logging.getLogger(__name__)

TIME_BASE = Fraction(1, 1_000_000)


def vaapi_available() -> bool:
    try:
        from av.codec.hwaccel import hwdevices_available

        return "vaapi" in hwdevices_available()
    except Exception:
        return False


class H264Decoder:
    """Feeds CONFIG (SPS/PPS) and access units to an FFmpeg H.264 decoder.

    - `low_delay` flag, slice threading only (frame threading adds one frame of delay per thread).
    - A new decoder is created whenever CONFIG changes (resolution/encoder restart).
    - Until a keyframe arrives after CONFIG or after a decode error, frames are skipped.
    - mode "vaapi" uses Intel/AMD hardware decoding when PyAV was built with VAAPI, and falls back
      to software if it can't be opened or rejects the stream (e.g. plain Baseline profile).
      On a fast CPU software decoding has the lower latency (docs/latency.md); VAAPI saves CPU.
    """

    def __init__(self, mode: str = "software") -> None:
        self._ctx: av.CodecContext | None = None
        self._csd: bytes | None = None
        self._need_key = True
        self._prepend_csd = False
        self._hw_failed = False
        self.mode = mode
        self.skipped = 0
        self.errors = 0
        self.name = "software (FFmpeg h264)"

    @property
    def configured(self) -> bool:
        return self._ctx is not None

    def set_mode(self, mode: str) -> None:
        """Switch decoder type; takes effect at the next keyframe."""
        if mode != self.mode:
            self.mode = mode
            self._hw_failed = False
            if self._csd is not None:
                self._open(self._csd)

    def configure(self, csd: bytes) -> None:
        if csd == self._csd and self._ctx is not None:
            return
        log.info("decoder (re)configured, %d bytes of SPS/PPS", len(csd))
        self._open(csd)

    def _open(self, csd: bytes) -> None:
        ctx = None
        if self.mode == "vaapi" and not self._hw_failed:
            if vaapi_available():
                try:
                    from av.codec.hwaccel import HWAccel

                    ctx = av.CodecContext.create(
                        "h264",
                        "r",
                        hwaccel=HWAccel(device_type="vaapi", allow_software_fallback=False),
                    )
                    ctx.flags |= Flags.low_delay
                    ctx.open()
                    self.name = "VAAPI (hardware)"
                except Exception as e:
                    log.warning("VAAPI decoder unavailable (%s); using software", e)
                    self._hw_failed = True
                    ctx = None
            else:
                log.warning("this PyAV build has no VAAPI (see scripts/build-pyav-vaapi.sh)")
                self._hw_failed = True
        if ctx is None:
            ctx = av.CodecContext.create("h264", "r")
            ctx.flags |= Flags.low_delay
            ctx.thread_type = "SLICE"
            ctx.thread_count = 2
            ctx.open()
            self.name = "software (FFmpeg h264)" + (" — VAAPI failed" if self._hw_failed else "")
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
            if self.name.startswith("VAAPI") and keyframe:
                log.warning("VAAPI rejected the stream (%s); falling back to software", e)
                self._hw_failed = True
                self._open(self._csd)
                self._need_key = False
                self._prepend_csd = False
                try:
                    return list(self._ctx.decode(av.Packet(self._csd + au)))
                except av.error.FFmpegError:
                    pass
            log.warning("decode error (%s); waiting for next keyframe", e)
            self._need_key = True
            return []

    def reset(self) -> None:
        self._ctx = None
        self._csd = None
        self._need_key = True
