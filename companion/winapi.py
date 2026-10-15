"""Win32 helpers: input injection, Windows touchpad settings, mouse button swap."""

import ctypes
import winreg
from ctypes import wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)

# Tag on every event we inject, so our own wheel/mouse events can be told apart.
COMPANION_TAG = 0x4D504657

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
BUTTON_FLAGS = {  # (down, up)
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

VK = {
    "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "tab": 0x09, "esc": 0x1B,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "a": 0x41, "d": 0x44, "n": 0x4E, "s": 0x53,
    "play_pause": 0xB3, "browser_back": 0xA6, "browser_forward": 0xA7,
}
EXTENDED_VKS = {0x25, 0x26, 0x27, 0x28, 0x5B}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SystemParametersInfoW.argtypes = [w.UINT, w.UINT, ctypes.c_void_p, w.UINT]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]


def _send(inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags, dx=0, dy=0, data=0):
    return INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, COMPANION_TAG)))


def mouse_move(dx, dy):
    """Relative move in pixels (subject to the Windows pointer acceleration setting)."""
    if dx or dy:
        _send([_mouse(MOUSEEVENTF_MOVE, int(dx), int(dy))])


def mouse_button(button, down):
    _send([_mouse(BUTTON_FLAGS[button][0 if down else 1])])


def mouse_click(button):
    down, up = BUTTON_FLAGS[button]
    _send([_mouse(down), _mouse(up)])


def wheel(vertical=0, horizontal=0):
    """Wheel deltas in WHEEL_DELTA units (120 = one notch). Positive vertical scrolls up, positive horizontal scrolls right."""
    events = []
    if vertical:
        events.append(_mouse(MOUSEEVENTF_WHEEL, data=int(vertical)))
    if horizontal:
        events.append(_mouse(MOUSEEVENTF_HWHEEL, data=int(horizontal)))
    if events:
        _send(events)


def _key(vk, up):
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VKS else 0)
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(vk, 0, flags, 0, COMPANION_TAG)))


def press_keys(*names):
    """Press a chord, e.g. press_keys('ctrl', 'win', 'right')."""
    vks = [VK[n] for n in names]
    _send([_key(vk, False) for vk in vks] + [_key(vk, True) for vk in reversed(vks)])


# -- Windows touchpad settings (SPI_GET/SETTOUCHPADPARAMETERS, Windows 11 24H2+) --

SPI_GETTOUCHPADPARAMETERS = 0x00AE
SPI_SETTOUCHPADPARAMETERS = 0x00AF
SPIF_UPDATEINIFILE_SENDCHANGE = 0x03


class TOUCHPAD_PARAMETERS(ctypes.Structure):
    _fields_ = [("versionNumber", w.UINT), ("maxSupportedContacts", w.UINT), ("legacyTouchpadFeatures", w.UINT),
                ("flags1", w.DWORD), ("flags2", w.DWORD), ("sensitivityLevel", w.UINT), ("cursorSpeed", w.UINT),
                ("feedbackIntensity", w.UINT), ("clickForceSensitivity", w.UINT),
                ("rightClickZoneWidth", w.UINT), ("rightClickZoneHeight", w.UINT)]


# Bit positions in flags2 (the writable user settings).
TOUCHPAD_FLAGS = {
    "allowActiveWhenMousePresent": 0, "feedbackEnabled": 1, "tapEnabled": 2, "tapAndDragEnabled": 3,
    "twoFingerTapEnabled": 4, "rightClickZoneEnabled": 5, "mouseAccelSettingHonored": 6,
    "panEnabled": 7, "zoomEnabled": 8, "scrollDirectionReversed": 9,
}


def get_touchpad():
    p = TOUCHPAD_PARAMETERS(versionNumber=1)
    if not user32.SystemParametersInfoW(SPI_GETTOUCHPADPARAMETERS, ctypes.sizeof(p), ctypes.byref(p), 0):
        raise OSError(ctypes.get_last_error(), "SPI_GETTOUCHPADPARAMETERS failed")
    values = {name: bool((p.flags2 >> bit) & 1) for name, bit in TOUCHPAD_FLAGS.items()}
    values.update(sensitivityLevel=p.sensitivityLevel, cursorSpeed=p.cursorSpeed)
    return values


def set_touchpad(**changes):
    """Apply touchpad settings live (read-modify-write). Keys: TOUCHPAD_FLAGS names, sensitivityLevel, cursorSpeed."""
    p = TOUCHPAD_PARAMETERS(versionNumber=1)
    if not user32.SystemParametersInfoW(SPI_GETTOUCHPADPARAMETERS, ctypes.sizeof(p), ctypes.byref(p), 0):
        raise OSError(ctypes.get_last_error(), "SPI_GETTOUCHPADPARAMETERS failed")
    for name, value in changes.items():
        if name in TOUCHPAD_FLAGS:
            bit = 1 << TOUCHPAD_FLAGS[name]
            p.flags2 = (p.flags2 | bit) if value else (p.flags2 & ~bit)
        else:
            setattr(p, name, int(value))
    if not user32.SystemParametersInfoW(SPI_SETTOUCHPADPARAMETERS, ctypes.sizeof(p), ctypes.byref(p),
                                        SPIF_UPDATEINIFILE_SENDCHANGE):
        raise OSError(ctypes.get_last_error(), "SPI_SETTOUCHPADPARAMETERS failed")


# -- Windows' own 3/4-finger gestures (registry only; Windows may not pick changes up until sign-out) --

PTP_KEY = r"Software\Microsoft\Windows\CurrentVersion\PrecisionTouchPad"
NATIVE_MULTI_FINGER = ("ThreeFingerSlideEnabled", "ThreeFingerTapEnabled", "FourFingerSlideEnabled", "FourFingerTapEnabled")
NATIVE_DEFAULTS = {"ThreeFingerSlideEnabled": 1, "ThreeFingerTapEnabled": 1, "FourFingerSlideEnabled": 2, "FourFingerTapEnabled": 2}


def get_native_gestures():
    values = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PTP_KEY) as key:
        for name in NATIVE_MULTI_FINGER:
            try:
                values[name] = winreg.QueryValueEx(key, name)[0]
            except FileNotFoundError:
                values[name] = NATIVE_DEFAULTS[name]
    return values


def set_native_gestures(values):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PTP_KEY, 0, winreg.KEY_SET_VALUE) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))


# -- Mouse button swap (applies live) --

SPI_SETMOUSEBUTTONSWAP = 0x0021
SM_SWAPBUTTON = 23


def get_buttons_swapped():
    return bool(user32.GetSystemMetrics(SM_SWAPBUTTON))


def set_buttons_swapped(swapped):
    user32.SystemParametersInfoW(SPI_SETMOUSEBUTTONSWAP, 1 if swapped else 0, None, SPIF_UPDATEINIFILE_SENDCHANGE)
