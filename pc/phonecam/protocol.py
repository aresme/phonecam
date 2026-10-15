"""PhoneCam wire protocol v1 — see docs/protocol.md (the source of truth)."""

from __future__ import annotations

import enum
import json
import struct
from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol

MAGIC = b"PCAM"
VERSION = 1
HEADER = struct.Struct(">4sBBBBQI")
HEADER_SIZE = HEADER.size  # 20
MAX_PAYLOAD = 4 * 1024 * 1024
MAX_PTS = 2**63 - 1

VIDEO_PORT = 47100
CONTROL_PORT = 47101

FLAG_KEYFRAME = 0x01


class PacketType(enum.IntEnum):
    HELLO = 0x01
    CONFIG = 0x02
    VIDEO = 0x03
    CONTROL_REQ = 0x10
    CONTROL_RES = 0x11
    STATE = 0x12
    PING = 0x20
    PONG = 0x21
    STATS = 0x30
    AUDIO = 0x40
    AUDIO_CONFIG = 0x41


class ProtocolError(Exception):
    """Fatal framing error; the connection must be closed. `kind` matches the test vectors."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class TruncatedError(ProtocolError):
    def __init__(self, message: str = "stream ended mid-packet") -> None:
        super().__init__("truncated", message)


@dataclass(frozen=True, slots=True)
class Packet:
    type: int  # a PacketType value, or an unknown type to be skipped
    payload: bytes = b""
    flags: int = 0
    pts_us: int = 0

    @property
    def is_keyframe(self) -> bool:
        return bool(self.flags & FLAG_KEYFRAME)

    @property
    def known(self) -> bool:
        return self.type in PacketType._value2member_map_

    def json(self) -> Any:
        return json.loads(self.payload)

    def encode(self) -> bytes:
        return encode_header(self.type, len(self.payload), self.flags, self.pts_us) + self.payload


def encode_header(ptype: int, length: int, flags: int = 0, pts_us: int = 0) -> bytes:
    if not 0 <= length <= MAX_PAYLOAD:
        raise ValueError(f"payload length {length} out of range")
    if not 0 <= pts_us <= MAX_PTS:
        raise ValueError(f"pts_us {pts_us} out of range")
    return HEADER.pack(MAGIC, VERSION, ptype, flags, 0, pts_us, length)


def decode_header(buf: bytes | memoryview) -> tuple[int, int, int, int]:
    """Validate a 20-byte header; return (type, flags, pts_us, length)."""
    magic, version, ptype, flags, _reserved, pts_us, length = HEADER.unpack(buf)
    if magic != MAGIC:
        raise ProtocolError("bad_magic", f"bad magic {bytes(magic)!r}")
    if version != VERSION:
        raise ProtocolError("bad_version", f"unsupported version {version}")
    if length > MAX_PAYLOAD:
        raise ProtocolError("too_large", f"payload length {length} exceeds {MAX_PAYLOAD}")
    if pts_us > MAX_PTS:
        raise ProtocolError("bad_pts", f"pts_us {pts_us} exceeds 2^63-1")
    return ptype, flags, pts_us, length


def json_packet(ptype: int, obj: Any, pts_us: int = 0) -> Packet:
    return Packet(ptype, json.dumps(obj, separators=(",", ":")).encode(), 0, pts_us)


class PacketDecoder:
    """Incremental decoder: feed arbitrary byte chunks, get complete packets back."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._header: tuple[int, int, int, int] | None = None

    def feed(self, data: bytes | memoryview) -> list[Packet]:
        self._buf += data
        out: list[Packet] = []
        while True:
            if self._header is None:
                if len(self._buf) < HEADER_SIZE:
                    break
                self._header = decode_header(memoryview(self._buf)[:HEADER_SIZE])
                del self._buf[:HEADER_SIZE]
            ptype, flags, pts_us, length = self._header
            if len(self._buf) < length:
                break
            out.append(Packet(ptype, bytes(self._buf[:length]), flags, pts_us))
            del self._buf[:length]
            self._header = None
        return out

    @property
    def pending(self) -> bool:
        """True if a partial packet is buffered (closing now would be a truncation)."""
        return self._header is not None or bool(self._buf)

    def close(self) -> None:
        if self.pending:
            raise TruncatedError()


class _Reader(Protocol):
    def recv_into(self, buffer: memoryview, nbytes: int = ..., /) -> int: ...


def _read_exact_sock(sock: _Reader, n: int) -> bytes:
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        r = sock.recv_into(view[got:], n - got)
        if r == 0:
            if got == 0:
                raise EOFError
            raise TruncatedError()
        got += r
    return bytes(buf)


def read_packet(sock: _Reader) -> Packet:
    """Blocking read of one packet from a socket. Raises EOFError on clean close at a boundary."""
    ptype, flags, pts_us, length = decode_header(_read_exact_sock(sock, HEADER_SIZE))
    try:
        payload = _read_exact_sock(sock, length) if length else b""
    except EOFError:
        raise TruncatedError() from None
    return Packet(ptype, payload, flags, pts_us)


def read_packet_stream(stream: BinaryIO) -> Packet:
    """Blocking read of one packet from a file-like object (used for recordings/tests)."""
    head = stream.read(HEADER_SIZE)
    if not head:
        raise EOFError
    if len(head) < HEADER_SIZE:
        raise TruncatedError()
    ptype, flags, pts_us, length = decode_header(head)
    payload = stream.read(length)
    if len(payload) < length:
        raise TruncatedError()
    return Packet(ptype, payload, flags, pts_us)


# --- PING / PONG -------------------------------------------------------------------------

_U64 = struct.Struct(">Q")
_U64X2 = struct.Struct(">QQ")


def ping_packet(t_pc_send_us: int) -> Packet:
    return Packet(PacketType.PING, _U64.pack(t_pc_send_us))


def pong_packet(t_pc_send_us: int, t_phone_us: int) -> Packet:
    return Packet(PacketType.PONG, _U64X2.pack(t_pc_send_us, t_phone_us), 0, t_phone_us)


def parse_ping(p: Packet) -> int:
    (t,) = _U64.unpack_from(p.payload)
    return int(t)


def parse_pong(p: Packet) -> tuple[int, int]:
    """Return (t_pc_send_us, t_phone_us)."""
    a, b = _U64X2.unpack_from(p.payload)
    return int(a), int(b)
