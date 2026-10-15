"""Virtual camera output thread: latest decoded frame -> v4l2loopback, continuously.

- A new frame is written as soon as it is decoded (no pacing delay).
- During short gaps (< HOLD_S) the last frame is repeated, so a reconnect blip looks like a
  freeze instead of a placeholder flash.
- Otherwise a placeholder ("PhoneCam disconnected" etc.) is written at IDLE_FPS, so video-call
  apps keep the device open and keep showing something sensible.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from av.video.reformatter import VideoReformatter

from phonecam.client import DecodedFrame
from phonecam.clock import ClockSync, now_us
from phonecam.frameslot import LatestFrameSlot
from phonecam.v4l2 import YUYV, PixelFormat, V4L2Writer, pack, pack_rgb

log = logging.getLogger(__name__)

HOLD_S = 1.5
IDLE_FPS = 10

PlaceholderRenderer = Callable[[int, int, str], np.ndarray]


def plain_placeholder(width: int, height: int, message: str) -> np.ndarray:
    """Fallback renderer without text (the GUI provides one with text)."""
    return np.full((height, width, 3), 24, np.uint8)


@dataclass
class OutputStats:
    fps: float = 0.0
    latency_ms: float | None = None  # capture -> written to v4l2
    pack_ms: float | None = None
    showing: str = "off"  # off | live | hold | placeholder


class VirtualCamera:
    def __init__(
        self,
        slot: LatestFrameSlot[DecodedFrame],
        clock: ClockSync,
        placeholder: PlaceholderRenderer = plain_placeholder,
    ) -> None:
        self._slot = slot
        self._clock = clock
        self._placeholder = placeholder
        self._placeholder_cache: dict[tuple[int, int, str, str], bytes] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._writer: V4L2Writer | None = None
        self.message = "PhoneCam disconnected"
        self.mirror = False  # horizontal flip of the output (like a mirror)
        self.error: str | None = None
        self.width, self.height, self.pixfmt = 1280, 720, YUYV
        self._written = 0
        self._lat_sum = 0
        self._lat_n = 0
        self._pack_sum = 0.0
        self._showing = "off"
        self._stats_t = time.monotonic()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def device(self) -> str | None:
        return self._writer.path if self._writer else None

    def start(
        self, device: str, width: int = 1280, height: int = 720, pixfmt: PixelFormat = YUYV
    ) -> None:
        """Open the device and start writing. Raises OSError if the device can't be used."""
        self.stop()
        self.width, self.height, self.pixfmt = width, height, pixfmt
        self._writer = V4L2Writer(device, width, height, pixfmt)
        self.error = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="v4l2-output", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(2)
            self._thread = None
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self._showing = "off"

    def stats(self) -> OutputStats:
        now = time.monotonic()
        dt = max(1e-3, now - self._stats_t)
        s = OutputStats(
            fps=self._written / dt,
            latency_ms=self._lat_sum / self._lat_n / 1000 if self._lat_n else None,
            pack_ms=self._pack_sum / self._lat_n * 1000 if self._lat_n else None,
            showing=self._showing,
        )
        self._written = self._lat_n = self._lat_sum = 0
        self._pack_sum = 0.0
        self._stats_t = now
        return s

    def _placeholder_bytes(self) -> bytes:
        key = (self.width, self.height, self.pixfmt.fourcc, self.message)
        data = self._placeholder_cache.get(key)
        if data is None:
            rgb = self._placeholder(self.width, self.height, self.message)
            data = pack_rgb(rgb, self.width, self.height, self.pixfmt)
            self._placeholder_cache = {key: data}  # keep only the current one
        return data

    def _run(self) -> None:
        writer = self._writer
        assert writer is not None
        last: bytes | None = None
        last_t = 0.0
        reformatter = VideoReformatter()  # owned by this thread (see v4l2.pack)
        try:
            while not self._stop.is_set():
                df = self._slot.get(1 / IDLE_FPS)
                now = time.monotonic()
                if df is not None:
                    t0 = time.perf_counter()
                    data = pack(
                        df.frame,
                        self.width,
                        self.height,
                        self.pixfmt,
                        df.rotation,
                        self.mirror,
                        reformatter,
                    )
                    writer.write(data)
                    t_written = now_us()
                    self._pack_sum += time.perf_counter() - t0
                    capture_pc = self._clock.phone_to_pc(df.pts_us)
                    if capture_pc is not None:
                        self._lat_sum += t_written - capture_pc
                        self._lat_n += 1
                    if log.isEnabledFor(logging.DEBUG) and self._written % 60 == 0:
                        log.debug(
                            "stages: decode->written %.1f ms, capture->written %s ms",
                            (t_written - df.decoded_us) / 1000,
                            None if capture_pc is None else (t_written - capture_pc) / 1000,
                        )
                    last, last_t = data, now
                    self._showing = "live"
                elif last is not None and now - last_t < HOLD_S:
                    writer.write(last)
                    self._showing = "hold"
                else:
                    last = None
                    writer.write(self._placeholder_bytes())
                    self._showing = "placeholder"
                self._written += 1
        except OSError as e:
            self.error = str(e)
            log.error("virtual camera stopped: %s", e)
            self._showing = "off"
