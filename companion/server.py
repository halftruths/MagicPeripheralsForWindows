"""Magic companion: local web app for live touchpad visualization, gestures, settings, recording and battery status.

    python companion/server.py [--port 8765]

Then open http://127.0.0.1:8765. Binds to localhost only.
"""

import argparse
import atexit
import json
import queue
import subprocess
import sys
import tempfile
import traceback
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from battery import DeviceMonitor  # noqa: E402
from gestures import GestureEngine  # noqa: E402
from input_capture import X_MAX, Y_MAX, InputCapture  # noqa: E402
from settings import DRIVER_KEYS, SCHEMA, Settings, driver_registry_values  # noqa: E402

WEB = ROOT / "web"
RECORDINGS = ROOT / "recordings"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css"}


class Hub:
    """Fans capture events out to SSE clients and the active recording."""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = {}  # queue -> set of event types it wants (None = all)
        self.recording = None  # (file, path, started)
        self.capture = InputCapture(self.on_capture)
        self.engine = None

    def on_capture(self, event):
        if event["type"] == "frame" and self.engine:
            try:
                self.engine.on_frame(event)
            except Exception:
                traceback.print_exc()  # a gesture bug must never stop capture
        self.publish(event)

    def on_action(self, name, detail):
        self.publish({"type": "action", "t": time.perf_counter(), "name": name, "detail": detail})

    def publish(self, event):
        line = json.dumps(event, separators=(",", ":"))
        with self.lock:
            if self.recording:
                self.recording[0].write(line + "\n")
            for q, kinds in self.clients.items():
                if kinds is not None and event["type"] not in kinds:
                    continue
                try:
                    q.put_nowait(line)
                except queue.Full:
                    pass  # slow browser tab; drop rather than stall the capture thread

    def _update_hook(self):
        # The low-level wheel hook sees every mouse event system-wide, so only run it when someone needs wheel events.
        wanted = any(kinds is None or "wheel" in kinds for kinds in self.clients.values())
        self.capture.set_wheel_hook(bool(wanted or self.recording))

    def add_client(self, kinds=None):
        q = queue.Queue(maxsize=4000)
        with self.lock:
            self.clients[q] = kinds
            self._update_hook()
        return q

    def remove_client(self, q):
        with self.lock:
            self.clients.pop(q, None)
            self._update_hook()

    def start_recording(self, label):
        RECORDINGS.mkdir(exist_ok=True)
        path = RECORDINGS / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        f = path.open("w", encoding="utf-8")
        f.write(json.dumps({"type": "meta", "label": label, "started": datetime.now().isoformat(),
                            "t": time.perf_counter(), "x_max": X_MAX, "y_max": Y_MAX}) + "\n")
        with self.lock:
            self.stop_recording_locked()
            self.recording = (f, path, time.perf_counter())
            self._update_hook()
        return path.name

    def stop_recording_locked(self):
        if self.recording:
            f, path, started = self.recording
            f.close()
            self.recording = None
            self._update_hook()
            return {"name": path.name, "seconds": round(time.perf_counter() - started, 1)}
        return None

    def stop_recording(self):
        with self.lock:
            return self.stop_recording_locked()


hub = Hub()
devices = DeviceMonitor()
settings = Settings()
hub.engine = GestureEngine(settings, hub.on_action)


# Command 0x00 on the USB driver's admin-only control pipe re-applies haptics live. In the installed v2.2 build the
# other settings (finger freezing etc.) are only read when the device starts, so USB still needs a re-plug
# (verified 2026-10-15: press-and-drag stayed broken after the reload and worked after re-plugging).
# Bluetooth needs a trackpad power-cycle.
USB_RELOAD_PS = ("try { $p = New-Object System.IO.Pipes.NamedPipeClientStream('.', 'AmtPtpControlPanelUsbUmInterface', 'InOut'); "
                 "$p.Connect(500); $p.WriteByte(0); $p.Flush(); $b = New-Object byte[] 2; [void]$p.Read($b, 0, 2); $p.Dispose() } catch {}")


def apply_driver_settings(values):
    """Write the Bluetooth and USB driver parameters through an elevated PowerShell (UAC prompt). Returns True if written."""
    regs = driver_registry_values(values)
    lines = []
    for key in DRIVER_KEYS:
        lines += [f"$k = 'HKLM:\\{key}'", "New-Item -Path $k -Force | Out-Null"]
        for name, value in regs.items():
            signed = value - (1 << 32) if value >= 1 << 31 else value  # PowerShell DWord takes int32
            lines.append(f"New-ItemProperty -Path $k -Name {name} -PropertyType DWord -Value ([int32]{signed}) -Force | Out-Null")
    lines.append(USB_RELOAD_PS)
    script = Path(tempfile.gettempdir()) / "magic-companion-driver.ps1"
    script.write_text("\n".join(lines), encoding="utf-8")
    # Start-Process throws when the UAC prompt is declined.
    launcher = (f"try {{ $p = Start-Process powershell -Verb RunAs -Wait -PassThru "
                f"-ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"{script}\"'; exit $p.ExitCode }} "
                f"catch {{ exit 1223 }}")
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", launcher], capture_output=True)
    return proc.returncode == 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/events":
            only = parse_qs(url.query).get("only")
            return self._events(set(only[0].split(",")) if only else None)
        if self.path == "/api/devices":
            return self._json(devices.status())
        if self.path == "/api/recordings":
            files = sorted(RECORDINGS.glob("*.jsonl"), reverse=True) if RECORDINGS.exists() else []
            return self._json([{"name": p.name, "bytes": p.stat().st_size} for p in files])
        if self.path == "/api/settings":
            return self._json({"schema": SCHEMA, "values": settings.snapshot()})
        if self.path == "/api/status":
            rec = hub.recording
            return self._json({"recording": rec[1].name if rec else None, "x_max": X_MAX, "y_max": Y_MAX})
        name = "index.html" if self.path == "/" else self.path.lstrip("/")
        file = (WEB / name).resolve()
        if WEB not in file.parents or not file.is_file():
            return self._json({"error": "not found"}, 404)
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(file.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Browsers send Origin on cross-site POSTs; reject anything not from this app.
        origin = self.headers.get("Origin")
        if origin and not origin.startswith(("http://127.0.0.1:", "http://localhost:")):
            return self._json({"error": "forbidden"}, 403)
        if self.path == "/api/record/start":
            return self._json({"name": hub.start_recording(self._body().get("label", ""))})
        if self.path == "/api/record/stop":
            return self._json(hub.stop_recording() or {})
        if self.path == "/api/settings":
            try:
                pending = settings.update(self._body().get("changes", {}))
            except (ValueError, OSError) as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"values": settings.snapshot(), "driverPending": pending})
        if self.path == "/api/driver/apply":
            values = settings.snapshot()
            values.update(self._body().get("changes", {}))
            ok = apply_driver_settings(values)
            return self._json({"ok": ok, "values": settings.snapshot()}, 200 if ok else 409)
        self._json({"error": "not found"}, 404)

    def _events(self, kinds):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = hub.add_client(kinds)
        try:
            while True:
                try:
                    lines = [q.get(timeout=5)]
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # keepalive; also detects closed tabs
                    self.wfile.flush()
                    continue
                try:
                    while len(lines) < 64:
                        lines.append(q.get_nowait())
                except queue.Empty:
                    pass
                self.wfile.write("".join(f"data: {line}\n\n" for line in lines).encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            hub.remove_client(q)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    settings.apply_side_effects()
    atexit.register(settings.restore_windows_scrolling)
    atexit.register(hub.engine.shutdown)
    hub.capture.start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(f"Magic companion running at http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop_recording()
        hub.capture.stop()


if __name__ == "__main__":
    main()
