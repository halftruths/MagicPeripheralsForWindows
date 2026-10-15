"""Gesture engine: turns touchpad frames into clicks, drags, scrolling and shortcuts.

Windows keeps handling 1-finger pointer movement, 1/2-finger taps and pinch-zoom. This engine adds
3-finger tap/drag, 4-finger tap/swipes, back/forward flicks and (optionally) its own 2-finger scrolling.

Coordinates arrive in the driver's logical units; everything here works in millimetres.
"""

import math
import threading
import time

import winapi
from input_capture import X_MAX, Y_MAX

# Physical size from the report descriptor (MagicTrackpad2.h): 160.0 x 114.9 mm.
UNITS_PER_MM_X = X_MAX / 160.0
UNITS_PER_MM_Y = Y_MAX / 114.9
TICK = 1 / 120
LOST_FRAME_TIMEOUT = 0.15  # end a session if frames stop without a lift frame (Bluetooth loss)

FOUR_TAP_KEYS = {
    "notifications": ("win", "n"), "quick_settings": ("win", "a"), "search": ("win", "s"),
    "task_view": ("win", "tab"), "play_pause": ("play_pause",),
}


def mm(contact):
    return contact["x"] / UNITS_PER_MM_X, contact["y"] / UNITS_PER_MM_Y


class GestureEngine:
    def __init__(self, settings, on_action=None):
        self.s = settings
        self.on_action = on_action or (lambda name, detail: None)
        self.lock = threading.Lock()
        self._reset_session()
        self.prev_count = 0
        self.prev_centroid = None
        self.prev_t = None
        self.last_frame_t = 0.0
        # drag
        self.dragging = False
        self.drag_locked_until = 0.0
        self.drag_px = [0.0, 0.0]
        # scroll
        self.scrolling = False
        self.scroll_axis = None   # None = free, "v" or "h"
        self.scroll_acc = [0.0, 0.0]  # wheel units waiting to be sent (v, h)
        self.velocity = [0.0, 0.0]    # mm/s of the 2-finger centroid (x, y)
        self.inertia = None           # [vx, vy] mm/s while coasting
        self._stop = threading.Event()
        self._ticker = threading.Thread(target=self._tick_loop, name="gesture-ticker", daemon=True)
        self._ticker.start()

    # -- public --

    def on_frame(self, frame):
        with self.lock:
            self._frame(frame)

    def shutdown(self):
        self._stop.set()
        with self.lock:
            self._release_drag("shutdown")

    # -- helpers --

    def _reset_session(self):
        self.session = None  # dict while any finger is down

    def _action(self, name, detail=""):
        self.on_action(name, detail)

    def _release_drag(self, why):
        if self.dragging:
            winapi.mouse_button(self.s.get("three.dragButton"), False)
            self.dragging = False
            self.drag_locked_until = 0.0
            self._action("drag end", why)

    def _wheel_units(self, dx_mm, dy_mm, speed_mm_s):
        """Finger motion (mm) to wheel units (vertical, horizontal), honouring direction and acceleration."""
        gain = self.s.get("scroll.speed") * (1 + self.s.get("scroll.acceleration") * speed_mm_s / 100)
        sign = 1 if self.s.get("scroll.natural") else -1
        # Natural: fingers down -> content down -> scroll up (positive WHEEL); fingers left -> scroll right (positive HWHEEL).
        v = dy_mm * gain * sign
        h = -dx_mm * gain * sign
        if not self.s.get("scroll.horizontal") or self.scroll_axis == "v":
            h = 0.0
        if self.scroll_axis == "h":
            v = 0.0
        return v, h

    def _emit_scroll(self, v, h):
        step = self.s.get("scroll.minDelta")
        self.scroll_acc[0] += v
        self.scroll_acc[1] += h
        out = []
        for i in (0, 1):
            n = int(self.scroll_acc[i] / step) * step
            self.scroll_acc[i] -= n
            out.append(n)
        if out[0] or out[1]:
            winapi.wheel(vertical=out[0], horizontal=out[1])

    # -- frame processing --

    def _frame(self, f):
        now = f["t"]
        self.last_frame_t = now
        touching = [c for c in f["contacts"] if c["tip"] and c["conf"]]
        n = len(touching)
        pts = {c["id"]: mm(c) for c in touching}

        if n and self.session is None:
            self.inertia = None
            self.session = {"t0": now, "max": 0, "start": {}, "move": 0.0, "flick": None, "swiped": False}
        if self.session is None:
            return
        sess = self.session
        sess["max"] = max(sess["max"], n)
        for cid, p in pts.items():
            sx, sy = sess["start"].setdefault(cid, p)
            sess["move"] = max(sess["move"], math.hypot(p[0] - sx, p[1] - sy))

        centroid = (sum(p[0] for p in pts.values()) / n, sum(p[1] for p in pts.values()) / n) if n else None
        same_set = n and n == self.prev_count and self.prev_centroid is not None
        dx = dy = 0.0
        dt = max(1e-3, now - self.prev_t) if self.prev_t else TICK
        if same_set:
            dx, dy = centroid[0] - self.prev_centroid[0], centroid[1] - self.prev_centroid[1]
        if n != self.prev_count:
            self._count_changed(self.prev_count, n, centroid, now)

        if n == 2 and same_set and not f["button"]:
            self._two_fingers(dx, dy, dt, now)
        elif n == 3 and same_set:
            self._three_fingers(dx, dy)
        elif n == 4 and same_set:
            self._four_fingers(centroid)

        if n == 0:
            self._session_end(now)
        self.prev_count, self.prev_centroid, self.prev_t = n, centroid, now

    def _count_changed(self, old, new, centroid, now):
        if old == 2 and self.scrolling and new != 0:
            self.scrolling = False  # 2 -> 1/3 fingers: stop scrolling without inertia
        if new == 2:
            self.scroll_ref = centroid
            self.scrolling = False
            self.scroll_axis = None
            self.velocity = [0.0, 0.0]
            if self.session:
                self.session["flick"] = {"t0": now, "t1": now, "start": centroid, "last": centroid}
        if new == 3:
            self.drag_ref = centroid
            if self.dragging:
                self.drag_locked_until = 0.0  # back to 3 fingers: resume moving
        if new == 4 and self.session:
            self.session["swipe_ref"] = centroid
        if old == 3 and self.dragging and 0 < new < 3 and self.s.get("three.dropOnLift"):
            self._release_drag("finger lifted")

    def _two_fingers(self, dx, dy, dt, now):
        flick = self.session.get("flick")
        if flick:
            flick["last"] = (flick["last"][0] + dx, flick["last"][1] + dy)
            flick["t1"] = now
        if self.s.get("scroll.mode") != "companion":
            return
        speed = math.hypot(dx, dy) / dt
        alpha = 0.35
        self.velocity = [self.velocity[0] * (1 - alpha) + dx / dt * alpha, self.velocity[1] * (1 - alpha) + dy / dt * alpha]
        if not self.scrolling:
            tx, ty = self.prev_centroid[0] + dx - self.scroll_ref[0], self.prev_centroid[1] + dy - self.scroll_ref[1]
            if math.hypot(tx, ty) < self.s.get("scroll.startDistance"):
                return
            self.scrolling = True
            self.scroll_acc = [0.0, 0.0]
            mode = self.s.get("scroll.axisLock")
            ax, ay = abs(tx), abs(ty)
            if mode == "always" or (mode == "dominant" and max(ax, ay) >= self.s.get("scroll.lockRatio") * max(min(ax, ay), 1e-6)):
                self.scroll_axis = "v" if ay >= ax else "h"
            else:
                self.scroll_axis = None
            # Include the motion that crossed the threshold so the start doesn't feel sticky.
            dx, dy = tx, ty
        self._emit_scroll(*self._wheel_units(dx, dy, speed))

    def _three_fingers(self, dx, dy):
        if not self.s.get("three.drag"):
            return
        if not self.dragging:
            tx, ty = self.prev_centroid[0] + dx - self.drag_ref[0], self.prev_centroid[1] + dy - self.drag_ref[1]
            if math.hypot(tx, ty) < self.s.get("three.dragStart"):
                return
            winapi.mouse_button(self.s.get("three.dragButton"), True)
            self.dragging = True
            self.drag_px = [0.0, 0.0]
            self._action("drag start", self.s.get("three.dragButton"))
            dx, dy = tx, ty
        gain = self.s.get("three.dragGain")
        self.drag_px[0] += dx * gain
        self.drag_px[1] += dy * gain
        mx, my = int(self.drag_px[0]), int(self.drag_px[1])
        self.drag_px[0] -= mx
        self.drag_px[1] -= my
        winapi.mouse_move(mx, my)

    def _four_fingers(self, centroid):
        sess = self.session
        if sess["swiped"] or "swipe_ref" not in sess:
            return
        tx, ty = centroid[0] - sess["swipe_ref"][0], centroid[1] - sess["swipe_ref"][1]
        if math.hypot(tx, ty) < self.s.get("four.swipeDistance"):
            return
        sess["swiped"] = True
        if abs(tx) >= abs(ty):
            side = self.s.get("four.swipeSide")
            # Natural: fingers move left -> go to the next desktop/app on the right.
            forward = (tx < 0) != self.s.get("four.swipeSideSwap")
            if side == "desktops":
                winapi.press_keys("ctrl", "win", "right" if forward else "left")
                self._action("4-finger swipe", "next desktop" if forward else "previous desktop")
            elif side == "apps":
                winapi.press_keys("alt", "tab") if forward else winapi.press_keys("alt", "shift", "tab")
                self._action("4-finger swipe", "next app" if forward else "previous app")
        elif ty < 0 and self.s.get("four.swipeUp") == "task_view":
            winapi.press_keys("win", "tab")
            self._action("4-finger swipe", "task view")
        elif ty > 0 and self.s.get("four.swipeDown") == "show_desktop":
            winapi.press_keys("win", "d")
            self._action("4-finger swipe", "show desktop")

    def _session_end(self, now):
        sess = self.session
        self._reset_session()
        duration = now - sess["t0"]
        is_tap = duration * 1000 <= self.s.get("tap.maxDuration") and sess["move"] <= self.s.get("tap.maxMovement")

        if self.dragging:
            lock_ms = self.s.get("three.dragLock")
            if lock_ms and sess["max"] >= 3 and not self.drag_locked_until:
                self.drag_locked_until = now + lock_ms / 1000
                self._action("drag lock", f"{lock_ms} ms")
            elif sess["max"] < 3 and is_tap:
                self._release_drag("tap ended drag lock")
            elif not lock_ms:
                self._release_drag("lifted")
            return

        if is_tap and sess["max"] == 3 and self.s.get("three.tap") == "middle":
            winapi.mouse_click("middle")
            self._action("3-finger tap", "middle click")
        elif is_tap and sess["max"] == 4:
            keys = FOUR_TAP_KEYS.get(self.s.get("four.tap"))
            if keys:
                winapi.press_keys(*keys)
                self._action("4-finger tap", self.s.get("four.tap"))

        flick = sess.get("flick")
        if flick and self.s.get("nav.flick") and sess["max"] == 2:
            fx, fy = flick["last"][0] - flick["start"][0], flick["last"][1] - flick["start"][1]
            speed = abs(fx) / max(flick["t1"] - flick["t0"], 1e-3)
            if abs(fx) >= self.s.get("nav.flickDistance") and abs(fx) > 2 * abs(fy) and speed >= self.s.get("nav.flickSpeed"):
                back = (fx > 0) != self.s.get("nav.flickSwap")  # macOS: swipe right = back
                winapi.press_keys("alt", "left" if back else "right")
                self._action("flick", "back" if back else "forward")

        if self.scrolling and self.s.get("scroll.inertia"):
            vx, vy = self.velocity
            if math.hypot(vx, vy) >= self.s.get("scroll.inertiaMinSpeed"):
                self.inertia = [vx, vy]
        self.scrolling = False

    # -- ticker: inertia, drag-lock timeout, lost frames --

    def _tick_loop(self):
        last = time.perf_counter()
        while not self._stop.wait(TICK):
            now = time.perf_counter()
            dt, last = now - last, now
            with self.lock:
                if self.session is not None and now - self.last_frame_t > LOST_FRAME_TIMEOUT:
                    self._session_end(now)
                    self.prev_count, self.prev_centroid = 0, None
                if self.dragging and self.drag_locked_until and now >= self.drag_locked_until and self.session is None:
                    self._release_drag("drag lock timeout")
                if self.inertia and self.session is None:
                    decay = math.exp(-self.s.get("scroll.friction") * dt)
                    self.inertia = [self.inertia[0] * decay, self.inertia[1] * decay]
                    speed = math.hypot(*self.inertia)
                    if speed < self.s.get("scroll.inertiaMinSpeed") / 4 or self.s.get("scroll.mode") != "companion":
                        self.inertia = None
                        self.scroll_axis = None
                    else:
                        self._emit_scroll(*self._wheel_units(self.inertia[0] * dt, self.inertia[1] * dt, speed))
