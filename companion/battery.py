"""Battery level of Apple Bluetooth devices via the vendor collection (page 0xFF00 usage 0x14), report 0x90.

Report layout measured on Magic Keyboard (0320) and Magic Trackpad (0324): [0x90, status flags, percent].
The meaning of the status bits is not confirmed yet, so they're passed through raw.
"""

import ctypes
import re

from tools.hidprobe import HIDD_ATTRIBUTES, hid, hid_interface_paths, kernel32, open_device

DEVICE_NAMES = {0x0320: "Magic Keyboard", 0x0324: "Magic Trackpad"}


def read_batteries():
    results = []
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
                results.append({
                    "device": DEVICE_NAMES.get(attrs.ProductID, f"{attrs.ProductID:04X}"),
                    "percent": buf.raw[2], "flags": buf.raw[1],
                })
        finally:
            kernel32.CloseHandle(h)
    return results
