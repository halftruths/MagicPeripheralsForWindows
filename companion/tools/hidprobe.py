"""hidprobe: dump report layouts of Apple HID collections and log live reports.

Usage:
    python hidprobe.py              # dump capabilities of every Apple HID collection
    python hidprobe.py --listen 20  # also log raw input reports for 20 seconds

No driver or elevation needed. Collections that Windows opens exclusively
(keyboard/mouse) can still be queried for capabilities but not read.
"""

import argparse
import ctypes
import threading
import time
from ctypes import wintypes as w

APPLE_VIDS = {0x004C, 0x05AC}

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ_WRITE = 0x03
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = w.HANDLE(-1).value
HIDP_STATUS_SUCCESS = 0x00110000
HidP_Input, HidP_Output, HidP_Feature = 0, 1, 2


class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", w.BYTE * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("InterfaceClassGuid", GUID), ("Flags", w.DWORD), ("Reserved", ctypes.c_size_t)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", w.ULONG), ("VendorID", w.USHORT), ("ProductID", w.USHORT), ("VersionNumber", w.USHORT)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", w.USHORT), ("UsagePage", w.USHORT),
        ("InputReportByteLength", w.USHORT), ("OutputReportByteLength", w.USHORT),
        ("FeatureReportByteLength", w.USHORT), ("Reserved", w.USHORT * 17),
        ("NumberLinkCollectionNodes", w.USHORT),
        ("NumberInputButtonCaps", w.USHORT), ("NumberInputValueCaps", w.USHORT), ("NumberInputDataIndices", w.USHORT),
        ("NumberOutputButtonCaps", w.USHORT), ("NumberOutputValueCaps", w.USHORT), ("NumberOutputDataIndices", w.USHORT),
        ("NumberFeatureButtonCaps", w.USHORT), ("NumberFeatureValueCaps", w.USHORT), ("NumberFeatureDataIndices", w.USHORT),
    ]


class _UsageRange(ctypes.Structure):
    # Range and NotRange share this layout; for NotRange, UsageMin is Usage and DataIndexMin is DataIndex.
    _fields_ = [
        ("UsageMin", w.USHORT), ("UsageMax", w.USHORT), ("StringMin", w.USHORT), ("StringMax", w.USHORT),
        ("DesignatorMin", w.USHORT), ("DesignatorMax", w.USHORT), ("DataIndexMin", w.USHORT), ("DataIndexMax", w.USHORT),
    ]


class HIDP_VALUE_CAPS(ctypes.Structure):
    _fields_ = [
        ("UsagePage", w.USHORT), ("ReportID", ctypes.c_ubyte), ("IsAlias", ctypes.c_ubyte),
        ("BitField", w.USHORT), ("LinkCollection", w.USHORT), ("LinkUsage", w.USHORT), ("LinkUsagePage", w.USHORT),
        ("IsRange", ctypes.c_ubyte), ("IsStringRange", ctypes.c_ubyte), ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte), ("HasNull", ctypes.c_ubyte), ("Reserved", ctypes.c_ubyte),
        ("BitSize", w.USHORT), ("ReportCount", w.USHORT), ("Reserved2", w.USHORT * 5),
        ("UnitsExp", w.ULONG), ("Units", w.ULONG),
        ("LogicalMin", w.LONG), ("LogicalMax", w.LONG), ("PhysicalMin", w.LONG), ("PhysicalMax", w.LONG),
        ("u", _UsageRange),
    ]


class HIDP_BUTTON_CAPS(ctypes.Structure):
    _fields_ = [
        ("UsagePage", w.USHORT), ("ReportID", ctypes.c_ubyte), ("IsAlias", ctypes.c_ubyte),
        ("BitField", w.USHORT), ("LinkCollection", w.USHORT), ("LinkUsage", w.USHORT), ("LinkUsagePage", w.USHORT),
        ("IsRange", ctypes.c_ubyte), ("IsStringRange", ctypes.c_ubyte), ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte), ("ReportCount", w.USHORT), ("Reserved2", w.USHORT),
        ("Reserved", w.ULONG * 9), ("u", _UsageRange),
    ]


hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
setupapi.SetupDiGetClassDevsW.restype = w.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), w.LPCWSTR, w.HWND, w.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), w.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [w.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [w.HANDLE]
kernel32.CreateFileW.restype = w.HANDLE
kernel32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
kernel32.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [w.HANDLE]
hid.HidD_GetAttributes.argtypes = [w.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetPreparsedData.argtypes = [w.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidD_GetProductString.argtypes = [w.HANDLE, ctypes.c_void_p, w.ULONG]
hid.HidD_GetFeature.argtypes = [w.HANDLE, ctypes.c_void_p, w.ULONG]
hid.HidD_GetInputReport.argtypes = [w.HANDLE, ctypes.c_void_p, w.ULONG]
hid.HidP_GetCaps.restype = w.LONG
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid.HidP_GetValueCaps.restype = w.LONG
hid.HidP_GetValueCaps.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(w.USHORT), ctypes.c_void_p]
hid.HidP_GetButtonCaps.restype = w.LONG
hid.HidP_GetButtonCaps.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(w.USHORT), ctypes.c_void_p]


def hid_interface_paths():
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    devs = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    try:
        index = 0
        while True:
            iface = SP_DEVICE_INTERFACE_DATA(cbSize=ctypes.sizeof(SP_DEVICE_INTERFACE_DATA))
            if not setupapi.SetupDiEnumDeviceInterfaces(devs, None, ctypes.byref(guid), index, ctypes.byref(iface)):
                return
            index += 1
            needed = w.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(iface), None, 0, ctypes.byref(needed), None)
            buf = ctypes.create_string_buffer(needed.value)
            # cbSize of SP_DEVICE_INTERFACE_DETAIL_DATA_W: 8 on 64-bit, 6 on 32-bit.
            ctypes.cast(buf, ctypes.POINTER(w.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(iface), buf, needed, None, None):
                yield ctypes.wstring_at(ctypes.addressof(buf) + 4)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devs)


def open_device(path):
    """Try read/write access first, then query-only access (works on exclusively-owned collections)."""
    for access in (GENERIC_READ | GENERIC_WRITE, GENERIC_READ, 0):
        h = kernel32.CreateFileW(path, access, FILE_SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None)
        if h != INVALID_HANDLE_VALUE:
            return h, access
    return None, None


def describe_access(access):
    if access & GENERIC_WRITE:
        return "read/write"
    if access & GENERIC_READ:
        return "read-only"
    return "query-only (owned by Windows)"


def usage_text(page, cap):
    u = cap.u
    usage = f"0x{u.UsageMin:02X}-0x{u.UsageMax:02X}" if cap.IsRange else f"0x{u.UsageMin:02X}"
    return f"page 0x{page:04X} usage {usage}"


def dump_caps(pp, caps):
    for rtype, name, nbtn, nval in (
        (HidP_Input, "Input", caps.NumberInputButtonCaps, caps.NumberInputValueCaps),
        (HidP_Output, "Output", caps.NumberOutputButtonCaps, caps.NumberOutputValueCaps),
        (HidP_Feature, "Feature", caps.NumberFeatureButtonCaps, caps.NumberFeatureValueCaps),
    ):
        if nbtn:
            arr = (HIDP_BUTTON_CAPS * nbtn)()
            n = w.USHORT(nbtn)
            if hid.HidP_GetButtonCaps(rtype, arr, ctypes.byref(n), pp) == HIDP_STATUS_SUCCESS:
                for c in arr[: n.value]:
                    print(f"    {name:7} button  report 0x{c.ReportID:02X}  {usage_text(c.UsagePage, c)}  link 0x{c.LinkUsagePage:04X}/0x{c.LinkUsage:02X}")
        if nval:
            arr = (HIDP_VALUE_CAPS * nval)()
            n = w.USHORT(nval)
            if hid.HidP_GetValueCaps(rtype, arr, ctypes.byref(n), pp) == HIDP_STATUS_SUCCESS:
                for c in arr[: n.value]:
                    print(f"    {name:7} value   report 0x{c.ReportID:02X}  {usage_text(c.UsagePage, c)}  "
                          f"{c.BitSize}b x{c.ReportCount}  logical {c.LogicalMin}..{c.LogicalMax}  link 0x{c.LinkUsagePage:04X}/0x{c.LinkUsage:02X}")


def try_get_feature_reports(h, caps):
    """Read each declared feature report once; useful for finding battery/config data."""
    if not caps.FeatureReportByteLength:
        return
    ids = set()
    for rtype_count, getter, cls in (
        (caps.NumberFeatureButtonCaps, hid.HidP_GetButtonCaps, HIDP_BUTTON_CAPS),
        (caps.NumberFeatureValueCaps, hid.HidP_GetValueCaps, HIDP_VALUE_CAPS),
    ):
        if rtype_count:
            arr = (cls * rtype_count)()
            n = w.USHORT(rtype_count)
            if getter(HidP_Feature, arr, ctypes.byref(n), try_get_feature_reports.pp) == HIDP_STATUS_SUCCESS:
                ids.update(c.ReportID for c in arr[: n.value])
    for rid in sorted(ids):
        buf = ctypes.create_string_buffer(caps.FeatureReportByteLength)
        buf[0] = rid
        if hid.HidD_GetFeature(h, buf, len(buf)):
            print(f"    GET feature 0x{rid:02X}: {buf.raw.hex(' ')}")
        else:
            print(f"    GET feature 0x{rid:02X}: failed (error {ctypes.get_last_error()})")


def probe(listen_seconds):
    readers = []
    for path in hid_interface_paths():
        h, access = open_device(path)
        if not h:
            continue
        attrs = HIDD_ATTRIBUTES(Size=ctypes.sizeof(HIDD_ATTRIBUTES))
        if not hid.HidD_GetAttributes(h, ctypes.byref(attrs)) or attrs.VendorID not in APPLE_VIDS:
            kernel32.CloseHandle(h)
            continue
        name = ctypes.create_unicode_buffer(128)
        hid.HidD_GetProductString(h, name, ctypes.sizeof(name))
        pp = ctypes.c_void_p()
        caps = HIDP_CAPS()
        if hid.HidD_GetPreparsedData(h, ctypes.byref(pp)):
            hid.HidP_GetCaps(pp, ctypes.byref(caps))
        label = f"{attrs.VendorID:04X}:{attrs.ProductID:04X} {name.value or '?'} [page 0x{caps.UsagePage:04X} usage 0x{caps.Usage:02X}]"
        print(f"\n== {label}")
        print(f"   path: {path}")
        print(f"   access: {describe_access(access)}; report bytes in/out/feature = "
              f"{caps.InputReportByteLength}/{caps.OutputReportByteLength}/{caps.FeatureReportByteLength}")
        if pp:
            dump_caps(pp, caps)
            try_get_feature_reports.pp = pp
            try_get_feature_reports(h, caps)
            hid.HidD_FreePreparsedData(pp)
        if listen_seconds and access & GENERIC_READ and caps.InputReportByteLength:
            readers.append((label, h, caps.InputReportByteLength))
        else:
            kernel32.CloseHandle(h)

    if not listen_seconds:
        return
    if not readers:
        print("\nNo readable Apple collections to listen on.")
        return
    print(f"\nListening on {len(readers)} collection(s) for {listen_seconds}s. Touch the trackpad, press Fn, F-keys...\n")
    start = time.perf_counter()

    def reader(label, h, size):
        buf = ctypes.create_string_buffer(size)
        got = w.DWORD()
        while kernel32.ReadFile(h, buf, size, ctypes.byref(got), None):
            print(f"[{time.perf_counter() - start:7.3f}] {label}: {buf.raw[: got.value].hex(' ')}", flush=True)

    for args in readers:
        threading.Thread(target=reader, args=args, daemon=True).start()
    time.sleep(listen_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--listen", type=float, default=0, metavar="SECONDS", help="log raw input reports for this long")
    probe(parser.parse_args().listen)
