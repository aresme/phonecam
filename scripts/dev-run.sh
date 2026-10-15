#!/usr/bin/env bash
# Build + install the APK, set up adb forwards, launch the phone app and the PC app.
#   scripts/dev-run.sh [-s SERIAL] [--no-build] [-- extra phonecam args]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERIAL_ARGS=()
BUILD=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        -s) SERIAL_ARGS=(-s "$2"); shift 2 ;;
        --no-build) BUILD=0; shift ;;
        --) shift; break ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if (( BUILD )); then
    (cd "$ROOT/android" && ./gradlew -q assembleDebug)
    adb "${SERIAL_ARGS[@]}" install -r "$ROOT"/android/app/build/outputs/apk/debug/PhoneCam-*-debug.apk
fi
adb "${SERIAL_ARGS[@]}" forward tcp:47100 tcp:47100
adb "${SERIAL_ARGS[@]}" forward tcp:47101 tcp:47101
adb "${SERIAL_ARGS[@]}" shell am start -n com.ares.phonecam/.MainActivity --ez autostart true >/dev/null

if [[ ! -x "$ROOT/pc/.venv/bin/python" ]]; then
    python3 -m venv --without-pip --system-site-packages "$ROOT/pc/.venv"
    "$ROOT/pc/.venv/bin/python" -m pip install -q -e "$ROOT/pc[dev]"
fi
exec "$ROOT/pc/.venv/bin/python" -m phonecam --debug "$@"
