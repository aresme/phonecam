# Troubleshooting

- [Video devices](#video-devices)
- [Connection](#connection)
- [Wi-Fi and networking](#wi-fi-and-networking)
- [Audio](#audio)
- [Desktop environment](#desktop-environment)
- [Diagnostics](#diagnostics)

---

## Video devices

### PhoneCam doesn't show up in Chrome or Meet

The module must be loaded with `exclusive_caps=1`:

```bash
cat /sys/module/v4l2loopback/parameters/exclusive_caps   # expect Y
```

With that option the device is only offered as a camera while a writer is active, so keep the
PhoneCam desktop app running with **Virtual camera: ON**. If an app still doesn't list it,
switch the format to **YUYV** in the virtual camera panel.

### Secure Boot: `Key was rejected by service`

`modprobe v4l2loopback` fails because the DKMS-built module isn't signed with an enrolled key.

```bash
sudo mokutil --import /var/lib/shim-signed/mok/MOK.der
```

Reboot, choose *Enroll MOK* in the blue MOK manager, and enter the one-time password.
Alternatively, disable Secure Boot.

### "Device busy" when switching the virtual camera on

Another program is writing to the loopback device (for example OBS Virtual Camera, or a second
PhoneCam), or a reader holds a different format. Close it and try again.

### `/dev/video10`: permission denied

The desktop session normally gets access automatically. Otherwise:

```bash
sudo usermod -aG video "$USER"    # then log out and in again
```

## Connection

### `adb devices` shows `unauthorized`

Unlock the phone and accept the RSA prompt. If it doesn't appear: `adb kill-server`, replug, and
in Developer options use *Revoke USB debugging authorizations*.

### "PhoneCam is not streaming on the phone"

The PC reached the phone over adb, but the app isn't streaming. Click **Launch app on phone**, or
tap **Start** in the phone app. The PC connects as soon as it streams.

### "Camera in use by another app — waiting"

Another app (for example the stock camera) took the camera. PhoneCam resumes automatically within
about a second of the camera being released. The PC keeps the connection and shows the last
frame, or the placeholder, meanwhile.

## Wi-Fi and networking

### Wi-Fi: the phone is listed but connecting times out

The network uses *client isolation* (common on guest, campus and public Wi-Fi): devices can reach
the router but not each other, although multicast discovery may still get through. Confirm with
`ping <phone-ip>`. Use USB, a home network, or the phone's hotspot. Connect the PC to the
hotspot; the phone shows its address there as "Hotspot".

### Wi-Fi: no phones found

Discovery uses mDNS (UDP 5353 multicast), which some networks or firewalls block. Type the IP
shown on the phone instead.

### "Wrong pairing code"

Enter the 4-digit code shown under the phone's Wi-Fi address. If it was renewed on the phone, the
PC asks again.

### Firewall (nftables)

The PC only makes outgoing connections to the phone (TCP 47100/47101), so the usual
`ct state established,related accept` input rule is enough. For discovery, also accept mDNS
replies:

```bash
sudo nft add rule inet filter input udp dport 5353 accept    # adapt table/chain to your ruleset
```

## Audio

### PhoneCam Mic is missing

The source exists while the desktop app runs. Check `pactl list short sources | grep phonecam`.
If the desktop app was killed abruptly, restart it; stale modules are cleaned up on start.

### The microphone toggle on the PC is greyed out

The phone app hasn't been granted the microphone permission yet. Tap 🎤 in the phone app once.

### Audio drifts or crackles occasionally

Frames that arrive more than 60 ms past their slot are dropped instead of accumulating delay. This
also absorbs clock drift between the phone and the PC. Brief glitches during Wi-Fi spikes are
expected; USB is steadier.

## Desktop environment

### Qt apps abort with `GLib-GIO-ERROR … no key named 'antialiasing'`

Qt's GTK3 platform theme reads a settings key that current GNOME removed. PhoneCam defaults to
`QT_QPA_PLATFORMTHEME=gnome`. Other Qt apps such as OBS hit the same bug: start them with
`QT_QPA_PLATFORMTHEME=gnome obs`, or add the variable to `~/.config/environment.d/qt.conf` and log
in again.

### No tray icon

GNOME needs the AppIndicator extension for tray icons (enabled by default on Ubuntu). Without it,
PhoneCam works the same, but closing the window quits.

## Diagnostics

| What | How |
|---|---|
| Desktop app logs | `phonecam --debug` (per-stage latency timings at debug level) |
| Phone logs | `adb shell setprop log.tag.PhoneCam VERBOSE`, then `adb logcat -s PhoneCam` |
| Per-stage latency | Hover the status bar in the desktop app |
| V4L2 device state | `v4l2-ctl -d /dev/video10 --all` |
| Play the virtual camera | `ffplay -f v4l2 /dev/video10` |
| Record PhoneCam Mic | `pw-record --target phonecam_mic out.wav` |
