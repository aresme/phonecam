"""Fake phone: serves a recorded .h264 file over the PhoneCam protocol.

    python -m phonecam.tools.fake_phone recording.h264 [--fps 30]

Then connect the PC app via Wi-Fi to 127.0.0.1. Used by the pipeline tests too.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import socket
import threading
import time
from pathlib import Path

from phonecam.clock import now_us
from phonecam.h264 import group_access_units
from phonecam.protocol import (
    CONTROL_PORT,
    FLAG_KEYFRAME,
    VIDEO_PORT,
    Packet,
    PacketDecoder,
    PacketType,
    json_packet,
    parse_ping,
    pong_packet,
)

log = logging.getLogger(__name__)

# The fake phone's clock runs 1 hour ahead of the PC's, to exercise offset estimation.
CLOCK_OFFSET_US = 3_600_000_000


class FakePhone:
    def __init__(
        self,
        stream: bytes,
        fps: int = 30,
        host: str = "127.0.0.1",
        video_port: int = 0,
        control_port: int = 0,
        capture_delay_ms: float = 0,
        pairing: str | None = None,
        announce: bool = False,
    ) -> None:
        self.config, self.aus = group_access_units(stream)
        first_key = next((i for i, au in enumerate(self.aus) if au.keyframe), None)
        if not self.config or first_key is None:
            raise ValueError("stream needs SPS/PPS and at least one IDR frame")
        self.aus = self.aus[first_key:]
        self.fps = fps
        self.capture_delay_us = int(capture_delay_ms * 1000)
        self._stop = threading.Event()
        self._video_srv = self._listen(host, video_port)
        self._ctrl_srv = self._listen(host, control_port)
        self.video_port = self._video_srv.getsockname()[1]
        self.control_port = self._ctrl_srv.getsockname()[1]
        self.frames_sent = 0
        self.pairing = pairing
        self._paired = False  # a control client sent the right code (gates the video port)
        self._announce = announce
        self._zc = None
        self._host = host
        self._clients: dict[str, socket.socket] = {}
        self._lock = threading.Lock()
        self._threads = [
            threading.Thread(
                target=self._accept, args=(self._video_srv, "video", self._serve_video), daemon=True
            ),
            threading.Thread(
                target=self._accept,
                args=(self._ctrl_srv, "control", self._serve_control),
                daemon=True,
            ),
        ]

    @staticmethod
    def _listen(host: str, port: int) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(1)
        return s

    def start(self) -> FakePhone:
        for t in self._threads:
            t.start()
        if self._announce:
            self._register_mdns()
        return self

    def _register_mdns(self) -> None:
        from zeroconf import ServiceInfo, Zeroconf

        self._zc = Zeroconf()
        self._service = ServiceInfo(
            "_phonecam._tcp.local.",
            f"PhoneCam Fake {self.control_port}._phonecam._tcp.local.",
            addresses=[socket.inet_aton(self._host if self._host != "0.0.0.0" else "127.0.0.1")],
            port=self.control_port,
            properties={
                "model": "Fake Phone",
                "video": str(self.video_port),
                "version": "1",
                "pairing": "1" if self.pairing else "0",
            },
        )
        self._zc.register_service(self._service)

    def stop(self) -> None:
        self._stop.set()
        if self._zc is not None:
            with contextlib.suppress(Exception):
                self._zc.unregister_service(self._service)
                self._zc.close()
        for s in (self._video_srv, self._ctrl_srv, *self._clients.values()):
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                s.close()
        for t in self._threads:
            t.join(2)

    def phone_now(self) -> int:
        return now_us() + CLOCK_OFFSET_US

    def _accept(self, srv: socket.socket, kind: str, serve) -> None:
        while not self._stop.is_set():
            try:
                sock, addr = srv.accept()
            except OSError:
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:  # one client per port: replace the previous one
                old = self._clients.pop(kind, None)
                self._clients[kind] = sock
            if old:
                with contextlib.suppress(OSError):
                    old.close()
            log.info("fake phone: %s client %s", kind, addr)
            threading.Thread(target=serve, args=(sock,), daemon=True).start()

    def _serve_video(self, sock: socket.socket) -> None:
        period = 1 / self.fps
        if self.pairing and not self._paired:
            sock.close()  # like the phone: video only for paired clients
            return
        try:
            sock.sendall(Packet(PacketType.CONFIG, self.config).encode())
            next_t = time.monotonic()
            i = 0
            while not self._stop.is_set():
                au = self.aus[i % len(self.aus)]
                i += 1
                pts = self.phone_now() - self.capture_delay_us
                sock.sendall(
                    Packet(
                        PacketType.VIDEO, au.data, FLAG_KEYFRAME if au.keyframe else 0, pts
                    ).encode()
                )
                self.frames_sent += 1
                next_t += period
                time.sleep(max(0.0, next_t - time.monotonic()))
        except OSError:
            pass

    def _serve_control(self, sock: socket.socket) -> None:
        dec = PacketDecoder()
        last_stats = time.monotonic()
        sock.settimeout(0.2)
        try:
            while not self._stop.is_set():
                if time.monotonic() - last_stats >= 1:
                    last_stats = time.monotonic()
                    sock.sendall(
                        json_packet(
                            PacketType.STATS, {"fps": self.fps, "dropped_total": 0}
                        ).encode()
                    )
                try:
                    data = sock.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    return
                for p in dec.feed(data):
                    if p.type == PacketType.HELLO and self.pairing:
                        got = p.json().get("pairing")
                        if got != self.pairing:
                            err = "pairing_failed" if got else "pairing_required"
                            sock.sendall(
                                json_packet(PacketType.HELLO, {"version": 1, "error": err}).encode()
                            )
                            return
                        self._paired = True
                    if p.type == PacketType.HELLO:
                        sock.sendall(
                            json_packet(
                                PacketType.HELLO,
                                {
                                    "server": "fake-phone",
                                    "version": 1,
                                    "device": {"model": "Fake Phone", "android": "0", "sdk": 0},
                                    "cameras": [],
                                    "encoders": [],
                                    "state": {},
                                },
                            ).encode()
                        )
                        sock.sendall(json_packet(PacketType.STATE, {"streaming": True}).encode())
                    elif p.type == PacketType.PING:
                        sock.sendall(pong_packet(parse_ping(p), self.phone_now()).encode())
        except OSError:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("file", type=Path)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--pairing", help="require this pairing code")
    ap.add_argument("--announce", action="store_true", help="announce via mDNS (_phonecam._tcp)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    phone = FakePhone(
        args.file.read_bytes(),
        args.fps,
        args.host,
        VIDEO_PORT,
        CONTROL_PORT,
        pairing=args.pairing,
        announce=args.announce,
    ).start()
    print(f"fake phone: {len(phone.aus)} frames on {args.host}:{VIDEO_PORT}/{CONTROL_PORT}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        phone.stop()


if __name__ == "__main__":
    main()
