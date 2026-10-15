"""Main window: connection panel, live preview, camera controls, virtual camera, stats bar."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QRadioButton,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from phonecam import __version__
from phonecam.adb import Adb, AdbDevice, AdbError, find_adb
from phonecam.client import Stats, StreamClient, Target
from phonecam.output import VirtualCamera
from phonecam.preview import PreviewWidget, PreviewWorker
from phonecam.setup_dialog import SetupDialog
from phonecam.v4l2 import PIXEL_FORMATS, find_loopback

log = logging.getLogger(__name__)

STYLE = """
QWidget { background: #16181c; color: #e6e6e6; font-size: 10pt; }
QGroupBox { border: 1px solid #2c3036; border-radius: 6px; margin-top: 14px; padding: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; color: #9aa4b2; }
QPushButton {
    background: #2a2f36; border: 1px solid #3a4048; border-radius: 5px; padding: 6px 12px;
}
QPushButton:hover { background: #333a42; }
QPushButton:disabled { color: #6b7280; }
QPushButton#primary { background: #2f6fed; border-color: #2f6fed; color: white; font-weight: 600; }
QPushButton#danger { background: #b3393b; border-color: #b3393b; color: white; font-weight: 600; }
QLineEdit, QComboBox {
    background: #1f2227; border: 1px solid #3a4048; border-radius: 4px; padding: 4px;
}
QStatusBar { background: #111316; color: #9aa4b2; }
QLabel#muted { color: #9aa4b2; }
QPushButton#vcamOn { background: #2e7d32; border-color: #2e7d32; color: white; font-weight: 600; }
"""

OUTPUT_SIZES = [(1280, 720), (1920, 1080), (960, 540), (640, 360)]


def render_placeholder(width: int, height: int, message: str) -> np.ndarray:
    """Placeholder frame for the virtual camera (called on the output thread; QPainter on a
    QImage is safe off the GUI thread)."""
    img = QImage(width, height, QImage.Format.Format_RGB888)
    img.fill(QColor("#16181c"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor("#2f6fed"))
    p.setBrush(QColor("#2f6fed"))
    r = height // 10
    p.drawEllipse(width // 2 - r, height // 2 - int(r * 2.2), 2 * r, 2 * r)
    title = QFont()
    title.setPixelSize(max(12, height // 12))
    title.setBold(True)
    p.setFont(title)
    p.setPen(QColor("#e6e6e6"))
    p.drawText(0, height // 2, width, height // 8, Qt.AlignmentFlag.AlignCenter, "PhoneCam")
    sub_font = QFont()
    sub_font.setPixelSize(max(10, height // 24))
    p.setFont(sub_font)
    p.setPen(QColor("#9aa4b2"))
    p.drawText(
        0, height // 2 + height // 8, width, height // 10, Qt.AlignmentFlag.AlignCenter, message
    )
    p.end()
    arr = np.frombuffer(img.constBits(), np.uint8).reshape(height, img.bytesPerLine())
    return arr[:, : width * 3].reshape(height, width, 3).copy()


def run_bg(fn: Callable[[], Any], name: str = "bg") -> None:
    """Run a blocking call (adb, socket joins) off the GUI thread."""
    threading.Thread(target=fn, name=name, daemon=True).start()


class Bridge(QObject):
    """ClientListener that forwards worker-thread callbacks to the GUI thread as Qt signals."""

    status = Signal(str, bool)
    hello = Signal(dict)
    state = Signal(dict)
    stats = Signal(object)
    devices = Signal(list)
    error = Signal(str)

    def on_status(self, status: str, connected: bool) -> None:
        self.status.emit(status, connected)

    def on_hello(self, hello: dict[str, Any]) -> None:
        self.hello.emit(hello)

    def on_state(self, state: dict[str, Any]) -> None:
        self.state.emit(state)

    def on_stats(self, stats: Stats) -> None:
        self.stats.emit(stats)


class AdbMonitor:
    """Polls `adb devices` so unplug/replug shows up in the device list."""

    def __init__(self, adb: Adb, bridge: Bridge, interval: float = 1.5) -> None:
        self._adb = adb
        self._bridge = bridge
        self._interval = interval
        self._stop = threading.Event()
        self._last: list[AdbDevice] | None = None
        self._thread = threading.Thread(target=self._run, name="adb-monitor", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                devs = self._adb.devices()
            except AdbError as e:
                log.debug("adb devices failed: %s", e)
                devs = []
            if devs != self._last:
                self._last = devs
                self._bridge.devices.emit(devs)
            self._stop.wait(self._interval)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PhoneCam")
        self.resize(1200, 680)

        adb_path = find_adb()
        self.adb = Adb(adb_path) if adb_path else None
        self.bridge = Bridge()
        self.client = StreamClient(self.bridge, self.adb)
        self.preview_worker = PreviewWorker(self.client.preview_slot)
        self.vcam = VirtualCamera(self.client.output_slot, self.client.clock, render_placeholder)
        self.settings = QSettings("PhoneCam", "PhoneCam")
        self._connected = False
        self._active = False  # user wants a connection (connect pressed, not disconnected)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._connection_panel())
        self.preview = PreviewWidget()
        splitter.addWidget(self.preview)
        splitter.addWidget(self._controls_panel())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 680, 260])
        self.setCentralWidget(splitter)

        self.stats_label = QLabel(self._format_stats(None))
        self.vcam_label = QLabel("")
        self.statusBar().addPermanentWidget(self.stats_label)
        self.statusBar().addPermanentWidget(self.vcam_label)
        self.vcam_timer = QTimer(self, interval=500, timeout=self._poll_vcam)
        self.vcam_timer.start()
        self.statusBar().showMessage("Disconnected")

        self.bridge.status.connect(self._on_status)
        self.bridge.stats.connect(self._on_stats)
        self.bridge.hello.connect(self._on_hello)
        self.bridge.devices.connect(self._on_devices)
        self.bridge.error.connect(self._on_error)
        self.preview_worker.frame.connect(self.preview.set_image)
        self.preview_worker.start()

        if self.adb:
            self.adb_monitor: AdbMonitor | None = AdbMonitor(self.adb, self.bridge)
            self.adb_monitor.start()
        else:
            self.adb_monitor = None
            self.device_combo.setPlaceholderText("adb not found")
            self.conn_status.setText(
                "adb was not found. Install it (sudo apt install adb) or use Wi-Fi."
            )
            self.wifi_radio.setChecked(True)
            self.usb_radio.setEnabled(False)
        self._restore_settings()
        QTimer.singleShot(0, self._init_vcam)

    # ------------------------------------------------------------------ layout

    def _connection_panel(self) -> QWidget:
        box = QGroupBox("Connection")
        lay = QVBoxLayout(box)
        transport = QHBoxLayout()
        self.usb_radio = QRadioButton("USB")
        self.wifi_radio = QRadioButton("Wi-Fi")
        self.usb_radio.setChecked(True)
        group = QButtonGroup(box)
        group.addButton(self.usb_radio)
        group.addButton(self.wifi_radio)
        transport.addWidget(self.usb_radio)
        transport.addWidget(self.wifi_radio)
        lay.addLayout(transport)
        self.device_combo = QComboBox()
        self.device_combo.setPlaceholderText("No adb device")
        lay.addWidget(self.device_combo)
        self.ip_edit = QLineEdit(placeholderText="Phone IP, e.g. 192.168.1.20")
        self.ip_edit.setVisible(False)
        self.ip_edit.returnPressed.connect(self._toggle_connect)
        lay.addWidget(self.ip_edit)
        self.wifi_radio.toggled.connect(self._on_transport_toggled)
        self.connect_btn = QPushButton("Connect", objectName="primary")
        self.connect_btn.clicked.connect(self._toggle_connect)
        lay.addWidget(self.connect_btn)
        self.launch_btn = QPushButton("Launch app on phone")
        self.launch_btn.setToolTip("Opens PhoneCam on the selected USB device and starts streaming")
        self.launch_btn.clicked.connect(self._launch_on_phone)
        lay.addWidget(self.launch_btn)
        self.conn_status = QLabel("Disconnected")
        self.conn_status.setWordWrap(True)
        lay.addWidget(self.conn_status)
        self.phone_info = QLabel("", objectName="muted")
        self.phone_info.setWordWrap(True)
        lay.addWidget(self.phone_info)
        lay.addStretch(1)
        return box

    def _controls_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)

        cam = QGroupBox("Camera")
        form = QFormLayout(cam)
        form.addRow(QPushButton("Switch camera"))
        form.addRow("Resolution", QComboBox())
        form.addRow("FPS", QComboBox())
        form.addRow("Bitrate", QSlider(Qt.Orientation.Horizontal))
        form.addRow(QPushButton("Torch"))
        form.addRow("Focus", QComboBox())
        form.addRow("Exposure", QSlider(Qt.Orientation.Horizontal))
        for w in cam.findChildren(QWidget):
            w.setEnabled(False)  # camera controls arrive in phase 4
        lay.addWidget(cam)

        vc = QGroupBox("Virtual camera")
        vform = QFormLayout(vc)
        self.vcam_btn = QPushButton("Virtual camera: OFF")
        self.vcam_btn.setCheckable(True)
        self.vcam_btn.toggled.connect(self._on_vcam_toggled)
        # Only a user click changes the saved preference (not errors or shutdown).
        self.vcam_btn.clicked.connect(lambda on: self.settings.setValue("vcam/on", on))
        vform.addRow(self.vcam_btn)
        self.vcam_device = QLabel("Looking for the PhoneCam device…", objectName="muted")
        self.vcam_device.setWordWrap(True)
        vform.addRow(self.vcam_device)
        self.vcam_size = QComboBox()
        for w, h in OUTPUT_SIZES:
            self.vcam_size.addItem(f"{w}×{h}", (w, h))
        vform.addRow("Output", self.vcam_size)
        self.vcam_format = QComboBox()
        for key, fmt in PIXEL_FORMATS.items():
            self.vcam_format.addItem(fmt.name, key)
        self.vcam_format.setToolTip("I420 is cheaper to produce; YUYV is the most widely supported")
        vform.addRow("Format", self.vcam_format)
        self.vcam_setup_btn = QPushButton("Set up virtual camera…")
        self.vcam_setup_btn.clicked.connect(self._run_setup)
        self.vcam_setup_btn.setVisible(False)
        vform.addRow(self.vcam_setup_btn)
        hint = QLabel(
            "Output size and format apply when the virtual camera is switched on. "
            "Apps using it must reopen the camera after a change.",
            objectName="muted",
        )
        hint.setWordWrap(True)
        vform.addRow(hint)
        lay.addWidget(vc)
        lay.addStretch(1)
        return panel

    # ------------------------------------------------------------------ virtual camera

    def _init_vcam(self) -> None:
        device = find_loopback()
        if device is None:
            self.vcam_device.setText("No PhoneCam device (v4l2loopback not set up).")
            self.vcam_setup_btn.setVisible(True)
            self.vcam_btn.setEnabled(False)
            if not self.settings.value("vcam/setup_declined", False, bool):
                self._run_setup()
            return
        self.vcam_setup_btn.setVisible(False)
        self.vcam_btn.setEnabled(True)
        self.vcam_device.setText(device)
        if self.settings.value("vcam/on", True, bool):
            self.vcam_btn.setChecked(True)

    def _run_setup(self) -> None:
        dlg = SetupDialog(self)
        if dlg.exec() == SetupDialog.DialogCode.Accepted and dlg.device:
            self.settings.setValue("vcam/setup_declined", False)
            self._init_vcam()
        elif find_loopback() is None:
            self.settings.setValue("vcam/setup_declined", True)
        else:
            self._init_vcam()

    def _on_vcam_toggled(self, on: bool) -> None:
        if on:
            device = find_loopback()
            size = self.vcam_size.currentData()
            fmt = PIXEL_FORMATS[self.vcam_format.currentData()]
            try:
                if device is None:
                    raise OSError("the PhoneCam device does not exist")
                self.vcam.message = self._vcam_message()
                self.vcam.start(device, size[0], size[1], fmt)
            except OSError as e:
                self.vcam_device.setText(f"Could not start: {e}")
                self.vcam_btn.blockSignals(True)
                self.vcam_btn.setChecked(False)
                self.vcam_btn.blockSignals(False)
                return
            self.vcam_device.setText(f"{device} · {size[0]}×{size[1]} · {fmt.fourcc}")
        else:
            self.vcam.stop()
            self.vcam_label.setText("")
        self.vcam_btn.setText(f"Virtual camera: {'ON' if on else 'OFF'}")
        self.vcam_btn.setObjectName("vcamOn" if on else "")
        self._restyle(self.vcam_btn)
        self.vcam_size.setEnabled(not on)
        self.vcam_format.setEnabled(not on)
        self.settings.setValue("vcam/size", list(self.vcam_size.currentData()))
        self.settings.setValue("vcam/format", self.vcam_format.currentData())

    def _vcam_message(self) -> str:
        if self._active:
            return "Reconnecting to the phone…"
        return "PhoneCam disconnected"

    def _poll_vcam(self) -> None:
        if self.vcam_btn.isChecked() and not self.vcam.running:
            # The output thread died (device removed, write error).
            self.vcam_btn.setChecked(False)
            self.vcam_device.setText(f"Stopped: {self.vcam.error or 'unknown error'}")
            return
        if not self.vcam.running:
            return
        st = self.vcam.stats()
        lat = f"{st.latency_ms:.0f} ms" if st.latency_ms is not None else "—"
        self.vcam_label.setText(f"  ·  v4l2 {st.fps:.0f} fps ({st.showing}) · capture→v4l2 {lat}")

    # ------------------------------------------------------------------ settings

    def _restore_settings(self) -> None:
        st = self.settings
        if st.value("conn/transport", "usb") == "wifi" or self.adb is None:
            self.wifi_radio.setChecked(True)
        self.ip_edit.setText(st.value("conn/ip", "", str))
        size = st.value("vcam/size", None)
        if size:
            idx = self.vcam_size.findData((int(size[0]), int(size[1])))
            if idx >= 0:
                self.vcam_size.setCurrentIndex(idx)
        idx = self.vcam_format.findData(st.value("vcam/format", "I420", str))
        if idx >= 0:
            self.vcam_format.setCurrentIndex(idx)

    # ------------------------------------------------------------------ actions

    def _on_transport_toggled(self, wifi: bool) -> None:
        self.device_combo.setVisible(not wifi)
        self.ip_edit.setVisible(wifi)
        self.launch_btn.setVisible(not wifi)

    def _selected_serial(self) -> str | None:
        dev = self.device_combo.currentData()
        return dev.serial if isinstance(dev, AdbDevice) else None

    def _toggle_connect(self) -> None:
        if self._active:
            self._active = False
            self.connect_btn.setEnabled(False)
            self.conn_status.setText("Disconnecting…")
            run_bg(self.client.disconnect, "disconnect")
            return
        if self.wifi_radio.isChecked():
            host = self.ip_edit.text().strip()
            if not host:
                self.conn_status.setText("Enter the phone's IP address.")
                return
            target = Target(host)
        else:
            serial = self._selected_serial()
            if serial is None:
                self.conn_status.setText("No USB device selected.")
                return
            target = Target("127.0.0.1", serial)
        self.settings.setValue("conn/transport", "wifi" if self.wifi_radio.isChecked() else "usb")
        self.settings.setValue("conn/ip", self.ip_edit.text().strip())
        self.settings.setValue("conn/serial", target.serial or "")
        self._active = True
        self._set_inputs_enabled(False)
        self.connect_btn.setText("Disconnect")
        self.connect_btn.setObjectName("danger")
        self._restyle(self.connect_btn)
        self.preview.clear("Connecting…")
        run_bg(lambda: self.client.connect(target), "connect")

    def _launch_on_phone(self) -> None:
        serial = self._selected_serial()
        if not (self.adb and serial):
            self.conn_status.setText("No USB device selected.")
            return
        adb = self.adb

        def launch() -> None:
            try:
                adb.start_app(serial, autostart=True)
            except AdbError as e:
                self.bridge.error.emit(f"Could not launch the app: {e}")

        run_bg(launch, "launch")

    def _set_inputs_enabled(self, enabled: bool) -> None:
        for w in (self.usb_radio, self.wifi_radio, self.device_combo, self.ip_edit):
            w.setEnabled(enabled)
        if self.adb is None:
            self.usb_radio.setEnabled(False)

    @staticmethod
    def _restyle(w: QWidget) -> None:
        w.style().unpolish(w)
        w.style().polish(w)

    # ------------------------------------------------------------------ client events

    def _on_status(self, status: str, connected: bool) -> None:
        self._connected = connected
        if status == "Disconnected":
            self._active = False
        self.vcam.message = self._vcam_message()
        self.conn_status.setText(status)
        self.statusBar().showMessage(status)
        if connected:
            self.preview.set_message("")
        elif self._active:
            self.preview.set_message(status)
        if status == "Disconnected":
            self._active = False
            self.connect_btn.setEnabled(True)
            self.connect_btn.setText("Connect")
            self.connect_btn.setObjectName("primary")
            self._restyle(self.connect_btn)
            self._set_inputs_enabled(True)
            self.preview.clear("Disconnected")
            self.phone_info.setText("")
            self.stats_label.setText(self._format_stats(None))

    def _on_hello(self, hello: dict[str, Any]) -> None:
        dev = hello.get("device", {})
        state = hello.get("state", {})
        enc = state.get("encoder") or "?"
        self.phone_info.setText(
            f"{dev.get('model', '?')} · Android {dev.get('android', '?')}\n"
            f"{state.get('width', '?')}×{state.get('height', '?')} @ {state.get('fps', '?')} fps"
            f" · {enc}"
        )

    def _on_stats(self, s: Stats) -> None:
        self.stats_label.setText(self._format_stats(s))

    @staticmethod
    def _format_stats(s: Stats | None) -> str:
        if s is None:
            return "latency — · RTT — · fps — / — · — Mbit/s · dropped 0 · decoder —"
        lat = f"{s.latency_ms:.0f} ms" if s.latency_ms is not None else "—"
        rtt = f"{s.rtt_ms:.1f} ms" if s.rtt_ms is not None else "—"
        phone_ms = s.phone.get("pipeline_ms")
        phone = f" (phone {phone_ms:.0f} ms)" if isinstance(phone_ms, int | float) else ""
        return (
            f"capture→decoded {lat}{phone} · RTT {rtt} · fps {s.fps_in:.0f} in / "
            f"{s.fps_decoded:.0f} dec · {s.bitrate_bps / 1e6:.1f} Mbit/s · "
            f"dropped {s.dropped_phone} · {s.width}×{s.height} · {s.decoder}"
        )

    def _on_devices(self, devices: list[AdbDevice]) -> None:
        current = self._selected_serial() or self.settings.value("conn/serial", "", str)
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for d in devices:
            self.device_combo.addItem(d.label, d)
        idx = next((i for i, d in enumerate(devices) if d.serial == current), None)
        if idx is None:
            idx = next((i for i, d in enumerate(devices) if d.ready), -1)
        self.device_combo.setCurrentIndex(idx)
        self.device_combo.blockSignals(False)

    def _on_error(self, message: str) -> None:
        self.conn_status.setText(message)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.vcam_timer.stop()
        if self.adb_monitor:
            self.adb_monitor.stop()
        self.preview_worker.stop()
        self.vcam.stop()
        # Bounded wait so adb forwards get removed; the threads are daemons regardless.
        t = threading.Thread(target=self.client.disconnect, daemon=True)
        t.start()
        t.join(2)
        super().closeEvent(event)


def run(argv: list[str]) -> int:
    app = QApplication(argv)
    app.setApplicationName("PhoneCam")
    app.setDesktopFileName("phonecam")
    app.setApplicationVersion(__version__)
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    log.info("PhoneCam %s started", __version__)
    return app.exec()
