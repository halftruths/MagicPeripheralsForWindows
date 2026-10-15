"""Connection state and battery level of Apple Bluetooth devices.

Connection: the Bluetooth device node (BTHENUM\\DEV_<address>) carries an "is connected"
property; its HID service node (BTHENUM\\{00001124-...}_VID&0001004C_PID&<pid>\\...&<address>_...)
maps the product ID to that address.

Wired: a present USB composite device USB\\VID_05AC&PID_<pid>. Over USB the battery report isn't exposed to
non-admin processes (the USB driver only serves it on an admin-only pipe), so a wired device shows its last
Bluetooth reading, kept across restarts in battery.local.json, and is reported as charging.

Battery: the vendor collection (page 0xFF00 usage 0x14), report 0x90. Layout measured on Magic
Keyboard (0320) and Magic Trackpad (0324): [0x90, status flags, percent]. The status bits are
not decoded yet, so they're passed through raw. A HID read can return a stale value while the
device is off, so battery is only trusted while the device is connected.
"""

import ctypes
import json
import re
import threading
import time
from ctypes import wintypes as w
from pathlib import Path

from tools.hidprobe import GUID, HIDD_ATTRIBUTES, hid, hid_interface_paths, kernel32, open_device, setupapi

DEVICE_NAMES = {0x0320: "Magic Keyboard", 0x0324: "Magic Trackpad"}
BATTERY_MAX_AGE = 60  # seconds between battery reads while connected
BATTERY_CACHE = Path(__file__).resolve().parent / "battery.local.json"
DIGCF_PRESENT = 0x02

DIGCF_ALLCLASSES = 0x04
DEVPROP_TYPE_BOOLEAN = 0x11


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("ClassGuid", GUID), ("DevInst", w.DWORD), ("Reserved", ctypes.c_size_t)]


class DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", w.ULONG)]


# {83DA6326-97A6-4088-9453-A1923F573B29} 15: device is currently connected (Bluetooth device node).
DEVPKEY_IS_CONNECTED = DEVPROPKEY(GUID(0x83DA6326, 0x97A6, 0x4088, (w.BYTE * 8)(*[b - 256 if b > 127 else b for b in
                                                                                  (0x94, 0x53, 0xA1, 0x92, 0x3F, 0x57, 0x3B, 0x29)])), 15)

setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, w.LPCWSTR, w.HWND, w.DWORD]
setupapi.SetupDiEnumDeviceInfo.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [w.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), w.LPWSTR, w.DWORD, ctypes.POINTER(w.DWORD)]
setupapi.SetupDiGetDevicePropertyW.argtypes = [w.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), ctypes.POINTER(DEVPROPKEY),
                                               ctypes.POINTER(w.ULONG), ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), w.DWORD]


def bluetooth_connection_states():
    """Return {product_id: connected_bool} for paired Apple devices we know about."""
    devs = setupapi.SetupDiGetClassDevsW(None, "BTHENUM", None, DIGCF_ALLCLASSES)
    pid_to_addr, addr_connected = {}, {}
    try:
        i = 0
        while True:
            info = SP_DEVINFO_DATA(cbSize=ctypes.sizeof(SP_DEVINFO_DATA))
            if not setupapi.SetupDiEnumDeviceInfo(devs, i, ctypes.byref(info)):
                break
            i += 1
            buf = ctypes.create_unicode_buffer(512)
            if not setupapi.SetupDiGetDeviceInstanceIdW(devs, ctypes.byref(info), buf, len(buf), None):
                continue
            iid = buf.value.upper()
            m = re.search(r"\{00001124-[0-9A-F-]+\}_VID&0001004C_PID&([0-9A-F]{4})\\.*&([0-9A-F]{12})_", iid)
            if m:
                pid_to_addr[int(m.group(1), 16)] = m.group(2)
                continue
            m = re.match(r"BTHENUM\\DEV_([0-9A-F]{12})\\", iid)
            if m:
                ptype, value = w.ULONG(), ctypes.c_ubyte()
                ok = setupapi.SetupDiGetDevicePropertyW(devs, ctypes.byref(info), ctypes.byref(DEVPKEY_IS_CONNECTED),
                                                        ctypes.byref(ptype), ctypes.byref(value), 1, None, 0)
                addr_connected[m.group(1)] = bool(ok and ptype.value == DEVPROP_TYPE_BOOLEAN and value.value)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devs)
    return {pid: addr_connected.get(addr, False) for pid, addr in pid_to_addr.items() if pid in DEVICE_NAMES}


def usb_connected_pids():
    """Product IDs of Apple devices currently attached over USB (composite device node present)."""
    devs = setupapi.SetupDiGetClassDevsW(None, "USB", None, DIGCF_ALLCLASSES | DIGCF_PRESENT)
    pids = set()
    try:
        i = 0
        while True:
            info = SP_DEVINFO_DATA(cbSize=ctypes.sizeof(SP_DEVINFO_DATA))
            if not setupapi.SetupDiEnumDeviceInfo(devs, i, ctypes.byref(info)):
                break
            i += 1
            buf = ctypes.create_unicode_buffer(512)
            if setupapi.SetupDiGetDeviceInstanceIdW(devs, ctypes.byref(info), buf, len(buf), None):
                m = re.match(r"USB\\VID_05AC&PID_([0-9A-F]{4})\\", buf.value.upper())
                if m and int(m.group(1), 16) in DEVICE_NAMES:
                    pids.add(int(m.group(1), 16))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devs)
    return pids


def read_battery_reports():
    """Return {product_id: (percent, flags)} for Apple vendor collections that answer report 0x90."""
    results = {}
    for path in hid_interface_paths():
        match = re.search(r"pid&([0-9a-f]{4})&col02", path, re.I)
        if not match or int(match.group(1), 16) not in DEVICE_NAMES:
            continue
        h, _ = open_device(path)
        if not h:
            continue
        try:
            attrs = HIDD_ATTRIBUTES(Size=ctypes.sizeof(HIDD_ATTRIBUTES))
            hid.HidD_GetAttributes(h, ctypes.byref(attrs))
            buf = ctypes.create_string_buffer(3)
            buf[0] = 0x90
            if hid.HidD_GetInputReport(h, buf, 3):
                results[attrs.ProductID] = (buf.raw[2], buf.raw[1])
        finally:
            kernel32.CloseHandle(h)
    return results


class DeviceMonitor:
    """Cheap connection checks on every call; battery re-read at most every BATTERY_MAX_AGE seconds."""

    def __init__(self):
        self.lock = threading.Lock()
        self.battery = {}       # pid -> (percent, flags, read_at); last Bluetooth reading
        self.was_connected = {}
        try:
            saved = json.loads(BATTERY_CACHE.read_text(encoding="utf-8"))
            self.battery = {int(pid): tuple(v) for pid, v in saved.items()}
        except (OSError, ValueError):
            pass

    def status(self):
        with self.lock:
            now = time.time()
            bluetooth = bluetooth_connection_states()
            usb = usb_connected_pids()
            stale = [pid for pid, on in bluetooth.items()
                     if on and (pid not in self.battery or now - self.battery[pid][2] > BATTERY_MAX_AGE
                                or not self.was_connected.get(pid))]  # re-read right after a reconnect
            if stale:
                for pid, (percent, flags) in read_battery_reports().items():
                    if bluetooth.get(pid):
                        self.battery[pid] = (percent, flags, now)
                try:
                    BATTERY_CACHE.write_text(json.dumps(self.battery), encoding="utf-8")
                except OSError:
                    pass
            self.was_connected = bluetooth
            out = []
            for pid, name in DEVICE_NAMES.items():
                if pid not in bluetooth and pid not in usb:
                    continue  # neither paired nor plugged in
                transport = "usb" if pid in usb else "bluetooth" if bluetooth.get(pid) else None
                percent, flags, read_at = self.battery.get(pid, (None, None, None))
                out.append({"device": name, "connected": transport is not None, "transport": transport,
                            "charging": transport == "usb", "percent": percent, "flags": flags, "read_at": read_at,
                            "percentLive": transport == "bluetooth"})
            return out
