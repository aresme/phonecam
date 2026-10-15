from __future__ import annotations

import io
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from phonecam.protocol import (
    HEADER_SIZE,
    MAX_PAYLOAD,
    Packet,
    PacketDecoder,
    PacketType,
    ProtocolError,
    TruncatedError,
    decode_header,
    encode_header,
    json_packet,
    parse_pong,
    ping_packet,
    pong_packet,
    read_packet,
    read_packet_stream,
)

VECTORS = Path(__file__).resolve().parents[2] / "docs" / "test-vectors"
MANIFEST: list[dict[str, Any]] = json.loads((VECTORS / "manifest.json").read_text())


def load_hex(name: str) -> bytes:
    text = (VECTORS / name).read_text()
    return bytes.fromhex(" ".join(line.split("#", 1)[0] for line in text.splitlines()))


def expected_packets(entry: dict[str, Any]) -> list[Packet]:
    return [
        Packet(p["type"], bytes.fromhex(p["payload"]), p["flags"], int(p["pts_us"]))
        for p in entry["packets"]
    ]


def decode_all(data: bytes, chunk: int) -> list[Packet]:
    dec = PacketDecoder()
    out: list[Packet] = []
    for i in range(0, len(data), chunk):
        out += dec.feed(data[i : i + chunk])
    dec.close()
    return out


@pytest.mark.parametrize("entry", MANIFEST, ids=[e["file"] for e in MANIFEST])
@pytest.mark.parametrize("chunk", [1, 7, 1 << 20])
def test_vector_incremental(entry: dict[str, Any], chunk: int) -> None:
    data = load_hex(entry["file"])
    if "error" in entry:
        with pytest.raises(ProtocolError) as ei:
            decode_all(data, chunk)
        assert ei.value.kind == entry["error"]
    else:
        assert decode_all(data, chunk) == expected_packets(entry)


@pytest.mark.parametrize("entry", MANIFEST, ids=[e["file"] for e in MANIFEST])
def test_vector_stream_reader(entry: dict[str, Any]) -> None:
    stream = io.BytesIO(load_hex(entry["file"]))
    got: list[Packet] = []
    if "error" in entry:
        with pytest.raises(ProtocolError) as ei:
            while True:
                got.append(read_packet_stream(stream))
        assert ei.value.kind == entry["error"]
        return
    with pytest.raises(EOFError):
        while True:
            got.append(read_packet_stream(stream))
    assert got == expected_packets(entry)


@pytest.mark.parametrize(
    "entry",
    [e for e in MANIFEST if "packets" in e],
    ids=[e["file"] for e in MANIFEST if "packets" in e],
)
def test_vector_reencode(entry: dict[str, Any]) -> None:
    encoded = b"".join(p.encode() for p in expected_packets(entry))
    assert encoded == load_hex(entry["file"])


def test_socket_reader_with_split_writes() -> None:
    a, b = socket.socketpair()
    with a, b:
        data = load_hex("11-stream.hex")
        for i in range(0, len(data), 5):
            a.sendall(data[i : i + 5])
        a.shutdown(socket.SHUT_WR)
        got = []
        with pytest.raises(EOFError):
            while True:
                got.append(read_packet(b))
        assert got == expected_packets(next(e for e in MANIFEST if e["file"] == "11-stream.hex"))


def test_socket_reader_truncated() -> None:
    a, b = socket.socketpair()
    with a, b:
        a.sendall(Packet(PacketType.VIDEO, b"abcdef").encode()[:-2])
        a.shutdown(socket.SHUT_WR)
        with pytest.raises(TruncatedError):
            read_packet(b)


def test_max_payload_boundary() -> None:
    assert decode_header(encode_header(PacketType.VIDEO, MAX_PAYLOAD))[3] == MAX_PAYLOAD
    with pytest.raises(ValueError):
        encode_header(PacketType.VIDEO, MAX_PAYLOAD + 1)


def test_decoder_rejects_oversize_before_payload_arrives() -> None:
    header = bytearray(encode_header(PacketType.VIDEO, 0))
    header[16:20] = (MAX_PAYLOAD + 1).to_bytes(4, "big")
    with pytest.raises(ProtocolError, match="exceeds"):
        PacketDecoder().feed(bytes(header))


def test_reserved_byte_ignored() -> None:
    raw = bytearray(Packet(PacketType.STATE, b"{}").encode())
    raw[7] = 0xAA
    assert PacketDecoder().feed(bytes(raw)) == [Packet(PacketType.STATE, b"{}")]


def test_header_size() -> None:
    assert HEADER_SIZE == 20


def test_json_and_ping_helpers() -> None:
    p = json_packet(PacketType.CONTROL_REQ, {"id": 1, "cmd": "get_status", "args": {}})
    assert p.json() == {"id": 1, "cmd": "get_status", "args": {}}
    assert len(ping_packet(42).payload) == 8
    pong = pong_packet(42, 10**12)
    assert parse_pong(pong) == (42, 10**12)
    assert pong.pts_us == 10**12


def test_unknown_type_flags() -> None:
    p = PacketDecoder().feed(load_hex("09-unknown-type.hex"))[0]
    assert not p.known
    assert not p.is_keyframe
    assert Packet(PacketType.VIDEO, flags=1).is_keyframe
