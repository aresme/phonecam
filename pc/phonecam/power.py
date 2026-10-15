"""Hold the "performance" power profile while streaming (power-profiles-daemon HoldProfile).

On a laptop in the balanced/power-saver profile the CPU clocks down between frames, and H.264
decoding of each frame then takes ~2x longer (measured 12.8 ms vs 5.6 ms per 720p frame on an
i7-8650U, docs/latency.md). The daemon releases the hold automatically if this process exits.
Calls block briefly on D-Bus: use them from a worker thread.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

_SERVICES = [
    ("org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles"),
    ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles"),  # power-profiles-daemon < 0.20
]


class PerformanceHold:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn = None
        self._service: tuple[str, str] | None = None
        self._cookie: int | None = None

    @property
    def held(self) -> bool:
        return self._cookie is not None

    def acquire(self, reason: str = "PhoneCam is streaming") -> bool:
        with self._lock:
            if self._cookie is not None:
                return True
            try:
                from gi.repository import Gio, GLib
            except ImportError:
                log.info("python3-gi not available; cannot request the performance profile")
                return False
            try:
                conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            except Exception as e:
                log.info("no system bus: %s", e)
                return False
            for name, path in _SERVICES:
                try:
                    res = conn.call_sync(
                        name,
                        path,
                        name,
                        "HoldProfile",
                        GLib.Variant("(sss)", ("performance", reason, "phonecam")),
                        GLib.VariantType("(u)"),
                        Gio.DBusCallFlags.NONE,
                        2000,
                        None,
                    )
                except Exception as e:
                    log.debug("HoldProfile via %s failed: %s", name, e)
                    continue
                self._conn, self._service, self._cookie = conn, (name, path), int(res.unpack()[0])
                log.info("holding the performance power profile (cookie %d)", self._cookie)
                return True
            log.info("power-profiles-daemon unavailable; CPU boost not active")
            return False

    def release(self) -> None:
        with self._lock:
            if self._cookie is None or self._conn is None or self._service is None:
                return
            from gi.repository import Gio, GLib

            name, path = self._service
            try:
                self._conn.call_sync(
                    name,
                    path,
                    name,
                    "ReleaseProfile",
                    GLib.Variant("(u)", (self._cookie,)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )
                log.info("released the performance power profile")
            except Exception as e:
                log.debug("ReleaseProfile failed: %s", e)
            self._cookie = None
