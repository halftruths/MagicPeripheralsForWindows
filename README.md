# MagicPeripheralsForWindows

Makes Apple's Magic Trackpad and Magic Keyboard feel more at home on Windows. A Precision Touchpad driver plus a small local companion app adds gestures, scrolling tuning, settings and battery status.

## Tested with

- Windows 11 (build 26300)
- Magic Trackpad, USB-C model (2024), over Bluetooth and USB-C
- Magic Keyboard, USB-C model (2024), over Bluetooth (battery and connection status only)

## Setup

1. Install the signed driver: download the latest `MT2FW11-…-MSSigned.zip` from [vitoplantamura's releases](https://github.com/vitoplantamura/MagicTrackpad2ForWindows/releases), unzip it, then right-click `AMD64\AmtPtpDevice.inf` and choose **Install**. Uninstall Magic Utilities or Trackpad++ first if you have them.
2. In an admin PowerShell, run `companion\tools\apply-driver-settings.ps1`. This fixes stuck fingers and press-and-drag. Then power-cycle the trackpad.
3. Install [Python 3.10+](https://www.python.org/downloads/). No packages are needed.
4. Run `python companion/server.py` and open http://127.0.0.1:8765.

Gestures only work while the companion is running. It turns off Windows' own 3- and 4-finger gestures so they don't fire twice; turn that setting off in the companion to hand them back to Windows.

## Credits

Built on [vitoplantamura/MagicTrackpad2ForWindows](https://github.com/vitoplantamura/MagicTrackpad2ForWindows), which is based on [imbushuo/mac-precision-touchpad](https://github.com/imbushuo/mac-precision-touchpad). The driver source in this repo is theirs and currently unchanged. Licensed under GPLv2 (see `LICENSE`).
