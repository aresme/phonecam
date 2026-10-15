"""adb CLI wrapper. All calls block; never call them on the GUI thread."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

APP_COMPONENT = "com.ares.phonecam/.MainActivity"


class AdbError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AdbDevice:
    serial: str
    state: str  # device, unauthorized, offline, ...
    model: str = ""

    @property
    def is_network(self) -> bool:
        return ":" in self.serial

    @property
    def ready(self) -> bool:
        return self.state == "device"

    @property
    def label(self) -> str:
        name = self.model.replace("_", " ") or self.serial
        suffix = "" if self.ready else f" ({self.state})"
        kind = "Wi-Fi adb" if self.is_network else "USB"
        return f"{name} · {kind} · {self.serial}{suffix}"


def find_adb() -> str | None:
    candidates = [shutil.which("adb")]
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(env):
            candidates.append(str(Path(os.environ[env]) / "platform-tools" / "adb"))
    candidates.append(str(Path.home() / "Android" / "Sdk" / "platform-tools" / "adb"))
    return next((c for c in candidates if c and os.access(c, os.X_OK)), None)


def parse_devices(text: str) -> list[AdbDevice]:
    """Parse `adb devices -l` output."""
    devices = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        props = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append(AdbDevice(parts[0], parts[1], props.get("model", "")))
    return devices


class Adb:
    def __init__(self, path: str) -> None:
        self.path = path

    def run(self, *args: str, serial: str | None = None, timeout: float = 5.0) -> str:
        cmd = [self.path, *(["-s", serial] if serial else []), *args]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"timed out: {' '.join(args)}") from e
        if res.returncode != 0:
            msg = (res.stderr or res.stdout).strip() or f"exit {res.returncode}"
            raise AdbError(f"adb {' '.join(args)}: {msg}")
        return res.stdout

    def devices(self) -> list[AdbDevice]:
        return parse_devices(self.run("devices", "-l"))

    def forward(self, serial: str, port: int) -> None:
        self.run("forward", f"tcp:{port}", f"tcp:{port}", serial=serial)

    def remove_forward(self, serial: str, port: int) -> None:
        try:
            self.run("forward", "--remove", f"tcp:{port}", serial=serial)
        except AdbError as e:
            log.debug("remove forward: %s", e)

    def start_app(self, serial: str, autostart: bool = True) -> None:
        args = ["shell", "am", "start", "-n", APP_COMPONENT]
        if autostart:
            args += ["--ez", "autostart", "true"]
        self.run(*args, serial=serial, timeout=10)
