# Latency

Targets: **< 150 ms** glass-to-glass over USB, **< 250 ms** over Wi-Fi.

## Summary (Infinix X6886 → HP EliteBook 840 G5, USB, 1280×720 @ 30 fps, 6 Mbit/s)

| Measurement | Result |
|---|---|
| Capture → written to `/dev/video10` (computed from timestamps) | **107–114 ms** |
| Screen flip → frame read from `/dev/video10` (automatic flash test, n=64) | **median 170–175 ms**, best 129–134 ms |
| Same over **Wi-Fi** (phone hotspot, n=29) | **median 187 ms**, best 137 ms (target < 250 ms ✓) |
| Timer snapshots (timer seen through the camera vs. timer at capture) | 165–195 ms |

Most of the delay happens **inside the phone's camera HAL**: 85–95 ms from the start of
exposure until the frame leaves the camera pipeline. The whole PC side (USB transfer, decode,
conversion, v4l2 write) adds 15–20 ms. The flash and timer numbers also include this laptop's own
display latency (GNOME compositor + panel, about 30–40 ms) and up to one frame (33 ms) of
sampling granularity.

**Verdict:** the 150 ms glass-to-glass target is not reachable on this phone at 30 fps. The camera
HAL alone uses ~90 ms of the budget, and the fixed 30 fps cap makes its 4-stage pipeline cost
33 ms per stage. Everything we control (encoder, transport, decoder, output) now adds about
20 ms on top of the sensor pipeline. See [Device limit](#device-limit) for what would help.

## Per-stage breakdown (medians)

| Stage | ms | How it is measured |
|---|---|---|
| Sensor exposure | 30 | `SENSOR_EXPOSURE_TIME` (AE at a fixed 30 fps range in indoor light) |
| Phone: sensor timestamp → capture result | 85–95 | `onCaptureCompleted` time − `SENSOR_TIMESTAMP` (same clock: REALTIME source); `REQUEST_PIPELINE_DEPTH` = 4 |
| Phone: sensor timestamp → encoded frame | 94–100 | encoder output time − frame pts |
| PC: → packet received over USB (adb forward) | +3–4 | PC receive time − (pts − PING/PONG clock offset) |
| PC: H.264 decode (software) | 5–7 | avcodec time per frame, with the CPU boost |
| PC: scale + I420 pack + `write()` to v4l2 | 2–3 | output thread |
| **PC: capture → written to v4l2** | **107–114** | |
| RTT (PING/PONG over adb forward) | 3–6 | |

These live in the app: hover the PC status bar for the breakdown, `adb shell setprop
log.tag.PhoneCam VERBOSE` for per-second phone stats, `phonecam --debug` for PC stage logs.

## How latency is measured

1. **Computed (always on).** Each `VIDEO` packet carries the sensor timestamp (`pts_us`, phone
   CLOCK_BOOTTIME; encoder-surface timestamps arrive in CLOCK_MONOTONIC and are mapped, see
   protocol.md §4). The PC converts it to its own clock with the lowest-RTT PING/PONG offset and
   subtracts it from the time the frame was written to `/dev/video10`. Because the reference is
   the *start* of exposure, this includes the exposure itself but not the consumer app's own
   pipeline or display.
2. **Latency test window** (PC app → *Latency test…*). Point the phone at the screen.
   - **Automatic flash test (F):** the window alternates black/white once per second. A reader
     opens `/dev/video10` the way a video-call app would and timestamps the first frame whose
     mean brightness crosses the midpoint after each flip. The result is
     *screen flip → frame available to a consumer*. It includes the source display's latency and
     up to one frame of sampling granularity; it does not include the consumer's display.
   - **Timer snapshots (Space):** a large millisecond timer next to the picture read back from
     `/dev/video10`. The snapshot file name contains the timer at capture; subtract the timer
     value visible inside the camera picture.

## Tuning experiments (2026-10-15)

Phone settings A/B-tested at runtime with the experimental `set_tuning` control command
(1280×720 @ 30 fps; values are medians over 4 s of steady state):

| Config | sensor→result | sensor→encoded | encode | stream |
|---|---|---|---|---|
| A: original (TEMPLATE_RECORD, HAL defaults, Baseline) | 85.8 | 99.5 | 13.7 | Baseline |
| B: TEMPLATE_PREVIEW, NR/edge FAST, EIS off | 93.7 | 108.1 | 14.4 | Baseline |
| C: B + NR/edge OFF | 92.4 | 106.9 | 14.4 | Baseline |
| D: B + `KEY_OPERATING_RATE` = max | 96.2 | 106.5 | **10.3** | Baseline |
| E: B + MTK `vilte` mode | 86.7 | 100.3 | 13.6 | Constrained Baseline |
| F: B + Main profile | 91.8 | 107.0 | 15.1 | Main (no B-frames) |
| G: B + High profile | 95.2 | 110.2 | 15.0 | High |

Other findings:

- The camera's sensor → result time (85–96 ms, pipeline depth 4) did not change with template,
  noise reduction, edge, stabilization, preview on/off (1 vs 2 output streams) or resolution
  (720p was the fastest; 540p and 1080p were slightly slower). It is fixed by the HAL.
- PC decode cost depends on profile and bitrate. On real 3.5 s phone recordings, software decode
  measured 4.3–6.0 ms per frame for Baseline/CBP, **3.6 ms for Main** and 8.5 ms for High
  (CPU at full clock). Plain Baseline cannot be decoded by VAAPI at all.
- **VAAPI vs software** (Intel UHD 620, iHD driver): VAAPI takes about 9–12 ms per frame
  including the GPU→CPU download, against about 5–7 ms for software at full clock. Software is
  the default (lower latency); VAAPI is optional (less CPU).
- **CPU frequency matters most on the PC.** On battery in the *balanced* power profile, the CPU
  idles between frames and software decode took 12.8–14 ms. Holding the *performance* profile
  (power-profiles-daemon `HoldProfile`, the "Boost CPU while streaming" option) brings it to
  5.6–7 ms. That's about 7 ms off end-to-end latency.
- Bugs found by the latency test: a 1 ms repaint timer and per-paint text rendering starved the
  v4l2 reader thread (GIL), and two threads converting the same `av.VideoFrame` shared one swscale
  context, which crashed inside libswscale with system FFmpeg. Both are fixed: glyph caching, and
  one `VideoReformatter` per thread.

**Chosen defaults:** TEMPLATE_PREVIEW, NR/edge FAST, EIS off (neutral on this phone, but they
rule out HAL extras), **Main profile without B-frames** (smallest stream, fastest decode,
VAAPI-compatible), **`KEY_OPERATING_RATE` = max** (−3 to 4 ms encode), `KEY_PRIORITY` 0,
`KEY_LATENCY` 1. PC: software decode, CPU boost on, I420 output.

## Wi-Fi

Measured with the phone's own hotspot. The office network ("KABU") isolates clients, so phone ↔
PC traffic is blocked there, although mDNS discovery still works. Over Wi-Fi the PC received
frames ~82 ms after capture (USB: ~98 ms in that session; the phone pipeline happened to be
faster in that run, 77 ms). The flash test median was 187 ms, about 12 ms above USB. RTT is 2–4 ms
with occasional spikes to ~100 ms. The phone's drop-to-keyframe queue keeps those from
accumulating. The phone holds a low-latency Wi-Fi lock while streaming.

## Device limit

The remaining ~90 ms is the camera HAL: about 30 ms of exposure, then readout and MediaTek ISP
stages at 33 ms per frame (pipeline depth 4). Options, none of which PhoneCam can force on this
phone:

- **Higher frame rate.** At 60 fps every pipeline stage would cost 16.7 ms instead of 33 ms. The
  MT6789 HAL only exposes AE ranges up to 30 fps and no constrained high-speed sessions for
  regular capture.
- **Shorter exposure** (bright light, or a manual exposure cap). That saves up to about 20 ms but
  gives a noisier or darker image. A possible future "low-latency exposure" option.
- **A phone with a faster camera pipeline.** Pixel/Samsung flagships typically show 50–70 ms
  sensor → buffer at 30 fps, and less at 60 fps.

## Results log

| Date | Phone | Transport | Resolution | FPS | Bitrate | Profile | Decoder | Capture→v4l2 (ms) | Flash test median (ms) | Notes |
|------|-------|-----------|------------|-----|---------|---------|---------|-------------------|------------------------|-------|
| 2026-10-15 | Infinix X6886 (MT6789) | USB | 1280×720 | 30 | 6 Mbit/s | Baseline | software | 108 | — | phase 3 baseline, before tuning |
| 2026-10-15 | Infinix X6886 (MT6789) | USB | 1280×720 | 30 | 6 Mbit/s | Main | software, balanced power | 114.5 | — | tuned phone, no CPU boost (decode 14 ms) |
| 2026-10-15 | Infinix X6886 (MT6789) | USB | 1280×720 | 30 | 6 Mbit/s | Main | VAAPI | 122 | — | decode 8.9 ms + GPU download |
| 2026-10-15 | Infinix X6886 (MT6789) | USB | 1280×720 | 30 | 6 Mbit/s | Main | software + CPU boost | **107.5–114** | **170 / 175** (best 129) | final phase-5 defaults; timer snapshots 165–195 ms |
| 2026-10-15 | Infinix X6886 (MT6789) | Wi-Fi (phone hotspot, RSSI ~−40) | 1280×720 | 30 | 6 Mbit/s | Main | software, no boost | 96–98 capture→decoded (max 170); RTT 4 ms (max 103) | **187** (best 137) | phase 6; same pipeline + ~12 ms vs USB |
