"""Full PC pipeline against a fake phone: network -> decode -> frame slots, plus reconnect."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from phonecam.client import DecodedFrame, Stats, StreamClient, Target
from phonecam.tools.fake_phone import FakePhone

SAMPLE = (Path(__file__).parent / "data" / "phone-720p-12f.h264").read_bytes()


class Recorder:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, bool]] = []
        self.hello: dict[str, Any] | None = None
        self.stats: list[Stats] = []
        self.lock = threading.Lock()

    def on_status(self, status: str, connected: bool) -> None:
        with self.lock:
            self.statuses.append((status, connected))

    def on_hello(self, hello: dict[str, Any]) -> None:
        self.hello = hello

    def on_state(self, state: dict[str, Any]) -> None:
        pass

    def on_stats(self, stats: Stats) -> None:
        with self.lock:
            self.stats.append(stats)

    def connected_count(self) -> int:
        with self.lock:
            return sum(1 for _, c in self.statuses if c)


def wait_for(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def collect_frames(client: StreamClient, n: int, timeout: float = 5.0) -> list[DecodedFrame]:
    frames: list[DecodedFrame] = []
    end = time.monotonic() + timeout
    while len(frames) < n and time.monotonic() < end:
        f = client.preview_slot.get(0.2)
        if f is not None:
            frames.append(f)
    return frames


def test_decode_latency_and_reconnect() -> None:
    phone = FakePhone(SAMPLE, fps=30, capture_delay_ms=40).start()
    rec = Recorder()
    client = StreamClient(rec)
    target = Target("127.0.0.1", None, phone.video_port, phone.control_port)
    try:
        client.connect(target)
        frames = collect_frames(client, 20)
        assert len(frames) >= 20, f"decoded only {len(frames)} frames"
        assert (frames[0].frame.width, frames[0].frame.height) == (1280, 720)
        assert rec.hello is not None and rec.hello["server"] == "fake-phone"

        # Latency: fake capture is 40 ms before send; the phone clock is 1 h ahead, which the
        # PING/PONG offset estimate must cancel out.
        assert wait_for(lambda: any(s.latency_ms is not None for s in rec.stats[2:]))
        lat = [s.latency_ms for s in rec.stats if s.latency_ms is not None][-1]
        assert 35 < lat < 200, lat
        assert any(s.rtt_ms is not None and s.rtt_ms < 50 for s in rec.stats)

        # Phone goes away -> client reports reconnecting; phone comes back on the same ports.
        vport, cport = phone.video_port, phone.control_port
        phone.stop()
        assert wait_for(lambda: rec.statuses[-1][0].startswith("Reconnecting"), 6)
        phone = FakePhone(SAMPLE, fps=30, video_port=vport, control_port=cport).start()
        assert wait_for(lambda: rec.connected_count() >= 2, 10), rec.statuses
        assert len(collect_frames(client, 10)) >= 10
    finally:
        client.disconnect()
        phone.stop()
    assert rec.statuses[-1] == ("Disconnected", False)
