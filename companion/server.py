"""Magic companion: local web app for live touchpad visualization, recording and battery status.

    python companion/server.py [--port 8765]

Then open http://127.0.0.1:8765. Binds to localhost only.
"""

import argparse
import json
import queue
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from battery import read_batteries  # noqa: E402
from input_capture import X_MAX, Y_MAX, InputCapture  # noqa: E402

WEB = ROOT / "web"
RECORDINGS = ROOT / "recordings"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css"}


class Hub:
    """Fans capture events out to SSE clients and the active recording."""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = set()
        self.recording = None  # (file, path, started)
        self.capture = InputCapture(self.publish)

    def publish(self, event):
        line = json.dumps(event, separators=(",", ":"))
        with self.lock:
            if self.recording:
                self.recording[0].write(line + "\n")
            for q in self.clients:
                try:
                    q.put_nowait(line)
                except queue.Full:
                    pass  # slow browser tab; drop rather than stall the capture thread

    def _update_hook(self):
        self.capture.set_wheel_hook(bool(self.clients or self.recording))

    def add_client(self):
        q = queue.Queue(maxsize=4000)
        with self.lock:
            self.clients.add(q)
            self._update_hook()
        return q

    def remove_client(self, q):
        with self.lock:
            self.clients.discard(q)
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
        if self.path == "/events":
            return self._events()
        if self.path == "/api/battery":
            return self._json(read_batteries())
        if self.path == "/api/recordings":
            files = sorted(RECORDINGS.glob("*.jsonl"), reverse=True) if RECORDINGS.exists() else []
            return self._json([{"name": p.name, "bytes": p.stat().st_size} for p in files])
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
        self._json({"error": "not found"}, 404)

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = hub.add_client()
        try:
            while True:
                try:
                    lines = [q.get(timeout=15)]
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
