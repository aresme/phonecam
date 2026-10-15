# Architecture

```
 ┌──────────────────────── Android phone ────────────────────────┐        ┌──────────────────────────── Ubuntu PC ─────────────────────────────┐
 │                                                               │        │                                                                    │
 │  Camera2 ──► encoder input Surface ──► MediaCodec (H.264, HW) │        │  net thread ──► decode thread ──► [latest-frame slot] ──► output  │
 │     │                                       │ async callback  │  USB   │  (video sock)   (PyAV, VAAPI→SW)       size 1, overwrite   thread  │
 │     └──► preview Surface (optional)         ▼                 │ (adb   │                       │                                    │      │
 │                                   bounded queue (2 AUs)       │ fwd)   │                       └──► preview signal (≤30 fps) ──► GUI   ▼      │
 │                                   drop non-key → req. IDR     │  or    │                                                   /dev/video10     │
 │                                             │                 │ Wi-Fi  │                                                  (v4l2loopback)    │
 │                              StreamServer :47100 video ───────┼───────►│                                                                    │
 │                              StreamServer :47101 control ◄────┼───────►│  control thread: HELLO, CONTROL_REQ/RES, STATE, STATS, PING/PONG    │
 │                                                               │        │                                                                    │
 │  StreamingService (foreground, camera type) owns all of it;   │        │  GUI thread (Qt) only renders and sends commands; never blocks.    │
 │  MainActivity (Compose) binds to it for preview + controls.   │        │                                                                    │
 └───────────────────────────────────────────────────────────────┘        └────────────────────────────────────────────────────────────────────┘
```

The wire format is specified in [protocol.md](protocol.md).

## Android

Package `com.ares.phonecam`. Kotlin, Compose + Material 3, Camera2, MediaCodec, coroutines.

| Component            | Responsibility |
|----------------------|----------------|
| `StreamingService`   | Foreground service (`foregroundServiceType="camera"`). Owns the camera, encoder and server so streaming survives screen-off and backgrounding. Holds a partial wake lock while streaming. Notification has a Stop action. |
| `CameraController`   | Opens the camera and builds one capture session with targets {encoder Surface, optional preview Surface}. Handles camera switch, torch, AF/AE regions, exposure compensation, and fixed FPS ranges (`[30,30]` preferred). |
| `VideoEncoder`       | MediaCodec H.264 with Surface input. Hardware encoder (`c2.mtk.avc.encoder` on the test phone), baseline/constrained baseline, no B-frames, 1 s IDR interval, CBR, `KEY_PRIORITY=0`, `KEY_LATENCY=1` on API 30+. Async callbacks. Emits CONFIG (SPS/PPS) and access units. Handles sync-frame requests and live bitrate changes. |
| `StreamServer`       | Two `ServerSocket`s (47100, 47101). One client per port; a new connection replaces the old one. `TCP_NODELAY`. The video sender drains a bounded queue of 2. |
| `SettingsRepository` | DataStore: last camera, resolution, fps, bitrate, transport, orientation. |
| `protocol/`          | `Packet`, `PacketReader`, `PacketWriter`, tested against `docs/test-vectors/`. |

### Threads

| Thread / dispatcher         | Work |
|-----------------------------|------|
| Main                        | Compose UI only. |
| `camera` HandlerThread      | Camera2 device and session callbacks. |
| `encoder` HandlerThread     | MediaCodec async callbacks. Copies each output buffer into a pooled `ByteArray` and offers it to the video queue without blocking. |
| `video-send` thread         | Blocking `take()` from the queue, then `PacketWriter.write` on the video socket. A slow socket only blocks this thread. |
| `server` coroutine scope (`Dispatchers.IO`) | Accept loops, control socket reader, PONG replies, 1 Hz STATS, STATE broadcasts. |

Structured concurrency: the service owns a `CoroutineScope(SupervisorJob() + Dispatchers.IO)`
that is cancelled in `onDestroy`. Camera, codec and sockets are closed in `finally` blocks.

### Drop policy

`encoder → queue(2) → socket`. When the queue is full, a non-key frame is dropped and a sync
frame is requested. A keyframe clears the queue and is enqueued. Under congestion the PC sees a
short freeze followed by a clean IDR, never growing delay or smeared P-frames.

## PC

Python 3.14 (Ubuntu 26.04), PySide6 (system package), PyAV, numpy. Output uses direct V4L2 writes (`ioctl(VIDIOC_S_FMT)` + `write()`), without pyvirtualcam.

| Thread    | Work | Hand-off |
|-----------|------|----------|
| `supervisor` | Runs `adb forward` and connects (control first, HELLO, then video). For the life of the session it does blocking reads on the video socket (3 s timeout). On any error it tears down and reconnects with backoff. | Bounded queue (8) → `decode`. A full queue applies backpressure, so the phone's drop queue takes over |
| `decode`  | PyAV `CodecContext` H.264. Waits for a keyframe after CONFIG or an error. Software decode for now; VAAPI comes in phase 5 (PyAV's bundled FFmpeg has no VAAPI). `flags=LOW_DELAY`, `thread_type=SLICE` (no frame threading, which adds a frame of delay per thread). | Writes into the **latest-frame slot** |
| `v4l2-output` | Waits on its slot (100 ms timeout). Converts with swscale to the fixed output size and format (I420 by default, YUYV optional; letterboxed if the aspect differs), then `write()`s to `/dev/video10` immediately. For gaps under 1.5 s it repeats the last frame; after that it writes a "PhoneCam disconnected" card at 10 fps, so the device stays open and listed (`exclusive_caps=1`). Measures capture→written latency. | — |
| `control` | Control socket: CONTROL_REQ/RES, STATE, STATS, PING every 500 ms, clock offset (min-RTT over a 20-sample window). No PONG for 3 s ends the session. Reports stats. | Listener → Qt signals → GUI |
| `preview` | Reads its own latest-frame slot, at most 30 fps: swscale to ≤960 px wide RGB, `QImage`, then a queued signal to the GUI. | — |
| `adb-monitor` | Polls `adb devices -l` every 1.5 s and updates the device list (unplug/replug). | Qt signal |
| GUI (main)| Qt widgets only. Never blocks: connect, disconnect and adb calls run on short-lived background threads. Stats arrive at 2 Hz. | — |

### Pixel format

Both I420 (YU12) and YUYV were tested in Chrome 154, Firefox 157 (snap) and OBS 32. All three
list "PhoneCam" and receive 1280×720 at 30 fps in either format. I420 is the default because
H.264 decodes to 4:2:0: packing takes about 2 ms per 720p frame, against about 6.5 ms for YUYV
(which needs chroma upsampling). YUYV stays available as a compatibility fallback.

### Latest-frame slot

A one-element mailbox protected by a `threading.Condition`: `put()` overwrites, and `get()`
blocks until a frame newer than the last one returned is available. Decode never waits on output
and output never consumes stale frames, so no backlog can form between them. This has unit tests.

### adb

`adb` runs only through `subprocess` from worker threads, never on the GUI thread. A device
monitor polls `adb devices -l` to detect unplug/replug. Each (re)connect re-runs
`adb -s SERIAL forward tcp:47100 tcp:47100` (and the same for 47101).

### Reconnect

On any socket error: close both sockets, set the status to "Reconnecting…", back off 0.5 s → 1 → 2
→ 4 → 5 s (max), re-run `adb forward` (USB), reconnect control then video, send HELLO, request a
keyframe. The output thread keeps writing frames the whole time, so video-call apps keep the
device open.
