from pathlib import Path

from phonecam.h264 import NAL_IDR, NAL_PPS, NAL_SPS, group_access_units, nal_type, split_nals

SAMPLE = Path(__file__).parent / "data" / "phone-720p-12f.h264"


def test_split_nals_handles_3_and_4_byte_start_codes() -> None:
    data = b"\x00\x00\x00\x01\x67\xaa\x00\x00\x01\x68\xbb\x00\x00\x00\x01\x65\xcc"
    assert split_nals(data) == [b"\x67\xaa", b"\x68\xbb", b"\x65\xcc"]


def test_phone_sample_groups_into_frames() -> None:
    config, aus = group_access_units(SAMPLE.read_bytes())
    assert [nal_type(n) for n in split_nals(config)] == [NAL_SPS, NAL_PPS]
    assert len(aus) == 12
    assert aus[0].keyframe and not any(a.keyframe for a in aus[1:])
    assert nal_type(split_nals(aus[0].data)[0]) == NAL_IDR
    # Parameter sets are moved to CONFIG, never left in access units.
    assert all(nal_type(n) not in (NAL_SPS, NAL_PPS) for a in aus for n in split_nals(a.data))
