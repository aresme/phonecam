"""Phone microphone → PipeWire virtual source "PhoneCam Mic".

- VirtualMic: `pactl load-module module-pipe-source` reading s16le/48 kHz/mono from a FIFO.
  PipeWire outputs silence when the FIFO is empty, so apps never stall.
- AudioPipeline: decodes AUDIO packets (Opus via PyAV, or raw PCM) and writes each 20 ms frame
  at *capture time + target latency* (a jitter buffer anchored on the phone's timestamps and the
  PING/PONG clock offset). That smooths network bursts, sets a fixed audio delay for lip sync
  with the video, and drops frames that arrive too late. The FIFO is shrunk to 4 KB (~42 ms) so
  no hidden backlog builds up. Bursts written faster than real time would be trimmed by the
  pipe source anyway.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import fcntl
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import av

from phonecam.clock import ClockSync, now_us

log = logging.getLogger(__name__)

SOURCE_NAME = "phonecam_mic"
DESCRIPTION = "PhoneCam Mic"
RATE = 48_000
F_SETPIPE_SZ = 1031


def fifo_path() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "phonecam-mic.fifo"


class VirtualMic:
    """Creates/removes the PipeWire source. pactl calls block briefly: use a worker thread."""

    def __init__(self) -> None:
        self.module_id: str | None = None
        self.error: str | None = None

    @staticmethod
    def available() -> bool:
        return shutil.which("pactl") is not None

    def _pactl(self, *args: str) -> str:
        res = subprocess.run(
            ["pactl", *args], capture_output=True, text=True, timeout=5, check=False
        )
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout).strip() or f"pactl {args[0]} failed")
        return res.stdout

    def _cleanup_stale(self) -> None:
        """Unload PhoneCam pipe sources left over from a previous run that didn't exit cleanly."""
        try:
            out = self._pactl("list", "short", "modules")
        except Exception:
            return
        for line in out.splitlines():
            parts = line.split("\t")
            if (
                len(parts) >= 3
                and parts[1] == "module-pipe-source"
                and f"source_name={SOURCE_NAME}" in parts[2]
            ):
                with contextlib.suppress(Exception):
                    self._pactl("unload-module", parts[0])
                    log.info("removed stale PhoneCam Mic module %s", parts[0])

    def create(self) -> bool:
        if self.module_id:
            return True
        if not self.available():
            self.error = "pactl not found (install pulseaudio-utils)"
            return False
        self._cleanup_stale()
        path = fifo_path()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        try:
            self.module_id = self._pactl(
                "load-module",
                "module-pipe-source",
                f"source_name={SOURCE_NAME}",
                f"file={path}",
                "format=s16le",
                f"rate={RATE}",
                "channels=1",
                f'source_properties=device.description="{DESCRIPTION}"',
            ).strip()
        except Exception as e:
            self.error = str(e)
            log.error("could not create %s: %s", DESCRIPTION, e)
            return False
        self.error = None
        log.info("created %s (module %s, %s)", DESCRIPTION, self.module_id, path)
        return True

    def remove(self) -> None:
        if self.module_id:
            with contextlib.suppress(Exception):
                self._pactl("unload-module", self.module_id)
            self.module_id = None
        with contextlib.suppress(FileNotFoundError):
            fifo_path().unlink()


@dataclass
class AudioStats:
    active: bool = False
    codec: str = ""
    latency_ms: float | None = None  # capture -> written to the PipeWire FIFO
    target_ms: float = 0.0
    late_dropped: int = 0
    frames: int = 0


class AudioPipeline:
    """Thread: AUDIO packets in, scheduled PCM out to the FIFO."""

    def __init__(self, clock: ClockSync) -> None:
        self._clock = clock
        self._q: queue.Queue[tuple[str, object, int]] = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # A/V sync: auto = match the video's capture->v4l2 latency (set by the GUI); manual = fixed.
        self.auto = True
        self.manual_ms = 120.0
        self.video_latency_ms: float | None = None
        self.arrival_ms: float | None = None  # EMA of capture -> decoded on the PC
        self.target_ms = 120.0
        self.max_late_ms = 60.0
        self._codec = ""
        self._decoder: av.CodecContext | None = None
        self._resampler: av.AudioResampler | None = None
        self._fd: int | None = None
        self._lat_sum = 0
        self._lat_n = 0
        self._late = 0
        self._frames = 0
        self._last_frame_t = 0.0

    # ---- producer side (called from the network thread; never blocks) ----

    def submit_config(self, payload: bytes) -> None:
        self._put(("config", payload, 0))

    def submit(self, payload: bytes, pts_us: int) -> None:
        self._put(("frame", payload, pts_us))

    def _put(self, item: tuple[str, object, int]) -> None:
        try:
            self._q.put_nowait(item)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self._q.get_nowait()  # drop the oldest
            with contextlib.suppress(queue.Full):
                self._q.put_nowait(item)

    # ---- lifecycle ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._put(("stop", b"", 0))
        if self._thread:
            self._thread.join(2)
        self._close_fifo()

    def stats(self) -> AudioStats:
        s = AudioStats(
            active=time.monotonic() - self._last_frame_t < 1.0,
            codec=self._codec,
            latency_ms=self._lat_sum / self._lat_n / 1000 if self._lat_n else None,
            target_ms=self.target_ms,
            late_dropped=self._late,
            frames=self._frames,
        )
        self._lat_sum = self._lat_n = 0
        return s

    # ---- worker ----

    def _open_fifo(self) -> int | None:
        if self._fd is not None:
            return self._fd
        try:
            # Non-blocking open fails (ENXIO) instead of hanging if PipeWire isn't reading.
            fd = os.open(fifo_path(), os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            if e.errno not in (errno.ENXIO, errno.ENOENT):
                log.warning("cannot open %s: %s", fifo_path(), e)
            return None
        os.set_blocking(fd, True)
        with contextlib.suppress(OSError):
            fcntl.fcntl(fd, F_SETPIPE_SZ, 4096)  # ~42 ms: no hidden backlog
        self._fd = fd
        return fd

    def _close_fifo(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None

    def _effective_target(self) -> float:
        """Never below what audio needs to arrive (else every frame would be late)."""
        floor = (self.arrival_ms or 60.0) + 25.0
        wanted = self.video_latency_ms if self.auto and self.video_latency_ms else self.manual_ms
        return max(floor, min(wanted, 600.0))

    def _configure(self, payload: bytes) -> None:
        cfg = json.loads(payload)
        self._codec = cfg.get("codec", "")
        self._decoder = None
        if self._codec == "opus":
            ctx = av.CodecContext.create("opus", "r")
            if cfg.get("csd"):
                head = base64.b64decode(cfg["csd"])
                i = head.find(b"OpusHead")  # tolerate Android's wrapped csd
                ctx.extradata = head[i : i + 19] if i >= 0 else head
            ctx.open()
            self._decoder = ctx
            self._resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        elif self._codec != "pcm_s16le":
            log.warning("unsupported audio codec %r", self._codec)
        log.info("audio stream: %s", cfg)

    def _decode(self, payload: bytes) -> bytes:
        if self._codec == "pcm_s16le":
            return payload
        if self._decoder is None or self._resampler is None:
            return b""
        out = []
        for frame in self._decoder.decode(av.Packet(payload)):
            for r in self._resampler.resample(frame):
                out.append(r.to_ndarray().astype("<i2").tobytes())
        return b"".join(out)

    def _run(self) -> None:
        while not self._stop.is_set():
            kind, payload, pts = self._q.get()
            if kind == "stop":
                break
            try:
                if kind == "config":
                    self._configure(payload)  # type: ignore[arg-type]
                    continue
                pcm = self._decode(payload)  # type: ignore[arg-type]
            except Exception as e:
                log.warning("audio decode failed: %s", e)
                continue
            if not pcm:
                continue
            self._frames += 1
            self._last_frame_t = time.monotonic()
            capture_pc = self._clock.phone_to_pc(pts)
            if capture_pc is not None:
                arrival = (now_us() - capture_pc) / 1000
                self.arrival_ms = (
                    arrival if self.arrival_ms is None else 0.95 * self.arrival_ms + 0.05 * arrival
                )
                self.target_ms = self._effective_target()
                due = capture_pc + int(self.target_ms * 1000)
                wait = (due - now_us()) / 1e6
                if wait < -self.max_late_ms / 1000:
                    self._late += 1  # too late for its slot: drop rather than drift
                    continue
                if wait > 0:
                    time.sleep(min(wait, 0.5))
            fd = self._open_fifo()
            if fd is None:
                continue
            try:
                os.write(fd, pcm)
            except OSError as e:
                log.warning("writing PhoneCam Mic FIFO failed: %s", e)
                self._close_fifo()
                continue
            if capture_pc is not None:
                self._lat_sum += now_us() - capture_pc
                self._lat_n += 1
