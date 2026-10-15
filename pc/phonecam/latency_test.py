"""Latency test mode (docs/latency.md).

Point the phone at this window. Two measurements:

1. Timer + snapshot: a large millisecond timer and, next to it, the PhoneCam picture read back
   from /dev/video10 exactly like a video-call app would. Space saves a snapshot; the timer value
   visible *inside* the camera picture subtracted from the timer at snapshot time is the
   glass-to-glass latency (it includes this monitor's own display delay once).
2. Automatic flash test: the window alternates black/white once per second. The reader detects the
   brightness step in the frames it reads from /dev/video10 and reports flip -> frame-read delay.
   Only some part of the screen needs to be visible to the camera.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import statistics
import termios
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import av
import numpy as np
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QPixmap,
)
from PySide6.QtWidgets import QWidget

from phonecam.clock import now_us

log = logging.getLogger(__name__)

SNAPSHOT_DIR = Path.home() / "Pictures" / "PhoneCam"


class FlashDetector:
    """Detects the brightness step caused by a screen flip in a stream of luma samples."""

    def __init__(
        self, min_contrast: float = 12.0, window_s: float = 4.0, max_delay_ms: float = 1000.0
    ) -> None:
        self.min_contrast = min_contrast
        self.max_delay_us = max_delay_ms * 1000
        self._samples: deque[tuple[int, float]] = deque()
        self._window_us = int(window_s * 1e6)
        self._flip_t: int | None = None
        self._to_white = False
        self.results: list[float] = []

    def flip(self, t_us: int, to_white: bool) -> None:
        self._flip_t = t_us
        self._to_white = to_white

    def levels(self) -> tuple[float, float] | None:
        if len(self._samples) < 10:
            return None
        values = sorted(v for _, v in self._samples)
        lo = values[len(values) // 10]
        hi = values[len(values) * 9 // 10]
        return (lo, hi) if hi - lo >= self.min_contrast else None

    def sample(self, t_us: int, luma: float) -> float | None:
        """Feed one frame; returns a delay in ms when a flip has been detected."""
        self._samples.append((t_us, luma))
        while self._samples and t_us - self._samples[0][0] > self._window_us:
            self._samples.popleft()
        if self._flip_t is None or t_us < self._flip_t:
            return None
        if t_us - self._flip_t > self.max_delay_us:
            self._flip_t = None
            return None
        lv = self.levels()
        if lv is None:
            return None
        mid = (lv[0] + lv[1]) / 2
        if (luma > mid) if self._to_white else (luma < mid):
            delay = (t_us - self._flip_t) / 1000
            self._flip_t = None
            self.results.append(delay)
            return delay
        return None

    @property
    def contrast_ok(self) -> bool:
        return self.levels() is not None


class AudioOnsetDetector:
    """Finds the first loud 10 ms block after each beep, relative to a quiet baseline."""

    def __init__(
        self, factor: float = 6.0, min_rms: float = 400.0, max_delay_ms: float = 1000.0
    ) -> None:
        self.factor = factor
        self.min_rms = min_rms
        self.max_delay_us = max_delay_ms * 1000
        self._quiet: deque[float] = deque(maxlen=100)
        self._beep_t: int | None = None
        self.results: list[float] = []

    def beep(self, t_us: int) -> None:
        self._beep_t = t_us

    def sample(self, t_us: int, rms: float) -> float | None:
        if self._beep_t is None or t_us < self._beep_t:
            self._quiet.append(rms)
            return None
        if t_us - self._beep_t > self.max_delay_us:
            self._beep_t = None
            return None
        base = statistics.median(self._quiet) if self._quiet else 0.0
        if rms > max(base * self.factor, base + self.min_rms):
            delay = (t_us - self._beep_t) / 1000
            self._beep_t = None
            self.results.append(delay)
            return delay
        return None


class Beeper:
    """Continuous playback stream (pw-cat) into which short beeps are injected on demand.
    A 4 KB stdin pipe keeps the backlog ~42 ms so the beep plays right when it's written."""

    RATE = 48_000

    def __init__(self) -> None:
        import fcntl
        import subprocess

        self._proc = subprocess.Popen(
            [
                "pw-cat",
                "--playback",
                "--raw",  # raw samples on stdin (otherwise "-" is parsed as a sound file)
                "--rate",
                "48000",
                "--channels",
                "1",
                "--format",
                "s16",
                "--latency",
                "10ms",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert self._proc.stdin is not None
        with contextlib.suppress(OSError):
            fcntl.fcntl(self._proc.stdin.fileno(), 1031, 4096)  # F_SETPIPE_SZ
        t = np.arange(int(self.RATE * 0.06)) / self.RATE
        self._beep = (np.sin(2 * np.pi * 1000 * t) * 16000).astype("<i2").tobytes()
        self._silence = bytes(self.RATE // 100 * 2)  # 10 ms
        self._pending = False
        self.on_beep = lambda t_us: None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="beeper", daemon=True)
        self._thread.start()

    def beep(self) -> None:
        self._pending = True

    def _run(self) -> None:
        out = self._proc.stdin
        assert out is not None
        try:
            while not self._stop.is_set():
                if self._pending:
                    self._pending = False
                    # The beep plays after whatever silence is still queued in the pipe.
                    queued = 0
                    with contextlib.suppress(OSError):
                        buf = fcntl.ioctl(out.fileno(), termios.FIONREAD, b"\0\0\0\0")
                        queued = int.from_bytes(buf, "little")
                    self.on_beep(now_us() + queued * 1_000_000 // (self.RATE * 2))
                    out.write(self._beep)
                else:
                    out.write(self._silence)
                out.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(Exception):
            self._proc.terminate()
        with contextlib.suppress(Exception):
            if self._proc.stdin:
                self._proc.stdin.close()
        with contextlib.suppress(Exception):
            if self._proc.stdin:
                self._proc.stdin.close()


class MicReader(QObject):
    """Reads the PhoneCam Mic source like a call app would (pw-record), 10 ms RMS blocks."""

    rms = Signal(object, float)
    error = Signal(str)

    def __init__(self, source: str = "phonecam_mic") -> None:
        super().__init__()
        import subprocess

        self._proc = subprocess.Popen(
            [
                "pw-record",
                "--raw",
                "--target",
                source,
                "--rate",
                "48000",
                "--channels",
                "1",
                "--format",
                "s16",
                "--latency",
                "10ms",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._thread = threading.Thread(target=self._run, name="mic-reader", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        out = self._proc.stdout
        assert out is not None
        block = 480 * 2
        while True:
            data = out.read(block)
            if not data or len(data) < block:
                break
            a = np.frombuffer(data, "<i2").astype(np.float32)
            self.rms.emit(now_us(), float(np.sqrt(np.mean(a * a))))

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._proc.terminate()


class V4L2Reader(QObject):
    """Reads the virtual camera like a consumer app; emits preview images and luma samples."""

    image = Signal(QImage)
    luma = Signal(object, float)  # (t_us: int > 2^31, luma)
    error = Signal(str)

    def __init__(self, device: str) -> None:
        super().__init__()
        self.device = device
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="v4l2-reader", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2)

    def _run(self) -> None:
        try:
            container = av.open(self.device, format="v4l2", options={"fflags": "nobuffer"})
        except Exception as e:
            self.error.emit(f"cannot open {self.device}: {e}")
            return
        last_img = 0.0
        try:
            for frame in container.decode(video=0):
                if self._stop.is_set():
                    break
                t = now_us()
                small = frame.reformat(width=64, height=36, format="gray").to_ndarray()
                self.luma.emit(t, float(small.mean()))
                if time.monotonic() - last_img > 1 / 30:
                    last_img = time.monotonic()
                    w = 640
                    h = max(2, round(frame.height * w / frame.width) // 2 * 2)
                    rgb = frame.reformat(width=w, height=h, format="rgb24").to_ndarray()
                    self.image.emit(
                        QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888).copy()
                    )
        except Exception as e:
            if not self._stop.is_set():
                self.error.emit(f"reading {self.device} failed: {e}")
        finally:
            container.close()


class LatencyWindow(QWidget):
    def __init__(self, device: str) -> None:
        super().__init__()
        self.setWindowTitle("PhoneCam latency test")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.BlankCursor)
        self.detector = FlashDetector()
        self.audio_detector = AudioOnsetDetector()
        self.av_mode = False
        self._beeper: Beeper | None = None
        self._mic: MicReader | None = None
        self.flash = False
        self.white = False
        self.camera_image: QImage | None = None
        self.message = ""
        self.last_snapshot = ""
        self._digit_cache: list[QPixmap] = []
        self._digit_key: tuple | None = None
        self._status_cache = QPixmap()
        self._status_key: tuple | None = None
        self.reader = V4L2Reader(device)
        self.reader.image.connect(self._on_image)
        self.reader.luma.connect(self._on_luma)
        self.reader.error.connect(self._on_error)
        self.reader.start()
        # ~100 Hz is plenty for a 60 Hz display; faster repainting starves the reader thread (GIL).
        self._tick = QTimer(
            self, interval=10, timerType=Qt.TimerType.PreciseTimer, timeout=self.update
        )
        self._tick.start()
        self._flipper = QTimer(self, interval=1000, timeout=self._flip)

    # ------------------------------------------------------------------ events

    def _on_image(self, img: QImage) -> None:
        # Scale once per camera frame, not on every repaint.
        w, h = self.width(), self.height()
        scale = min((w // 2 - 40) / img.width(), (h - 200) / img.height())
        self.camera_image = img.scaled(
            max(1, int(img.width() * scale)), max(1, int(img.height() * scale))
        )

    def _on_luma(self, t: int, luma: float) -> None:
        delay = self.detector.sample(t, luma)
        if delay is not None:
            log.info("flash test: %.0f ms", delay)

    def _on_error(self, msg: str) -> None:
        self.message = msg

    def _flip(self) -> None:
        self.white = not self.white
        if self.av_mode and self.white and self._beeper is not None:
            self._beeper.beep()  # the beep thread timestamps the moment it writes the samples
        self.repaint()  # synchronous paint, then timestamp
        self.detector.flip(now_us(), self.white)

    def _start_av(self) -> None:
        """Flash test plus a beep on every flip to white, heard through PhoneCam Mic."""
        try:
            self._beeper = Beeper()
            self._mic = MicReader()
        except OSError as e:
            self.message = f"A/V test unavailable: {e}"
            return
        self._beeper.on_beep = self.audio_detector.beep
        self._mic.rms.connect(lambda t, r: self.audio_detector.sample(t, r))
        self.audio_detector.results.clear()
        self.av_mode = True

    def _stop_av(self) -> None:
        self.av_mode = False
        for obj in (self._beeper, self._mic):
            if obj is not None:
                obj.close()
        self._beeper = self._mic = None

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.close()
        elif e.key() == Qt.Key.Key_F:
            self.flash = not self.flash
            if self.flash:
                self.detector.results.clear()
                self._flipper.start()
            else:
                self._flipper.stop()
                self.white = False
        elif e.key() == Qt.Key.Key_A:
            if self.av_mode:
                self._stop_av()
                self._flipper.stop()
                self.flash = False
                self.white = False
            else:
                self._start_av()
                self.flash = True
                self.detector.results.clear()
                self._flipper.start()
        elif e.key() == Qt.Key.Key_Space:
            self._snapshot()

    def _snapshot(self) -> None:
        t_ms = now_us() // 1000 % 100_000
        img = self.grab()
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SNAPSHOT_DIR / f"latency-{datetime.now():%Y%m%d-%H%M%S}-{t_ms:05d}.png"
        img.save(str(path))
        self.last_snapshot = f"Saved {path.name} (timer {t_ms:05d} ms at capture)"
        log.info("latency snapshot %s", path)

    def closeEvent(self, event) -> None:
        self._stop_av()
        self._tick.stop()
        self._flipper.stop()
        self.reader.stop()
        super().closeEvent(event)

    # ------------------------------------------------------------------ drawing

    def _digits(self, color: QColor, height: int) -> list[QPixmap]:
        """Pre-rendered digit glyphs: drawing five pixmaps per repaint instead of large antialiased
        text keeps the GUI thread from starving the v4l2 reader thread (GIL)."""
        key = (color.name(), height)
        if self._digit_key != key:
            font = QFont("monospace")
            font.setStyleHint(QFont.StyleHint.Monospace)
            font.setPixelSize(height)
            font.setBold(True)
            fm = QFontMetrics(font)
            dw = max(fm.horizontalAdvance(str(d)) for d in range(10))
            pixmaps = []
            for d in range(10):
                pm = QPixmap(dw, fm.height())
                pm.fill(Qt.GlobalColor.transparent)
                q = QPainter(pm)
                q.setFont(font)
                q.setPen(color)
                q.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, str(d))
                q.end()
                pixmaps.append(pm)
            self._digit_cache, self._digit_key = pixmaps, key
        return self._digit_cache

    def _status_lines(self) -> list[str]:
        res = self.detector.results[-30:]
        if self.flash:
            if not self.detector.contrast_ok:
                flash_line = (
                    "Flash test: point the phone at this screen (not enough brightness change yet)"
                )
            elif res:
                flash_line = (
                    f"Flash test: median {statistics.median(res):.0f} ms · min {min(res):.0f} · "
                    f"max {max(res):.0f} · n={len(res)}  (screen flip → frame read from v4l2)"
                )
            else:
                flash_line = "Flash test: measuring…"
        else:
            flash_line = (
                "F: start the automatic flash test (alternates black/white once per second)"
            )
        av_line = "A: A/V sync test (flash + beep; needs the phone mic streaming to PhoneCam Mic)"
        ares = self.audio_detector.results[-30:]
        if self.av_mode and ares and res:
            av_line = (
                f"A/V test: audio median {statistics.median(ares):.0f} ms, video median "
                f"{statistics.median(res):.0f} ms → audio is "
                f"{statistics.median(ares) - statistics.median(res):+.0f} ms vs video "
                f"(n={len(ares)})"
            )
        elif self.av_mode:
            av_line = "A/V test: listening for the beep on PhoneCam Mic…"
        return [
            flash_line,
            av_line,
            "Space: snapshot to ~/Pictures/PhoneCam "
            "(latency = timer at capture - timer seen in the camera picture)",
            "Esc: close",
            self.last_snapshot or self.message,
        ]

    def _status_pixmap(self, color: QColor) -> QPixmap:
        lines = self._status_lines()
        key = (tuple(lines), color.name(), self.width(), self.height())
        if self._status_key != key:
            size = max(14, self.height() // 45)
            line_h = int(size * 1.6)
            pm = QPixmap(max(1, self.width() - 40), line_h * len(lines))
            pm.fill(Qt.GlobalColor.transparent)
            q = QPainter(pm)
            f = QFont()
            f.setPixelSize(size)
            q.setFont(f)
            q.setPen(color)
            for i, line in enumerate(lines):
                q.drawText(0, i * line_h, pm.width(), line_h, Qt.AlignmentFlag.AlignLeft, line)
            q.end()
            self._status_cache, self._status_key = pm, key
        return self._status_cache

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        light = self.flash and self.white
        p.fillRect(self.rect(), QColor("white") if light else QColor("black"))
        w, h = self.width(), self.height()

        # Timer (left half), from cached digit glyphs.
        digits = self._digits(QColor("black") if light else QColor("white"), max(40, h // 5))
        t_ms = now_us() // 1000 % 100_000
        dw, dh = digits[0].width(), digits[0].height()
        x = (w // 2 - 5 * dw) // 2
        y = (h - dh) // 2
        for i, ch in enumerate(f"{t_ms:05d}"):
            p.drawPixmap(x + i * dw, y, digits[int(ch)])

        # Camera picture as read from /dev/video10 (right half).
        if self.camera_image is not None:
            img = self.camera_image
            p.drawImage(w // 2 + (w // 2 - img.width()) // 2, (h - img.height()) // 2, img)

        status = self._status_pixmap(QColor("#444") if light else QColor("#9aa4b2"))
        p.drawPixmap(20, h - status.height() - 10, status)
        p.end()
