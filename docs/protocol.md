# PhoneCam wire protocol — version 1

This document is the source of truth. The Android (`android/app/src/main/java/com/ares/phonecam/protocol/`)
and PC (`pc/phonecam/protocol.py`) implementations are both written from it, and both are tested
against the shared vectors in [`test-vectors/`](test-vectors/).

## 1. Transport

The phone is the TCP **server** on two ports. The PC is the client.

| Port  | Name    | Direction     | Content                          |
|-------|---------|---------------|----------------------------------|
| 47100 | video   | phone → PC    | `CONFIG`, `VIDEO` (later `AUDIO`) |
| 47101 | control | bidirectional | JSON control packets, `PING`/`PONG`, `STATS` |

- **USB:** the PC runs `adb forward tcp:47100 tcp:47100` and `adb forward tcp:47101 tcp:47101`,
  then connects to `127.0.0.1`.
- **Wi-Fi:** the PC connects directly to the phone's IP on the same ports.
- Both ends set `TCP_NODELAY`.
- **One client at a time.** When a new connection is accepted on a port, the phone closes the
  previous connection on that port. The PC opens control first, then video.
- There is no encryption or authentication. Use it on USB or a trusted LAN only.

## 2. Packet framing

Every packet is a 20-byte header followed by `length` payload bytes. All integers are big-endian.

```
offset size field
0      4    magic      = "PCAM" (0x50 0x43 0x41 0x4D)
4      1    version    = 1
5      1    type
6      1    flags
7      1    reserved   = 0
8      8    pts_us     uint64, phone clock (see §4), microseconds
16     4    length     uint32, payload length in bytes
20     n    payload
```

### Receiver rules

1. If `magic` ≠ `PCAM` → protocol error, close the connection.
2. If `version` ≠ 1 → protocol error, close the connection.
3. If `length` > 4 194 304 (4 MiB) → protocol error, close the connection. Check this before
   allocating the payload buffer.
4. `reserved` is ignored on receive and must be sent as 0.
5. Packets with an **unknown `type`** are read in full and skipped. This keeps new packet types
   backward compatible.
6. `pts_us` is sent as uint64, but senders keep it below 2⁶³ so it fits a signed 64-bit integer
   (Kotlin `Long`). A received value ≥ 2⁶³ is a protocol error. If the field doesn't apply, it
   is sent as 0.
7. A connection closed in the middle of a packet is a truncated stream. Discard the partial packet.

## 3. Packet types

| Type | Name          | Channel | Direction  | Payload |
|------|---------------|---------|------------|---------|
| 0x01 | `HELLO`       | control | both       | JSON |
| 0x02 | `CONFIG`      | video   | phone → PC | H.264 SPS + PPS, Annex-B |
| 0x03 | `VIDEO`       | video   | phone → PC | one H.264 access unit, Annex-B |
| 0x10 | `CONTROL_REQ` | control | PC → phone | JSON |
| 0x11 | `CONTROL_RES` | control | phone → PC | JSON |
| 0x12 | `STATE`       | control | phone → PC | JSON |
| 0x20 | `PING`        | control | PC → phone | 8 bytes |
| 0x21 | `PONG`        | control | phone → PC | 16 bytes |
| 0x30 | `STATS`       | control | phone → PC | JSON |
| 0x40 | `AUDIO`       | video   | phone → PC | reserved (phase 7) |

JSON payloads are UTF-8 objects without a BOM. Receivers ignore unknown keys.

### Flags

| Bit | Name       | Valid on | Meaning |
|-----|------------|----------|---------|
| 0   | `KEYFRAME` | `VIDEO`  | Access unit contains an IDR slice |
| 1–7 | —          | —        | Reserved. Send as 0, ignore on receive |

### 0x01 HELLO

The PC sends it first on the control connection:

```json
{"client": "phonecam-pc/0.1.0", "version": 1}
```

The phone replies with its capabilities and current settings:

```json
{
  "server": "phonecam-android/0.1.0",
  "version": 1,
  "device": {"model": "Infinix X6886", "android": "16", "sdk": 36},
  "cameras": [
    {
      "id": "0", "facing": "back",
      "sizes": [[1920, 1080], [1280, 720], [640, 480]],
      "fps_ranges": [[15, 15], [30, 30], [15, 30]],
      "has_flash": true,
      "af_modes": ["off", "auto", "macro", "continuous-video", "continuous-picture"],
      "exposure_comp": {"min": -20, "max": 20, "step": 0.1}
    }
  ],
  "encoders": [{"name": "c2.mtk.avc.encoder", "hardware": true}],
  "state": { "...": "see STATE" }
}
```

`sizes` lists only sizes that **both** the camera (PRIVATE/Surface output) and the chosen
encoder support. Sizes are given in sensor (landscape) orientation.

### 0x02 CONFIG

The payload is the H.264 SPS and PPS NAL units, each with a `00 00 00 01` start code. `pts_us` is
0. The phone sends `CONFIG`:

- as the first packet on every new video connection, and
- whenever the encoder restarts, for example after a resolution or camera change.

The PC must (re)initialize its decoder when it receives a `CONFIG` that differs from the last one.

### 0x03 VIDEO

The payload is one complete H.264 access unit (Annex-B, start codes included). It never carries
SPS/PPS, which travel only in `CONFIG`. Set `flags` bit 0 on IDR frames. `pts_us` is the capture
time of the frame (§4).

After sending `CONFIG` on a new video connection, the phone requests a sync frame from the
encoder. The PC drops `VIDEO` packets until the first keyframe after a `CONFIG`.

### 0x10 CONTROL_REQ

```json
{"id": 7, "cmd": "set_resolution", "args": {"width": 1280, "height": 720}}
```

`id` is a PC-chosen integer that the matching `CONTROL_RES` echoes back.

| `cmd`               | `args` |
|---------------------|--------|
| `set_camera`        | `{"id": "1"}` or `{"facing": "front"\|"back"}` |
| `set_resolution`    | `{"width": int, "height": int}` |
| `set_fps`           | `{"fps": int}` |
| `set_bitrate`       | `{"bps": int}` (applied live, no encoder restart) |
| `set_torch`         | `{"on": bool}` |
| `focus_at`          | `{"x": float, "y": float}`, normalized 0..1 in the frame **as streamed** |
| `set_af`            | `{"mode": "auto"\|"continuous-video"\|"continuous-picture"\|"macro"\|"off"\|"locked"}` |
| `set_exposure_comp` | `{"ev": float}` (EV; the phone rounds to its step) |
| `set_orientation`   | `{"orientation": "landscape"\|"portrait"}` |
| `request_keyframe`  | `{}` |
| `get_status`        | `{}` |

### 0x11 CONTROL_RES

```json
{"id": 7, "ok": true, "state": { "...": "see STATE" }}
{"id": 8, "ok": false, "error": "unsupported size 9999x9999", "state": { "...": "..." }}
```

### 0x12 STATE

The phone sends `STATE` without being asked whenever any setting changes, including changes made on
the phone's own UI. The PC treats it as authoritative and updates its controls to match.

```json
{
  "streaming": true,
  "camera_id": "0", "facing": "back",
  "width": 1280, "height": 720, "fps": 30, "bitrate": 6000000,
  "orientation": "landscape",
  "torch": false, "af_mode": "continuous-video", "exposure_comp": 0.0,
  "encoder": "c2.mtk.avc.encoder"
}
```

### 0x20 PING / 0x21 PONG

- `PING` payload: 8 bytes, `t_pc_send_us` (uint64, PC monotonic clock). The header `pts_us` is 0.
- `PONG` payload: 16 bytes, `t_pc_send_us` echoed back, then `t_phone_us` (uint64, phone clock
  (§4) sampled when the phone handles the PING). The header `pts_us` is `t_phone_us`.

The phone answers each PING immediately, on the control connection.

The PC records `t_pc_recv_us` when the PONG arrives and computes:

```
rtt    = t_pc_recv_us - t_pc_send_us
offset = t_phone_us - (t_pc_send_us + t_pc_recv_us) / 2      # phone_clock - pc_clock
```

Over a sliding window (the last 20 samples), the PC uses the offset from the sample with the
**lowest RTT**. That sample has the least queuing asymmetry. The end-to-end latency of a frame is
then:

```
latency_us = t_pc_written_us - (pts_us - offset)
```

where `t_pc_written_us` is the PC monotonic time at which the frame was written to v4l2.

### 0x30 STATS

The phone sends `STATS` at 1 Hz while streaming:

```json
{
  "fps": 30.0, "bitrate": 5980000, "dropped": 0, "dropped_total": 3,
  "encode_ms": 9.4, "queue": 0,
  "battery_pct": 81, "battery_temp_c": 33.5, "thermal_status": 0,
  "client": "127.0.0.1"
}
```

### 0x40 AUDIO

Reserved for phase 7. Receivers that don't implement it skip it like any unknown type.

## 4. Clock

`pts_us` and all phone timestamps use Android `SystemClock.elapsedRealtimeNanos() / 1000`
(CLOCK_BOOTTIME). It is monotonic and keeps counting through deep sleep. On cameras with
`SENSOR_INFO_TIMESTAMP_SOURCE == REALTIME` (the case on the Infinix X6886), `SENSOR_TIMESTAMP`
and the encoder's Surface timestamps are already on this clock. On `UNKNOWN` sources, the phone
measures the offset between sensor timestamps and `elapsedRealtimeNanos` when the session starts
and corrects for it.

The PC uses `time.monotonic_ns() // 1000`.

## 5. Connection lifecycle

1. The PC connects to control (47101) and sends `HELLO`. The phone replies `HELLO`, then `STATE`.
2. The PC connects to video (47100). The phone sends `CONFIG` and forces an IDR frame.
3. The phone streams `VIDEO`. The PC sends `PING` every 500 ms and receives `STATS` at 1 Hz.
4. If either socket errors, the PC closes both and reconnects with backoff (0.5 s, doubling, max
   5 s), then repeats from step 1.

## 6. Flow control (phone side)

Outgoing video goes through a bounded queue of 2 access units. If the queue is full when a new
frame arrives:

- non-key frames are dropped, and the phone requests a sync frame from the encoder, so the
  decoder recovers quickly instead of showing corrupted references;
- a keyframe clears the queue and is enqueued.

Latency therefore stays bounded instead of growing with a TCP backlog.

## 7. Test vectors

[`test-vectors/manifest.json`](test-vectors/manifest.json) lists each vector file (`*.hex`:
whitespace-separated hex, `#` starts a comment) and the expected result: either the decoded
packets (`type`, `flags`, `pts_us` as a decimal string, payload as hex) or an `error` kind
(`bad_magic`, `bad_version`, `too_large`, `bad_pts`, `truncated`). Both implementations have tests that
decode every vector and re-encode every valid one byte-for-byte.
