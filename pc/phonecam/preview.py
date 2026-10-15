"""GUI preview: a worker thread turns decoded frames into QImages at a capped rate, so the
preview never slows down the decode/v4l2 path, and a widget paints them."""

from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

from phonecam.client import DecodedFrame
from phonecam.frameslot import LatestFrameSlot

log = logging.getLogger(__name__)

MAX_PREVIEW_W = 960


class PreviewWorker(QObject):
    """Reads its own LatestFrameSlot; emits at most `max_fps` downscaled RGB QImages per second."""

    frame = Signal(QImage)

    def __init__(self, slot: LatestFrameSlot[DecodedFrame], max_fps: float = 30.0) -> None:
        super().__init__()
        self._slot = slot
        self._period = 1.0 / max_fps
        self._stop = threading.Event()
        self.enabled = True
        self._thread = threading.Thread(target=self._run, name="preview", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(1)

    def _run(self) -> None:
        next_t = 0.0
        while not self._stop.is_set():
            df = self._slot.get(0.25)
            if df is None or not self.enabled:
                continue
            now = time.monotonic()
            if now < next_t:
                continue  # rate cap: skip, the slot will hand us a newer frame
            next_t = now + self._period
            f = df.frame
            w = min(MAX_PREVIEW_W, f.width)
            h = max(2, round(f.height * w / f.width) // 2 * 2)
            try:
                rgb = f.reformat(width=w, height=h, format="rgb24").to_ndarray()
            except Exception:
                log.exception("preview conversion failed")
                continue
            img = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888).copy()
            self.frame.emit(img)


class PreviewWidget(QWidget):
    """Paints the latest preview image with letterboxing. Clicks report normalized coordinates
    within the video (used for tap-to-focus)."""

    clicked = Signal(float, float)

    def __init__(self) -> None:
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(480, 270)
        self._image: QImage | None = None
        self._message = "No stream"
        self._dim = False

    def set_image(self, img: QImage) -> None:
        self._image = img
        self.update()

    def set_message(self, message: str, dim: bool = True) -> None:
        """Overlay text (e.g. 'Reconnecting…'); empty string removes it."""
        self._message = message
        self._dim = dim and bool(message)
        self.update()

    def clear(self, message: str) -> None:
        self._image = None
        self.set_message(message)

    def _video_rect(self) -> QRectF | None:
        if self._image is None or self._image.width() == 0:
            return None
        iw, ih = self._image.width(), self._image.height()
        scale = min(self.width() / iw, self.height() / ih)
        w, h = iw * scale, ih * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0b0c0e"))
        rect = self._video_rect()
        if rect is not None and self._image is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(rect, self._image)
            if self._dim:
                p.fillRect(rect, QColor(0, 0, 0, 150))
        if self._message:
            p.setPen(QColor("#9aa4b2"))
            f = p.font()
            f.setPointSize(14)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
        p.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        rect = self._video_rect()
        pos: QPointF = event.position()
        if rect is not None and rect.contains(pos):
            x = (pos.x() - rect.x()) / rect.width()
            y = (pos.y() - rect.y()) / rect.height()
            self.clicked.emit(x, y)
