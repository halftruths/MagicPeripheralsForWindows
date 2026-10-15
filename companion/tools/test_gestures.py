"""Synthetic-frame tests for the gesture engine. Input injection is stubbed, so nothing is clicked or typed.

    python companion/tools/test_gestures.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import winapi  # noqa: E402

log = []
winapi.mouse_button = lambda b, d: log.append(("button", b, d))
winapi.mouse_click = lambda b: log.append(("click", b))
winapi.mouse_move = lambda x, y: log.append(("move", x, y)) if (x or y) else None
winapi.wheel = lambda vertical=0, horizontal=0: log.append(("wheel", vertical, horizontal))
winapi.press_keys = lambda *k: log.append(("keys",) + k)

import settings as S  # noqa: E402
from gestures import UNITS_PER_MM_X as UX, UNITS_PER_MM_Y as UY, GestureEngine  # noqa: E402

BASE = [(60, 50), (80, 50), (100, 50), (120, 50)]  # finger positions in mm


class FakeSettings:
    def __init__(self, **overrides):
        self.v = {s["key"]: s["default"] for s in S.SCHEMA}
        self.v.update(overrides)

    def get(self, key):
        return self.v[key]


def frame(t, pts, button=False):
    return {"t": t, "button": button, "contacts": [
        {"id": i, "tip": True, "conf": True, "x": int(x * UX), "y": int(y * UY)} for i, (x, y) in enumerate(pts)]}


def swipe(t0, fingers, step, count=31, dt=0.011):
    """Frames moving `fingers` of BASE by `step` mm per frame, then a lift frame."""
    frames = [frame(t0 + i * dt, [(x + step[0] * i, y + step[1] * i) for x, y in BASE[:fingers]]) for i in range(count)]
    return frames + [frame(t0 + count * dt + 0.01, [])]


def run(engine, frames):
    log.clear()
    for f in frames:
        engine.on_frame(f)
    return list(log)


def wheel_sum(entries):
    return sum(e[1] for e in entries if e[0] == "wheel"), sum(e[2] for e in entries if e[0] == "wheel")


failures = []


def check(name, ok, detail):
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    if not ok:
        failures.append(name)


eng = GestureEngine(FakeSettings())
out = run(eng, swipe(0.0, 3, (0, 0), count=8))
check("3-finger tap = middle click", out == [("click", "middle")], out)

out = run(eng, swipe(1.0, 3, (1.0, 0)))
moved = sum(e[1] for e in out if e[0] == "move")
check("3-finger drag", out[0] == ("button", "left", True) and out[-1] == ("button", "left", False) and 340 <= moved <= 370,
      f"first {out[0]}, last {out[-1]}, moved {moved}px")
eng.shutdown()

eng = GestureEngine(FakeSettings(**{"three.dragLock": 300}))
run(eng, swipe(2.0, 3, (1.0, 0)))
held = log[-1] == ("move", 12, 0) or all(e != ("button", "left", False) for e in log)
time.sleep(0.5)
check("drag lock holds, then times out", held and log[-1] == ("button", "left", False), f"held={held}, last {log[-1]}")
eng.shutdown()

eng = GestureEngine(FakeSettings(**{"scroll.mode": "companion", "scroll.inertia": False}))
v, h = wheel_sum(run(eng, swipe(3.0, 2, (0.5, 0.5), count=41)))
check("diagonal scroll uses both axes", v > 200 and h < -200 and abs(v + h) < 0.2 * v, f"v={v}, h={h}")

v, h = wheel_sum(run(eng, swipe(4.0, 2, (0.05, 0.6), count=41)))
check("near-vertical scroll locks to vertical", v > 200 and h == 0, f"v={v}, h={h}")
eng.shutdown()

eng = GestureEngine(FakeSettings(**{"scroll.mode": "companion", "scroll.inertia": False, "scroll.natural": False}))
v, h = wheel_sum(run(eng, swipe(5.0, 2, (0, 0.6), count=41)))
check("non-natural scroll reverses direction", v < -200, f"v={v}")
eng.shutdown()

eng = GestureEngine(FakeSettings())
out = run(eng, swipe(6.0, 4, (-1.0, 0)))
check("4-finger swipe left = next desktop", out == [("keys", "ctrl", "win", "right")], out)
out = run(eng, swipe(7.0, 4, (0, -1.0)))
check("4-finger swipe up = task view", out == [("keys", "win", "tab")], out)
out = run(eng, swipe(8.0, 4, (0, 0), count=8))
check("4-finger tap = notifications", out == [("keys", "win", "n")], out)
eng.shutdown()

eng = GestureEngine(FakeSettings(**{"nav.flick": True}))
out = run(eng, swipe(9.0, 2, (3.0, 0), count=11, dt=0.01))
check("fast swipe right = back", out == [("keys", "alt", "left")], out)
out = run(eng, swipe(10.0, 2, (0.3, 0), count=11, dt=0.01))
check("slow swipe is not a flick", out == [], out)
eng.shutdown()

eng = GestureEngine(FakeSettings(**{"scroll.mode": "companion"}))
now = time.perf_counter()
run(eng, swipe(now, 2, (0, 1.5), count=20))
n0 = len(log)
time.sleep(0.6)
after = log[n0:]
check("inertia continues after lift", len(after) > 10 and wheel_sum(after)[0] > 100, f"{len(after)} events, v={wheel_sum(after)[0]}")
eng.shutdown()

eng = GestureEngine(FakeSettings())  # Windows scroll mode: engine must not scroll
v, h = wheel_sum(run(eng, swipe(12.0, 2, (0.5, 0.5), count=41)))
check("Windows scroll mode emits no wheel events", (v, h) == (0, 0), f"v={v}, h={h}")
eng.shutdown()

print(f"\n{len(failures)} failure(s)" if failures else "\nall passed")
sys.exit(1 if failures else 0)
