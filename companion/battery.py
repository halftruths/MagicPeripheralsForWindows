"""Connection state and battery level of Apple Bluetooth devices.

Connection: the Bluetooth device node (BTHENUM\\DEV_<address>) carries an "is connected"
property; its HID service node (BTHENUM\\{00001124-...}_VID&0001004C_PID&<pid>\\...&<address>_...)
maps the product ID to that address. The same DEV_ node's friendly name is the name the user gave the device.

Wired: a present USB composite device USB\\VID_05AC&PID_<pid>. Over USB the battery report isn't exposed to
non-admin processes (the USB driver only serves it on an admin-only pipe), so a wired device shows its last
Bluetooth reading, kept across restarts in battery.local.json, and is reported as charging. The composite
node also yields the serial (instance id suffix) and firmware revision (REV_xxxx of the hardware id).

Battery: the vendor collection (page 0xFF00 usage 0x14), report 0x90. Layout measured on Magic
Keyboard (0320) and Magic Trackpad (0324): [0x90, status flags, percent]. Status bits (decoded with the
HID parser against the report descriptor, then confirmed by unplugging/replugging a Magic Keyboard's
charger, 2026-10-15):
  bit0 (Battery System usage 0x46)  external power connected
  bit1 (usage 0x44, "Charging")     charging (stays set at 100% while on power)
  bit2 (usage 0x61)                 running on battery
  e.g. 0x04 unplugged, 0x05 just plugged in, 0x03 plugged in and charging.
A HID read can return a stale value while the device is off, so battery is only trusted while the device is
connected.
"""

import ctypes
import json
import re
import threading
import time
from ctypes import wintypes as w
from pathlib import Path

from paths import DATA_DIR
from tools.hidprobe import GUID, HIDD_ATTRIBUTES, hid, hid_interface_paths, kernel32, open_device, setupapi

DEVICE_NAMES = {0x0320: "Magic Keyboard", 0x0324: "Magic Trackpad"}
DEVICE_IDS = {0x0320: "keyboard", 0x0324: "trackpad"}
BATTERY_MAX_AGE = 10  # seconds between battery reads while connected (short so plugging in a charger shows quickly)
FLAG_EXTERNAL_POWER, FLAG_CHARGING = 0x01, 0x02
INFO_MAX_AGE = 60     # seconds between reads of the slow properties (name, address, serial, firmware)
SAVE_INTERVAL = 60    # seconds between cache writes caused only by lastSeen advancing while connected
BATTERY_CACHE = DATA_DIR / "battery.local.json"
DIGCF_PRESENT = 0x02

DIGCF_ALLCLASSES = 0x04
DEVPROP_TYPE_BOOLEAN = 0x11
DEVPROP_TYPE_STRING = 0x12
SPDRP_HARDWAREID = 1
SPDRP_FRIENDLYNAME = 12


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("ClassGuid", GUID), ("DevInst", w.DWORD), ("Reserved", ctypes.c_size_t)]


class DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", w.ULONG)]


def _bytes8(*values):
    return (w.BYTE * 8)(*[b - 256 if b > 127 else b for b in values])


# {83DA6326-97A6-4088-9453-A1923F573B29} 15: device is currently connected (Bluetooth device node).
DEVPKEY_IS_CONNECTED = DEVPROPKEY(GUID(0x83DA6326, 0x97A6, 0x4088, _bytes8(0x94, 0x53, 0xA1, 0x92, 0x3F, 0x57, 0x3B, 0x29)), 15)
# {A45C254E-DF1C-4EFD-8020-67D146A850E0} 14: DEVPKEY_Device_FriendlyName.
DEVPKEY_FRIENDLY_NAME = DEVPROPKEY(GUID(0xA45C254E, 0xDF1C, 0x4EFD, _bytes8(0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0)), 14)

setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, w.LPCWSTR, w.HWND, w.DWORD]
setupapi.SetupDiEnumDeviceInfo.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [w.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), w.LPWSTR, w.DWORD, ctypes.POINTER(w.DWORD)]
setupapi.SetupDiGetDevicePropertyW.argtypes = [w.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), ctypes.POINTER(DEVPROPKEY),
                                               ctypes.POINTER(w.ULONG), ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), w.DWORD]
setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [w.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), w.DWORD, ctypes.POINTER(w.DWORD),
                                                       ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]


def _utf16_text(raw):
    return raw.decode("utf-16-le", "ignore").split("\x00")[0].strip() or None


def _property_string(devs, info, key):
    """A string-typed DEVPKEY property, or None."""
    ptype, size = w.ULONG(), w.DWORD()
    setupapi.SetupDiGetDevicePropertyW(devs, ctypes.byref(info), ctypes.byref(key), ctypes.byref(ptype), None, 0, ctypes.byref(size), 0)
    if not 0 < size.value <= 4096:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if not setupapi.SetupDiGetDevicePropertyW(devs, ctypes.byref(info), ctypes.byref(key), ctypes.byref(ptype), buf, size.value, None, 0):
        return None
    return _utf16_text(buf.raw[:size.value]) if ptype.value == DEVPROP_TYPE_STRING else None


def _registry_string(devs, info, prop):
    """A SPDRP_* registry property as text (first string if it is a multi-string), or None."""
    size = w.DWORD()
    setupapi.SetupDiGetDeviceRegistryPropertyW(devs, ctypes.byref(info), prop, None, None, 0, ctypes.byref(size))
    if not 0 < size.value <= 4096:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if not setupapi.SetupDiGetDeviceRegistryPropertyW(devs, ctypes.byref(info), prop, None, buf, size.value, None):
        return None
    return _utf16_text(buf.raw[:size.value])


def bluetooth_scan(with_names=False):
    """Return {product_id: {"connected": bool, "address": "AA:BB:CC:DD:EE:FF", "name": str|None}} for paired Apple devices we know about.
    Names are only read when asked for (they change rarely)."""
    devs = setupapi.SetupDiGetClassDevsW(None, "BTHENUM", None, DIGCF_ALLCLASSES)
    pid_to_addr, addr_connected, addr_name = {}, {}, {}
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
                if with_names:
                    addr_name[m.group(1)] = (_property_string(devs, info, DEVPKEY_FRIENDLY_NAME)
                                             or _registry_string(devs, info, SPDRP_FRIENDLYNAME))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devs)
    return {pid: {"connected": addr_connected.get(addr, False), "name": addr_name.get(addr),
                  "address": ":".join(addr[k:k + 2] for k in range(0, 12, 2))}
            for pid, addr in pid_to_addr.items() if pid in DEVICE_NAMES}


def bluetooth_connection_states():
    """Return {product_id: connected_bool} for paired Apple devices we know about."""
    return {pid: d["connected"] for pid, d in bluetooth_scan().items()}


def usb_scan(with_details=False):
    """Return {product_id: {"serial": str|None, "firmware": str|None}} for Apple devices attached over USB (composite
    device node present). The serial is the instance id suffix; Windows invents one containing '&' for devices that
    report none, which is not a serial and is skipped. Firmware is the REV_xxxx of the hardware id."""
    devs = setupapi.SetupDiGetClassDevsW(None, "USB", None, DIGCF_ALLCLASSES | DIGCF_PRESENT)
    found = {}
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
            m = re.match(r"USB\\VID_05AC&PID_([0-9A-F]{4})\\(.*)", buf.value, re.I)
            if not m or int(m.group(1), 16) not in DEVICE_NAMES:
                continue
            detail = {"serial": None, "firmware": None}
            if with_details:
                if m.group(2) and "&" not in m.group(2):
                    detail["serial"] = m.group(2)
                rev = re.search(r"REV_([0-9A-F]{4})", _registry_string(devs, info, SPDRP_HARDWAREID) or "", re.I)
                if rev:
                    detail["firmware"] = rev.group(1)
            found[int(m.group(1), 16)] = detail
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devs)
    return found


def usb_connected_pids():
    """Product IDs of Apple devices currently attached over USB (composite device node present)."""
    return set(usb_scan())


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
    """Cheap connection checks on every call; battery re-read at most every BATTERY_MAX_AGE seconds and the slow
    properties (name, address, serial, firmware) at most every INFO_MAX_AGE seconds.

    Cache file battery.local.json: {"version": 2, "battery": {pid: [percent, flags, read_at]},
    "lastSeen": {pid: epoch}, "info": {pid: {name, address, serial, firmware}}}. The original format was just the
    battery map ({pid: [percent, flags, read_at]}); both are read."""

    def __init__(self):
        self.lock = threading.Lock()
        self.battery = {}       # pid -> (percent, flags, read_at); last Bluetooth reading
        self.last_seen = {}     # pid -> epoch seconds the device was last seen connected (Bluetooth or USB)
        self.info = {}          # pid -> {"name", "address", "serial", "firmware"}
        self.info_at = 0
        self.saved_at = 0
        self.was_connected = {}  # Bluetooth state, to re-read the battery right after a reconnect
        self.was_online = {}     # Bluetooth-or-USB state, for connected -> disconnected transitions
        self._load()

    def _load(self):
        try:
            saved = json.loads(BATTERY_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            return
        def reading(v):
            if not isinstance(v, (list, tuple)) or len(v) != 3:
                raise ValueError(v)
            return (int(v[0]), int(v[1]), float(v[2]))

        sections = (("battery", saved.get("battery") if "version" in saved else saved, self.battery, reading),
                    ("lastSeen", saved.get("lastSeen"), self.last_seen, float),
                    ("info", saved.get("info"), self.info, dict))
        for _, source, target, convert in sections:
            if not isinstance(source, dict):
                continue
            for pid, value in source.items():
                try:
                    target[int(pid)] = convert(value)
                except (TypeError, ValueError):
                    pass

    def _save(self):
        data = {"version": 2, "battery": {str(p): list(v) for p, v in self.battery.items()},
                "lastSeen": {str(p): t for p, t in self.last_seen.items()},
                "info": {str(p): v for p, v in self.info.items()}}
        try:
            tmp = BATTERY_CACHE.with_name(BATTERY_CACHE.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(BATTERY_CACHE)
        except OSError:
            pass
        self.saved_at = time.time()

    def status(self):
        with self.lock:
            now = time.time()
            refresh_info = now - self.info_at > INFO_MAX_AGE
            scan = bluetooth_scan(with_names=refresh_info)
            usb = usb_scan(with_details=refresh_info)
            bluetooth = {pid: d["connected"] for pid, d in scan.items()}
            dirty = False
            if refresh_info:
                self.info_at = now
                for pid in set(scan) | set(usb):
                    old = self.info.get(pid, {})
                    bt, wired = scan.get(pid, {}), usb.get(pid, {})
                    new = {key: source.get(key) or old.get(key)
                           for key, source in (("name", bt), ("address", bt), ("serial", wired), ("firmware", wired))}
                    if new != old:
                        self.info[pid] = new
                        dirty = True
            stale = [pid for pid, on in bluetooth.items()
                     if on and (pid not in self.battery or now - self.battery[pid][2] > BATTERY_MAX_AGE
                                or not self.was_connected.get(pid))]  # re-read right after a reconnect
            if stale:
                for pid, (percent, flags) in read_battery_reports().items():
                    if bluetooth.get(pid):
                        if self.battery.get(pid, (None, None, None))[:2] != (percent, flags):
                            dirty = True  # persist only real changes; reads happen every BATTERY_MAX_AGE seconds
                        self.battery[pid] = (percent, flags, now)
            online = {pid: bool(bluetooth.get(pid)) or pid in usb for pid in set(bluetooth) | set(usb)}
            for pid, on in online.items():
                if on:
                    self.last_seen[pid] = now
                elif self.was_online.get(pid):
                    dirty = True  # just disconnected: lastSeen holds the last connected poll, persist it now
            if dirty or (any(online.values()) and now - self.saved_at > SAVE_INTERVAL):
                self._save()
            self.was_connected = bluetooth
            self.was_online = online
            out = []
            for pid, name in DEVICE_NAMES.items():
                if pid not in bluetooth and pid not in usb:
                    continue  # neither paired nor plugged in
                transport = "usb" if pid in usb else "bluetooth" if bluetooth.get(pid) else None
                percent, flags, read_at = self.battery.get(pid, (None, None, None))
                info = self.info.get(pid, {})
                # Wired devices charge from the cable; Bluetooth devices report power/charging in the status byte.
                charging = transport == "usb" or (transport == "bluetooth" and bool((flags or 0) & (FLAG_EXTERNAL_POWER | FLAG_CHARGING)))
                out.append({"device": name, "connected": transport is not None, "transport": transport,
                            "charging": charging, "percent": percent, "flags": flags, "read_at": read_at,
                            "percentLive": transport == "bluetooth",
                            "id": DEVICE_IDS[pid], "pid": pid, "name": info.get("name") or name,
                            "address": info.get("address"), "serial": info.get("serial"),
                            "firmware": info.get("firmware"), "lastSeen": self.last_seen.get(pid)})
            return out
