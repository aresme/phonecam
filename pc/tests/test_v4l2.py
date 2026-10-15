from __future__ import annotations

import os
import time

import av
import numpy as np
import pytest

from phonecam import output as output_mod
from phonecam.client import DecodedFrame
from phonecam.clock import ClockSync, now_us
from phonecam.frameslot import LatestFrameSlot
from phonecam.output import HOLD_S, VirtualCamera, plain_placeholder
from phonecam.setup_dialog import build_script
from phonecam.v4l2 import (
    I420,
    VIDIOC_G_FMT,
    VIDIOC_S_FMT,
    YUYV,
    V4L2Writer,
    find_loopback,
    fit,
    fourcc,
    pack,
    pack_rgb,
)


def test_ioctl_numbers_and_fourcc() -> None:
    assert VIDIOC_G_FMT == 0xC0D05604
    assert VIDIOC_S_FMT == 0xC0D05605
    assert fourcc("YUYV") == 0x56595559
    assert fourcc("YU12") == 0x32315559


def test_fit() -> None:
    assert fit(1280, 720, 1280, 720) == (1280, 720, 0, 0)
    assert fit(1920, 1080, 1280, 720) == (1280, 720, 0, 0)
    w, h, x, y = fit(640, 480, 1280, 720)  # 4:3 into 16:9 -> pillarbox
    assert (w, h, y) == (960, 720, 0) and x == 160
    assert all(v % 2 == 0 for v in fit(1001, 563, 1280, 720))


def rgb_frame(w: int, h: int, value: int = 200) -> av.VideoFrame:
    return av.VideoFrame.from_ndarray(np.full((h, w, 3), value, np.uint8), format="rgb24")


@pytest.mark.parametrize("fmt", [YUYV, I420])
def test_pack_sizes(fmt) -> None:
    assert len(pack(rgb_frame(1280, 720), 1280, 720, fmt)) == fmt.frame_size(1280, 720)
    assert len(pack(rgb_frame(1920, 1080), 1280, 720, fmt)) == fmt.frame_size(1280, 720)
    assert len(pack(rgb_frame(640, 480), 1280, 720, fmt)) == fmt.frame_size(1280, 720)


def test_letterbox_bars_are_black() -> None:
    data = np.frombuffer(pack(rgb_frame(640, 480, 255), 1280, 720, I420), np.uint8)
    y = data[: 1280 * 720].reshape(720, 1280)
    assert y[:, :100].max() == 16  # left bar
    assert y[:, 640].min() > 200  # bright picture in the middle
    yuyv = np.frombuffer(pack(rgb_frame(640, 480, 255), 1280, 720, YUYV), np.uint8).reshape(
        720, 2560
    )
    assert yuyv[:, 0:200:2].max() == 16 and set(np.unique(yuyv[:, 1:200:2])) == {128}


class FakeWriter:
    def __init__(self, path: str, width: int, height: int, pixfmt) -> None:
        self.path = path
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    def close(self) -> None:
        pass


def test_output_live_hold_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(output_mod, "V4L2Writer", FakeWriter)
    slot: LatestFrameSlot[DecodedFrame] = LatestFrameSlot()
    clock = ClockSync()
    clock.add(1000, 1000, 1000)  # offset 0
    vc = VirtualCamera(slot, clock)
    vc.start("/dev/fake", 320, 240, I420)
    try:
        writer = vc._writer
        assert isinstance(writer, FakeWriter)
        slot.put(DecodedFrame(rgb_frame(320, 240), now_us() - 50_000, now_us()))
        time.sleep(0.15)
        assert vc.stats().showing in ("live", "hold")
        live = writer.writes[0]
        assert writer.writes[1] == live  # repeated while holding
        time.sleep(HOLD_S + 0.3)
        st = vc.stats()
        assert st.showing == "placeholder"
        assert writer.writes[-1] == pack_rgb(plain_placeholder(320, 240, ""), 320, 240, I420)
        assert 5 <= st.fps <= 15  # idle output keeps the device alive at ~10 fps
    finally:
        vc.stop()


def test_output_latency_uses_clock_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(output_mod, "V4L2Writer", FakeWriter)
    slot: LatestFrameSlot[DecodedFrame] = LatestFrameSlot()
    clock = ClockSync()
    off = 3_600_000_000  # phone clock 1 h ahead
    t = now_us()
    clock.add(t, t + off, t)
    vc = VirtualCamera(slot, clock)
    vc.start("/dev/fake", 320, 240, YUYV)
    try:
        slot.put(DecodedFrame(rgb_frame(320, 240), now_us() + off - 80_000, now_us()))
        time.sleep(0.1)
        lat = vc.stats().latency_ms
        assert lat is not None and 80 <= lat < 150
    finally:
        vc.stop()


def test_setup_script() -> None:
    s = build_script(10, persist=True)
    assert "modprobe v4l2loopback devices=1 video_nr=10 card_label=PhoneCam exclusive_caps=1" in s
    assert "/etc/modprobe.d/phonecam.conf" in s and "/etc/modules-load.d/phonecam.conf" in s
    assert "/etc/modprobe.d" not in build_script(11, persist=False)


DEVICE = find_loopback()


@pytest.mark.skipif(
    DEVICE is None or not os.access(DEVICE, os.W_OK), reason="no writable PhoneCam device"
)
def test_real_device_accepts_both_formats() -> None:
    assert DEVICE is not None
    for fmt in (I420, YUYV):
        try:
            w = V4L2Writer(DEVICE, 640, 360, fmt)
        except OSError as e:
            pytest.skip(f"device busy: {e}")
        try:
            w.write(pack(rgb_frame(640, 360), 640, 360, fmt))
        finally:
            w.close()


def marker_frame(w: int = 320, h: int = 180) -> av.VideoFrame:
    """Black frame with a white square in the top-left corner."""
    rgb = np.zeros((h, w, 3), np.uint8)
    rgb[: h // 4, : w // 4] = 255
    return av.VideoFrame.from_ndarray(rgb, format="rgb24")


def luma(data: bytes, w: int, h: int, fmt) -> np.ndarray:
    a = np.frombuffer(data, np.uint8)
    if fmt is YUYV:
        return a.reshape(h, w * 2)[:, 0::2]
    return a[: w * h].reshape(h, w)


def bright_corner(y: np.ndarray) -> str:
    h, w = y.shape
    corners = {
        "tl": y[: h // 8, : w // 8],
        "tr": y[: h // 8, -w // 8 :],
        "bl": y[-h // 8 :, : w // 8],
        "br": y[-h // 8 :, -w // 8 :],
    }
    return max(corners, key=lambda k: corners[k].mean())


@pytest.mark.parametrize("fmt", [I420, YUYV])
@pytest.mark.parametrize(
    ("rotation", "mirror", "corner"),
    [
        (0, False, "tl"),
        (90, False, "tr"),
        (180, False, "br"),
        (270, False, "bl"),
        (0, True, "tr"),
        (90, True, "tl"),
    ],
)
def test_rotation_and_mirror(fmt, rotation: int, mirror: bool, corner: str) -> None:
    quarter = rotation in (90, 270)
    w, h = (180, 320) if quarter else (320, 180)  # output matches the upright aspect
    data = pack(marker_frame(), w, h, fmt, rotation, mirror)
    assert len(data) == fmt.frame_size(w, h)
    assert bright_corner(luma(data, w, h, fmt)) == corner


def test_rotated_portrait_is_pillarboxed_in_landscape_output() -> None:
    data = pack(marker_frame(1280, 720), 1280, 720, I420, rotation=90)
    y = luma(data, 1280, 720, I420)
    assert y[:, :300].max() == 16 and y[:, -300:].max() == 16  # bars left and right
