from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from phonecam.controls import ControlsPanel

HELLO: dict[str, Any] = {
    "cameras": [
        {
            "id": "0",
            "facing": "back",
            "sizes": [[1920, 1080], [1280, 720]],
            "fps_ranges": [[15, 15], [30, 30], [15, 30]],
            "has_flash": True,
            "af_modes": ["off", "auto", "continuous-video", "locked"],
            "exposure_comp": {"min": -20, "max": 20, "step": 0.1},
        },
        {
            "id": "1",
            "facing": "front",
            "sizes": [[1280, 720]],
            "fps_ranges": [[30, 30]],
            "has_flash": False,
            "af_modes": ["off"],
            "exposure_comp": {"min": -20, "max": 20, "step": 0.1},
        },
    ],
    "state": {
        "camera_id": "0",
        "width": 1280,
        "height": 720,
        "fps": 30,
        "bitrate": 6000000,
        "torch": False,
        "af_mode": "continuous-video",
        "exposure_comp": 0.0,
        "orientation": "landscape",
    },
}


@pytest.fixture
def panel() -> ControlsPanel:
    QApplication.instance() or QApplication([])
    p = ControlsPanel()
    p.set_capabilities(HELLO)
    p.set_connected(True)
    return p


def test_options_follow_capabilities(panel: ControlsPanel) -> None:
    assert [panel.resolution.itemText(i) for i in range(panel.resolution.count())] == [
        "1920×1080",
        "1280×720",
    ]
    assert panel.resolution.currentData() == "1280x720"
    assert [panel.fps.itemData(i) for i in range(panel.fps.count())] == [15, 30]
    assert panel.bitrate.value() == 60
    assert panel.torch.isEnabled()
    assert panel.af.currentData() == "continuous-video"


def test_state_updates_do_not_echo(panel: ControlsPanel) -> None:
    sent: list[tuple[str, dict]] = []
    panel.request.connect(lambda c, a: sent.append((c, a)))
    panel.set_state(
        {
            "camera_id": "1",
            "width": 1280,
            "height": 720,
            "torch": False,
            "af_mode": "off",
            "bitrate": 3000000,
            "exposure_comp": 1.0,
        }
    )
    assert sent == []
    assert panel.camera.currentData() == "1"
    assert not panel.torch.isEnabled()  # front camera has no flash
    assert panel.bitrate.value() == 30 and panel.exposure.value() == 10


def test_user_actions_send_requests(panel: ControlsPanel) -> None:
    sent: list[tuple[str, dict]] = []
    panel.request.connect(lambda c, a: sent.append((c, a)))
    panel.resolution.activated.emit(0)
    panel.resolution.setCurrentIndex(0)
    panel._on_resolution(0)
    panel.switch_camera()
    panel.bitrate.setValue(45)  # keyboard-style change sends immediately
    panel.exposure.setValue(-5)
    assert ("set_resolution", {"width": 1920, "height": 1080}) in sent
    assert ("set_camera", {"id": "1"}) in sent
    assert ("set_bitrate", {"bps": 4_500_000}) in sent
    assert ("set_exposure_comp", {"ev": -0.5}) in sent


def test_disconnected_disables_controls() -> None:
    QApplication.instance() or QApplication([])
    p = ControlsPanel()
    assert not p.resolution.isEnabled() and not p.torch.isEnabled()
