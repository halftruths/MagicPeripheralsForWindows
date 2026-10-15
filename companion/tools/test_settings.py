"""Tests for per-device settings storage, the settings HTTP API and battery alerts. Touches no real Windows settings.

    python companion/tools/test_settings.py
"""

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import winapi  # noqa: E402

# Never touch the real system: stub every winapi call settings can make.
_tp = {"panEnabled": True, "tapEnabled": True, "tapAndDragEnabled": True, "sensitivityLevel": 2,
       "twoFingerTapEnabled": True, "rightClickZoneEnabled": True, "scrollDirectionReversed": False,
       "zoomEnabled": True, "cursorSpeed": 10, "allowActiveWhenMousePresent": True}
calls = []
winapi.get_touchpad = lambda: dict(_tp)
winapi.set_touchpad = lambda **kw: calls.append(("set_touchpad", kw))
winapi.get_buttons_swapped = lambda: False
winapi.set_buttons_swapped = lambda v: calls.append(("swap", v))
winapi.get_native_gestures = lambda: dict(winapi.NATIVE_DEFAULTS)
winapi.set_native_gestures = lambda w: calls.append(("native", w))

import settings as S  # noqa: E402
from alerts import BatteryAlerts  # noqa: E402

S.read_driver = lambda: {}  # no registry access

tmp = Path(tempfile.mkdtemp())
failures = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{': ' + str(detail) if not ok else ''}")
    if not ok:
        failures.append(name)


def fresh(name, content=None):
    S.STORE = tmp / name
    if content is not None:
        S.STORE.write_text(content, encoding="utf-8")
    return S.Settings()


# ---- migration ----
old = {"scroll.speed": 33.0, "three.move": "drag", "native.disableMultiFinger": False, "bogus.key": 1,
       "driver.clickForce": 99}
s = fresh("m.json", json.dumps(old))
check("migrated trackpad value", s.get("scroll.speed") == 33.0 and s.get("three.move") == "drag")
check("migrated global value", s.get("native.disableMultiFinger") is False)
new = json.loads(S.STORE.read_text(encoding="utf-8"))
check("v2 written", new["version"] == 2 and new["devices"]["trackpad"]["scroll.speed"] == 33.0
      and new["global"]["native.disableMultiFinger"] is False, new)
check("globals not under devices", "native.disableMultiFinger" not in new["devices"]["trackpad"])
check("unknown keys dropped", "bogus.key" not in json.dumps(new) and "driver.clickForce" not in json.dumps(new))
bak = S.STORE.with_name("m.json.bak")
check("backup is the old file", bak.exists() and json.loads(bak.read_text(encoding="utf-8")) == old)
s2 = S.Settings()
check("v2 reload", s2.get("scroll.speed") == 33.0 and s2.get("three.move") == "drag")

# ---- missing / corrupt ----
s = fresh("missing.json")
check("missing file -> defaults", s.get("scroll.speed") == S.BY_KEY["scroll.speed"]["default"])
s = fresh("bad.json", "{not json")
check("corrupt file -> defaults", s.get("scroll.speed") == S.BY_KEY["scroll.speed"]["default"])
s = fresh("bad2.json", json.dumps({"version": 2, "devices": "x", "global": 5}))
check("malformed v2 -> defaults", s.get("kb.batteryThreshold") == 20)
s = fresh("list.json", "[1,2]")
check("non-object -> defaults", s.get("kb.batteryThreshold") == 20)

# ---- API on Settings ----
s = fresh("api.json")
view = s.view("trackpad")
check("view.get trackpad + global", view.get("scroll.speed") == 18.0 and view.get("native.disableMultiFinger") is True)
try:
    s.view("keyboard").get("scroll.speed")
    check("keyboard view rejects trackpad key", False)
except KeyError:
    check("keyboard view rejects trackpad key", True)
check("schema_for keyboard", {x["key"] for x in S.schema_for("keyboard")} ==
      {"kb.batteryAlert", "kb.batteryThreshold", "native.disableMultiFinger", "mouse.swapButtons",
       "win.allowActiveWhenMousePresent"})
check("schema_for trackpad has tp battery, no kb",
      any(x["key"] == "tp.batteryAlert" for x in S.schema_for("trackpad"))
      and not any(x["key"].startswith("kb.") for x in S.schema_for("trackpad")))
check("global specs tagged", all(S.BY_KEY[k]["device"] == "global" for k in
                                 ("mouse.swapButtons", "win.allowActiveWhenMousePresent", "native.disableMultiFinger")))
s.update("keyboard", {"kb.batteryThreshold": 99})
check("update clamps and stores per device", s.get("kb.batteryThreshold") == 50 and s.get("tp.batteryThreshold") == 20)
saved = json.loads(S.STORE.read_text(encoding="utf-8"))
check("persisted under keyboard", saved["devices"]["keyboard"]["kb.batteryThreshold"] == 50)
for dev, key in (("keyboard", "scroll.speed"), ("trackpad", "kb.batteryAlert"), ("trackpad", "nope")):
    try:
        s.update(dev, {key: 1})
        check(f"reject {dev}/{key}", False)
    except ValueError:
        check(f"reject {dev}/{key}", True)
try:
    s.update("keyboard", {"kb.batteryAlert": False, "scroll.speed": 1})
except ValueError:
    pass
check("rejected update is atomic", s.get("kb.batteryAlert") is True)
s.update("keyboard", {"mouse.swapButtons": True})
check("global key allowed from keyboard", ("swap", True) in calls)
check("snapshot keyboard keys", set(s.snapshot("keyboard")) == {x["key"] for x in S.schema_for("keyboard")})
check("snapshot trackpad has win.*", "win.tapEnabled" in s.snapshot("trackpad"))

# ---- HTTP API ----
S.STORE = tmp / "http.json"
import server  # noqa: E402

server.settings = S.Settings()
srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"


def call(path, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Origin": base, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


try:
    st, b = call("/api/settings")
    check("GET default is trackpad", st == 200 and b["device"] == "trackpad" and "scroll.speed" in b["values"])
    st, b = call("/api/settings?device=keyboard")
    check("GET keyboard", st == 200 and b["device"] == "keyboard" and "kb.batteryAlert" in b["values"]
          and "scroll.speed" not in b["values"] and all(x["device"] in ("keyboard", "global") for x in b["schema"]))
    st, b = call("/api/settings?device=toaster")
    check("GET unknown device 400", st == 400)
    st, b = call("/api/settings", {"changes": {"scroll.speed": 20}})
    check("POST default device", st == 200 and b["values"]["scroll.speed"] == 20)
    st, b = call("/api/settings", {"device": "keyboard", "changes": {"kb.batteryThreshold": 30}})
    check("POST keyboard", st == 200 and b["values"]["kb.batteryThreshold"] == 30)
    st, b = call("/api/settings", {"device": "keyboard", "changes": {"scroll.speed": 5}})
    check("POST wrong-device key 400", st == 400)
finally:
    srv.shutdown()
    srv.server_close()

# ---- alerts ----
class Fake:
    def __init__(self, **v):
        self.v = {"tp.batteryAlert": True, "tp.batteryThreshold": 20, "kb.batteryAlert": True, "kb.batteryThreshold": 20}
        self.v.update(v)

    def get(self, key):
        return self.v[key]


def dev(name, percent, connected=True, charging=False):
    return {"device": name, "connected": connected, "percent": percent, "charging": charging}


def run(fake, seq):
    out = []
    a = BatteryAlerts(None, fake, out.append)
    return a, out, [a.check(x) for x in seq]


K = "Magic Keyboard"
a, out, _ = run(Fake(), [[dev(K, 25)], [dev(K, 20)], [dev(K, 19)], [dev(K, 18)]])
check("alerts once per crossing", len(out) == 1 and out[0]["detail"] == "Magic Keyboard 20%"
      and out[0]["name"] == "Low battery" and out[0]["type"] == "action", out)
a, out, _ = run(Fake(), [[dev(K, 18)], [dev(K, 22)], [dev(K, 18)]])
check("no re-arm inside margin", len(out) == 1, out)
a, out, _ = run(Fake(), [[dev(K, 18)], [dev(K, 26)], [dev(K, 18)]])
check("re-arm above threshold+5", len(out) == 2, out)
a, out, _ = run(Fake(), [[dev(K, 18)], [dev(K, 18, charging=True)], [dev(K, 18)]])
check("re-arm after charging", len(out) == 2, out)
a, out, _ = run(Fake(), [[dev(K, 18, charging=True)], [dev(K, 18, connected=False)], [dev(K, None)]])
check("no alert when charging/disconnected/unknown", out == [], out)
a, out, _ = run(Fake(**{"kb.batteryAlert": False}), [[dev(K, 5)]])
check("disabled -> silent", out == [], out)
a, out, _ = run(Fake(**{"kb.batteryThreshold": 10}), [[dev(K, 15)], [dev(K, 9)]])
check("threshold respected", len(out) == 1 and out[0]["detail"].endswith("9%"), out)
a, out, _ = run(Fake(), [[dev(K, 10), dev("Magic Trackpad", 12), dev("Other", 1)]])
check("devices independent, unknown ignored", [e["detail"] for e in out] == ["Magic Keyboard 10%", "Magic Trackpad 12%"], out)
a, out, _ = run(Fake(), [[dev(K, 10)]])
a.settings.v["kb.batteryAlert"] = False
a.check([dev(K, 10)])
a.settings.v["kb.batteryAlert"] = True
a.check([dev(K, 10)])
check("enable after already alerted stays quiet until re-arm", len(out) == 1, out)

print("\nFAILED: " + ", ".join(failures) if failures else "\nall passed")
sys.exit(1 if failures else 0)
