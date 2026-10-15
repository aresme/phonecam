"""Connection to the phone: reconnect supervisor, video/decode/control threads.

Threading (docs/architecture.md):
- supervisor thread: adb forward, connect, then reads the video socket for the session's life
- decode thread: PyAV decode, publishes frames to the latest-frame slots
- control thread: HELLO, PING every 500 ms, PONG/STATE/STATS handling, stats reporting
Consumers (GUI preview, v4l2 output) each read their own LatestFrameSlot.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import av

from phonecam import __version__
from phonecam.adb import Adb, AdbError
from phonecam.clock import ClockSync, now_us
from phonecam.decoder import H264Decoder
from phonecam.frameslot import LatestFrameSlot
from phonecam.protocol import (
    CONTROL_PORT,
    FLAG_KEYFRAME,
    VIDEO_PORT,
    PacketDecoder,
    PacketType,
    ProtocolError,
    json_packet,
    parse_pong,
    ping_packet,
    read_packet,
)

log = logging.getLogger(__name__)

PING_INTERVAL = 0.5
VIDEO_TIMEOUT = 3.0  # the encoder repeats frames, so silence this long means a dead link
PONG_TIMEOUT = 3.0
BACKOFF_MIN = 0.5
BACKOFF_MAX = 5.0


@dataclass(frozen=True)
class Target:
    host: str = "127.0.0.1"
    serial: str | None = None  # set for USB: adb forward is (re)run on every connect
    video_port: int = VIDEO_PORT
    control_port: int = CONTROL_PORT
    pairing: str | None = None  # Wi-Fi pairing code shown on the phone

    @property
    def transport(self) -> str:
        return "USB" if self.serial else "Wi-Fi"


@dataclass(slots=True)
class DecodedFrame:
    frame: av.VideoFrame
    pts_us: int  # phone clock
    decoded_us: int  # PC clock
    rotation: int = 0  # clockwise degrees to make the frame upright (VIDEO flag bits 1-2)
    received_us: int = 0  # PC clock, when the packet was read from the socket


@dataclass
class Stats:
    fps_in: float = 0.0
    fps_decoded: float = 0.0
    bitrate_bps: float = 0.0
    latency_ms: float | None = None  # capture -> decoded
    received_ms: float | None = None  # capture -> received on the PC (phone pipeline + network)
    decode_ms: float | None = None  # avcodec time per frame
    rtt_ms: float | None = None
    dropped_phone: int = 0
    decoder: str = ""
    width: int = 0
    height: int = 0
    phone: dict[str, Any] = field(default_factory=dict)


class ClientListener(Protocol):
    """Called from worker threads; implementations must be thread-safe (e.g. emit Qt signals)."""

    def on_status(self, status: str, connected: bool) -> None: ...
    def on_hello(self, hello: dict[str, Any]) -> None: ...
    def on_state(self, state: dict[str, Any]) -> None: ...
    def on_stats(self, stats: Stats) -> None: ...
    def on_pairing(self, kind: str) -> None: ...  # "pairing_required" | "pairing_failed"


class _SessionEnded(Exception):
    pass


class PairingError(_SessionEnded):
    """The phone wants a (correct) pairing code; retrying without user input is pointless."""

    def __init__(self, kind: str) -> None:
        super().__init__(
            "pairing code required" if kind == "pairing_required" else "wrong pairing code"
        )
        self.kind = kind


class StreamClient:
    def __init__(self, listener: ClientListener, adb: Adb | None = None) -> None:
        self.listener = listener
        self.adb = adb
        self.clock = ClockSync()
        self.decoder_name = ""
        self.decoder_mode = "software"  # or "vaapi"; applied live by the decode thread
        # One slot per consumer: GUI preview, and (phase 3) the virtual camera output.
        self.preview_slot: LatestFrameSlot[DecodedFrame] = LatestFrameSlot()
        self.output_slot: LatestFrameSlot[DecodedFrame] = LatestFrameSlot()
        self._target: Target | None = None
        self._stop = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._lock = threading.Lock()
        self._video_sock: socket.socket | None = None
        self._ctrl_sock: socket.socket | None = None
        self._ctrl_send_lock = threading.Lock()
        self._next_req_id = 1
        # counters (written by worker threads, read by the stats reporter)
        self._packets = 0
        self._bytes = 0
        self._decoded = 0
        self._lat_sum = 0
        self._lat_n = 0
        self._recv_sum = self._dec_sum = self._dec_n = 0
        self._phone_stats: dict[str, Any] = {}
        self._size = (0, 0)

    # ------------------------------------------------------------------ public API

    @property
    def running(self) -> bool:
        return self._supervisor is not None and self._supervisor.is_alive()

    def connect(self, target: Target) -> None:
        self.disconnect(notify=False)  # replacing a session is not a user-visible disconnect
        self._target = target
        self._stop.clear()
        self.preview_slot.reopen()
        self.output_slot.reopen()
        self._supervisor = threading.Thread(target=self._supervise, name="supervisor", daemon=True)
        self._supervisor.start()

    def disconnect(self, timeout: float = 3.0, notify: bool = True) -> None:
        sup = self._supervisor
        if sup is None:
            return
        self._stop.set()
        self._close_sockets()
        sup.join(timeout)
        self._supervisor = None
        target = self._target
        if target and target.serial and self.adb:
            for port in (target.video_port, target.control_port):
                self.adb.remove_forward(target.serial, port)
        if notify:
            self.listener.on_status("Disconnected", False)

    def send_request(self, cmd: str, **args: Any) -> int | None:
        """Send a CONTROL_REQ; returns its id, or None if not connected."""
        with self._lock:
            req_id = self._next_req_id
            self._next_req_id += 1
        return (
            req_id
            if self._send_ctrl(
                json_packet(PacketType.CONTROL_REQ, {"id": req_id, "cmd": cmd, "args": args})
            )
            else None
        )

    # ------------------------------------------------------------------ supervisor

    def _supervise(self) -> None:
        assert self._target is not None
        target = self._target
        backoff = BACKOFF_MIN
        first = True
        while not self._stop.is_set():
            self.listener.on_status("Connecting…" if first else "Reconnecting…", False)
            first = False
            started = time.monotonic()
            reason = ""
            try:
                self._session(target)
            except PairingError as e:
                log.info("pairing: %s", e)
                self.listener.on_status(f"Pairing: {e}", False)
                if hasattr(self.listener, "on_pairing"):
                    self.listener.on_pairing(e.kind)
                break  # wait for the user to enter the code
            except AdbError as e:
                reason = "waiting for the USB device" if "not found" in str(e) else str(e)
            except (OSError, EOFError, ProtocolError, _SessionEnded, ValueError) as e:
                reason = str(e) or type(e).__name__
            except Exception as e:
                log.exception("unexpected session failure")
                reason = str(e) or type(e).__name__
            finally:
                self._close_sockets()
            if self._stop.is_set():
                break
            log.info("session ended: %s", reason)
            if time.monotonic() - started > 5:
                backoff = BACKOFF_MIN  # we had a working session; retry fast
            self.listener.on_status(f"Reconnecting in {backoff:.1f} s… ({reason})", False)
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, BACKOFF_MAX)
        log.info("supervisor exiting")

    def _session(self, target: Target) -> None:
        if target.serial:
            if self.adb is None:
                raise _SessionEnded("adb not available")
            self.adb.forward(target.serial, target.video_port)
            self.adb.forward(target.serial, target.control_port)

        ctrl = socket.create_connection((target.host, target.control_port), timeout=3)
        ctrl.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self._lock:
            self._ctrl_sock = ctrl
        hello_req: dict[str, Any] = {"client": f"phonecam-pc/{__version__}", "version": 1}
        if target.pairing:
            hello_req["pairing"] = target.pairing
        ctrl.sendall(json_packet(PacketType.HELLO, hello_req).encode())
        try:
            hello = read_packet(ctrl)
        except (EOFError, ProtocolError) as e:
            # Over adb forward, a phone that isn't listening shows up as an immediate close.
            raise _SessionEnded("PhoneCam is not streaming on the phone") from e
        if hello.type != PacketType.HELLO:
            raise _SessionEnded(f"expected HELLO, got type {hello.type:#x}")
        hello_json = hello.json()
        if hello_json.get("error") in ("pairing_required", "pairing_failed"):
            raise PairingError(hello_json["error"])
        self.listener.on_hello(hello_json)

        video = socket.create_connection((target.host, target.video_port), timeout=3)
        video.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        video.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)
        video.settimeout(VIDEO_TIMEOUT)
        with self._lock:
            self._video_sock = video

        self.clock.reset()
        self._reset_counters()
        session_stop = threading.Event()
        decode_q: queue.Queue[tuple[int, bytes, int, bool] | None] = queue.Queue(maxsize=8)
        decoder = H264Decoder(self.decoder_mode)
        self.decoder_name = decoder.name
        workers = [
            threading.Thread(
                target=self._decode_loop, args=(decoder, decode_q), name="decode", daemon=True
            ),
            threading.Thread(
                target=self._control_loop, args=(ctrl, session_stop), name="control", daemon=True
            ),
        ]
        for w in workers:
            w.start()
        self.listener.on_status(f"Connected via {target.transport}", True)
        log.info("connected to %s:%d (%s)", target.host, target.video_port, target.transport)
        try:
            self._video_loop(video, decode_q, session_stop)
        finally:
            session_stop.set()
            with contextlib.suppress(queue.Full):
                decode_q.put_nowait(None)
            self._close_sockets()
            for w in workers:
                w.join(2)

    def _video_loop(
        self, video: socket.socket, decode_q: queue.Queue, session_stop: threading.Event
    ) -> None:
        while not self._stop.is_set() and not session_stop.is_set():
            try:
                p = read_packet(video)
            except TimeoutError as e:
                raise _SessionEnded("no video for 3 s") from e
            except EOFError as e:
                raise _SessionEnded("phone closed the video connection") from e
            self._packets += 1
            self._bytes += len(p.payload) + 20
            if p.type in (PacketType.CONFIG, PacketType.VIDEO):
                item = (p.type, p.payload, p.pts_us, p.flags, now_us())
                while True:
                    try:
                        decode_q.put(item, timeout=0.5)
                        break
                    except queue.Full:
                        if session_stop.is_set() or self._stop.is_set():
                            return
            # other types (AUDIO later, unknown) are skipped

    # ------------------------------------------------------------------ decode

    def _decode_loop(self, decoder: H264Decoder, decode_q: queue.Queue) -> None:
        while True:
            item = decode_q.get()
            if item is None:
                return
            try:
                self._decode_one(decoder, *item)
            except Exception:
                log.exception("decoder failure; resetting decoder")
                decoder.reset()

    def _decode_one(
        self,
        decoder: H264Decoder,
        ptype: int,
        payload: bytes,
        pts_us: int,
        flags: int,
        recv_us: int,
    ) -> None:
        if decoder.mode != self.decoder_mode:  # switched in the GUI: reopen, ask for an IDR
            decoder.set_mode(self.decoder_mode)
            self.send_request("request_keyframe")
        if ptype == PacketType.CONFIG:
            decoder.configure(payload)
            self.decoder_name = decoder.name
            return
        key = bool(flags & FLAG_KEYFRAME)
        rotation = ((flags >> 1) & 0x3) * 90
        t_start = now_us()
        frames = decoder.decode(payload, pts_us, key)
        self.decoder_name = decoder.name
        for frame in frames:
            t = now_us()
            self._dec_sum += t - t_start
            self._dec_n += 1
            df = DecodedFrame(frame, pts_us, t, rotation, recv_us)
            self.preview_slot.put(df)
            self.output_slot.put(df)
            self._decoded += 1
            self._size = (frame.width, frame.height)
            pc_capture = self.clock.phone_to_pc(pts_us)
            if pc_capture is not None:
                self._lat_sum += t - pc_capture
                self._recv_sum += recv_us - pc_capture
                self._lat_n += 1
            if (
                log.isEnabledFor(logging.DEBUG)
                and self._decoded % 30 == 0
                and pc_capture is not None
            ):
                log.debug(
                    "stages: capture->received %.1f ms, decode %.1f ms, capture->decoded %.1f ms",
                    (recv_us - pc_capture) / 1000,
                    (t - t_start) / 1000,
                    (t - pc_capture) / 1000,
                )

    # ------------------------------------------------------------------ control

    def _send_ctrl(self, packet: Any) -> bool:
        with self._lock:
            sock = self._ctrl_sock
        if sock is None:
            return False
        try:
            with self._ctrl_send_lock:
                sock.sendall(packet.encode())
            return True
        except OSError:
            return False

    def _control_loop(self, ctrl: socket.socket, session_stop: threading.Event) -> None:
        dec = PacketDecoder()
        next_ping = time.monotonic()
        last_pong = time.monotonic()
        last_report = time.monotonic()
        try:
            while not session_stop.is_set():
                now = time.monotonic()
                if now >= next_ping:
                    if now - last_pong > PONG_TIMEOUT:
                        raise _SessionEnded("no PONG for 3 s")
                    self._send_ctrl(ping_packet(now_us()))
                    next_ping = now + PING_INTERVAL
                if now - last_report >= 0.5:
                    self._report_stats(now - last_report)
                    last_report = now
                ctrl.settimeout(max(0.01, next_ping - time.monotonic()))
                try:
                    data = ctrl.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    raise _SessionEnded("phone closed the control connection")
                for p in dec.feed(data):
                    if p.type == PacketType.PONG:
                        t_send, t_phone = parse_pong(p)
                        self.clock.add(t_send, t_phone, now_us())
                        last_pong = time.monotonic()
                    elif p.type == PacketType.STATS:
                        self._phone_stats = p.json()
                    elif p.type in (PacketType.STATE, PacketType.CONTROL_RES):
                        body = p.json()
                        state = (
                            body.get("state", body) if p.type == PacketType.CONTROL_RES else body
                        )
                        if p.type == PacketType.CONTROL_RES and not body.get("ok", False):
                            log.warning("request %s failed: %s", body.get("id"), body.get("error"))
                        self.listener.on_state(state)
        except (OSError, ProtocolError, _SessionEnded, ValueError, json.JSONDecodeError) as e:
            if not session_stop.is_set():
                log.info("control loop ended: %s", e)
        finally:
            session_stop.set()
            # Unblock the video reader so the supervisor tears the session down.
            self._close_sockets()

    def _report_stats(self, dt: float) -> None:
        packets, nbytes, decoded = self._packets, self._bytes, self._decoded
        lat_sum, lat_n = self._lat_sum, self._lat_n
        recv_sum, dec_sum, dec_n = self._recv_sum, self._dec_sum, self._dec_n
        self._reset_counters()
        rtt = self.clock.last_rtt_us
        phone = self._phone_stats
        self.listener.on_stats(
            Stats(
                fps_in=packets / dt,
                fps_decoded=decoded / dt,
                bitrate_bps=nbytes * 8 / dt,
                latency_ms=(lat_sum / lat_n / 1000) if lat_n else None,
                received_ms=(recv_sum / lat_n / 1000) if lat_n else None,
                decode_ms=(dec_sum / dec_n / 1000) if dec_n else None,
                rtt_ms=None if rtt is None else rtt / 1000,
                dropped_phone=int(phone.get("dropped_total", 0) or 0),
                decoder=self.decoder_name,
                width=self._size[0],
                height=self._size[1],
                phone=phone,
            )
        )

    def _reset_counters(self) -> None:
        self._packets = self._bytes = self._decoded = 0
        self._lat_sum = self._lat_n = 0
        self._recv_sum = self._dec_sum = self._dec_n = 0

    def _close_sockets(self) -> None:
        with self._lock:
            socks = [self._video_sock, self._ctrl_sock]
            self._video_sock = self._ctrl_sock = None
        for s in socks:
            if s is not None:
                with contextlib.suppress(OSError):
                    s.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(OSError):
                    s.close()
