# Latency

Targets: **< 150 ms** glass-to-glass over USB, **< 250 ms** over Wi-Fi.

## What is measured

1. **Computed (always on):** capture → written to v4l2. Each `VIDEO` packet carries the sensor
   timestamp (`pts_us`, phone CLOCK_BOOTTIME). The PC converts it to its own clock using the
   min-RTT PING/PONG offset estimate (see [protocol.md §3](protocol.md#0x20-ping--0x21-pong))
   and subtracts it from the time the frame was written to `/dev/video10`. This excludes sensor
   exposure/readout and the consumer app's own pipeline and display.
2. **Glass-to-glass (latency test mode):** the PC app shows a full-screen millisecond timer. Point
   the phone at it and open `/dev/video10` in a viewer (e.g. `ffplay -fflags nobuffer
   /dev/video10`, or Meet's preview) next to the timer. Take a photo of the screen with another
   camera, or a screenshot, and subtract the two readings. Average at least 5 readings. This
   includes the monitor refresh (~8–16 ms at 60 Hz) and the viewer's own buffering.

Per-stage timings (encode, network, decode, output) are logged at debug level (`phonecam
--debug`, logcat tag `PhoneCam`).

## Results

| Date | Phone | Transport | Resolution | FPS | Bitrate | Decoder | Computed (ms) | Glass-to-glass (ms) | Notes |
|------|-------|-----------|------------|-----|---------|---------|---------------|---------------------|-------|
| —    | Infinix X6886 (MT6789) | USB | 1280×720 | 30 | 6 Mbit/s | — | — | — | measured in phase 5 |
