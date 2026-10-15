"""Wi-Fi features without a phone: pairing handshake and mDNS discovery against the fake phone."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from phonecam.client import Stats, StreamClient, Target
from phonecam.discovery import PhoneBrowser
from phonecam.tools.fake_phone import FakePhone

SAMPLE = (Path(__file__).parent / "data" / "phone-720p-12f.h264").read_bytes()


class Rec:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, bool]] = []
        self.pairing: list[str] = []
        self.hello: dict[str, Any] | None = None

    def on_status(self, status: str, connected: bool) -> None:
        self.statuses.append((status, connected))

    def on_hello(self, hello: dict[str, Any]) -> None:
        self.hello = hello

    def on_state(self, state: dict[str, Any]) -> None:
        pass

    def on_stats(self, stats: Stats) -> None:
        pass

    def on_pairing(self, kind: str) -> None:
        self.pairing.append(kind)


def wait(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.parametrize(
    ("code", "expected"), [(None, "pairing_required"), ("0000", "pairing_failed")]
)
def test_pairing_rejected_stops_retrying(code: str | None, expected: str) -> None:
    phone = FakePhone(SAMPLE, pairing="4321").start()
    rec = Rec()
    client = StreamClient(rec)
    try:
        client.connect(
            Target("127.0.0.1", None, phone.video_port, phone.control_port, pairing=code)
        )
        assert wait(lambda: rec.pairing == [expected])
        time.sleep(0.8)
        assert rec.pairing == [expected], "must not keep retrying a wrong/missing code"
        assert not client.running
    finally:
        client.disconnect()
        phone.stop()


def test_correct_pairing_streams() -> None:
    phone = FakePhone(SAMPLE, pairing="4321").start()
    rec = Rec()
    client = StreamClient(rec)
    try:
        client.connect(
            Target("127.0.0.1", None, phone.video_port, phone.control_port, pairing="4321")
        )
        assert client.preview_slot.get(5) is not None
        assert rec.hello is not None and rec.pairing == []
    finally:
        client.disconnect()
        phone.stop()


def test_mdns_discovery_finds_fake_phone() -> None:
    phone = FakePhone(SAMPLE, pairing="1234", announce=True).start()
    found: list = []
    lock = threading.Lock()

    def on_change(phones):
        with lock:
            found[:] = phones

    browser = PhoneBrowser(on_change)
    if not browser.start():
        phone.stop()
        pytest.skip("mDNS not available here")
    try:
        ok = wait(lambda: any(p.port == phone.control_port for p in found), 10)
        if not ok:
            pytest.skip("no multicast loopback on this host")
        p = next(p for p in found if p.port == phone.control_port)
        assert p.host == "127.0.0.1" and p.model == "Fake Phone" and p.pairing
        assert p.video_port == phone.video_port
    finally:
        browser.stop()
        phone.stop()
