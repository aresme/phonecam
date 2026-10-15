from pathlib import Path

import pytest

from phonecam.decoder import H264Decoder, vaapi_available
from phonecam.h264 import group_access_units

DATA = Path(__file__).parent / "data"


def decode_all(dec: H264Decoder, name: str) -> list:
    cfg, aus = group_access_units((DATA / name).read_bytes())
    dec.configure(cfg)
    frames = []
    for i, au in enumerate(aus):
        frames += dec.decode(au.data, i * 33_333, au.keyframe)
    return frames


@pytest.mark.parametrize("name", ["phone-720p-12f.h264", "phone-720p-main-10f.h264"])
def test_software_decodes_phone_streams(name: str) -> None:
    dec = H264Decoder("software")
    frames = decode_all(dec, name)
    assert len(frames) >= 10 and frames[0].width == 1280
    assert dec.name.startswith("software")


@pytest.mark.skipif(not vaapi_available(), reason="PyAV built without VAAPI")
def test_vaapi_falls_back_on_plain_baseline() -> None:
    dec = H264Decoder("vaapi")
    frames = decode_all(dec, "phone-720p-12f.h264")  # plain Baseline: VAAPI refuses it
    assert len(frames) >= 11
    assert dec.name.startswith("software")


@pytest.mark.skipif(not vaapi_available(), reason="PyAV built without VAAPI")
def test_vaapi_decodes_main_profile() -> None:
    dec = H264Decoder("vaapi")
    frames = decode_all(dec, "phone-720p-main-10f.h264")
    if not dec.name.startswith("VAAPI"):
        pytest.skip(f"no usable VAAPI device: {dec.name}")
    assert len(frames) == 10 and frames[0].width == 1280


def test_mode_switch_keeps_decoding() -> None:
    dec = H264Decoder("software")
    cfg, aus = group_access_units((DATA / "phone-720p-main-10f.h264").read_bytes())
    dec.configure(cfg)
    assert dec.decode(aus[0].data, 0, True)
    dec.set_mode("vaapi")  # reopens; waits for the next keyframe
    assert dec.decode(aus[1].data, 1, False) == []
    assert dec.decode(aus[0].data, 2, True)
