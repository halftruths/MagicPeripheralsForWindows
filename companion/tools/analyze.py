"""Analyze a companion recording (.jsonl) for scroll-quality problems.

    python companion/tools/analyze.py [recording.jsonl]   # defaults to the newest recording

Reports frozen contact positions, tip flicker, and for every two-finger swipe the finger
motion direction versus the wheel events Windows emitted.
"""

import json
import math
import sys
from collections import Counter
from pathlib import Path

RECORDINGS = Path(__file__).resolve().parent.parent / "recordings"


def load(path):
    meta, frames, wheels = {}, [], []
    for line in path.open(encoding="utf-8"):
        e = json.loads(line)
        if e["type"] == "meta":
            meta = e
        elif e["type"] == "frame":
            frames.append(e)
        elif e["type"] == "wheel":
            wheels.append(e)
    return meta, frames, wheels


def analyze(path):
    meta, frames, wheels = load(path)
    print(f"Recording: {path.name}  label: {meta.get('label') or '-'}")
    if not frames:
        print("No touchpad frames.")
        return
    duration = frames[-1]["t"] - frames[0]["t"]
    print(f"Frames: {len(frames)} over {duration:.1f}s ({len(frames) / max(duration, 1e-6):.0f} Hz)")
    print(f"Wheel events: {sum(w['axis'] == 'v' for w in wheels)} vertical, {sum(w['axis'] == 'h' for w in wheels)} horizontal")

    # Frozen positions and tip flicker
    prev = {}
    gone = {}
    frozen = []
    flickers = []
    touch_pressure = Counter()
    touch_state = Counter()
    for f in frames:
        touching = [c for c in f["contacts"] if c["tip"]]
        for c in touching:
            touch_pressure["0" if c["p"] == 0 else "1-9" if c["p"] < 10 else "10+"] += 1
            touch_state[c["state"]] += 1
        moved = {c["id"] for c in touching
                 if c["id"] in prev and prev[c["id"]]["tip"] and abs(prev[c["id"]]["x"] - c["x"]) + abs(prev[c["id"]]["y"] - c["y"]) > 4}
        if moved:
            for c in touching:
                p = prev.get(c["id"])
                if c["id"] not in moved and p and p["tip"] and (p["x"], p["y"]) == (c["x"], c["y"]):
                    frozen.append(c)
        present = {c["id"] for c in f["contacts"]}
        for cid, p in prev.items():
            if cid not in present and p["tip"]:
                gone[cid] = (f["t"], p)
        for c in f["contacts"]:
            p = prev.get(c["id"])
            if p and p["tip"] and not c["tip"]:
                gone[c["id"]] = (f["t"], c)
            if c["tip"] and (not p or not p["tip"]) and c["id"] in gone and f["t"] - gone[c["id"]][0] < 0.15:
                flickers.append((gone[c["id"]][1], c, f["t"] - gone[c["id"]][0]))
            if c["tip"]:
                gone.pop(c["id"], None)
        prev = {c["id"]: c for c in f["contacts"]}

    print(f"\nTouching-contact pressure distribution: {dict(touch_pressure)}")
    print(f"Touching-contact raw state distribution: {dict(sorted(touch_state.items()))}")
    print(f"\nFrozen-position frames: {len(frozen)}")
    if frozen:
        print(f"  pressure at freeze: {dict(Counter(c['p'] for c in frozen).most_common(6))}")
        print(f"  state at freeze:    {dict(Counter(c['state'] for c in frozen).most_common(6))}")
    print(f"Tip flickers (lift then re-touch < 150 ms): {len(flickers)}")
    for before, after, gap in flickers[:8]:
        print(f"  id {after['id']}: gap {gap * 1000:.0f} ms, before state {before['state']} p{before['p']} -> after state {after['state']} p{after['p']}")

    # Press-and-drag: while the button is held, do contacts keep moving and keep their tip?
    presses, cur = [], None
    for f in frames:
        if f["button"]:
            cur = cur or []
            cur.append(f)
        elif cur:
            presses.append(cur)
            cur = None
    if cur:
        presses.append(cur)
    print(f"\nButton presses: {len(presses)}")
    for i, p in enumerate(presses, 1):
        ids = {c["id"] for f in p for c in f["contacts"]}
        print(f"  press {i}: {len(p)} frames, {p[-1]['t'] - p[0]['t']:.2f}s, contact ids {sorted(ids)}")
        for cid in sorted(ids):
            track = [(c["x"], c["y"], c["tip"], c["state"], c["p"]) for f in p for c in f["contacts"] if c["id"] == cid]
            xs = {(x, y) for x, y, *_ in track}
            tips = Counter(t for _, _, t, _, _ in track)
            states = Counter(s for *_, s, _ in track)
            first, last = track[0], track[-1]
            print(f"    id {cid}: moved {abs(last[0] - first[0]) + abs(last[1] - first[1])} units, {len(xs)} distinct positions / {len(track)} frames, "
                  f"tip {dict(tips)}, states {dict(states)}, pressure {min(t[4] for t in track)}-{max(t[4] for t in track)}")
        empty = sum(1 for f in p if not any(c["tip"] for c in f["contacts"]))
        if empty:
            print(f"    !! {empty} frames with the button down but no touching contact")

    # Two-finger swipes: finger direction vs emitted wheel direction
    segments, cur = [], None
    for f in frames:
        touching = [c for c in f["contacts"] if c["tip"]]
        if len(touching) == 2:
            cx = sum(c["x"] for c in touching) / 2
            cy = sum(c["y"] for c in touching) / 2
            if cur is None:
                cur = {"t0": f["t"], "x0": cx, "y0": cy}
            cur.update(t1=f["t"], x1=cx, y1=cy)
        elif cur:
            segments.append(cur)
            cur = None
    if cur:
        segments.append(cur)

    print(f"\nTwo-finger swipes: {len(segments)}")
    print("  #   dur   finger dx    dy  angle | wheel H     V  angle | verdict")
    for i, s in enumerate(segments, 1):
        dx, dy = s["x1"] - s["x0"], s["y1"] - s["y0"]
        if abs(dx) + abs(dy) < 150:
            continue  # taps and rests
        # Windows keeps emitting inertia wheel events after lift, so include a tail.
        ws = [w for w in wheels if s["t0"] <= w["t"] <= s["t1"] + 0.6]
        wh = sum(w["delta"] for w in ws if w["axis"] == "h")
        wv = sum(w["delta"] for w in ws if w["axis"] == "v")
        fa = math.degrees(math.atan2(abs(dy), abs(dx)))
        wa = math.degrees(math.atan2(abs(wv), abs(wh))) if (wh or wv) else float("nan")
        diagonal = 25 <= fa <= 65
        if not ws:
            verdict = "no wheel events (app may use DirectManipulation)"
        elif diagonal and (wh == 0 or wv == 0):
            verdict = "DIAGONAL SWIPE COLLAPSED TO ONE AXIS"
        elif diagonal:
            verdict = "both axes ok"
        else:
            verdict = "mostly straight"
        print(f"  {i:<3} {s['t1'] - s['t0']:4.1f}s {dx:9.0f} {dy:6.0f} {fa:5.0f}° | {wh:6d} {wv:5d} {wa:5.0f}° | {verdict}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        files = sorted(RECORDINGS.glob("*.jsonl"))
        if not files:
            sys.exit("No recordings yet.")
        target = files[-1]
    analyze(target)
