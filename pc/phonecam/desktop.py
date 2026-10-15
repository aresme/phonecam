"""Install/uninstall the GNOME app-grid entry (.desktop file + icon) for the current user."""

from __future__ import annotations

import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

DATA = Path.home() / ".local" / "share"
DESKTOP_FILE = DATA / "applications" / "phonecam.desktop"
ICON_SVG = DATA / "icons" / "hicolor" / "scalable" / "apps" / "phonecam.svg"


def icon_path() -> Path:
    return Path(str(resources.files("phonecam") / "resources" / "phonecam.svg"))


def _quote(arg: str) -> str:
    """Quote an Exec= argument per the Desktop Entry spec (paths may contain spaces)."""
    if any(c in arg for c in " \t\"'\\$`"):
        return (
            '"'
            + arg.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
            + '"'
        )
    return arg


def desktop_entry() -> str:
    exe = _quote(sys.executable)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=PhoneCam\n"
        "GenericName=Phone Webcam\n"
        "Comment=Use an Android phone as a low-latency webcam\n"
        f"Exec={exe} -m phonecam\n"
        "Icon=phonecam\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Video;\n"
        "Keywords=webcam;camera;android;phone;v4l2;\n"
        "StartupWMClass=phonecam\n"
    )


def install() -> list[Path]:
    DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    ICON_SVG.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(icon_path(), ICON_SVG)
    DESKTOP_FILE.write_text(desktop_entry())
    DESKTOP_FILE.chmod(0o755)
    for cmd in (
        ["update-desktop-database", str(DESKTOP_FILE.parent)],
        ["gtk-update-icon-cache", "-q", "-t", str(DATA / "icons" / "hicolor")],
    ):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, check=False, capture_output=True)
    return [DESKTOP_FILE, ICON_SVG]


def uninstall() -> list[Path]:
    removed = []
    for p in (DESKTOP_FILE, ICON_SVG):
        if p.exists():
            p.unlink()
            removed.append(p)
    return removed
