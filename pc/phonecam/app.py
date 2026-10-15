"""Main window: connection panel, live preview, (phase 4) camera controls, stats bar."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QFrame,
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
from phonecam.preview import PreviewWidget, PreviewWorker

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
"""


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
        self.statusBar().addPermanentWidget(self.stats_label)
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
        box = QGroupBox("Camera")
        form = QFormLayout(box)
        form.addRow(QPushButton("Switch camera"))
        form.addRow("Resolution", QComboBox())
        form.addRow("FPS", QComboBox())
        form.addRow("Bitrate", QSlider(Qt.Orientation.Horizontal))
        form.addRow(QPushButton("Torch"))
        form.addRow("Focus", QComboBox())
        form.addRow("Exposure", QSlider(Qt.Orientation.Horizontal))
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        form.addRow(line)
        form.addRow(QPushButton("Virtual camera: OFF"))
        form.addRow(QLabel("/dev/video— not found"))
        for w in box.findChildren(QWidget):
            w.setEnabled(False)  # camera controls arrive in phase 4, virtual camera in phase 3
        return box

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
        current = self._selected_serial()
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
        if self.adb_monitor:
            self.adb_monitor.stop()
        self.preview_worker.stop()
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
