"""keyprobe: interactive probe showing what each key press looks like to Windows.

Usage:
    python keyprobe.py --list            # list Magic Keyboard HID collections, caps, openability
    python keyprobe.py 60                # capture for 60 seconds while you press keys
    python keyprobe.py 60 --pid 0x0320   # (default PID 0x0320)

Sources logged concurrently (all pass-through, no keys are swallowed):
    RI-KBD   Raw Input keyboard (usage page 1 / usage 6, INPUTSINK) with device name
    RI-HID   Raw Input HID, consumer page 0x0C and vendor pages 0xFF00 / 0xFF01 (PAGEONLY)
    READ     Direct ReadFile on every collection Windows lets us open for read
    LL       WH_KEYBOARD_LL hook (vkCode / scanCode / flags / injected)

No driver or elevation needed.
"""

import argparse
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes as w

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hidprobe as hp  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = hp.kernel32

LRESULT = ctypes.c_ssize_t
WM_INPUT, WM_QUIT, WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x00FF, 0x0012, 0x100, 0x101, 0x104, 0x105
RIM_TYPEMOUSE, RIM_TYPEKEYBOARD, RIM_TYPEHID = 0, 1, 2
RID_INPUT = 0x10000003
RIDI_DEVICENAME = 0x20000007
RIDEV_PAGEONLY, RIDEV_INPUTSINK = 0x20, 0x100
HWND_MESSAGE = -3
WH_KEYBOARD_LL = 13
SYNC_GAP = 0.25  # seconds; events closer than this are grouped in the summary

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, w.WPARAM, w.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", w.HANDLE), ("hIcon", w.HANDLE), ("hCursor", w.HANDLE), ("hbrBackground", w.HANDLE),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", w.USHORT), ("usUsage", w.USHORT), ("dwFlags", w.DWORD), ("hwndTarget", w.HWND)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", w.DWORD), ("scanCode", w.DWORD), ("flags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", w.HWND), ("message", w.UINT), ("wParam", w.WPARAM), ("lParam", w.LPARAM),
                ("time", w.DWORD), ("pt", w.POINT)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.restype = w.HWND
user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, w.HWND, w.HANDLE, w.HANDLE, ctypes.c_void_p]
user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), w.UINT, w.UINT]
user32.GetRawInputData.argtypes = [w.HANDLE, w.UINT, ctypes.c_void_p, ctypes.POINTER(w.UINT), w.UINT]
user32.GetRawInputDeviceInfoW.argtypes = [w.HANDLE, w.UINT, ctypes.c_void_p, ctypes.POINTER(w.UINT)]
user32.SetWindowsHookExW.restype = w.HANDLE
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, w.HANDLE, w.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [w.HANDLE, ctypes.c_int, w.WPARAM, w.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [w.HANDLE]
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), w.HWND, w.UINT, w.UINT]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
user32.DestroyWindow.argtypes = [w.HWND]
kernel32.GetModuleHandleW.restype = w.HANDLE
kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
kernel32.CancelIoEx.argtypes = [w.HANDLE, ctypes.c_void_p]

# ---------------------------------------------------------------- VK names
VK_NAMES = {0x08: "BACK", 0x09: "TAB", 0x0D: "RETURN", 0x10: "SHIFT", 0x11: "CONTROL", 0x12: "MENU", 0x13: "PAUSE",
            0x14: "CAPITAL", 0x1B: "ESCAPE", 0x20: "SPACE", 0x21: "PRIOR(PgUp)", 0x22: "NEXT(PgDn)", 0x23: "END",
            0x24: "HOME", 0x25: "LEFT", 0x26: "UP", 0x27: "RIGHT", 0x28: "DOWN", 0x2C: "SNAPSHOT(PrtScn)",
            0x2D: "INSERT", 0x2E: "DELETE", 0x5B: "LWIN", 0x5C: "RWIN", 0x5D: "APPS", 0x5F: "SLEEP",
            0x90: "NUMLOCK", 0x91: "SCROLL", 0xA0: "LSHIFT", 0xA1: "RSHIFT", 0xA2: "LCONTROL", 0xA3: "RCONTROL",
            0xA4: "LMENU", 0xA5: "RMENU", 0xAD: "VOLUME_MUTE", 0xAE: "VOLUME_DOWN", 0xAF: "VOLUME_UP",
            0xB0: "MEDIA_NEXT", 0xB1: "MEDIA_PREV", 0xB2: "MEDIA_STOP", 0xB3: "MEDIA_PLAY_PAUSE",
            0xB4: "LAUNCH_MAIL", 0xB5: "LAUNCH_MEDIA", 0xBA: "OEM_1(;)", 0xBB: "OEM_PLUS", 0xBC: "OEM_COMMA",
            0xBD: "OEM_MINUS", 0xBE: "OEM_PERIOD", 0xBF: "OEM_2(/)", 0xC0: "OEM_3(`)", 0xDB: "OEM_4([)",
            0xDC: "OEM_5(\\)", 0xDD: "OEM_6(])", 0xDE: "OEM_7(')", 0xE2: "OEM_102(<>)", 0xFF: "VK_FF(fake/none)"}
for _i in range(24):
    VK_NAMES[0x70 + _i] = f"F{_i + 1}"
for _i in range(10):
    VK_NAMES[0x30 + _i] = str(_i)
    VK_NAMES[0x60 + _i] = f"NUMPAD{_i}"
for _i in range(26):
    VK_NAMES[0x41 + _i] = chr(0x41 + _i)
MSG_NAMES = {WM_KEYDOWN: "KEYDOWN", WM_KEYUP: "KEYUP", WM_SYSKEYDOWN: "SYSKEYDOWN", WM_SYSKEYUP: "SYSKEYUP"}


def vk_name(vk):
    return VK_NAMES.get(vk, f"VK_0x{vk:02X}")


# ---------------------------------------------------------------- event log
START = time.perf_counter()
LOCK = threading.Lock()
EVENTS = []  # (t, source, device_tag, text)
VENDOR_REPORTS = []  # (t, source, text)


def log(source, tag, text, vendor=False):
    t = time.perf_counter() - START
    with LOCK:
        EVENTS.append((t, source, tag, text))
        if vendor:
            VENDOR_REPORTS.append((t, source, text))
        print(f"[{t:8.3f}] {source:7} {tag:8} {text}", flush=True)


class Matcher:
    def __init__(self, pid):
        self.needles = (f"vid_05ac&pid_{pid:04x}", f"0001004c&pid_{pid:04x}", f"vid&0001004c_pid&{pid:04x}",
                        f"vid_004c&pid_{pid:04x}")

    def is_magic(self, name):
        n = name.lower()
        return any(x in n for x in self.needles) or f"pid&{int(self.needles[0][-4:], 16):04x}" in n and "004c" in n


_name_cache = {}


def device_name(handle, matcher):
    if handle in _name_cache:
        return _name_cache[handle]
    size = w.UINT(0)
    user32.GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, None, ctypes.byref(size))
    buf = ctypes.create_unicode_buffer(size.value + 2)
    user32.GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, buf, ctypes.byref(size))
    name = buf.value or "?"
    tag = "MAGIC" if matcher.is_magic(name) else "other"
    _name_cache[handle] = (tag, name)
    return tag, name


# ---------------------------------------------------------------- raw input
def handle_raw_input(lparam, matcher):
    size = w.UINT(0)
    hdr = ctypes.sizeof(ctypes.c_void_p) * 2 + 8  # RAWINPUTHEADER
    user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), hdr)
    buf = ctypes.create_string_buffer(size.value)
    if user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size), hdr) == 0xFFFFFFFF:
        return
    raw = buf.raw
    rtype = int.from_bytes(raw[0:4], "little")
    handle = int.from_bytes(raw[8:8 + ctypes.sizeof(ctypes.c_void_p)], "little")
    tag, name = device_name(handle, matcher)
    d = hdr
    if rtype == RIM_TYPEKEYBOARD:
        make, flags, _res, vk, msg = (int.from_bytes(raw[d + o:d + o + n], "little") for o, n in ((0, 2), (2, 2), (4, 2), (6, 2), (8, 4)))
        fl = []
        fl.append("BREAK" if flags & 1 else "MAKE")
        if flags & 2:
            fl.append("E0")
        if flags & 4:
            fl.append("E1")
        log("RI-KBD", tag, f"make=0x{make:04X} flags=0x{flags:02X}({'|'.join(fl)}) vk=0x{vk:02X} {vk_name(vk)} "
            f"msg={MSG_NAMES.get(msg, hex(msg))}  dev={name}")
    elif rtype == RIM_TYPEHID:
        item = int.from_bytes(raw[d:d + 4], "little")
        count = int.from_bytes(raw[d + 4:d + 8], "little")
        for i in range(count):
            rep = raw[d + 8 + i * item:d + 8 + (i + 1) * item]
            page_hint = "consumer?" if rep[:1] in (b"\x02", b"\x03") else ""
            log("RI-HID", tag, f"rid=0x{rep[0]:02X} {rep.hex(' ')} {page_hint}  dev={name}", vendor=True)


def run_message_loop(seconds, matcher):
    """Creates the message-only window, registers Raw Input, installs the LL hook, pumps messages."""
    wndproc_holder = []

    def wndproc(hwnd, msg, wp, lp):
        if msg == WM_INPUT:
            try:
                handle_raw_input(lp, matcher)
            except Exception as e:  # never let an exception cross the callback boundary
                print(f"RI error: {e!r}", flush=True)
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    proc = WNDPROC(wndproc)
    wndproc_holder.append(proc)
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.lpszClassName = "KeyProbeWnd"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, "KeyProbeWnd", "keyprobe", 0, 0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
    if not hwnd:
        print(f"CreateWindowEx failed: {ctypes.get_last_error()}")
        return

    regs = [(1, 6, RIDEV_INPUTSINK, "keyboard 1/6"),
            (0x0C, 0, RIDEV_INPUTSINK | RIDEV_PAGEONLY, "consumer page 0x0C"),
            (0xFF00, 0, RIDEV_INPUTSINK | RIDEV_PAGEONLY, "vendor page 0xFF00"),
            (0xFF01, 0, RIDEV_INPUTSINK | RIDEV_PAGEONLY, "vendor page 0xFF01")]
    for page, usage, flags, label in regs:
        rd = RAWINPUTDEVICE(page, usage, flags, hwnd)
        ok = user32.RegisterRawInputDevices(ctypes.byref(rd), 1, ctypes.sizeof(rd))
        print(f"RegisterRawInputDevices {label}: {'ok' if ok else 'FAILED err ' + str(ctypes.get_last_error())}")

    def hook(code, wp, lp):
        try:
            if code >= 0:
                k = ctypes.cast(lp, ctypes.POINTER(KBDLLHOOKSTRUCT))[0]
                f = k.flags
                fl = [n for bit, n in ((0x01, "EXT"), (0x10, "LOWER_IL_INJ"), (0x02, "INJECTED"), (0x20, "ALTDOWN"), (0x80, "UP")) if f & bit]
                log("LL", "-", f"vk=0x{k.vkCode:02X} {vk_name(k.vkCode)} scan=0x{k.scanCode:02X} flags=0x{f:02X}({'|'.join(fl) or '-'}) "
                    f"{'UP' if f & 0x80 else 'DOWN'} injected={'yes' if f & 0x03 else 'no'}")
        except Exception as e:
            print(f"LL error: {e!r}", flush=True)
        return user32.CallNextHookEx(None, code, wp, lp)

    hook_proc = HOOKPROC(hook)
    hhook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook_proc, hinst, 0)
    print(f"WH_KEYBOARD_LL hook: {'ok' if hhook else 'FAILED err ' + str(ctypes.get_last_error())}")

    tid = kernel32.GetCurrentThreadId()
    timer = threading.Timer(seconds, lambda: user32.PostThreadMessageW(tid, WM_QUIT, 0, 0))
    timer.daemon = True
    timer.start()
    msg = MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.DispatchMessageW(ctypes.byref(msg))
    if hhook:
        user32.UnhookWindowsHookEx(hhook)
    user32.DestroyWindow(hwnd)


# ---------------------------------------------------------------- HID collections
def enumerate_collections(pid):
    out = []
    for path in hp.hid_interface_paths():
        h = kernel32.CreateFileW(path, 0, hp.FILE_SHARE_READ_WRITE, None, hp.OPEN_EXISTING, 0, None)
        if h == hp.INVALID_HANDLE_VALUE:
            continue
        attrs = hp.HIDD_ATTRIBUTES(Size=ctypes.sizeof(hp.HIDD_ATTRIBUTES))
        if not hp.hid.HidD_GetAttributes(h, ctypes.byref(attrs)) or attrs.VendorID not in hp.APPLE_VIDS or attrs.ProductID != pid:
            kernel32.CloseHandle(h)
            continue
        name = ctypes.create_unicode_buffer(128)
        hp.hid.HidD_GetProductString(h, name, ctypes.sizeof(name))
        pp = ctypes.c_void_p()
        caps = hp.HIDP_CAPS()
        if hp.hid.HidD_GetPreparsedData(h, ctypes.byref(pp)):
            hp.hid.HidP_GetCaps(pp, ctypes.byref(caps))
        kernel32.CloseHandle(h)
        out.append({"path": path, "attrs": attrs, "name": name.value, "caps": caps})
    return out


def list_collections(pid):
    cols = enumerate_collections(pid)
    if not cols:
        print(f"No HID collections found for Apple PID 0x{pid:04X}.")
        return cols
    for c in cols:
        a, caps = c["attrs"], c["caps"]
        hr = kernel32.CreateFileW(c["path"], hp.GENERIC_READ, hp.FILE_SHARE_READ_WRITE, None, hp.OPEN_EXISTING, 0, None)
        readable = hr != hp.INVALID_HANDLE_VALUE
        err = 0 if readable else ctypes.get_last_error()
        if readable:
            kernel32.CloseHandle(hr)
        print(f"\n== {a.VendorID:04X}:{a.ProductID:04X} {c['name'] or '?'} [page 0x{caps.UsagePage:04X} usage 0x{caps.Usage:02X}]")
        print(f"   path: {c['path']}")
        print(f"   openable for read: {'YES' if readable else f'NO (win32 error {err}; Windows-owned)'}; "
              f"report bytes in/out/feature = {caps.InputReportByteLength}/{caps.OutputReportByteLength}/{caps.FeatureReportByteLength}")
        # Reuse hidprobe's cap dumper (needs a query-capable handle).
        hq = kernel32.CreateFileW(c["path"], 0, hp.FILE_SHARE_READ_WRITE, None, hp.OPEN_EXISTING, 0, None)
        if hq != hp.INVALID_HANDLE_VALUE:
            pp = ctypes.c_void_p()
            if hp.hid.HidD_GetPreparsedData(hq, ctypes.byref(pp)):
                hp.dump_caps(pp, caps)
                hp.hid.HidD_FreePreparsedData(pp)
            kernel32.CloseHandle(hq)
    return cols


def start_readers(pid):
    handles = []
    for c in enumerate_collections(pid):
        caps = c["caps"]
        label = f"UP{caps.UsagePage:04X}/U{caps.Usage:02X}"
        h = kernel32.CreateFileW(c["path"], hp.GENERIC_READ, hp.FILE_SHARE_READ_WRITE, None, hp.OPEN_EXISTING, 0, None)
        if h == hp.INVALID_HANDLE_VALUE or not caps.InputReportByteLength:
            why = "no input reports" if h != hp.INVALID_HANDLE_VALUE else f"cannot open for read (err {ctypes.get_last_error()})"
            if h != hp.INVALID_HANDLE_VALUE:
                kernel32.CloseHandle(h)
            print(f"READ skip {label}: {why}  {c['path']}")
            continue
        print(f"READ open {label} ({caps.InputReportByteLength} bytes)  {c['path']}")
        vendor = caps.UsagePage >= 0xFF00
        handles.append(h)

        def reader(h=h, size=caps.InputReportByteLength, label=label, vendor=vendor):
            buf = ctypes.create_string_buffer(size)
            got = w.DWORD()
            while kernel32.ReadFile(h, buf, size, ctypes.byref(got), None):
                data = buf.raw[:got.value]
                log("READ", label, f"rid=0x{data[0]:02X} {data.hex(' ')}", vendor=vendor)

        threading.Thread(target=reader, daemon=True).start()
    return handles


# ---------------------------------------------------------------- summary
def summarize():
    print("\n================ SUMMARY ================")
    with LOCK:
        events = list(EVENTS)
    if not events:
        print("No events captured.")
        return
    groups, cur = [], [events[0]]
    for e in events[1:]:
        if e[0] - cur[-1][0] <= SYNC_GAP:
            cur.append(e)
        else:
            groups.append(cur)
            cur = [e]
    groups.append(cur)
    print(f"{len(events)} events in {len(groups)} cluster(s) (events within {SYNC_GAP}s grouped)\n")
    for g in groups:
        srcs = {}
        for _, s, tag, _ in g:
            key = s + (f"[{tag}]" if s in ("RI-KBD", "RI-HID", "READ") else "")
            srcs[key] = srcs.get(key, 0) + 1
        keys = []
        for _, s, _, text in g:
            if s == "LL" and "DOWN" in text:
                keys.append(text.split(" scan")[0])
            elif s == "RI-KBD" and "MAKE" in text:
                keys.append("RI:" + text.split(" msg")[0].split("vk=")[1])
        print(f"t={g[0][0]:8.3f}  sources: " + ", ".join(f"{k} x{v}" for k, v in sorted(srcs.items())))
        if keys:
            print("            keys: " + "; ".join(dict.fromkeys(keys)))
        if not any(s in ("LL", "RI-KBD") for _, s, _, _ in g):
            print("            NOTE: HID-only activity (no keyboard event) - key invisible to normal keyboard path")
    print("\nSources that saw at least one event: " + ", ".join(sorted({e[1] for e in events})))
    with LOCK:
        vend = list(VENDOR_REPORTS)
    if vend:
        print(f"Vendor/HID-level reports seen: {len(vend)} (candidates for Fn state; compare with key presses):")
        for t, s, text in vend[:40]:
            print(f"   [{t:8.3f}] {s} {text}")
    else:
        print("No vendor-page or raw HID reports arrived: Fn state was NOT visible to any user-mode source.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("seconds", nargs="?", type=float, default=0, help="capture duration")
    p.add_argument("--list", action="store_true", help="list Magic Keyboard HID collections and exit")
    p.add_argument("--pid", type=lambda s: int(s, 0), default=0x0320)
    a = p.parse_args()
    if a.list or not a.seconds:
        list_collections(a.pid)
        if not a.list:
            p.print_help()
        return
    matcher = Matcher(a.pid)
    print(f"keyprobe: capturing {a.seconds}s. 'MAGIC' tag = event from PID 0x{a.pid:04X}. Press keys per KEYPROBE.md.\n")
    handles = start_readers(a.pid)
    global START
    START = time.perf_counter()
    run_message_loop(a.seconds, matcher)
    for h in handles:
        kernel32.CancelIoEx(h, None)
    time.sleep(0.2)
    summarize()


if __name__ == "__main__":
    main()
