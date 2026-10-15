"""Main window. Phase 0: layout skeleton only; panels are wired up in later phases."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
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
QLineEdit, QComboBox {
    background: #1f2227; border: 1px solid #3a4048; border-radius: 4px; padding: 4px;
}
QStatusBar { background: #111316; color: #9aa4b2; }
QLabel#preview { background: #0b0c0e; border-radius: 8px; color: #6b7280; font-size: 14pt; }
"""


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PhoneCam")
        self.resize(1200, 680)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._connection_panel())
        self.preview = QLabel("No stream", objectName="preview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(480, 270)
        splitter.addWidget(self.preview)
        splitter.addWidget(self._controls_panel())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 680, 260])
        self.setCentralWidget(splitter)

        self.stats = QLabel("latency — · RTT — · fps — / — · — Mbit/s · dropped 0 · decoder —")
        self.statusBar().addPermanentWidget(self.stats)
        self.statusBar().showMessage("Disconnected")

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
        lay.addWidget(self.ip_edit)
        self.wifi_radio.toggled.connect(self._on_transport_toggled)
        self.connect_btn = QPushButton("Connect", objectName="primary")
        self.connect_btn.setEnabled(False)  # wired up in phase 2
        lay.addWidget(self.connect_btn)
        self.conn_status = QLabel("Disconnected")
        lay.addWidget(self.conn_status)
        lay.addStretch(1)
        return box

    def _on_transport_toggled(self, wifi: bool) -> None:
        self.device_combo.setVisible(not wifi)
        self.ip_edit.setVisible(wifi)

    def _controls_panel(self) -> QWidget:
        box = QGroupBox("Camera")
        form = QFormLayout(box)
        form.addRow(QPushButton("Switch camera"))
        form.addRow("Resolution", QComboBox())
        form.addRow("FPS", QComboBox())
        bitrate = QSlider(Qt.Orientation.Horizontal)
        form.addRow("Bitrate", bitrate)
        form.addRow(QPushButton("Torch"))
        form.addRow("Focus", QComboBox())
        form.addRow("Exposure", QSlider(Qt.Orientation.Horizontal))
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        form.addRow(line)
        form.addRow(QPushButton("Virtual camera: OFF"))
        form.addRow(QLabel("/dev/video— not found"))
        for w in box.findChildren(QWidget):
            w.setEnabled(False)  # controls come alive in phase 4
        box.setEnabled(True)
        return box


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
