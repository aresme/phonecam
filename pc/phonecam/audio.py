"""Phone microphone → PipeWire virtual source "PhoneCam Mic".

- VirtualMic: a null sink with `media.class=Audio/Source/Virtual`: a microphone apps can select,
  with an input port we play into. (module-pipe-source was measured at 108-180 ms of extra,
  growing latency; this path adds ~13-50 ms. See docs/latency.md.)
- PwCatSink: a persistent `pw-cat --playback` stream (20 ms node latency, 4 KB stdin pipe so no
  backlog hides in it), explicitly linked to the virtual source with pw-link. PipeWire plays
  silence when we don't write, so apps never stall.
- AudioPipeline: decodes AUDIO packets (Opus via PyAV, or raw PCM) and writes each 20 ms frame
  at *capture time + target latency* (a jitter buffer anchored on the phone's timestamps and the
  PING/PONG clock offset). That smooths network bursts, sets the audio delay for lip sync with
  the video, and drops frames that arrive too late (which also absorbs clock drift).
"""

from __future__ import annotations

import base64
import contextlib
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

import av

from phonecam.clock import ClockSync, now_us

log = logging.getLogger(__name__)

SOURCE_NAME = "phonecam_mic"
DESCRIPTION = "PhoneCam Mic"
RATE = 48_000
F_SETPIPE_SZ = 1031
# Measured latency of feed -> virtual source -> an app reading it (13-50 ms, median ~30 ms).
# The video has no such stage after its v4l2 write, so auto A/V sync subtracts it.
OUTPUT_LATENCY_MS = 30.0
JITTER_MARGIN_MS = 15.0


class VirtualMic:
    """Creates/removes the PipeWire source. pactl calls block briefly: use a worker thread."""

    def __init__(self) -> None:
        self.module_id: str | None = None
        self.error: str | None = None

    @staticmethod
    def available() -> bool:
        return shutil.which("pactl") is not None and shutil.which("pw-cat") is not None

    def _pactl(self, *args: str) -> str:
        res = subprocess.run(
            ["pactl", *args], capture_output=True, text=True, timeout=5, check=False
        )
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout).strip() or f"pactl {args[0]} failed")
        return res.stdout

    def _cleanup_stale(self) -> None:
        """Unload PhoneCam sources left over from a previous run that didn't exit cleanly."""
        try:
            out = self._pactl("list", "short", "modules")
        except Exception:
            return
        for line in out.splitlines():
            parts = line.split("\t")
            if (
                len(parts) >= 3
                and parts[1] in ("module-null-sink", "module-pipe-source")
                and (
                    f"sink_name={SOURCE_NAME}" in parts[2]
                    or f"source_name={SOURCE_NAME}" in parts[2]
                )
            ):
                with contextlib.suppress(Exception):
                    self._pactl("unload-module", parts[0])
                    log.info("removed stale PhoneCam Mic module %s", parts[0])

    def create(self) -> bool:
        if self.module_id:
            return True
        if not self.available():
            self.error = "pactl/pw-cat not found (install pulseaudio-utils and pipewire-bin)"
            return False
        self._cleanup_stale()
        try:
            self.module_id = self._pactl(
                "load-module",
                "module-null-sink",
                "media.class=Audio/Source/Virtual",
                f"sink_name={SOURCE_NAME}",
                "channel_map=mono",
                f'sink_properties=device.description="{DESCRIPTION}"',
            ).strip()
        except Exception as e:
            self.error = str(e)
            log.error("could not create %s: %s", DESCRIPTION, e)
            return False
        self.error = None
        log.info("created %s (module %s)", DESCRIPTION, self.module_id)
        return True

    def remove(self) -> None:
        if self.module_id:
            with contextlib.suppress(Exception):
                self._pactl("unload-module", self.module_id)
            self.module_id = None


class PwCatSink:
    """Low-latency playback stream into the virtual source."""

    def __init__(self, target: str | None = None) -> None:
        self.target = target or SOURCE_NAME
        # Unique per process: two PhoneCam instances must never grab each other's feed node.
        self.FEED = f"phonecam_mic_feed_{os.getpid()}"
        self._proc: subprocess.Popen | None = None
        self._stuck = 0
        self.dropped = 0

    def write(self, pcm: bytes) -> bool:
        """Non-blocking: an unlinked or stalled stream stops consuming, and a blocking write
        would then freeze the audio thread. Drop the frame instead; restart after ~1 s stuck."""
        proc = self._ensure()
        if proc is None or proc.stdin is None:
            return False
        fd = proc.stdin.fileno()
        view = memoryview(pcm)
        deadline = time.monotonic() + 0.02
        try:
            while view:
                try:
                    n = os.write(fd, view)
                    view = view[n:]
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        break
                    time.sleep(0.002)
        except (BrokenPipeError, OSError, ValueError):
            self.close()
            return False
        if view:
            self.dropped += 1
            self._stuck += 1
            if self._stuck > 50:
                log.warning("PhoneCam Mic feed stalled; restarting it")
                self.close()
            return False
        self._stuck = 0
        return True

    def _ensure(self) -> subprocess.Popen | None:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        self.close()
        try:
            proc = subprocess.Popen(
                [
                    "pw-cat",
                    "--playback",
                    "--raw",
                    "--target",
                    "0",
                    "-P",
                    f"{{ node.name = {self.FEED} node.description = PhoneCamMicFeed }}",
                    "--rate",
                    str(RATE),
                    "--channels",
                    "1",
                    "--format",
                    "s16",
                    "--latency",
                    "20ms",
                    "-",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            log.warning("cannot start pw-cat: %s", e)
            return None
        assert proc.stdin is not None
        with contextlib.suppress(OSError):
            fcntl.fcntl(proc.stdin.fileno(), F_SETPIPE_SZ, 4096)  # ~42 ms: no hidden backlog
        os.set_blocking(proc.stdin.fileno(), False)
        self._proc = proc
        self._link()
        return proc

    def _link(self) -> None:
        """Link our feed's output to the virtual source's input (ports appear asynchronously)."""
        dest = f"{self.target}:input_MONO"
        for _ in range(20):
            out = subprocess.run(
                ["pw-link", "-o"], capture_output=True, text=True, check=False
            ).stdout
            ports = [p.strip() for p in out.splitlines() if p.strip().startswith(self.FEED + ":")]
            if ports:
                for port in ports:
                    subprocess.run(["pw-link", port, dest], capture_output=True, check=False)
                log.info("PhoneCam Mic feed linked: %s -> %s", ports, dest)
                return
            time.sleep(0.05)
        log.warning("PhoneCam Mic feed ports did not appear; audio may not reach the source")

    def close(self) -> None:
        if self._proc is not None:
            with contextlib.suppress(Exception):
                if self._proc.stdin:
                    self._proc.stdin.close()
            with contextlib.suppress(Exception):
                self._proc.terminate()
                self._proc.wait(1)
            self._proc = None


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

    def __init__(self, clock: ClockSync, sink=None) -> None:
        self._clock = clock
        self._sink = sink if sink is not None else PwCatSink()
        self._q: queue.Queue[tuple[str, object, int, int]] = queue.Queue(maxsize=200)
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
        self._lat_sum = 0
        self._lat_n = 0
        self._late = 0
        self._frames = 0
        self._last_frame_t = 0.0

    # ---- producer side (called from the network thread; never blocks) ----

    def submit_config(self, payload: bytes) -> None:
        self._put(("config", payload, 0, 0))

    def submit(self, payload: bytes, pts_us: int) -> None:
        # Arrival is stamped here, on receipt: measuring it after the jitter-buffer wait would
        # feed the wait back into the target (a runaway latency loop).
        self._put(("frame", payload, pts_us, now_us()))

    def _put(self, item: tuple[str, object, int, int]) -> None:
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
        self._put(("stop", b"", 0, 0))
        if self._thread:
            self._thread.join(2)
        self._sink.close()

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

    def _effective_target(self) -> float:
        """Never below what audio needs to arrive (else every frame would be late)."""
        floor = (self.arrival_ms or 60.0) + JITTER_MARGIN_MS
        if self.auto and self.video_latency_ms:
            wanted = self.video_latency_ms - OUTPUT_LATENCY_MS
        else:
            wanted = self.manual_ms
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
            kind, payload, pts, recv_us = self._q.get()
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
                arrival = (recv_us - capture_pc) / 1000
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
            if not self._sink.write(pcm):
                continue
            if capture_pc is not None:
                self._lat_sum += now_us() - capture_pc
                self._lat_n += 1
