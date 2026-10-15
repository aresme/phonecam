"""Camera control panel: mirrors the phone's settings and sends CONTROL_REQs.

Options come from the phone's HELLO capabilities. STATE packets are authoritative: widgets are
updated from them with signals blocked, so a state update never echoes back as a request.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
)

AF_LABELS = {
    "continuous-video": "Continuous (video)",
    "continuous-picture": "Continuous (picture)",
    "auto": "Auto (single scan)",
    "locked": "Locked",
    "macro": "Macro",
    "off": "Off (fixed)",
}
ORIENTATIONS = [
    ("landscape", "Landscape"),
    ("portrait", "Portrait"),
    ("auto", "Auto (follow phone)"),
]
BITRATE_STEP = 100_000  # slider units


class ControlsPanel(QGroupBox):
    request = Signal(str, dict)  # cmd, args
    mirror_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__("Camera")
        self._cameras: dict[str, dict[str, Any]] = {}
        self._state: dict[str, Any] = {}
        self._ev_step = 0.1
        self._connected = False
        form = QFormLayout(self)

        self.camera = QComboBox()
        self.switch_btn = QPushButton("Switch")
        self.switch_btn.setToolTip("Next camera")
        row = QHBoxLayout()
        row.addWidget(self.camera, 1)
        row.addWidget(self.switch_btn)
        holder = QWidget()
        holder.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        form.addRow("Camera", holder)

        self.resolution = QComboBox()
        form.addRow("Resolution", self.resolution)
        self.fps = QComboBox()
        form.addRow("FPS", self.fps)

        self.bitrate = QSlider(Qt.Orientation.Horizontal)
        self.bitrate.setRange(5, 200)  # 0.5 .. 20 Mbit/s
        self.bitrate_label = QLabel("—")
        self.bitrate_label.setMinimumWidth(70)
        brow = QHBoxLayout()
        brow.addWidget(self.bitrate, 1)
        brow.addWidget(self.bitrate_label)
        bholder = QWidget()
        bholder.setLayout(brow)
        brow.setContentsMargins(0, 0, 0, 0)
        form.addRow("Bitrate", bholder)

        self.torch = QPushButton("Torch: off")
        self.torch.setCheckable(True)
        form.addRow(self.torch)

        self.af = QComboBox()
        form.addRow("Focus", self.af)
        hint = QLabel("Click the preview to focus there.", objectName="muted")
        form.addRow(hint)

        self.exposure = QSlider(Qt.Orientation.Horizontal)
        self.ev_label = QLabel("EV 0.0")
        self.ev_label.setMinimumWidth(70)
        erow = QHBoxLayout()
        erow.addWidget(self.exposure, 1)
        erow.addWidget(self.ev_label)
        eholder = QWidget()
        eholder.setLayout(erow)
        erow.setContentsMargins(0, 0, 0, 0)
        form.addRow("Exposure", eholder)

        self.orientation = QComboBox()
        for key, label in ORIENTATIONS:
            self.orientation.addItem(label, key)
        form.addRow("Orientation", self.orientation)

        self.mic = QCheckBox("Stream phone microphone")
        self.mic.setToolTip("Sends the phone's mic to the PhoneCam Mic input on this PC")
        self.mic_source = QComboBox()
        self.mic_source.addItem("Voice (noise suppression)", "voice")
        self.mic_source.addItem("Raw", "raw")
        form.addRow(self.mic)
        form.addRow("Mic mode", self.mic_source)

        self.mirror = QCheckBox("Mirror output (PC side)")
        self.mirror.setToolTip("Flip the video horizontally before it reaches the virtual camera")
        form.addRow(self.mirror)

        # user actions -> requests
        self.camera.activated.connect(
            lambda _: self._send("set_camera", id=self.camera.currentData())
        )
        self.switch_btn.clicked.connect(self.switch_camera)
        self.resolution.activated.connect(self._on_resolution)
        self.fps.activated.connect(lambda _: self._send("set_fps", fps=self.fps.currentData()))
        self.bitrate.valueChanged.connect(self._on_bitrate_moved)
        self.bitrate.sliderReleased.connect(self._send_bitrate)
        self.torch.clicked.connect(lambda on: self._send("set_torch", on=on))
        self.af.activated.connect(lambda _: self._send("set_af", mode=self.af.currentData()))
        self.exposure.valueChanged.connect(self._on_ev_moved)
        self.exposure.sliderReleased.connect(self._send_ev)
        self.orientation.activated.connect(
            lambda _: self._send("set_orientation", orientation=self.orientation.currentData())
        )
        self.mirror.toggled.connect(self.mirror_changed)
        self.mic.clicked.connect(lambda on: self._send("set_audio", enabled=on))
        self.mic_source.activated.connect(
            lambda _: self._send("set_audio", mic=self.mic_source.currentData())
        )
        self.set_connected(False)

    # ------------------------------------------------------------------ inbound

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        for w in (
            self.camera,
            self.switch_btn,
            self.resolution,
            self.fps,
            self.bitrate,
            self.torch,
            self.af,
            self.exposure,
            self.orientation,
            self.mic,
            self.mic_source,
        ):
            w.setEnabled(connected)
        if connected:
            self._apply_capabilities()

    def set_capabilities(self, hello: dict[str, Any]) -> None:
        self._cameras = {str(c["id"]): c for c in hello.get("cameras", [])}
        with _Blocked(self.camera):
            self.camera.clear()
            for cid, c in self._cameras.items():
                self.camera.addItem(f"{str(c.get('facing', '?')).capitalize()} · {cid}", cid)
        self.set_state(hello.get("state", {}))

    def set_state(self, state: dict[str, Any]) -> None:
        if not state:
            return
        self._state = {**self._state, **state}
        self._apply_capabilities()

    # ------------------------------------------------------------------ helpers

    def _cam(self) -> dict[str, Any]:
        return self._cameras.get(str(self._state.get("camera_id")), {})

    def _apply_capabilities(self) -> None:
        st, cam = self._state, self._cam()
        if not st:
            return
        with _Blocked(self.camera):
            idx = self.camera.findData(str(st.get("camera_id")))
            if idx >= 0:
                self.camera.setCurrentIndex(idx)
        self.switch_btn.setEnabled(self._connected and len(self._cameras) > 1)

        with _Blocked(self.resolution):
            self.resolution.clear()
            for w, h in cam.get("sizes", []):
                self.resolution.addItem(f"{w}×{h}", f"{int(w)}x{int(h)}")  # str: findData-safe
            self.resolution.setCurrentIndex(
                self.resolution.findData(f"{st.get('width')}x{st.get('height')}")
            )

        with _Blocked(self.fps):
            self.fps.clear()
            for f in sorted({int(r[1]) for r in cam.get("fps_ranges", [])}):
                self.fps.addItem(f"{f} fps", f)
            self.fps.setCurrentIndex(self.fps.findData(st.get("fps")))

        if not self.bitrate.isSliderDown() and st.get("bitrate"):
            with _Blocked(self.bitrate):
                self.bitrate.setValue(round(int(st["bitrate"]) / BITRATE_STEP))
            self.bitrate_label.setText(f"{int(st['bitrate']) / 1e6:.1f} Mbit/s")

        has_flash = bool(cam.get("has_flash"))
        with _Blocked(self.torch):
            self.torch.setChecked(bool(st.get("torch")))
        self.torch.setText(
            f"Torch: {'on' if st.get('torch') else 'off'}" if has_flash else "Torch: n/a"
        )
        self.torch.setEnabled(self._connected and has_flash)

        with _Blocked(self.af):
            self.af.clear()
            for mode in cam.get("af_modes", []):
                self.af.addItem(AF_LABELS.get(mode, mode), mode)
            self.af.setCurrentIndex(self.af.findData(st.get("af_mode")))

        ec = cam.get("exposure_comp", {})
        self._ev_step = float(ec.get("step") or 0.1)
        if not self.exposure.isSliderDown():
            with _Blocked(self.exposure):
                self.exposure.setRange(int(ec.get("min", 0)), int(ec.get("max", 0)))
                self.exposure.setValue(round(float(st.get("exposure_comp", 0)) / self._ev_step))
            self.ev_label.setText(f"EV {float(st.get('exposure_comp', 0)):+.1f}")

        with _Blocked(self.orientation):
            self.orientation.setCurrentIndex(self.orientation.findData(st.get("orientation")))

        with _Blocked(self.mic):
            self.mic.setChecked(bool(st.get("audio")))
        with _Blocked(self.mic_source):
            self.mic_source.setCurrentIndex(self.mic_source.findData(st.get("mic", "voice")))
        allowed = bool(st.get("audio_permission", True))
        self.mic.setEnabled(self._connected and allowed)
        if st.get("audio_error"):
            self.mic.setToolTip(str(st["audio_error"]))
        elif not allowed:
            self.mic.setToolTip("Allow the microphone once in the phone app (tap its mic button)")
        else:
            self.mic.setToolTip("Sends the phone's mic to the PhoneCam Mic input on this PC")

    def _send(self, cmd: str, **args: Any) -> None:
        self.request.emit(cmd, args)

    def switch_camera(self) -> None:
        ids = list(self._cameras)
        if len(ids) < 2:
            return
        cur = str(self._state.get("camera_id"))
        nxt = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
        self._send("set_camera", id=nxt)

    def _on_resolution(self, _: int) -> None:
        w, h = (int(v) for v in self.resolution.currentData().split("x"))
        self._send("set_resolution", width=w, height=h)

    def _on_bitrate_moved(self, v: int) -> None:
        self.bitrate_label.setText(f"{v * BITRATE_STEP / 1e6:.1f} Mbit/s")
        if not self.bitrate.isSliderDown():  # keyboard / wheel
            self._send_bitrate()

    def _send_bitrate(self) -> None:
        self._send("set_bitrate", bps=self.bitrate.value() * BITRATE_STEP)

    def _on_ev_moved(self, v: int) -> None:
        self.ev_label.setText(f"EV {v * self._ev_step:+.1f}")
        if not self.exposure.isSliderDown():
            self._send_ev()

    def _send_ev(self) -> None:
        self._send("set_exposure_comp", ev=round(self.exposure.value() * self._ev_step, 3))


class _Blocked:
    """Context manager: block a widget's signals while updating it programmatically."""

    def __init__(self, w: QWidget) -> None:
        self.w = w

    def __enter__(self) -> None:
        self._prev = self.w.blockSignals(True)

    def __exit__(self, *exc: object) -> None:
        self.w.blockSignals(self._prev)
