# PhoneCam

Use an Android phone as a low-latency webcam on Ubuntu. The phone captures and encodes H.264
with Camera2 + MediaCodec. The Ubuntu app receives the stream over USB (`adb forward`) or Wi-Fi,
decodes it, and exposes it as a real V4L2 webcam (`/dev/video10`, "PhoneCam") that Meet, Zoom,
OBS, Firefox and Chrome can use.

No third-party streaming libraries are used. The wire protocol is our own: see
[docs/protocol.md](docs/protocol.md).

> **Status:** Phase 1. The phone captures, previews and hardware-encodes H.264 to a file.
> Streaming to the PC arrives in phase 2, the virtual webcam in phase 3.

> **Security:** the stream is unencrypted and unauthenticated. Use it over USB or on a trusted
> LAN only.

## Layout

```
docs/            protocol spec, architecture, latency method + results, shared test vectors
android/         Android app (Kotlin, Compose, Camera2, MediaCodec), minSdk 24
pc/              Ubuntu app (Python, PySide6, PyAV)
scripts/         setup-v4l2loopback.sh, dev-run.sh, gen-test-vectors.py
```

## Requirements

- **PC:** Ubuntu 26.04 (tested), Python 3.12+, `adb`, and these packages:
  `sudo apt install python3-pyside6.qtwidgets python3-numpy adb`
  (`v4l2loopback-dkms` is needed from phase 3 on; `scripts/setup-v4l2loopback.sh` installs it).
- **Phone:** Android 7.0+ (API 24), USB debugging enabled.
- **Building the APK:** JDK 17+ and the Android SDK (compileSdk 37). Gradle comes from the wrapper.

## Build and run (development)

```bash
# PC app. The venv reuses Ubuntu's PySide6/numpy; Ubuntu's python lacks ensurepip, so the
# venv is created without pip and uses the system pip module.
cd pc
python3 -m venv --without-pip --system-site-packages .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/phonecam --debug
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .

# Android app
cd android
./gradlew assembleDebug testDebugUnitTest
adb install -r app/build/outputs/apk/debug/PhoneCam-0.1.0-debug.apk

# Or all at once
scripts/dev-run.sh
```

## Phase 1: record a test clip

1. Open PhoneCam on the phone and allow camera access.
2. Tap **Start recording**, wait a few seconds, then tap **Stop**. Streaming continues with the
   screen off or the app in the background; the notification has a Stop action.
3. Pull and play the clip:

```bash
adb shell ls /sdcard/Android/data/com.ares.phonecam/files/recordings/
adb pull /sdcard/Android/data/com.ares.phonecam/files/recordings/<file>.h264
ffplay -f h264 -framerate 30 <file>.h264
```

The clip is in sensor orientation (landscape). It's upright when the phone is held in landscape
with its top to the left.

Debug logging on the phone: `adb shell setprop log.tag.PhoneCam VERBOSE`, then
`adb logcat -s PhoneCam` shows per-second encoder stats.

## Troubleshooting

- **PC app aborts with `GLib-GIO-ERROR … does not contain a key named 'antialiasing'`.**
  This is Qt's GTK3 platform theme on current GNOME. PhoneCam defaults to
  `QT_QPA_PLATFORMTHEME=gnome` to avoid it. If you override that variable, don't set it to
  `gtk3` or `xdgdesktopportal`.
- **`adb devices` shows `unauthorized`.** Unlock the phone and accept the RSA prompt. If it
  doesn't appear, run `adb kill-server`, replug, and in Developer options use "Revoke USB debugging
  authorizations".

More troubleshooting (v4l2loopback/`exclusive_caps`, Secure Boot + DKMS, firewall/nftables)
comes with the phases that need it.
