"""System-wide capture of Precision Touchpad frames (Raw Input) and wheel events (low-level mouse hook).

Runs a Win32 message loop on its own thread and hands parsed events to a callback.
"""

import ctypes
import struct
import threading
import time
from ctypes import wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, w.WPARAM, w.LPARAM)

WM_INPUT = 0x00FF
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WM_APP_SET_HOOK = 0x8000 + 1
WM_APP_QUIT = 0x8000 + 2
WH_MOUSE_LL = 14
RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100
RIM_TYPEHID = 2
HWND_MESSAGE = w.HWND(-3)
LLMHF_INJECTED = 0x01

# Layout of the PTP input report produced by AmtPtpHidFilter (see HidMiniport.h, PTP_REPORT).
REPORTID_MULTITOUCH = 0x05
CONTACT = struct.Struct("<BIHHHHHBBBBBB")  # flags, id, x, y, width, height, pressure, size, major, minor, orient, finger, state
REPORT_TAIL = struct.Struct("<HBHB")      # scan time, contact count, mechanical force, button
REPORT_SIZE = 1 + 5 * CONTACT.size + REPORT_TAIL.size

# Logical ranges from the report descriptor (MagicTrackpad2.h).
X_MAX, Y_MAX = 7612, 5065


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", w.USHORT), ("usUsage", w.USHORT), ("dwFlags", w.DWORD), ("hwndTarget", w.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", w.DWORD), ("dwSize", w.DWORD), ("hDevice", w.HANDLE), ("wParam", w.WPARAM)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.UINT), ("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE),
        ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR), ("hIconSm", w.HICON),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", w.POINT), ("mouseData", w.DWORD), ("flags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.CreateWindowExW.restype = w.HWND
user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), w.UINT, w.UINT]
user32.GetRawInputData.argtypes = [w.HANDLE, w.UINT, w.LPVOID, ctypes.POINTER(w.UINT), w.UINT]
user32.SetWindowsHookExW.restype = w.HANDLE
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, w.HINSTANCE, w.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [w.HANDLE, ctypes.c_int, w.WPARAM, w.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [w.HANDLE]
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
user32.DestroyWindow.argtypes = [w.HWND]
kernel32.GetModuleHandleW.restype = w.HMODULE
kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]


def parse_ptp_report(data):
    """Parse one AmtPtpHidFilter PTP report into a frame dict, or None if it isn't one."""
    if len(data) < REPORT_SIZE or data[0] != REPORTID_MULTITOUCH:
        return None
    scan_time, count, force, button = REPORT_TAIL.unpack_from(data, 1 + 5 * CONTACT.size)
    contacts = []
    for i in range(min(count, 5)):
        flags, cid, x, y, wd, ht, pressure, size, major, minor, orient, finger, state = CONTACT.unpack_from(data, 1 + i * CONTACT.size)
        contacts.append({
            "id": cid, "tip": bool(flags & 2), "conf": bool(flags & 1), "x": x, "y": y, "w": wd, "h": ht,
            "p": pressure, "size": size, "major": major, "minor": minor, "orient": orient, "finger": finger, "state": state,
        })
    return {"scan": scan_time, "button": bool(button & 1), "force": force, "contacts": contacts}


class InputCapture:
    """Owns the capture thread. `on_event(dict)` is called from that thread and must be quick."""

    def __init__(self, on_event):
        self.on_event = on_event
        self.hwnd = None
        self._hook = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="input-capture", daemon=True)
        self.error = None

    def start(self):
        self._thread.start()
        self._ready.wait(5)
        if self.error:
            raise RuntimeError(self.error)

    def set_wheel_hook(self, enabled):
        """Install/remove the wheel hook. Done on the capture thread, which owns the hook."""
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_SET_HOOK, 1 if enabled else 0, 0)

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_QUIT, 0, 0)

    # -- capture thread --

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            self._handle_raw_input(lparam)
        elif msg == WM_APP_SET_HOOK:
            self._apply_hook(bool(wparam))
            return 0
        elif msg == WM_APP_QUIT:
            self._apply_hook(False)
            user32.DestroyWindow(hwnd)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle_raw_input(self, lparam):
        size = w.UINT()
        header_size = ctypes.sizeof(RAWINPUTHEADER)
        user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), header_size)
        buf = ctypes.create_string_buffer(size.value)
        if user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size), header_size) != size.value:
            return
        header = RAWINPUTHEADER.from_buffer(buf)
        if header.dwType != RIM_TYPEHID:
            return
        size_hid, count = struct.unpack_from("<II", buf, header_size)
        raw = buf.raw[header_size + 8: header_size + 8 + size_hid * count]
        now = time.perf_counter()
        for i in range(count):
            frame = parse_ptp_report(raw[i * size_hid:(i + 1) * size_hid])
            if frame:
                frame["type"] = "frame"
                frame["t"] = now
                self.on_event(frame)

    def _hook_proc(self, code, wparam, lparam):
        if code >= 0 and wparam in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
            info = MSLLHOOKSTRUCT.from_address(lparam)
            delta = ctypes.c_short(info.mouseData >> 16).value
            self.on_event({
                "type": "wheel", "t": time.perf_counter(), "axis": "v" if wparam == WM_MOUSEWHEEL else "h",
                "delta": delta, "injected": bool(info.flags & LLMHF_INJECTED),
            })
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _apply_hook(self, enabled):
        if enabled and not self._hook:
            self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._hook_cb, kernel32.GetModuleHandleW(None), 0)
        elif not enabled and self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def _run(self):
        # Keep callback objects referenced for the thread's lifetime.
        self._wndproc_cb = WNDPROC(self._wndproc)
        self._hook_cb = HOOKPROC(self._hook_proc)
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._wndproc_cb, hInstance=hinst,
                         lpszClassName="MagicCompanionCapture")
        user32.RegisterClassExW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "MagicCompanion", 0, 0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
        if not self.hwnd:
            self.error = f"CreateWindowExW failed ({ctypes.get_last_error()})"
            self._ready.set()
            return
        # Digitizer / Touch Pad: the Precision Touchpad top-level collection.
        rid = RAWINPUTDEVICE(0x0D, 0x05, RIDEV_INPUTSINK, self.hwnd)
        if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)):
            self.error = f"RegisterRawInputDevices failed ({ctypes.get_last_error()})"
            self._ready.set()
            return
        self._ready.set()
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.DispatchMessageW(ctypes.byref(msg))
