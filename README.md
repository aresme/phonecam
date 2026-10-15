# PhoneCam

Use an Android phone as a low-latency webcam on Ubuntu. The phone captures and encodes H.264
with Camera2 + MediaCodec. The Ubuntu app receives the stream over USB (`adb forward`) or Wi-Fi,
decodes it, and exposes it as a real V4L2 webcam (`/dev/video10`, "PhoneCam") that Meet, Zoom,
OBS, Firefox and Chrome can use.

No third-party streaming libraries are used. The wire protocol is our own: see
[docs/protocol.md](docs/protocol.md).

> **Status:** Phase 4. PhoneCam webcam (`/dev/video10`) with full remote control from the PC
> (camera, resolution, fps, bitrate, torch, focus, exposure, orientation), kept in sync with the
> phone's own controls. Tray icon and app-grid entry.

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

## One-time setup: the virtual camera

PhoneCam writes into a v4l2loopback device. Either let the PC app's first-run dialog set it up
(it asks for your password via the normal GNOME prompt), or run:

```bash
scripts/setup-v4l2loopback.sh --persist   # installs v4l2loopback-dkms if needed, loads it, loads at boot
```

This creates `/dev/video10` with the label `PhoneCam` and `exclusive_caps=1` (required by Chrome).

## Install in the app grid

```bash
pc/.venv/bin/phonecam --install-desktop    # ~/.local/share/applications + icon
pc/.venv/bin/phonecam --uninstall-desktop
```

## Daily use (USB)

1. Plug in the phone (USB debugging on) and start the PC app: `pc/.venv/bin/phonecam`.
2. Press **Launch app on phone** (or open PhoneCam on the phone and tap **Start**).
3. Pick the device and press **Connect**. If the cable is unplugged, the phone app restarts, or
   the phone is not streaming yet, the PC keeps retrying and reconnects on its own.
4. In Meet/Zoom/OBS/the browser, pick the camera **PhoneCam**. The virtual camera is on by
   default and shows a "PhoneCam disconnected" card while the phone isn't connected, so apps
   keep the device. Output size and format (I420 default, YUYV fallback) are in the right panel.

### Controls

Everything can be changed from either side, and both UIs stay in sync:

- **PC (right panel):** camera, resolution, fps, bitrate, torch, focus mode, exposure,
  orientation, and *Mirror output*. Click the preview to focus at that point; the focus mode
  becomes *Locked*. Choose *Continuous* to return to autofocus.
- **Phone:** switch camera, torch, AF auto/locked, and the EV slider under the preview. Tap the
  preview to focus. The ⚙ settings sheet has resolution, fps, bitrate, orientation, stats overlay,
  keep screen on, and record to file.
- **Orientation:** *Landscape* assumes the phone is held with its top to the left. *Portrait*
  streams upright for a phone standing upright, *Auto* follows the phone. For a portrait
  stream, pick a portrait output size (720×1280) in the virtual camera panel; otherwise it is
  pillarboxed.
- Settings are remembered on both sides (phone: DataStore, PC: `~/.config/PhoneCam/PhoneCam.conf`).
  *Connect automatically on launch* reconnects to the last device.

### Tray icon

With a tray (Ubuntu's AppIndicator extension, enabled by default), closing the window keeps
PhoneCam running in the tray. The tray menu has Show window, Connect/Disconnect, Switch camera,
Virtual camera and Quit. Without a tray (plain GNOME without the extension), closing the window
quits, and everything else works the same.

Starting streaming at phone boot is intentionally not offered: Android 14+ does not allow
camera foreground services to start from a boot receiver.

Developing without a phone: `python -m phonecam.tools.fake_phone some.h264` serves a recording
on 127.0.0.1. Connect to it with Wi-Fi mode and IP `127.0.0.1`.

## Record a test clip on the phone

1. Open PhoneCam on the phone and allow camera access.
2. Turn on **Also record to file**, tap **Start**, wait a few seconds, then tap **Stop**. Streaming continues with the
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

- **PhoneCam doesn't show up in Chrome/Meet.** The module must be loaded with
  `exclusive_caps=1`. Check with `cat /sys/module/v4l2loopback/parameters/exclusive_caps`. With
  that option, the device is only offered as a camera while the PhoneCam app is writing to it, so
  keep the PC app running with **Virtual camera: ON**. If an app still doesn't list it, try
  format **YUYV**.
- **`modprobe: ERROR: could not insert 'v4l2loopback': Key was rejected by service`.** Secure
  Boot is on and the DKMS-built module isn't signed with an enrolled key. Run
  `sudo mokutil --import /var/lib/shim-signed/mok/MOK.der`, reboot, choose *Enroll MOK* in the
  blue MOK manager and enter the one-time password. Alternatively, disable Secure Boot.
- **"Device busy" when switching the virtual camera on.** Another program (e.g. OBS Virtual
  Camera) is writing to the loopback device, or a reader is holding a different format. Close it
  and try again.
- **`/dev/video10` permission denied.** Your desktop session normally gets access automatically;
  otherwise run `sudo usermod -aG video $USER` and log in again.
- **OBS (or another Qt app) crashes on start with the `antialiasing` GLib error.** This is the
  same GNOME/Qt bug: start it with `QT_QPA_PLATFORMTHEME=gnome obs`, or add
  `QT_QPA_PLATFORMTHEME=gnome` to `~/.config/environment.d/qt.conf` and log in again.

More troubleshooting (Wi-Fi, firewall/nftables) comes with the phases that need it.
