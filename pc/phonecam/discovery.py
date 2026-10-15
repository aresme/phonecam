"""Find phones on the LAN: mDNS/DNS-SD browse for `_phonecam._tcp` (announced by Android NSD)."""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

SERVICE_TYPE = "_phonecam._tcp.local."


@dataclass(frozen=True, slots=True)
class DiscoveredPhone:
    name: str  # DNS-SD instance name, e.g. "PhoneCam Infinix X6886"
    host: str  # IPv4 address
    port: int  # control port
    model: str = ""
    pairing: bool = True  # phone requires a pairing code
    video_port: int = 47100

    @property
    def label(self) -> str:
        return f"{self.model or self.name} · {self.host}" + (" · code" if self.pairing else "")


def _props(info) -> dict[str, str]:
    out = {}
    for k, v in (info.properties or {}).items():
        try:
            out[k.decode()] = v.decode() if isinstance(v, bytes) else str(v)
        except Exception:
            continue
    return out


class PhoneBrowser:
    """Keeps a live list of phones; calls `on_change(list)` from a zeroconf thread."""

    def __init__(self, on_change: Callable[[list[DiscoveredPhone]], None]) -> None:
        self._on_change = on_change
        self._phones: dict[str, DiscoveredPhone] = {}
        self._lock = threading.Lock()
        self._zc = None
        self._browser = None

    def start(self) -> bool:
        try:
            from zeroconf import ServiceBrowser, Zeroconf
        except ImportError:
            log.info("zeroconf not installed; phone discovery disabled")
            return False
        try:
            self._zc = Zeroconf()
            self._browser = ServiceBrowser(self._zc, SERVICE_TYPE, handlers=[self._on_service])
        except OSError as e:
            log.warning("mDNS discovery unavailable: %s", e)
            return False
        return True

    def stop(self) -> None:
        if self._zc is not None:
            with contextlib.suppress(Exception):
                self._zc.close()
            self._zc = None

    @property
    def phones(self) -> list[DiscoveredPhone]:
        with self._lock:
            return sorted(self._phones.values(), key=lambda p: p.label)

    def _on_service(self, zeroconf, service_type: str, name: str, state_change) -> None:
        from zeroconf import ServiceStateChange

        if state_change is ServiceStateChange.Removed:
            with self._lock:
                changed = self._phones.pop(name, None) is not None
        else:
            info = zeroconf.get_service_info(service_type, name, timeout=3000)
            if info is None:
                return
            v4 = [a for a in info.addresses if len(a) == 4]
            if not v4:
                return
            props = _props(info)
            phone = DiscoveredPhone(
                name=name.removesuffix("." + service_type),
                host=socket.inet_ntoa(v4[0]),
                port=info.port or 47101,
                model=props.get("model", ""),
                pairing=props.get("pairing", "1") != "0",
                video_port=int(props.get("video", "47100") or 47100),
            )
            with self._lock:
                changed = self._phones.get(name) != phone
                self._phones[name] = phone
        if changed:
            log.info("phones on the network: %s", [p.label for p in self.phones])
            self._on_change(self.phones)
