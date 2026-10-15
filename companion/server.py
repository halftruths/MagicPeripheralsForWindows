"""Magic companion: local web app for live touchpad visualization, gestures, settings, recording and battery status.

    python companion/server.py [--port 8765]

Then open http://127.0.0.1:8765. Binds to localhost only.
"""

import argparse
import atexit
import json
import os
import queue
import secrets
import subprocess
import sys
import tempfile
import traceback
import webbrowser
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import tray  # noqa: E402
from paths import APP_DIR, DATA_DIR, FROZEN  # noqa: E402
from alerts import BatteryAlerts  # noqa: E402
import drivers  # noqa: E402
from battery import DeviceMonitor  # noqa: E402
from gestures import GestureEngine  # noqa: E402
from input_capture import X_MAX, Y_MAX, InputCapture  # noqa: E402
from settings import DRIVER_KEYS, DEVICES, Settings, driver_registry_values, schema_for  # noqa: E402

WEB = APP_DIR / "web"
RECORDINGS = DATA_DIR / "recordings"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
PAGES = {"/": "home.html", "/live": "live.html", "/settings": "settings.html", "/drivers": "drivers.html"}
REDIRECTS = {"/index.html": "/live", "/settings.html": "/settings"}


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
hub.engine = GestureEngine(settings.view("trackpad"), hub.on_action)
drivers.set_publisher(hub.publish)

# Per-run secret injected into served pages (<meta name="mc-token">); every POST must echo it in X-Companion-Token, so a
# page on another origin (or a stray local process that never loaded our HTML) cannot drive the API.
TOKEN = secrets.token_urlsafe(32)


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
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", launcher], capture_output=True, creationflags=0x08000000)
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

    def _host_ok(self):
        # DNS-rebinding defence: only answer requests addressed to our own loopback name and port.
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _post_ok(self):
        """Origin/token/content-type checks for every POST; sends the error response itself and returns False on failure."""
        port = self.server.server_address[1]
        origin = self.headers.get("Origin")
        error = None
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            error = (403, "forbidden origin")
        elif self.path.startswith("/api/drivers/") and not origin:
            error = (403, "origin required")
        elif not secrets.compare_digest(self.headers.get("X-Companion-Token", "").encode(), TOKEN.encode()):
            error = (403, "missing or wrong token")
        elif int(self.headers.get("Content-Length") or 0) and not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            error = (415, "JSON body required")
        if error:
            self._json({"error": error[1]}, error[0])
        return error is None

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "forbidden host"}, 403)
        url = urlparse(self.path)
        if url.path.startswith("/api/drivers"):
            status, obj = drivers.api_get(url.path)
            return self._json(obj, status)
        if url.path == "/events":
            only = parse_qs(url.query).get("only")
            return self._events(set(only[0].split(",")) if only else None)
        if self.path == "/api/devices":
            return self._json(devices.status())
        if self.path == "/api/recordings":
            files = sorted(RECORDINGS.glob("*.jsonl"), reverse=True) if RECORDINGS.exists() else []
            return self._json([{"name": p.name, "bytes": p.stat().st_size} for p in files])
        if url.path == "/api/settings":
            device = (parse_qs(url.query).get("device") or ["trackpad"])[0]
            if device not in DEVICES:
                return self._json({"error": f"unknown device {device}"}, 400)
            return self._json({"device": device, "schema": schema_for(device), "values": settings.snapshot(device)})
        if self.path == "/api/status":
            rec = hub.recording
            return self._json({"recording": rec[1].name if rec else None, "x_max": X_MAX, "y_max": Y_MAX})
        if url.path in REDIRECTS:
            self.send_response(301)
            self.send_header("Location", REDIRECTS[url.path] + (f"?{url.query}" if url.query else ""))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        name = PAGES.get(url.path) or url.path.lstrip("/")
        file = (WEB / name).resolve()
        if WEB not in file.parents or not file.is_file():
            return self._json({"error": "not found"}, 404)
        body = file.read_bytes()
        if file.suffix == ".html":
            body = body.replace(b"</head>", f'<meta name="mc-token" content="{TOKEN}"></head>'.encode(), 1)
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(file.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "forbidden host"}, 403)
        if not self._post_ok():
            return
        if self.path.startswith("/api/drivers/"):
            try:
                body = self._body()
            except ValueError:
                return self._json({"error": "bad JSON"}, 400)
            status, obj = drivers.api_post(self.path, body)
            return self._json(obj, status)
        if self.path == "/api/record/start":
            return self._json({"name": hub.start_recording(self._body().get("label", ""))})
        if self.path == "/api/record/stop":
            return self._json(hub.stop_recording() or {})
        if self.path == "/api/settings":
            body = self._body()
            device = body.get("device") or "trackpad"
            try:
                pending = settings.update(device, body.get("changes", {}))
            except (ValueError, OSError) as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"device": device, "values": settings.snapshot(device), "driverPending": pending})
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


def _redirect_output():
    """The windowed build has no console; keep a small log instead."""
    log = DATA_DIR / "companion.log"
    try:
        if log.exists() and log.stat().st_size > 1_000_000:
            log.replace(log.with_suffix(".log.old"))
        sys.stdout = sys.stderr = open(log, "a", buffering=1, encoding="utf-8")
    except OSError:
        sys.stdout = sys.stderr = open(os.devnull, "w")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--background", action="store_true", help="start without opening a browser")
    parser.add_argument("--tray", action="store_true", help="show a tray icon (always on in the packaged app)")
    parser.add_argument("--quit", action="store_true", help="ask the running instance to quit, then exit")
    args = parser.parse_args()
    use_tray = FROZEN or args.tray
    if FROZEN:
        _redirect_output()
    if args.quit:
        sys.exit(0 if tray.quit_running() else 1)
    if use_tray and not tray.acquire_instance():
        if not args.background:
            webbrowser.open(f"http://127.0.0.1:{tray.running_port() or args.port}/")
        return
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print(f"cannot listen on port {args.port}: {e}")
        if use_tray and not args.background:
            tray.message_box(f"Magic Companion could not start: port {args.port} is in use.\n\n"
                             "Is another copy running? Use --port to pick a different one.")
        sys.exit(1)
    server.daemon_threads = True
    icon = tray.Tray(args.port, server.shutdown) if use_tray else None
    if icon:
        atexit.register(icon.close)  # registered first so it runs last, after the restores below
    settings.apply_side_effects()
    atexit.register(settings.restore_windows_scrolling)
    atexit.register(hub.engine.shutdown)
    BatteryAlerts(devices.status, settings, hub.publish).start()
    hub.capture.start()
    drivers.warm_probe()  # background first driver scan (~7 s) so /drivers is usually ready when opened
    print(f"Magic companion running at http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    if icon:
        icon.start()
        if not args.background:
            icon.open_browser()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop_recording()
        hub.capture.stop()


if __name__ == "__main__":
    main()
