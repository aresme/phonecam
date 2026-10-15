"""Direct V4L2 output to a v4l2loopback device (no pyvirtualcam needed).

The writer sets a fixed format once (VIDIOC_S_FMT on the OUTPUT queue) and then write()s whole
frames. With `exclusive_caps=1` the device only advertises CAPTURE (and so only appears in
Chrome/Firefox) while a writer is active, so the output thread writes continuously.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import struct
from dataclasses import dataclass
from pathlib import Path

import av
import numpy as np

log = logging.getLogger(__name__)

SYSFS = Path("/sys/devices/virtual/video4linux")
LABEL = "PhoneCam"

V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
V4L2_FIELD_NONE = 1
V4L2_COLORSPACE_SRGB = 8

# struct v4l2_format is 208 bytes on 64-bit Linux: u32 type, 4 bytes padding (the union holds
# pointers, so it is 8-byte aligned), then a 200-byte union whose first member is v4l2_pix_format.
_FMT_SIZE = 208
_PIX = struct.Struct("=IIIIIIIIIIII")  # width height pixelformat field bytesperline sizeimage
#                                        colorspace priv flags ycbcr_enc quantization xfer_func


def _iowr(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("V") << 8) | nr


VIDIOC_G_FMT = _iowr(4, _FMT_SIZE)
VIDIOC_S_FMT = _iowr(5, _FMT_SIZE)


def fourcc(code: str) -> int:
    a, b, c, d = (ord(ch) for ch in code)
    return a | (b << 8) | (c << 16) | (d << 24)


@dataclass(frozen=True)
class PixelFormat:
    name: str  # UI label
    fourcc: str  # V4L2
    av_format: str  # PyAV/FFmpeg pixel format

    def frame_size(self, w: int, h: int) -> int:
        return w * h * 2 if self.fourcc == "YUYV" else w * h * 3 // 2


YUYV = PixelFormat("YUYV 4:2:2", "YUYV", "yuyv422")
I420 = PixelFormat("I420 (YU12) 4:2:0", "YU12", "yuv420p")
PIXEL_FORMATS = {"YUYV": YUYV, "I420": I420}


# ------------------------------------------------------------------ device discovery


def module_loaded() -> bool:
    return Path("/sys/module/v4l2loopback").exists()


def find_loopback(label: str = LABEL) -> str | None:
    """Path of the v4l2loopback device whose card label is `label`, e.g. /dev/video10."""
    if not SYSFS.exists():
        return None
    for d in sorted(SYSFS.iterdir()):
        try:
            if (d / "name").read_text().strip() == label:
                return f"/dev/{d.name}"
        except OSError:
            continue
    return None


# ------------------------------------------------------------------ writer


class V4L2Writer:
    def __init__(self, path: str, width: int, height: int, pixfmt: PixelFormat) -> None:
        if width % 2 or height % 2:
            raise ValueError("width and height must be even")
        self.path = path
        self.width = width
        self.height = height
        self.pixfmt = pixfmt
        self.frame_size = pixfmt.frame_size(width, height)
        self._fd = os.open(path, os.O_RDWR)
        try:
            self._set_format()
        except OSError:
            os.close(self._fd)
            raise

    def _set_format(self) -> None:
        bpl = self.width * 2 if self.pixfmt.fourcc == "YUYV" else self.width
        buf = bytearray(_FMT_SIZE)
        struct.pack_into("=I", buf, 0, V4L2_BUF_TYPE_VIDEO_OUTPUT)
        _PIX.pack_into(
            buf,
            8,
            self.width,
            self.height,
            fourcc(self.pixfmt.fourcc),
            V4L2_FIELD_NONE,
            bpl,
            self.frame_size,
            V4L2_COLORSPACE_SRGB,
            0,
            0,
            0,
            0,
            0,
        )
        try:
            fcntl.ioctl(self._fd, VIDIOC_S_FMT, buf)
        except OSError as e:
            if e.errno == errno.EBUSY:
                raise OSError(
                    e.errno,
                    f"{self.path} is busy: another program is writing to it, "
                    "or a reader holds a different format",
                ) from e
            raise
        w, h, pf = _PIX.unpack_from(buf, 8)[:3]
        if (w, h, pf) != (self.width, self.height, fourcc(self.pixfmt.fourcc)):
            raise OSError(
                errno.EINVAL,
                f"device accepted {w}x{h} {pf:#x} instead of the requested "
                f"{self.width}x{self.height} {self.pixfmt.fourcc}",
            )
        log.info("v4l2 output %s: %dx%d %s", self.path, w, h, self.pixfmt.fourcc)

    def write(self, data: bytes | memoryview) -> None:
        n = os.write(self._fd, data)
        if n != len(data):
            raise OSError(errno.EIO, f"short write to {self.path}: {n}/{len(data)}")

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


# ------------------------------------------------------------------ frame packing


def fit(src_w: int, src_h: int, dst_w: int, dst_h: int) -> tuple[int, int, int, int]:
    """Aspect-preserving fit: (w, h, x, y) inside dst, all even (chroma alignment)."""
    scale = min(dst_w / src_w, dst_h / src_h)
    w = min(dst_w, max(2, round(src_w * scale / 2) * 2))
    h = min(dst_h, max(2, round(src_h * scale / 2) * 2))
    return w, h, (dst_w - w) // 4 * 2, (dst_h - h) // 4 * 2


def _plane(frame: av.VideoFrame, index: int, rows: int, row_bytes: int) -> np.ndarray:
    p = frame.planes[index]
    return np.frombuffer(p, np.uint8).reshape(-1, p.line_size)[:rows, :row_bytes]


def pack(frame: av.VideoFrame, dst_w: int, dst_h: int, pixfmt: PixelFormat) -> bytes:
    """Scale (swscale, letterboxed if the aspect differs) and pack into contiguous frame bytes."""
    w, h, x, y = fit(frame.width, frame.height, dst_w, dst_h)
    f = frame.reformat(width=w, height=h, format=pixfmt.av_format)
    if pixfmt.fourcc == "YUYV":
        src = _plane(f, 0, h, w * 2)
        if (w, h) == (dst_w, dst_h):
            return src.tobytes()
        canvas = np.empty((dst_h, dst_w * 2), np.uint8)
        canvas[:, 0::2] = 16  # Y black (limited range)
        canvas[:, 1::2] = 128  # neutral chroma
        canvas[y : y + h, x * 2 : (x + w) * 2] = src
        return canvas.tobytes()
    ys, us, vs = _plane(f, 0, h, w), _plane(f, 1, h // 2, w // 2), _plane(f, 2, h // 2, w // 2)
    if (w, h) == (dst_w, dst_h):
        return ys.tobytes() + us.tobytes() + vs.tobytes()
    cy = np.full((dst_h, dst_w), 16, np.uint8)
    cu = np.full((dst_h // 2, dst_w // 2), 128, np.uint8)
    cv = cu.copy()
    cy[y : y + h, x : x + w] = ys
    cu[y // 2 : (y + h) // 2, x // 2 : (x + w) // 2] = us
    cv[y // 2 : (y + h) // 2, x // 2 : (x + w) // 2] = vs
    return cy.tobytes() + cu.tobytes() + cv.tobytes()


def pack_rgb(rgb: np.ndarray, dst_w: int, dst_h: int, pixfmt: PixelFormat) -> bytes:
    """Pack an RGB24 image (e.g. the placeholder) into the output format."""
    return pack(
        av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format="rgb24"), dst_w, dst_h, pixfmt
    )
