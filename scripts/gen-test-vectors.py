#!/usr/bin/env python3
"""Generate docs/test-vectors/ from first principles (plain struct, no project code).

The vectors are committed; rerun this only when deliberately changing them.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "test-vectors"


def pkt(ptype: int, payload: bytes = b"", flags: int = 0, pts: int = 0,
        magic: bytes = b"PCAM", version: int = 1, length: int | None = None) -> bytes:
    n = len(payload) if length is None else length
    return magic + struct.pack(">BBBBQI", version, ptype, flags, 0, pts, n) + payload


def hexdump(data: bytes) -> str:
    lines = []
    for i in range(0, len(data), 16):
        lines.append(" ".join(f"{b:02x}" for b in data[i:i + 16]))
    return "\n".join(lines) + "\n"


def expect(ptype: int, payload: bytes = b"", flags: int = 0, pts: int = 0) -> dict:
    return {"type": ptype, "flags": flags, "pts_us": str(pts), "payload": payload.hex()}


def js(obj: object) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


SPS_PPS = bytes.fromhex("0000000167428028da0280bfe5c044000003000400000300f23c60ca80"
                        "0000000168ce06e2")
IDR = bytes.fromhex("0000000165b8000c3f0a1b2c3d4e5f60")
P_FRAME = bytes.fromhex("0000000141e0021f8b")
HELLO_PC = js({"client": "phonecam-pc/0.1.0", "version": 1})
REQ = js({"id": 7, "cmd": "set_resolution", "args": {"width": 1280, "height": 720}})
PING = struct.pack(">Q", 123456789)
PONG = struct.pack(">QQ", 123456789, 987654321012)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    vectors: list[tuple[str, str, bytes, dict]] = []

    def ok(name: str, desc: str, packets: list[tuple[bytes, dict]]) -> None:
        data = b"".join(p for p, _ in packets)
        vectors.append((name, desc, data, {"packets": [e for _, e in packets]}))

    def err(name: str, desc: str, data: bytes, kind: str) -> None:
        vectors.append((name, desc, data, {"error": kind}))

    ok("01-hello-pc", "HELLO from PC with JSON payload",
       [(pkt(0x01, HELLO_PC), expect(0x01, HELLO_PC))])
    ok("02-config", "CONFIG: SPS+PPS Annex-B, pts 0",
       [(pkt(0x02, SPS_PPS), expect(0x02, SPS_PPS))])
    ok("03-video-key", "VIDEO keyframe (flags bit0), realistic boottime pts",
       [(pkt(0x03, IDR, 1, 1234567890123), expect(0x03, IDR, 1, 1234567890123))])
    ok("04-video-delta", "VIDEO non-key frame",
       [(pkt(0x03, P_FRAME, 0, 1234567923456), expect(0x03, P_FRAME, 0, 1234567923456))])
    ok("05-control-req", "CONTROL_REQ set_resolution",
       [(pkt(0x10, REQ), expect(0x10, REQ))])
    ok("06-ping", "PING with 8-byte PC timestamp",
       [(pkt(0x20, PING), expect(0x20, PING))])
    ok("07-pong", "PONG echo + phone timestamp, header pts = phone ts",
       [(pkt(0x21, PONG, 0, 987654321012), expect(0x21, PONG, 0, 987654321012))])
    ok("08-empty-payload", "zero-length payload (request without body)",
       [(pkt(0x12), expect(0x12))])
    ok("09-unknown-type", "unknown type 0x7f must decode (and be skippable)",
       [(pkt(0x7F, b"\xde\xad\xbe\xef", 0x80, 5), expect(0x7F, b"\xde\xad\xbe\xef", 0x80, 5))])
    ok("10-max-pts", "largest allowed pts (2^63-1)",
       [(pkt(0x03, P_FRAME, 0, 2**63 - 1), expect(0x03, P_FRAME, 0, 2**63 - 1))])
    ok("11-stream", "several packets back to back",
       [(pkt(0x02, SPS_PPS), expect(0x02, SPS_PPS)),
        (pkt(0x03, IDR, 1, 1000), expect(0x03, IDR, 1, 1000)),
        (pkt(0x7F, b"skip"), expect(0x7F, b"skip")),
        (pkt(0x03, P_FRAME, 0, 34333), expect(0x03, P_FRAME, 0, 34333))])
    # The exactly-4-MiB boundary is covered by unit tests on each side, not a committed vector.
    err("20-bad-magic", "magic 'XCAM'", pkt(0x03, P_FRAME, magic=b"XCAM"), "bad_magic")
    err("21-bad-version", "version 2", pkt(0x03, P_FRAME, version=2), "bad_version")
    err("22-too-large", "length 4 MiB + 1 (header only)",
        pkt(0x03, b"", length=4 * 1024 * 1024 + 1), "too_large")
    err("25-bad-pts", "pts_us = 2^63 (does not fit a signed 64-bit integer)",
        pkt(0x03, P_FRAME, 0, 2**63), "bad_pts")
    err("23-truncated-header", "stream ends inside header", pkt(0x03, P_FRAME)[:11], "truncated")
    err("24-truncated-payload", "stream ends inside payload", pkt(0x03, P_FRAME)[:-3], "truncated")

    manifest = []
    for name, desc, data, result in vectors:
        (OUT / f"{name}.hex").write_text(f"# {desc}\n" + hexdump(data))
        manifest.append({"file": f"{name}.hex", "description": desc, **result})
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {len(manifest)} vectors to {OUT}")


if __name__ == "__main__":
    main()
