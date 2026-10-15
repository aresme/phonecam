from __future__ import annotations

import base64
import json
import subprocess
import time

import av
import numpy as np
import pytest

from phonecam import audio as audio_mod
from phonecam.audio import AudioPipeline, VirtualMic
from phonecam.clock import ClockSync, now_us

FRAME = 960


class ListSink:
    """In-memory stand-in for the pw-cat feed: records (time, bytes) of every write."""

    def __init__(self) -> None:
        self.chunks: list[tuple[float, bytes]] = []

    def write(self, pcm: bytes) -> bool:
        self.chunks.append((time.monotonic(), pcm))
        return True

    def close(self) -> None:
        pass


@pytest.fixture
def fifo() -> ListSink:
    return ListSink()


def synced_clock() -> ClockSync:
    c = ClockSync()
    t = now_us()
    c.add(t, t, t)  # phone clock == PC clock
    return c


def pcm_config() -> bytes:
    return json.dumps({"codec": "pcm_s16le", "sample_rate": 48000, "channels": 1}).encode()


def test_pcm_frames_are_written_at_capture_plus_target(fifo) -> None:
    p = AudioPipeline(synced_clock(), fifo)
    p.auto = False
    p.manual_ms = 150
    p.start()
    try:
        p.submit_config(pcm_config())
        t_cap = now_us()
        frame = (np.ones(FRAME) * 1000).astype("<i2").tobytes()
        t0 = time.monotonic()
        p.submit(frame, t_cap)
        assert _wait(lambda: sum(len(c) for _, c in fifo.chunks) >= len(frame))
        delay = fifo.chunks[0][0] - t0
        assert 0.10 < delay < 0.30, delay  # held back to ~capture + 150 ms (floor = arrival + 25)
    finally:
        p.stop()


def test_late_frames_are_dropped(fifo) -> None:
    p = AudioPipeline(synced_clock(), fifo)
    p.auto = False
    p.manual_ms = 100
    p.start()
    try:
        p.submit_config(pcm_config())
        p.arrival_ms = 10  # pretend audio normally arrives fast
        p.submit(b"\0\0" * FRAME, now_us() - 2_000_000)  # captured 2 s ago
        time.sleep(0.3)
        assert p.stats().late_dropped >= 1
    finally:
        p.stop()


def test_target_never_below_arrival() -> None:
    p = AudioPipeline(synced_clock(), ListSink())
    p.auto = False
    p.manual_ms = 0
    p.arrival_ms = 80
    assert p._effective_target() == pytest.approx(95)
    p.auto = True
    p.video_latency_ms = 150
    assert p._effective_target() == pytest.approx(120)  # video - output latency (30)


def opus_packets(seconds: float = 0.5) -> tuple[bytes, list[bytes]]:
    enc = av.CodecContext.create("libopus", "w")
    enc.sample_rate = 48000
    enc.layout = "mono"
    enc.format = "s16"
    enc.bit_rate = 48000
    enc.open()
    t = np.arange(int(48000 * seconds)) / 48000
    pcm = (np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2")
    packets = []
    for i in range(0, len(pcm) - FRAME + 1, FRAME):
        f = av.AudioFrame.from_ndarray(
            pcm[i : i + FRAME].reshape(1, -1), format="s16", layout="mono"
        )
        f.sample_rate = 48000
        f.pts = i
        packets += [bytes(p) for p in enc.encode(f)]
    return bytes(enc.extradata), packets


def test_opus_roundtrip_through_pipeline(fifo) -> None:
    head, packets = opus_packets()
    # Android wraps OpusHead in its own container; the pipeline must find it.
    wrapped = b"AOPUSHDR" + len(head).to_bytes(8, "little") + head
    p = AudioPipeline(synced_clock(), fifo)
    p.start()
    try:
        cfg = {
            "codec": "opus",
            "sample_rate": 48000,
            "channels": 1,
            "csd": base64.b64encode(wrapped).decode(),
        }
        p.submit_config(json.dumps(cfg).encode())
        t = now_us()
        for i, pkt in enumerate(packets):
            p.submit(pkt, t + i * 20_000)
        assert _wait(lambda: sum(len(c) for _, c in fifo.chunks) >= 0.3 * 48000 * 2, 3)
        pcm = np.frombuffer(b"".join(c for _, c in fifo.chunks), "<i2").astype(float)
        assert np.sqrt(np.mean(pcm[4800:] ** 2)) > 2000  # the tone survived
    finally:
        p.stop()


@pytest.mark.skipif(not VirtualMic.available(), reason="pactl not available")
def test_virtual_mic_source_created_and_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio_mod, "SOURCE_NAME", "phonecam_mic_test")
    mic = VirtualMic()
    if not mic.create():
        pytest.skip(f"cannot create source: {mic.error}")
    try:
        out = subprocess.run(
            ["pactl", "list", "short", "sources"], capture_output=True, text=True
        ).stdout
        assert "phonecam_mic_test" in out
    finally:
        mic.remove()
    out = subprocess.run(
        ["pactl", "list", "short", "sources"], capture_output=True, text=True
    ).stdout
    assert "phonecam_mic_test" not in out


def _wait(cond, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_queue_wait_does_not_inflate_arrival(fifo) -> None:
    """Regression: arrival must be measured on receipt, not after the jitter-buffer wait."""
    p = AudioPipeline(synced_clock(), fifo)
    p.auto = False
    p.manual_ms = 200
    p.start()
    try:
        p.submit_config(pcm_config())
        t = now_us()
        for i in range(10):  # 10 frames arriving at once, captured "now"
            p.submit(b"\0\0" * FRAME, t + i * 20_000)
        time.sleep(0.6)
        assert p.arrival_ms is not None and p.arrival_ms < 50, p.arrival_ms
        assert p.target_ms == pytest.approx(200)
    finally:
        p.stop()


@pytest.mark.skipif(not VirtualMic.available(), reason="pactl/pw-cat not available")
def test_pwcat_feed_reaches_virtual_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real PipeWire: tone written through PwCatSink must be recordable from the virtual source."""
    from phonecam.audio import PwCatSink

    monkeypatch.setattr(audio_mod, "SOURCE_NAME", "phonecam_mic_it")
    mic = VirtualMic()
    if not mic.create():
        pytest.skip(f"cannot create source: {mic.error}")
    rec = subprocess.Popen(
        [
            "pw-record",
            "--raw",
            "--target",
            "phonecam_mic_it",
            "--rate",
            "48000",
            "--channels",
            "1",
            "--format",
            "s16",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    sink = PwCatSink("phonecam_mic_it")
    try:
        time.sleep(0.3)
        t = np.arange(FRAME) / 48000
        tone = (np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2").tobytes()
        start = time.monotonic()
        for i in range(50):  # 1 s, paced
            assert sink.write(tone)
            time.sleep(max(0.0, start + (i + 1) * 0.02 - time.monotonic()))
        time.sleep(0.3)
    finally:
        sink.close()
        rec.terminate()
        data = rec.stdout.read() if rec.stdout else b""
        mic.remove()
    a = np.frombuffer(data[: len(data) // 2 * 2], "<i2").astype(float)
    assert len(a) > 24000 and np.sqrt(np.mean(a**2)) > 1500
