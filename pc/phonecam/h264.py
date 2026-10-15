"""Minimal H.264 Annex-B helpers: NAL splitting and access-unit grouping (no decoding)."""

from __future__ import annotations

from dataclasses import dataclass

NAL_SLICE = 1
NAL_IDR = 5
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9


def split_nals(data: bytes) -> list[bytes]:
    """Split an Annex-B stream into NAL units (start codes stripped)."""
    nals: list[bytes] = []
    i = data.find(b"\x00\x00\x01")
    while i != -1:
        start = i + 3
        nxt = data.find(b"\x00\x00\x01", start)
        end = len(data) if nxt == -1 else nxt
        nal = data[start:end]
        # A 4-byte start code leaves a trailing zero on the previous NAL.
        if nxt != -1 and nal.endswith(b"\x00"):
            nal = nal.rstrip(b"\x00")
        if nal:
            nals.append(nal)
        i = nxt
    return nals


def nal_type(nal: bytes) -> int:
    return nal[0] & 0x1F


def _first_mb_is_zero(nal: bytes) -> bool:
    # first_mb_in_slice is ue(v); it is 0 exactly when the first bit after the header is 1.
    return len(nal) > 1 and bool(nal[1] & 0x80)


@dataclass(frozen=True, slots=True)
class AccessUnit:
    data: bytes  # Annex-B, 4-byte start codes
    keyframe: bool


def _annexb(nals: list[bytes]) -> bytes:
    return b"".join(b"\x00\x00\x00\x01" + n for n in nals)


def group_access_units(stream: bytes) -> tuple[bytes, list[AccessUnit]]:
    """Return (SPS+PPS config, access units). Parameter sets are taken out of the AUs,
    matching the PhoneCam protocol (CONFIG carries them, VIDEO never does)."""
    config: list[bytes] = []
    aus: list[AccessUnit] = []
    pending: list[bytes] = []  # SEI etc. waiting for their slice
    current: list[bytes] = []
    key = False

    def flush() -> None:
        nonlocal current, key
        if current:
            aus.append(AccessUnit(_annexb(current), key))
        current, key = [], False

    for nal in split_nals(stream):
        t = nal_type(nal)
        if t in (NAL_SPS, NAL_PPS):
            if not any(c == nal for c in config):
                config.append(nal)
            flush()
        elif t == NAL_AUD:
            flush()
        elif t in (NAL_SLICE, NAL_IDR):
            if _first_mb_is_zero(nal):
                flush()
                current.extend(pending)
                pending = []
            current.append(nal)
            key = key or t == NAL_IDR
        else:
            if current:
                flush()
            pending.append(nal)
    flush()
    return _annexb(config), aus
