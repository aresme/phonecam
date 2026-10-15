# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-15

First public release.

### Added

- **Video:** Camera2 → hardware H.264 (MediaCodec, Main profile without B-frames) streamed over
  USB (`adb forward`) or Wi-Fi, decoded with PyAV and written to a v4l2loopback device
  (`/dev/video10`, "PhoneCam") in I420 or YUYV, with letterboxing, rotation and mirroring.
- **Audio:** phone microphone → Opus → PipeWire virtual source "PhoneCam Mic", with a jitter
  buffer anchored on capture timestamps and automatic A/V sync.
- **Protocol:** a 20-byte framed binary protocol with a JSON control channel, PING/PONG clock
  sync, and shared Kotlin/Python test vectors.
- **Controls:** camera, resolution, fps, live bitrate, torch, tap-to-focus and AF modes, exposure,
  and orientation (landscape/portrait/auto), synchronized between phone and PC.
- **Wi-Fi:** mDNS discovery (`_phonecam._tcp`), a pairing code for non-USB clients, and a
  low-latency Wi-Fi lock.
- **Robustness:** reconnect with backoff, a placeholder frame while disconnected, a foreground
  service with wake lock, automatic resume when another app releases the camera, and a
  single-instance guard on the desktop.
- **Latency lab:** per-stage statistics, a millisecond timer with snapshots, an automatic flash
  test, and an A/V sync test.
- **Desktop integration:** tray icon, app-grid entry, persistent settings, a first-run
  v4l2loopback setup dialog, optional VAAPI decoding, and a CPU performance hold while streaming.

[0.1.0]: https://github.com/aresme/phonecam/releases/tag/v0.1.0
