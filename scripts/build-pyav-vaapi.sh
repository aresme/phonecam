#!/usr/bin/env bash
# Rebuild PyAV in the PC venv against Ubuntu's FFmpeg, which has VAAPI (the PyPI wheel's bundled
# FFmpeg does not). Only needed for the optional "Hardware decode (VAAPI)" setting.
# PyAV 18.0.0 is the newest release that compiles against FFmpeg 8.0 (Ubuntu 26.04).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKGS=(libavcodec-dev libavformat-dev libavdevice-dev libavfilter-dev libswscale-dev
      libswresample-dev libavutil-dev python3-dev pkg-config build-essential intel-media-va-driver vainfo)
missing=()
for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if (( ${#missing[@]} )); then
    echo ">> Installing build dependencies (sudo): ${missing[*]}"
    sudo apt-get install -y "${missing[@]}"
fi
"$ROOT/pc/.venv/bin/python" -m pip install --force-reinstall --no-deps --no-binary av "av==18.0.0"
"$ROOT/pc/.venv/bin/python" -c "import av; from av.codec.hwaccel import hwdevices_available as h; print('PyAV', av.__version__, 'hw:', h())"
