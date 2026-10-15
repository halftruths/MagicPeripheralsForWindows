"""Tray icon, single-instance mutex and graceful --quit for the windowed build. Stdlib/ctypes only.

The tray runs a hidden message window on its own thread. Another process asks it to quit by posting WM_CLOSE to
that window (found by class name), so nothing about quitting is reachable over HTTP.
"""
import ctypes
import sys
import threading
import time
import webbrowser
import winreg
from ctypes import wintypes as w

import paths

CLASS_NAME = "MagicCompanionTrayWindow"
MUTEX_NAME = "Local\\MagicCompanion"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "MagicCompanion"
DEFAULT_PORT = 8765

WM_DESTROY, WM_CLOSE, WM_ENDSESSION, WM_NULL = 0x0002, 0x0010, 0x0016, 0x0000
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_TRAY, WM_CLOSE_TRAY = 0x8001, 0x8002  # WM_APP + 1, + 2
NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MF_STRING, MF_CHECKED, MF_SEPARATOR = 0, 8, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 2, 0x100
ID_OPEN, ID_AUTOSTART, ID_QUIT = 1, 2, 3
ERROR_ALREADY_EXISTS = 183

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT), ("uCallbackMessage", w.UINT),
                ("hIcon", w.HICON), ("szTip", w.WCHAR * 128), ("dwState", w.DWORD), ("dwStateMask", w.DWORD),
                ("szInfo", w.WCHAR * 256), ("uTimeout", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", w.HICON)]


user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
user32.CreateWindowExW.restype = w.HWND
user32.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
user32.FindWindowW.restype = w.HWND
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.DestroyWindow.argtypes = [w.HWND]
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.RegisterWindowMessageW.argtypes = [w.LPCWSTR]
user32.CreatePopupMenu.restype = w.HMENU
user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
user32.SetMenuDefaultItem.argtypes = [w.HMENU, w.UINT, w.UINT]
user32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID]
user32.DestroyMenu.argtypes = [w.HMENU]
user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
user32.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
user32.LoadImageW.restype = w.HANDLE
user32.LoadIconW.argtypes = [w.HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = w.HICON
user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.MessageBoxW.argtypes = [w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT]
kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
kernel32.GetModuleHandleW.restype = w.HMODULE
kernel32.CreateMutexW.argtypes = [w.LPVOID, w.BOOL, w.LPCWSTR]
kernel32.CreateMutexW.restype = w.HANDLE
shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]


def acquire_instance():
    """True if this process is the only instance. The mutex handle lives for the process lifetime."""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def _find_window():
    return user32.FindWindowW(CLASS_NAME, None)


def running_port():
    """Port of the running instance (kept in its window title), or None."""
    hwnd = _find_window()
    if not hwnd:
        return None
    buf = ctypes.create_unicode_buffer(64)
    user32.GetWindowTextW(hwnd, buf, 64)
    try:
        return int(buf.value.rsplit(" ", 1)[-1])
    except ValueError:
        return None


def quit_running(timeout=10.0):
    """Ask the running instance to quit gracefully and wait for it. True when none is left."""
    deadline = time.time() + timeout
    hwnd = _find_window()
    if hwnd:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    while time.time() < deadline:
        if not _find_window():
            return True
        time.sleep(0.2)
    return not _find_window()


def message_box(text, title="Magic Companion"):
    user32.MessageBoxW(None, text, title, 0x10)


def _run_command(port):
    if paths.FROZEN:
        cmd = f'"{sys.executable}" --background'
    else:
        cmd = f'"{sys.executable}" "{paths.APP_DIR / "server.py"}" --tray --background'
    return cmd + (f" --port {port}" if port != DEFAULT_PORT else "")


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(enabled, port=DEFAULT_PORT):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, _run_command(port))
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass


def icon_path():
    return paths.APP_DIR / ("magic.ico" if paths.FROZEN else "packaging/magic.ico")


class Tray:
    def __init__(self, port, on_quit):
        self.port, self.on_quit = port, on_quit
        self.url = f"http://127.0.0.1:{port}/"
        self.ready = threading.Event()
        self.hwnd = None
        self._last_open = 0.0
        self._wndproc = WNDPROC(self._proc)  # keep a reference alive
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self.thread = threading.Thread(target=self._run, name="tray", daemon=True)

    def start(self):
        self.thread.start()
        self.ready.wait(5)

    def close(self):
        """Remove the icon and window; run last at exit so --quit callers see the window vanish only after cleanup."""
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE_TRAY, 0, 0)
            self.thread.join(3)

    def open_browser(self):
        if time.time() - self._last_open > 1:  # a double-click delivers two button-ups
            self._last_open = time.time()
            webbrowser.open(self.url)

    def _icon(self):
        size = user32.GetSystemMetrics(49)  # SM_CXSMICON
        h = user32.LoadImageW(None, str(icon_path()), 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        return h or user32.LoadIconW(None, 32512)  # IDI_APPLICATION

    def _nid(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd, nid.uID = self.hwnd, 1
        return nid

    def _add_icon(self):
        nid = self._nid()
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = "Magic Companion"
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    def _remove_icon(self):
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))

    def _menu(self):
        pt = w.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "Open Magic Companion")
        user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if autostart_enabled() else 0), ID_AUTOSTART, "Start with Windows")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Quit")
        user32.SetMenuDefaultItem(menu, ID_OPEN, 0)
        user32.SetForegroundWindow(self.hwnd)  # required so the menu closes when clicking elsewhere
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd == ID_OPEN:
            self.open_browser()
        elif cmd == ID_AUTOSTART:
            try:
                set_autostart(not autostart_enabled(), self.port)
            except OSError:
                message_box("Could not change the Start with Windows setting.")
        elif cmd == ID_QUIT:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                if lparam == WM_LBUTTONUP:
                    self.open_browser()
                elif lparam == WM_RBUTTONUP:
                    self._menu()
                return 0
            if msg == self._taskbar_created:
                self._add_icon()
                return 0
            if msg == WM_CLOSE:
                threading.Thread(target=self.on_quit, daemon=True).start()  # window stays until cleanup is done (close())
                return 0
            if msg == WM_CLOSE_TRAY:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_ENDSESSION and wparam:
                self.on_quit()
                return 0
            if msg == WM_DESTROY:
                self._remove_icon()
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._wndproc, hinst, CLASS_NAME
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, CLASS_NAME, f"Magic Companion {self.port}", 0, 0, 0, 0, 0, None, None, hinst, None)
        self._hicon = self._icon()
        self._add_icon()
        self.ready.set()
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
