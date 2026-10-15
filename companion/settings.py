"""Settings schema and persistence.

Each setting has a backend:
  windows   applied live through SPI_SETTOUCHPADPARAMETERS / SPI_SETMOUSEBUTTONSWAP; Windows is the source of truth
  companion used by the gesture engine in this process; stored in settings.local.json
  driver    AmtPtpHidFilter (Bluetooth) and AmtPtpDeviceUsbUm (USB) registry parameters; needs elevation and a
            trackpad power-cycle / re-plug
"""

import json
import threading
import winreg
from pathlib import Path

import winapi

STORE = Path(__file__).resolve().parent / "settings.local.json"
# Bluetooth goes through the kernel filter, USB through the user-mode driver; each reads its own key.
DRIVER_KEYS = (
    r"SYSTEM\CurrentControlSet\Services\AmtPtpHidFilter\Parameters",
    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\AmtPtpDeviceUsbUm\Parameters",
)
DRIVER_KEY = DRIVER_KEYS[0]


def S(key, group, label, kind, default, backend="companion", unit="", min=None, max=None, step=None, choices=None, help="",
      depends=None, device="trackpad", basic=False):
    """depends: (other_key, allowed_values) - the setting only has an effect while other_key is one of allowed_values.
    device: "trackpad", "keyboard", or "global" (system-wide; shown in the trackpad view but shared by every device).
    basic: shown by default on the settings page (at most 3 per group and device); the rest sit behind Advanced."""
    return {"key": key, "group": group, "label": label, "kind": kind, "default": default, "backend": backend,
            "unit": unit, "min": min, "max": max, "step": step, "choices": choices, "help": help, "depends": depends,
            "device": device, "basic": basic}


# Shared by 3-finger and 4-finger swipes.
SWIPE_SIDE_CHOICES = [
    ["off", "Nothing"], ["desktops", "Switch virtual desktop"], ["apps", "Switch app (Alt+Tab)"],
    ["back_forward", "Back/forward (Alt+Left/Right)"], ["media", "Previous/next track"]]
SWIPE_VERTICAL_CHOICES = [
    ["off", "Nothing"], ["task_view", "Task View (Win+Tab)"], ["show_desktop", "Show desktop (Win+D)"],
    ["notifications", "Notification center (Win+N)"], ["quick_settings", "Quick settings (Win+A)"],
    ["search", "Search (Win+S)"], ["play_pause", "Play/pause"]]

SCHEMA = [
    # 1 finger
    S("win.tapEnabled", "1 finger", "Tap to left click", "bool", True, "windows"),
    S("win.tapAndDragEnabled", "1 finger", "Double tap to drag", "bool", True, "windows"),
    S("win.sensitivityLevel", "1 finger", "Accidental-touch filtering", "int", 2, "windows", "level", 0, 4, 1,
      help="Windows touchpad sensitivity: 0 = most sensitive (never ignores touches) ... 4 = least sensitive."),

    # 2 fingers
    S("win.twoFingerTapEnabled", "2 fingers", "Tap to right click", "bool", True, "windows"),
    S("win.rightClickZoneEnabled", "2 fingers", "Click bottom-right corner to right click", "bool", True, "windows"),
    S("scroll.mode", "2 fingers", "Scrolling handled by", "choice", "windows", choices=[
        ["windows", "Windows (native)"], ["companion", "Companion engine"], ["off", "Off"]],
      help="Companion engine: free diagonal scrolling with the tuning below. Windows: native scrolling (apps decide axis locking)."),
    S("win.scrollDirectionReversed", "2 fingers", "Windows scrolling: reverse direction", "bool", False, "windows",
      depends=("scroll.mode", ["windows"])),
    S("scroll.natural", "Scrolling (companion engine)", "Content follows fingers (natural)", "bool", True,
      depends=("scroll.mode", ["companion"])),
    S("scroll.horizontal", "Scrolling (companion engine)", "Horizontal scrolling", "bool", True,
      depends=("scroll.mode", ["companion"])),
    S("scroll.axisLock", "Scrolling (companion engine)", "Axis lock", "choice", "dominant", choices=[
        ["free", "Free (any direction)"], ["dominant", "Lock straight swipes, free diagonals"], ["always", "Always lock to one axis"]],
      depends=("scroll.mode", ["companion"])),
    S("scroll.lockRatio", "Scrolling (companion engine)", "Lock when dominant axis exceeds", "float", 2.5, unit="x the other", min=1.0, max=10.0, step=0.1,
      depends=("scroll.axisLock", ["dominant"])),
    S("scroll.speed", "Scrolling (companion engine)", "Scroll speed", "float", 18.0, unit="wheel units/mm", min=1.0, max=60.0, step=0.5,
      help="120 wheel units = one mouse-wheel notch. Browsers scroll about 100 px per 120 units at 100% zoom.",
      depends=("scroll.mode", ["companion"])),
    S("scroll.acceleration", "Scrolling (companion engine)", "Acceleration", "float", 0.45, unit="extra gain per 100 mm/s", min=0.0, max=5.0, step=0.05,
      depends=("scroll.mode", ["companion"])),
    S("scroll.startDistance", "Scrolling (companion engine)", "Start threshold", "float", 1.5, unit="mm", min=0.0, max=10.0, step=0.1,
      depends=("scroll.mode", ["companion"])),
    S("scroll.minDelta", "Scrolling (companion engine)", "Smallest wheel event", "int", 6, unit="wheel units", min=1, max=120, step=1,
      help="Deltas are batched up to this size. 120 makes every app scroll in whole notches.",
      depends=("scroll.mode", ["companion"])),
    S("scroll.inertia", "Scrolling (companion engine)", "Inertia after lift", "bool", True,
      depends=("scroll.mode", ["companion"])),
    S("scroll.friction", "Scrolling (companion engine)", "Inertia friction", "float", 4.0, unit="1/s", min=0.5, max=20.0, step=0.1,
      help="Speed decays by e^(-friction x seconds). Higher stops sooner.",
      depends=("scroll.inertia", [True])),
    S("scroll.inertiaMinSpeed", "Scrolling (companion engine)", "Inertia start/stop speed", "float", 40.0, unit="mm/s", min=5.0, max=300.0, step=5.0,
      depends=("scroll.inertia", [True])),
    S("nav.flick", "2 fingers", "Fast horizontal swipe = back/forward", "bool", False),
    S("nav.flickSwap", "2 fingers", "Swap back/forward direction", "bool", False,
      depends=("nav.flick", [True])),
    S("nav.flickSpeed", "2 fingers", "Swipe speed needed", "float", 250.0, unit="mm/s", min=50.0, max=1000.0, step=10.0,
      depends=("nav.flick", [True])),
    S("nav.flickDistance", "2 fingers", "Swipe distance needed", "float", 15.0, unit="mm", min=3.0, max=80.0, step=1.0,
      depends=("nav.flick", [True])),
    S("win.zoomEnabled", "2 fingers", "Pinch to zoom", "bool", True, "windows"),

    # 3 fingers
    S("three.tap", "3 fingers", "Tap", "choice", "middle", choices=[["off", "Nothing"], ["middle", "Middle click"]]),
    S("three.move", "3 fingers", "3-finger movement", "choice", "swipe", choices=[
        ["swipe", "Swipe gestures"], ["drag", "Drag"]],
      help="Three fingers can either swipe (Task View, switch app...) or drag things around, not both - like macOS. "
           "Taps work in both modes."),
    S("three.swipeSide", "3 fingers", "Swipe left/right", "choice", "apps", choices=SWIPE_SIDE_CHOICES,
      depends=("three.move", ["swipe"])),
    S("three.swipeSideSwap", "3 fingers", "Swap left/right direction", "bool", False,
      depends=("three.swipeSide", ["desktops", "apps", "back_forward", "media"])),
    S("three.swipeUp", "3 fingers", "Swipe up", "choice", "task_view", choices=SWIPE_VERTICAL_CHOICES,
      depends=("three.move", ["swipe"])),
    S("three.swipeDown", "3 fingers", "Swipe down", "choice", "show_desktop", choices=SWIPE_VERTICAL_CHOICES,
      depends=("three.move", ["swipe"])),
    S("three.swipeDistance", "3 fingers", "Swipe distance needed", "float", 20.0, unit="mm", min=5.0, max=80.0, step=1.0,
      depends=("three.move", ["swipe"])),
    S("three.dragButton", "3 fingers", "Drag with button", "choice", "left", choices=[
        ["left", "Primary"], ["middle", "Middle"], ["right", "Secondary"]],
      depends=("three.move", ["drag"])),
    S("three.dragGain", "3 fingers", "Drag pointer speed", "float", 12.0, unit="px/mm", min=1.0, max=60.0, step=0.5,
      depends=("three.move", ["drag"])),
    S("three.dragStart", "3 fingers", "Drag start threshold", "float", 3.0, unit="mm", min=0.0, max=15.0, step=0.1,
      depends=("three.move", ["drag"])),
    S("three.dropOnLift", "3 fingers", "Drop when a finger is lifted", "bool", False,
      depends=("three.move", ["drag"])),
    S("three.dragLock", "3 fingers", "Drag lock after lifting", "int", 0, unit="ms (0 = off)", min=0, max=5000, step=50,
      help="Keeps the button held after lifting; put 3 fingers back to keep dragging. A 1-2 finger tap ends it early.",
      depends=("three.move", ["drag"])),

    # 4 fingers
    S("four.tap", "4 fingers", "Tap", "choice", "notifications", choices=[
        ["off", "Nothing"], ["notifications", "Notification center (Win+N)"], ["quick_settings", "Quick settings (Win+A)"],
        ["search", "Search (Win+S)"], ["task_view", "Task View (Win+Tab)"], ["play_pause", "Play/pause"]]),
    S("four.swipeSide", "4 fingers", "Swipe left/right", "choice", "desktops", choices=SWIPE_SIDE_CHOICES),
    S("four.swipeSideSwap", "4 fingers", "Swap left/right direction", "bool", False,
      depends=("four.swipeSide", ["desktops", "apps", "back_forward", "media"])),
    S("four.swipeUp", "4 fingers", "Swipe up", "choice", "task_view", choices=SWIPE_VERTICAL_CHOICES),
    S("four.swipeDown", "4 fingers", "Swipe down", "choice", "show_desktop", choices=SWIPE_VERTICAL_CHOICES),
    S("four.swipeDistance", "4 fingers", "Swipe distance needed", "float", 20.0, unit="mm", min=5.0, max=80.0, step=1.0),

    # Taps (companion gestures)
    S("tap.maxDuration", "Taps", "Longest tap", "int", 220, unit="ms", min=50, max=600, step=10),
    S("tap.maxMovement", "Taps", "Most finger movement in a tap", "float", 2.5, unit="mm", min=0.5, max=10.0, step=0.1),
    S("native.disableMultiFinger", "Taps", "Turn off Windows' own 3/4-finger gestures", "bool", True,
      help="Stops Windows and the companion from both acting on the same gesture. Registry change; Windows may need a sign-out to pick it up.",
      device="global"),

    # Pointer
    S("win.cursorSpeed", "Pointer", "Touchpad cursor speed", "int", 10, "windows", "Windows level", 1, 20, 1),
    S("mouse.swapButtons", "Pointer", "Swap primary/secondary buttons", "bool", False, "windows", device="global"),
    S("win.allowActiveWhenMousePresent", "Pointer", "Keep touchpad on when a mouse is connected", "bool", True, "windows", device="global"),
    S("tp.batteryAlert", "Battery", "Low battery alert", "bool", True),
    S("tp.batteryThreshold", "Battery", "Alert at or below", "int", 20, unit="%", min=5, max=50, step=1,
      depends=("tp.batteryAlert", [True])),

    # Click and driver
    S("driver.ButtonDisabled", "Click (driver)", "Ignore physical click", "bool", False, "driver",
      help="Disables the force-touch click and haptics entirely."),
    S("driver.clickForce", "Click (driver)", "Click force threshold", "int", 30, "driver", "raw (light 21, medium 23, firm 30)", 0, 255, 1),
    S("driver.releaseForce", "Click (driver)", "Release force threshold", "int", 24, "driver", "raw (light 16, medium 20, firm 24)", 0, 255, 1),
    S("driver.clickHaptic", "Click (driver)", "Click haptic strength", "int", 8, "driver", "raw (0 = silent; light 4, medium 6, firm 8)", 0, 255, 1),
    S("driver.releaseHaptic", "Click (driver)", "Release haptic strength", "int", 2, "driver", "raw (0 = silent; medium 0, firm 2)", 0, 255, 1),
    S("driver.IgnoreButtonFinger", "Click (driver)", "Freeze all fingers while clicked", "bool", False, "driver",
      help="Driver default is on, which breaks press-and-drag."),
    S("driver.freezeOnZeroPressure", "Click (driver)", "Freeze fingers reading zero pressure", "bool", False, "driver",
      help="Driver default is on, which causes stuck fingers and choppy scrolling."),
    S("driver.IgnoreNearFingers", "Click (driver)", "Ignore hovering fingers", "bool", True, "driver"),
    S("driver.PalmRejection", "Click (driver)", "Palm rejection (finger-type based)", "bool", False, "driver"),

    # Magic Keyboard
    S("kb.batteryAlert", "Battery", "Low battery alert", "bool", True, device="keyboard"),
    S("kb.batteryThreshold", "Battery", "Alert at or below", "int", 20, unit="%", min=5, max=50, step=1,
      depends=("kb.batteryAlert", [True]), device="keyboard"),
]

# The up-to-3 settings per group (and device) shown before the user turns on Advanced.
BASIC_KEYS = {
    "win.tapEnabled", "win.tapAndDragEnabled", "win.sensitivityLevel",
    "win.twoFingerTapEnabled", "scroll.mode", "win.zoomEnabled",
    "three.tap", "three.move", "three.swipeUp",
    "four.tap", "four.swipeSide", "four.swipeUp",
    "driver.clickForce", "driver.clickHaptic", "driver.ButtonDisabled",
    "tap.maxDuration", "tap.maxMovement", "native.disableMultiFinger",
    "win.cursorSpeed", "mouse.swapButtons", "win.allowActiveWhenMousePresent",
    "tp.batteryAlert", "tp.batteryThreshold", "kb.batteryAlert", "kb.batteryThreshold",
    "scroll.speed", "scroll.acceleration", "scroll.inertia",
}
for _s in SCHEMA:
    _s["basic"] = _s["key"] in BASIC_KEYS

BY_KEY = {s["key"]: s for s in SCHEMA}
DEVICES = ("trackpad", "keyboard")
STORE_VERSION = 2


def schema_for(device):
    """Specs shown for a device: its own plus the system-wide ones."""
    if device not in DEVICES:
        raise ValueError(f"unknown device {device!r}")
    return [s for s in SCHEMA if s["device"] in (device, "global")]
WINDOWS_TOUCHPAD_KEYS = {k[4:] for k in BY_KEY if k.startswith("win.")}


def coerce(spec, value):
    kind = spec["kind"]
    if kind == "bool":
        return bool(value)
    if kind == "choice":
        allowed = [c[0] for c in spec["choices"]]
        if value not in allowed:
            raise ValueError(f"{spec['key']}: {value!r} not in {allowed}")
        return value
    num = int(value) if kind == "int" else float(value)
    if spec["min"] is not None:
        num = max(spec["min"], num)
    if spec["max"] is not None:
        num = min(spec["max"], num)
    return num


# -- driver registry encoding --

def read_driver():
    raw = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, DRIVER_KEY) as key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                raw[name] = value
                i += 1
    except FileNotFoundError:
        pass
    # Driver defaults (AmtPtpHidFilter/Driver.c) for anything not set.
    click = raw.get("FeedbackClick", 0x08081E)
    release = raw.get("FeedbackRelease", 0x020218)
    return {
        "driver.ButtonDisabled": bool(raw.get("ButtonDisabled", 0)),
        "driver.clickForce": click & 0xFF, "driver.clickHaptic": (click >> 8) & 0xFF,
        "driver.releaseForce": release & 0xFF, "driver.releaseHaptic": (release >> 8) & 0xFF,
        "driver.IgnoreButtonFinger": bool(raw.get("IgnoreButtonFinger", 1)),
        "driver.freezeOnZeroPressure": raw.get("StopPressure", 0) != 0xFFFFFFFF,
        "driver.IgnoreNearFingers": bool(raw.get("IgnoreNearFingers", 1)),
        "driver.PalmRejection": bool(raw.get("PalmRejection", 0)),
    }


def driver_registry_values(values):
    """Turn driver.* settings into AmtPtpHidFilter registry DWORDs."""
    def feedback(force, haptic):
        return (haptic << 16) | (haptic << 8) | force
    return {
        "ButtonDisabled": int(values["driver.ButtonDisabled"]),
        "FeedbackClick": feedback(values["driver.clickForce"], values["driver.clickHaptic"]),
        "FeedbackRelease": feedback(values["driver.releaseForce"], values["driver.releaseHaptic"]),
        "IgnoreButtonFinger": int(values["driver.IgnoreButtonFinger"]),
        "StopPressure": 0 if values["driver.freezeOnZeroPressure"] else 0xFFFFFFFF,
        "StopSize": 0xFFFFFFFF,
        "IgnoreNearFingers": int(values["driver.IgnoreNearFingers"]),
        "PalmRejection": int(values["driver.PalmRejection"]),
    }


class DeviceView:
    """get(key) for one device: that device's settings, or the system-wide ones. What the gesture engine reads."""

    def __init__(self, settings, device):
        self.settings, self.device = settings, device

    def get(self, key):
        spec = BY_KEY[key]
        if spec["device"] not in (self.device, "global"):
            raise KeyError(key)
        return self.settings.get(key)


class Settings:
    def __init__(self):
        self.lock = threading.Lock()
        # Companion-backed values only; the others live in Windows / the driver registry.
        self.devices = {d: {s["key"]: s["default"] for s in SCHEMA if s["backend"] == "companion" and s["device"] == d}
                        for d in DEVICES}
        self.glob = {s["key"]: s["default"] for s in SCHEMA if s["backend"] == "companion" and s["device"] == "global"}
        self.listeners = []
        self._load()

    def _bucket(self, spec):
        return self.glob if spec["device"] == "global" else self.devices[spec["device"]]

    def _adopt(self, key, value):
        spec = BY_KEY.get(key)
        if spec and spec["backend"] == "companion":
            try:
                self._bucket(spec)[key] = coerce(spec, value)
            except (ValueError, TypeError):
                pass

    def _load(self):
        try:
            raw = STORE.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            saved = json.loads(raw)
            if not isinstance(saved, dict):
                raise ValueError("not an object")
        except ValueError:
            return  # corrupt: run on defaults, leave the file for the user to inspect
        if "version" in saved:
            devs = saved.get("devices")
            for dev, vals in (devs.items() if isinstance(devs, dict) else ()):
                if dev in self.devices and isinstance(vals, dict):
                    for key, value in vals.items():
                        if key in self.devices[dev]:
                            self._adopt(key, value)
            if isinstance(saved.get("global"), dict):
                for key, value in saved["global"].items():
                    if key in self.glob:
                        self._adopt(key, value)
            return
        # v1: one flat dict. Keep a backup, move each key to where its spec lives, rewrite as v2.
        for key, value in saved.items():
            self._adopt(key, value)
        try:
            STORE.with_name(STORE.name + ".bak").write_text(raw, encoding="utf-8")
            self._save()
        except OSError:
            pass

    def _save(self):
        data = {"version": STORE_VERSION, "devices": self.devices, "global": self.glob}
        STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get(self, key):
        spec = BY_KEY[key]
        if spec["backend"] != "companion":
            raise KeyError(key)
        return self._bucket(spec)[key]

    def view(self, device):
        if device not in DEVICES:
            raise ValueError(f"unknown device {device!r}")
        return DeviceView(self, device)

    @property
    def companion(self):
        """Flat merged view of every companion-backed value (read-only convenience)."""
        merged = dict(self.glob)
        for vals in self.devices.values():
            merged.update(vals)
        return merged

    def snapshot(self, device="trackpad"):
        """Current values for a device (plus system-wide ones), reading Windows and the driver registry live."""
        keys = {s["key"] for s in schema_for(device)}
        values = {k: v for k, v in self.companion.items() if k in keys}
        values["mouse.swapButtons"] = winapi.get_buttons_swapped()
        tp = winapi.get_touchpad()
        for name in WINDOWS_TOUCHPAD_KEYS:
            values["win." + name] = tp[name]
        if device == "trackpad":
            values.update(read_driver())
        return {k: v for k, v in values.items() if k in keys}

    def update(self, device, changes):
        """Apply a dict of changes for a device. Driver settings are returned for the caller to apply elevated."""
        allowed = {s["key"] for s in schema_for(device)}
        for key in changes:
            if key not in BY_KEY:
                raise ValueError(f"unknown setting {key}")
            if key not in allowed:
                raise ValueError(f"{key} does not belong to {device}")
        touchpad, driver = {}, {}
        with self.lock:
            for key, value in changes.items():
                spec = BY_KEY[key]
                value = coerce(spec, value)
                if spec["backend"] == "companion":
                    self._bucket(spec)[key] = value
                elif key == "mouse.swapButtons":
                    winapi.set_buttons_swapped(value)
                elif key.startswith("win."):
                    touchpad[key[4:]] = value
                else:
                    driver[key] = value
            if touchpad:
                winapi.set_touchpad(**touchpad)
            self._save()
        self.apply_side_effects()
        for fn in self.listeners:
            fn()
        return driver

    def apply_side_effects(self):
        """Keep Windows consistent with companion choices: who scrolls, and whether Windows' own 3/4-finger gestures run."""
        mode = self.get("scroll.mode")
        if winapi.get_touchpad()["panEnabled"] != (mode == "windows"):
            winapi.set_touchpad(panEnabled=(mode == "windows"))
        if self.get("native.disableMultiFinger"):
            wanted = {name: 0 for name in winapi.NATIVE_MULTI_FINGER}
        else:
            wanted = dict(winapi.NATIVE_DEFAULTS)
        if winapi.get_native_gestures() != wanted:
            winapi.set_native_gestures(wanted)

    def restore_windows_scrolling(self):
        """Called on exit so the touchpad never stays without scrolling if the companion isn't running."""
        if self.get("scroll.mode") == "companion":
            winapi.set_touchpad(panEnabled=True)
