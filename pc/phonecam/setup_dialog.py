"""First-run helper: installs/loads v4l2loopback through pkexec (graphical password prompt)."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

from phonecam.v4l2 import LABEL, find_loopback, module_loaded

log = logging.getLogger(__name__)


def free_video_nr(start: int = 10) -> int:
    n = start
    while Path(f"/dev/video{n}").exists():
        n += 1
    return n


def module_installed() -> bool:
    return (
        subprocess.run(["modinfo", "v4l2loopback"], capture_output=True, check=False).returncode
        == 0
    )


def build_script(video_nr: int, persist: bool) -> str:
    """Root shell script equivalent to scripts/setup-v4l2loopback.sh."""
    opts = f"devices=1 video_nr={video_nr} card_label={LABEL} exclusive_caps=1"
    lines = [
        "set -e",
        "if ! modinfo v4l2loopback >/dev/null 2>&1; then",
        "  echo 'Installing v4l2loopback-dkms...'",
        "  export DEBIAN_FRONTEND=noninteractive",
        '  apt-get install -y v4l2loopback-dkms "linux-headers-$(uname -r)"',
        "fi",
        "if lsmod | grep -q '^v4l2loopback'; then",
        "  echo 'Reloading v4l2loopback (fails if another app is using a loopback device)...'",
        "  modprobe -r v4l2loopback",
        "fi",
        f"modprobe v4l2loopback {opts}",
        f"echo 'Loaded: {opts}'",
    ]
    if persist:
        lines += [
            "echo v4l2loopback > /etc/modules-load.d/phonecam.conf",
            f"echo 'options v4l2loopback {opts}' > /etc/modprobe.d/phonecam.conf",
            "echo 'Will load automatically at boot.'",
        ]
    return "\n".join(lines) + "\n"


class _Runner(QObject):
    output = Signal(str)
    finished = Signal(bool)

    def run(self, script: str) -> None:
        def work() -> None:
            try:
                proc = subprocess.Popen(
                    ["pkexec", "/bin/sh", "-c", script],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    self.output.emit(line.rstrip())
                ok = proc.wait() == 0
            except OSError as e:
                self.output.emit(str(e))
                ok = False
            self.finished.emit(ok)

        threading.Thread(target=work, name="pkexec", daemon=True).start()


class SetupDialog(QDialog):
    """Explains what's missing and fixes it. `device` holds the path once it exists."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set up the PhoneCam virtual camera")
        self.setMinimumWidth(560)
        self.device: str | None = None
        lay = QVBoxLayout(self)

        installed = module_installed()
        loaded = module_loaded()
        if not installed:
            what = (
                "The <b>v4l2loopback</b> kernel module is not installed. It creates the virtual "
                "webcam that Meet, Zoom, OBS and browsers see as “PhoneCam”.<br><br>"
                "Set up will install <code>v4l2loopback-dkms</code> and load it. This needs your "
                "password and can take a minute while the module is built."
            )
        elif loaded:
            what = (
                "<b>v4l2loopback</b> is loaded, but without a “PhoneCam” device. Set up will "
                "reload it with the PhoneCam options. Close any app using a loopback camera first."
            )
        else:
            what = (
                "The <b>v4l2loopback</b> module is installed but not loaded. Set up will load it "
                "with a “PhoneCam” device. This needs your password."
            )
        intro = QLabel(what)
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(intro)
        if shutil.which("mokutil") and installed is False:
            sb = QLabel(
                "If Secure Boot is enabled, the newly built module must be signed with an "
                "enrolled key (MOK). See README → Troubleshooting."
            )
            sb.setWordWrap(True)
            sb.setObjectName("muted")
            lay.addWidget(sb)

        self.persist = QCheckBox("Load automatically at boot")
        self.persist.setChecked(True)
        lay.addWidget(self.persist)
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setVisible(False)
        self.log_view.setMinimumHeight(140)
        lay.addWidget(self.log_view)

        self.buttons = QDialogButtonBox()
        self.setup_btn = self.buttons.addButton(
            "Set up now", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.buttons.addButton("Not now", QDialogButtonBox.ButtonRole.RejectRole)
        self.setup_btn.clicked.connect(self._run)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

        self._runner = _Runner()
        self._runner.output.connect(self.log_view.appendPlainText)
        self._runner.finished.connect(self._done)

    def _run(self) -> None:
        self.setup_btn.setEnabled(False)
        self.persist.setEnabled(False)
        self.log_view.setVisible(True)
        self.log_view.appendPlainText("Waiting for authorization…")
        self._runner.run(build_script(free_video_nr(), self.persist.isChecked()))

    def _done(self, ok: bool) -> None:
        self.device = find_loopback()
        if ok and self.device:
            self.log_view.appendPlainText(f"Ready: {self.device}")
            self.setup_btn.setText("Done")
            self.setup_btn.setEnabled(True)
            self.setup_btn.clicked.disconnect()
            self.setup_btn.clicked.connect(self.accept)
        else:
            self.log_view.appendPlainText(
                "Setup did not complete. See the messages above, or run "
                "scripts/setup-v4l2loopback.sh in a terminal."
            )
            self.setup_btn.setText("Retry")
            self.setup_btn.setEnabled(True)
            self.persist.setEnabled(True)
