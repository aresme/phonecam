#!/usr/bin/env bash
# Install, load and (optionally) persist the v4l2loopback "PhoneCam" device.
#   scripts/setup-v4l2loopback.sh            # install if needed + load now
#   scripts/setup-v4l2loopback.sh --persist  # also load automatically at boot
# Needs sudo. Secure Boot: the DKMS-built module must be signed with an enrolled MOK key,
# otherwise modprobe fails with "Key was rejected by service" (see README → Troubleshooting).
set -euo pipefail

VIDEO_NR=10
LABEL="PhoneCam"
OPTIONS="devices=1 video_nr=${VIDEO_NR} card_label=${LABEL} exclusive_caps=1"
PERSIST=0
[[ "${1:-}" == "--persist" ]] && PERSIST=1

if ! modinfo v4l2loopback >/dev/null 2>&1; then
    echo ">> v4l2loopback module not found; installing v4l2loopback-dkms (sudo)"
    sudo apt-get update
    sudo apt-get install -y v4l2loopback-dkms v4l2loopback-utils "linux-headers-$(uname -r)"
fi

if grep -qs "^${LABEL}$" /sys/devices/virtual/video4linux/*/name; then
    echo ">> A ${LABEL} device already exists:"
else
    if lsmod | grep -q '^v4l2loopback'; then
        echo ">> v4l2loopback is loaded with other options; reloading (sudo)."
        echo "   This fails if another app currently has a loopback device open."
        sudo modprobe -r v4l2loopback
    fi
    echo ">> Loading v4l2loopback (sudo): ${OPTIONS}"
    sudo modprobe v4l2loopback ${OPTIONS}
fi
for d in /sys/devices/virtual/video4linux/*; do
    [[ "$(cat "$d/name")" == "${LABEL}" ]] && echo "   /dev/$(basename "$d")"
done

if (( PERSIST )); then
    echo ">> Persisting (sudo): /etc/modules-load.d/phonecam.conf, /etc/modprobe.d/phonecam.conf"
    echo "v4l2loopback" | sudo tee /etc/modules-load.d/phonecam.conf >/dev/null
    echo "options v4l2loopback ${OPTIONS}" | sudo tee /etc/modprobe.d/phonecam.conf >/dev/null
fi
echo ">> Done."
