# GMMK Numpad Suite

An unofficial, open-source replacement for Glorious CORE for the **Glorious GMMK Numpad**.
It runs in the system tray and serves a dashboard at `http://127.0.0.1:8765`. It talks to the
numpad directly over USB or Bluetooth, so Glorious CORE isn't needed (keep CORE closed or
uninstalled: both writing to the numpad overwrite each other's settings).

> Not affiliated with or endorsed by Glorious. Flashing firmware is at your own risk.

**Requires Windows 10/11.** Linux is not supported yet.

## Features

**Numpad**
- Per-key lighting, effects, brightness, speed, Multicolour, side bars, layer indicator
- Key remapping: keys and shortcuts, media keys, macros, profile / layer switching, app actions
- 3 profiles × 3 layers, all stored on the numpad (switch with Fn+9 / Fn+7 and Fn+3 / Fn+1)
- Knob and slider per layer: Windows volume, one program's volume, OBS, VoiceMeeter
- Polling rate, key scan interval, lighting off when idle
- Battery gauge on the side bars, low-battery warning over Bluetooth
- Backup / restore of all settings

**Integrations** (keys light up with the live status)
- **OBS Studio** (WebSocket): record, stream, pause, replay buffer, scene keys with tally lights,
  Studio Mode TAKE and T-bar on the slider, source mute, volume on the knob / slider
- **Discord** (desktop app): mute and deafen, with hold to talk / hold to mute
- **VoiceMeeter** (Basic / Banana / Potato): mute inputs and outputs, routing, Macro Buttons,
  gain on the knob / slider
- **Windows**: mic mute for every app at once, media keys (Spotify, browsers… with a playing light),
  open a program / file / URL

**Firmware add-on (optional)**
- A small add-on patched into the stock firmware 0088: instant status lights, no missed key taps
  while lights change, no flash wear from lighting changes
- Installed from the Settings tab over USB; keeps Bluetooth and your settings; stock can always be
  restored from the same place

## Install

1. Download `GMMK-Numpad-Suite-<version>.exe` from [Releases](../../releases).
2. Run it. A tray icon appears and the dashboard opens in your browser.
3. Settings → **Start with Windows** to have it run at login.

Settings are kept in `%APPDATA%\GMMK Numpad Suite`, so the exe can be moved or replaced freely.
Windows SmartScreen may warn about an unsigned exe: **More info → Run anyway**.

## Setting up integrations

All of them are in the dashboard's **Integrations** tab, each with its own step-by-step guide.

- **OBS Studio:** Tools → WebSocket Server Settings → enable the server (port 4455). The suite
  reads the password from OBS's own settings on the same PC, so there's nothing to type.
- **Discord:** Discord only allows mute / deafen control for apps you own, so you create a free
  app in the Discord Developer Portal, add your own account under **App Testers**, and paste its
  client ID and secret into the suite. The first connect shows an **Authorize** popup in Discord.
  The secret and tokens stay on your PC and are never included in backups.
- **VoiceMeeter:** switch it on in the suite; nothing to set up in VoiceMeeter. Macro Button keys
  need the VoiceMeeter Macro Buttons app running.
- **Windows** actions (mic mute, media keys, program volume) need no setup.

## Run from source

Python 3.12 on Windows:

```powershell
pip install -r requirements.txt
python suite/app.py            # --no-browser to skip opening the dashboard
```

## Build the exe

```powershell
python fw/build.py             # optional: firmware add-on, needs arm-none-eabi-gcc + GMMK_Numpad_88.hex
python packaging/build_exe.py  # -> dist/GMMK Numpad Suite.exe
```

The Glorious stock firmware (`GMMK_Numpad_88.hex`) is not part of this repository. Put it in the
repo root to bundle the firmware tools; without it the exe is built and works normally, but it can't
update the numpad's firmware.

### Releases (GitHub Actions)

`.github/workflows/release.yml` builds the exe on every `v*` tag and attaches it to a GitHub release
(**Run workflow** in the Actions tab makes a test build). It downloads the stock firmware from a
private S3 / MinIO bucket, checks its SHA-256, and builds the add-on with a pinned arm-none-eabi-gcc.
Repository secrets:

| Secret | Example |
|---|---|
| `MINIO_ENDPOINT` | `https://minio.example.com` |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | a read-only user for the firmware file |
| `FIRMWARE_S3_PATH` | `s3://gmmk-asset/stock/GMMK_Numpad_88.hex` |

```powershell
git tag v1.0.0
git push origin v1.0.0
```

## Project layout

| Path | What |
|---|---|
| `suite/` | the app: tray, web server, device manager, integrations, dashboard (`suite/web`) |
| `tools/` | HID protocol library (`gmmk_numpad.py`), firmware flasher, research scripts |
| `fw/` | the firmware add-on (C) and its build / patch script |
| `packaging/` | PyInstaller build |
