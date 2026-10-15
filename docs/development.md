# Development

## Repository map

| Path | What |
|---|---|
| `android/app/src/main/java/com/ares/phonecam/` | `engine/` (reconciler, settings, tuning), `camera/`, `encoder/`, `audio/`, `net/` (server, queues, discovery), `protocol/`, `service/`, `ui/` |
| `pc/phonecam/` | `client.py` (sessions, reconnect, decode), `decoder.py`, `v4l2.py`, `output.py`, `audio.py`, `controls.py`, `app.py` (GUI), `latency_test.py`, `discovery.py`, `tools/fake_phone.py` |
| `docs/test-vectors/` | Byte-level protocol vectors shared by the Kotlin and Python tests |
| `scripts/` | `setup-v4l2loopback.sh`, `build-pyav-vaapi.sh`, `dev-run.sh`, `gen-test-vectors.py` |

## Desktop app

```bash
cd pc
python3 -m venv --without-pip --system-site-packages .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest          # ~150 tests, incl. end-to-end against a simulated phone
.venv/bin/python -m ruff check .    # lint (ruff format for formatting)
.venv/bin/phonecam --debug
```

Tests that need real hardware or services (a writable `/dev/video10`, PipeWire, VAAPI) skip
automatically when those aren't available.

### Optional: VAAPI decoding

The PyPI PyAV wheel ships an FFmpeg without VAAPI. To enable the *VAAPI hardware* decoder option,
rebuild PyAV against the system FFmpeg:

```bash
scripts/build-pyav-vaapi.sh    # PyAV 18.0.0 (newest that builds against FFmpeg 8.0)
```

## Android app

```bash
cd android
./gradlew testDebugUnitTest assembleDebug
adb install -r app/build/outputs/apk/debug/PhoneCam-*-debug.apk
adb shell am start -n com.ares.phonecam/.MainActivity --ez autostart true   # start streaming
```

AGP 9 with built-in Kotlin, Compose BOM, compileSdk 37 / targetSdk 36 / minSdk 24.
`scripts/dev-run.sh` builds, installs, sets up forwards and starts the desktop app in one go.

## Working without a phone

`fake_phone` serves a recorded H.264 file over the real protocol, including capabilities, STATE,
PING/PONG, pairing and mDNS:

```bash
.venv/bin/python -m phonecam.tools.fake_phone tests/data/phone-720p-12f.h264 --announce --pairing 1234
```

In the desktop app, choose **Wi-Fi** and select *Fake Phone · 127.0.0.1*, or type `127.0.0.1`.

## Protocol changes

`docs/protocol.md` is the source of truth. When changing the wire format:

1. Update the specification.
2. Update `scripts/gen-test-vectors.py` and regenerate the vectors (`python3 scripts/gen-test-vectors.py`).
3. Make both `ProtocolTest.kt` and `pc/tests/test_protocol.py` pass.

Unknown packet types must stay skippable, so older peers keep working.

## Debugging

| Symptom | Where to look |
|---|---|
| Phone pipeline | `adb shell setprop log.tag.PhoneCam VERBOSE` → `adb logcat -s PhoneCam` (per-second stats incl. capture, encode and pipeline depth) |
| PC stages | `phonecam --debug`, and the status-bar tooltip |
| Latency regressions | **Latency test… → F** and the tables in [latency.md](latency.md) |
| Encoder experiments | The experimental `set_tuning` control command ([protocol.md](protocol.md)) switches template, noise reduction, profile, operating rate and vendor keys at runtime |

## Continuous integration

`.github/workflows/ci.yml` runs ruff and pytest for the desktop app, and the Android unit tests
plus a debug build, on every push and pull request.
