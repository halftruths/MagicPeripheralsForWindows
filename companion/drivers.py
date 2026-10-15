"""Driver management for the Magic Trackpad driver (vitoplantamura/MagicTrackpad2ForWindows).

Read side: probes installed package, per-device binding, competing software and environment (cached ~30 s), plus a
GitHub release check cached on disk for 6 h. The status snapshot never touches the network.

Write side (install / reinstall / uninstall / restart device): a non-admin pipeline downloads, verifies and extracts the
release; one elevated PowerShell (a single UAC prompt, passed as -EncodedCommand so there is no script file to tamper
with) copies the verified files into an admin-only staging directory, re-hashes them there and runs pnputil. Nothing
downloaded is ever executed; only pnputil touches the INF.

Set MC_DRIVER_DRY_RUN=1 (or drivers.DRY_RUN = True) to run everything up to, but not including, the elevated step.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import winreg
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from paths import BUILD_DIR, DATA_DIR

STATE_FILE = DATA_DIR / "driver.local.json"
ZIP_CACHE = DATA_DIR / "driver-cache"
INF_SOURCE = BUILD_DIR / "AmtPtpDevice_AMD64.inf"

DRY_RUN = os.environ.get("MC_DRIVER_DRY_RUN") == "1"
AUTO_CHECK = True  # GET /api/drivers may start a background release check when the cache is older than RELEASE_TTL

REPO = "vitoplantamura/MagicTrackpad2ForWindows"
API_URL = f"https://api.github.com/repos/{REPO}/releases"
DOWNLOAD_RE = re.compile(r"^https://github\.com/vitoplantamura/MagicTrackpad2ForWindows/releases/download/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.zip$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
SIGNER = "Microsoft Windows Hardware Compatibility Publisher"
MAX_ZIP_BYTES = 40 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024

PROBE_TTL = 30
RELEASE_TTL = 6 * 3600
RETRY_AFTER_FAILURE = 600
CREATE_NO_WINDOW = 0x08000000

# Hardware IDs this driver binds (from the INF; the fallback covers the Trackpad 2 and its USB-C revision).
DEFAULT_PIDS = {0x0265: "01", 0x0324: "01"}
KEYBOARD_PIDS = {0x0320, 0x0267}  # Magic Keyboard: inbox drivers only
PROBLEM_TEXT = {
    1: "not configured correctly", 10: "device cannot start", 22: "disabled", 28: "no driver installed",
    31: "driver failed to load", 37: "driver returned a failure", 39: "driver is corrupt or missing", 43: "device reported a problem",
    52: "Windows cannot verify the driver signature",
}


def _load_hardware_ids():
    """Trackpad PIDs and the USB interface the driver binds, read from the INF in this repo."""
    usb = {}
    try:
        text = INF_SOURCE.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"USB\\Vid_05ac&Pid_([0-9a-f]{4})&MI_(\d\d)", text, re.I):
            usb[int(m.group(1), 16)] = m.group(2)
    except OSError:
        pass
    return usb or dict(DEFAULT_PIDS)


USB_MI = _load_hardware_ids()  # pid -> bound interface, e.g. {0x0324: "01"}
TRACKPAD_PIDS = set(USB_MI)

publish = lambda event: None  # set by the server to hub.publish


def set_publisher(fn):
    global publish
    publish = fn


class DriverError(Exception):
    """A user-presentable failure; its message ends up in the action log."""


class Busy(Exception):
    pass


# --------------------------------------------------------------------------------------------- PowerShell helpers

def run_ps(script, env=None, timeout=90):
    """Run a script in a hidden Windows PowerShell as UTF-8 and return (exit code, stdout, stderr)."""
    full = "$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + script
    enc = base64.b64encode(full.encode("utf-16-le")).decode()
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], capture_output=True,
                       timeout=timeout, env=e, creationflags=CREATE_NO_WINDOW)
    return p.returncode, p.stdout.decode("utf-8-sig", "replace"), p.stderr.decode("utf-8", "replace")


def psq(s):
    return "'" + str(s).replace("'", "''") + "'"


# --------------------------------------------------------------------------------------------- state file

_state_lock = threading.RLock()


def _read_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _update_state(**changes):
    with _state_lock:
        st = _read_state()
        st.update(changes)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
        return st


# --------------------------------------------------------------------------------------------- probes

_PROBE_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$want = 'DEVPKEY_Device_DriverInfPath','DEVPKEY_Device_DriverVersion','DEVPKEY_Device_DriverDate','DEVPKEY_Device_ProblemCode','DEVPKEY_Device_Service','DEVPKEY_Device_DriverProvider'
$out = foreach ($d in Get-PnpDevice) {
  if ($d.InstanceId -notmatch 'VID_05AC|VID&0001004C|VID&0002004C') { continue }
  $o = [ordered]@{ id = $d.InstanceId; present = [bool]$d.Present; name = $d.FriendlyName; cls = [string]$d.Class }
  foreach ($p in (Get-PnpDeviceProperty -InstanceId $d.InstanceId)) {
    if ($want -contains $p.KeyName) { $o[$p.KeyName.Replace('DEVPKEY_Device_','')] = if ($p.Data -is [datetime]) { $p.Data.ToString('yyyy-MM-dd') } else { $p.Data } }
  }
  [pscustomobject]$o
}
@($out) | ConvertTo-Json -Compress -Depth 3
"""


def _read_nodes():
    code, out, err = run_ps(_PROBE_PS, timeout=120)
    if code != 0 or not out.strip():
        raise DriverError("device probe failed: " + (err.strip() or f"exit {code}"))
    data = json.loads(out)
    return [data] if isinstance(data, dict) else data


def _reg_values(key):
    vals = {}
    i = 0
    while True:
        try:
            name, value, _ = winreg.EnumValue(key, i)
        except OSError:
            return vals
        vals[name] = value
        i += 1


def _subkeys(key):
    i = 0
    while True:
        try:
            yield winreg.EnumKey(key, i)
        except OSError:
            return
        i += 1


def _driver_packages():
    """Driver store packages (readable without admin): [{package, oem, provider, inf, signer}]."""
    pk = []
    try:
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\DriverDatabase\DriverPackages", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
    except OSError:
        return pk
    with base:
        for name in _subkeys(base):
            try:
                with winreg.OpenKey(base, name) as k:
                    v = _reg_values(k)
            except OSError:
                continue
            pk.append({"package": name, "oem": str(v.get("", "")), "provider": str(v.get("Provider", "")),
                       "inf": str(v.get("InfName", "")), "signer": str(v.get("SignerName", ""))})
    return pk


def _parse_driver_ver(text):
    m = re.search(r"^\s*DriverVer\s*=\s*(\d+)/(\d+)/(\d+)\s*,\s*([\d.]+)", text, re.M | re.I)
    if not m:
        return None, None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat(), m.group(4)
    except ValueError:
        return None, m.group(4)


def _store_driver_ver(package):
    d = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "DriverStore" / "FileRepository" / package
    try:
        for f in d.glob("*.inf"):
            return _parse_driver_ver(f.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pass
    return None, None


def _ours(pkg):
    return pkg["package"].lower().startswith("amtptpdevice.inf") or pkg["inf"].lower() == "amtptpdevice.inf"


COMPETE_APPS = re.compile(r"magic ?utilities|trackpad ?\+\+|boot ?camp|magic ?trackpad|magic ?mouse|mac-precision|applewtp|applemtp", re.I)
COMPETE_SERVICES = re.compile(r"magic ?utilit|magicmouse|magictrackpad|^mu[_a-z]*service|applewtp|applemtp|trackpadplus|trackpad\+\+|bootcamp", re.I)
COMPETE_PACKAGES = re.compile(r"trackpad|multitouch|applemtp|applewtp|appleuserhid|magic ?(mouse|utilit)|bootcamp", re.I)


def _competitors(packages):
    found = []
    roots = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY),
             (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0)]
    for hive, path, flag in roots:
        try:
            base = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | flag)
        except OSError:
            continue
        with base:
            for name in _subkeys(base):
                try:
                    with winreg.OpenKey(base, name) as k:
                        v = _reg_values(k)
                except OSError:
                    continue
                title, pub = str(v.get("DisplayName", "")), str(v.get("Publisher", ""))
                if title and COMPETE_APPS.search(title) and "plantamura" not in pub.lower():
                    found.append({"kind": "app", "name": title, "detail": pub})
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            for name in _subkeys(k):
                if COMPETE_SERVICES.search(name) and not name.lower().startswith("amtptp"):
                    found.append({"kind": "service", "name": name, "detail": "Windows service / driver"})
    except OSError:
        pass
    for p in packages:
        if _ours(p):
            continue
        if COMPETE_PACKAGES.search(p["package"]) or re.search(r"magic utilities|trackpad", p["provider"], re.I):
            found.append({"kind": "driver", "name": p["package"].split(".inf_")[0] + ".inf", "detail": f'{p["provider"]} ({p["oem"]})'})
    seen, unique = set(), []
    for f in found:
        if (f["kind"], f["name"].lower()) not in seen:
            seen.add((f["kind"], f["name"].lower()))
            unique.append(f)
    return unique


def _environment():
    env = []
    sb = None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\SecureBoot\State", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            sb = int(winreg.QueryValueEx(k, "UEFISecureBootEnabled")[0])
    except OSError:
        pass
    if sb == 0:
        env.append({"id": "secureboot", "level": "info", "title": "Secure Boot: off",
                    "text": "Expected here. Microsoft-signed releases load either way. Some games with kernel anti-cheat (Vanguard, FACEIT, some others) refuse to run with it off."})
    elif sb == 1:
        env.append({"id": "secureboot", "level": "info", "title": "Secure Boot: on",
                    "text": "Microsoft-signed releases load fine; self-signed or test-signed builds will not."})
    else:
        env.append({"id": "secureboot", "level": "info", "title": "Secure Boot: unknown", "text": "Windows does not report a Secure Boot state (legacy BIOS or unsupported)."})
    opts = ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            opts = str(winreg.QueryValueEx(k, "SystemStartOptions")[0])
    except OSError:
        pass
    if "TESTSIGNING" in opts.upper():
        env.append({"id": "testsigning", "level": "warn", "title": "Test signing: on",
                    "text": "Windows is in test-signing mode (watermark on the desktop, anti-cheat will object). Not needed for the Microsoft-signed release."})
    return env


def _classify(nodes, packages):
    ours_oems = {p["oem"].lower() for p in packages if _ours(p)}
    bt_re = re.compile(r"^HID\\\{00001124-[0-9A-F-]+\}_VID&000[12]004C_PID&([0-9A-F]{4})&COL(0[12])\\", re.I)
    usb_re = re.compile(r"^USB\\VID_05AC&PID_([0-9A-F]{4})&MI_(\d\d)\\", re.I)
    kb_re = re.compile(r"^HID\\\{00001124-[0-9A-F-]+\}_VID&000[12]004C_PID&([0-9A-F]{4})&COL(\d\d)\\", re.I)
    groups = {
        "tp-bt": {"id": "tp-bt", "title": "Trackpad over Bluetooth", "transport": "bluetooth", "rows": [],
                  "hint": "Settings are re-read only after the trackpad is power-cycled (button on its side)."},
        "tp-usb": {"id": "tp-usb", "title": "Trackpad over USB-C", "transport": "usb", "rows": [],
                   "hint": "Settings are re-read after re-plugging the cable."},
        "kb-bt": {"id": "kb-bt", "title": "Keyboard over Bluetooth", "transport": "bluetooth", "rows": [],
                  "hint": "Uses Windows' inbox drivers. Nothing to install."},
        "kb-usb": {"id": "kb-usb", "title": "Keyboard over USB-C", "transport": "usb", "rows": [],
                   "hint": "Uses Windows' inbox drivers. Nothing to install."},
    }
    # Magic Keyboard interfaces/collections (inbox drivers): BT exposes COL01 keyboard + COL02 vendor; USB exposes
    # MI_00 Device Management (problem 10 like the trackpad's, harmless) and MI_01 with keyboard/media/vendor collections.
    kb_bt_names = {"01": "Keyboard (COL01)", "02": "Vendor, battery (COL02)"}
    kb_mi_names = {"00": "device management", "01": "keyboard"}
    kb_col_names = {"01": "Keyboard (COL01)", "02": "Media keys (COL02)", "03": "Vendor, battery (COL03)"}
    kb_hid_usb_re = re.compile(r"^HID\\VID_05AC&PID_([0-9A-F]{4})&MI_(\d\d)&COL(\d\d)\\", re.I)

    def row(n, label, expect_ours, mi=None):
        inf = str(n.get("DriverInfPath") or "")
        provider = str(n.get("DriverProvider") or "")
        is_ours = inf.lower() in ours_oems or "plantamura" in provider.lower()
        present = bool(n.get("present"))
        problem = int(n.get("ProblemCode") or 0) if present else 0
        r = {"instanceId": n["id"], "label": label, "present": present, "problem": problem, "inf": inf, "service": str(n.get("Service") or ""),
             "version": str(n.get("DriverVersion") or ""), "provider": provider, "ours": is_ours, "note": ""}
        if not present:
            r.update(state="muted", note="Not connected.")
        elif problem:
            r["state"] = "problem"
            r["note"] = f"Problem code {problem}: {PROBLEM_TEXT.get(problem, 'see Device Manager')}."
            if mi == "00" and problem == 10:
                r["note"] += " Known upstream issue #50 (interface 0); the trackpad usually works regardless."
        elif expect_ours and not is_ours:
            r.update(state="warn", note="Windows' inbox driver is bound instead of the Magic driver. Install or reinstall the driver, then reconnect the trackpad.")
        elif is_ours:
            r.update(state="ok", note="Magic driver bound.")
        else:
            r.update(state="ok", note="Inbox Windows driver (expected).")
        return r

    for n in nodes:
        pid_id = n.get("id", "")
        m = bt_re.match(pid_id)
        if m and int(m.group(1), 16) in TRACKPAD_PIDS:
            col = m.group(2)
            groups["tp-bt"]["rows"].append(row(n, "Touchpad (COL01)" if col == "01" else "Auxiliary services (COL02)", True))
            continue
        m = usb_re.match(pid_id)
        if m and int(m.group(1), 16) in TRACKPAD_PIDS:
            mi = m.group(2)
            bound = USB_MI[int(m.group(1), 16)] == mi
            groups["tp-usb"]["rows"].append(row(n, f"Interface {int(mi)} (MI_{mi})" + (" - touchpad" if bound else ""), bound, mi))
            continue
        if m and int(m.group(1), 16) in KEYBOARD_PIDS:
            mi = m.group(2)
            suffix = kb_mi_names.get(mi)
            groups["kb-usb"]["rows"].append(row(n, f"Interface {int(mi)} (MI_{mi})" + (f" - {suffix}" if suffix else ""), False, mi))
            continue
        m = kb_hid_usb_re.match(pid_id)
        if m and int(m.group(1), 16) in KEYBOARD_PIDS:
            groups["kb-usb"]["rows"].append(row(n, kb_col_names.get(m.group(3), f"Collection COL{m.group(3)}"), False))
            continue
        m = kb_re.match(pid_id)
        if m and int(m.group(1), 16) in KEYBOARD_PIDS:
            groups["kb-bt"]["rows"].append(row(n, kb_bt_names.get(m.group(2), f"Interface COL{m.group(2)}"), False))
    out = []
    for g in groups.values():
        g["rows"].sort(key=lambda r: (not r["present"], r["instanceId"].upper()))
        g["connected"] = any(r["present"] for r in g["rows"])
        g["rows"] = [r for r in g["rows"] if r["present"]] or g["rows"]  # remembered (absent) nodes only when nothing is connected
        if g["rows"]:
            out.append(g)
    return out


_probe = {"t": 0.0, "data": None, "error": None, "phase": None, "label": None}
_probe_lock = threading.Lock()


def _phase(phase, label):
    """Record and publish the probe step that is starting (shown as the status phrase on the Drivers page)."""
    _probe["phase"], _probe["label"] = phase, label
    try:
        publish({"type": "driver", "probe": phase, "label": label})
    except Exception:
        pass


def _do_probe():
    _phase("store", "Reading the driver store…")
    packages = _driver_packages()
    ours = [p for p in packages if _ours(p)]
    installed = []
    for p in ours:
        d, v = _store_driver_ver(p["package"])
        installed.append({"oem": p["oem"], "inf": p["inf"], "provider": p["provider"], "signer": p["signer"], "package": p["package"], "date": d, "version": v})
    _phase("devices", "Checking how each device is bound…")  # the slow PowerShell step
    nodes = _read_nodes()
    groups = _classify(nodes, packages)
    _phase("software", "Looking for conflicting software…")
    competitors = _competitors(packages)
    _phase("environment", "Checking Secure Boot and test signing…")
    environment = _environment()
    if _read_state().get("release"):
        _phase("release", "Comparing with the latest release…")
    return {"t": time.time(), "installed": installed, "groups": groups, "competitors": competitors, "environment": environment,
            "node_ids": {n["id"] for n in nodes if n.get("present")}}


def refresh_probe(shared=False):
    """Run a probe. With shared=True, a probe already in flight is awaited instead of starting a second one."""
    if shared:
        if not _probe_lock.acquire(blocking=False):
            with _probe_lock:  # wait for the running probe, then use its result
                return
    else:
        _probe_lock.acquire()
    try:
        try:
            _probe["data"] = _do_probe()
            _probe["error"] = None
        except Exception as e:  # keep serving the previous snapshot
            _probe["error"] = str(e)
        _probe["t"] = time.time()
    finally:
        _probe["phase"] = _probe["label"] = None
        _probe_lock.release()
        try:
            publish({"type": "driver", "probe": "done"})
        except Exception:
            pass


def warm_probe():
    """Start the first probe in the background (called at server startup)."""
    if _probe["data"] is None and not _probe_lock.locked() and time.time() - _probe["t"] > PROBE_TTL:  # t == 0 before the first probe
        threading.Thread(target=refresh_probe, kwargs={"shared": True}, daemon=True, name="driver-probe-warm").start()


def invalidate():
    """Mark the cached snapshot stale (e.g. a device was plugged in): the next GET refreshes it in the background."""
    _probe["t"] = 1.0 if _probe["data"] is not None else _probe["t"]


def start_refresh():
    """Start a probe in the background, or join the one already running. Returns immediately."""
    if not _probe_lock.locked():
        threading.Thread(target=refresh_probe, kwargs={"shared": True}, daemon=True, name="driver-probe-refresh").start()
    return True


def get_probe(block=True):
    """Cached probe; refreshes in the background when stale. Blocks only for the very first probe (shares one in flight),
    or returns (None, error) immediately with block=False."""
    age = time.time() - _probe["t"]
    if _probe["data"] is None:
        if block:
            refresh_probe(shared=True)
        else:
            warm_probe()
    elif age > PROBE_TTL and not _probe_lock.locked():
        threading.Thread(target=refresh_probe, kwargs={"shared": True}, daemon=True).start()
    return _probe["data"], _probe["error"]


# --------------------------------------------------------------------------------------------- release info

class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urlparse(newurl)
        host = u.hostname or ""
        if u.scheme != "https" or not (host == "github.com" or host.endswith(".githubusercontent.com")):
            raise urllib.error.URLError(f"redirect to disallowed location {host!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)
_UA = {"User-Agent": "MagicCompanion-driver-check", "Accept": "application/vnd.github+json"}


def _fetch_releases():
    req = urllib.request.Request(API_URL, headers=_UA)
    with _opener.open(req, timeout=12) as r:
        return json.loads(r.read(4_000_000).decode("utf-8"))


def _pick_release(releases):
    """Newest stable release with a Microsoft-signed zip. The SelfSigned zip is never considered."""
    best = None
    for rel in releases:
        if rel.get("prerelease") or rel.get("draft"):
            continue
        tag = str(rel.get("tag_name", ""))
        if not SAFE_NAME.match(tag):
            continue
        for a in rel.get("assets", []):
            name, url = str(a.get("name", "")), str(a.get("browser_download_url", ""))
            if not (name.lower().endswith(".zip") and "mssigned" in name.lower() and "selfsigned" not in name.lower()):
                continue
            if not (SAFE_NAME.match(name) and DOWNLOAD_RE.match(url) and url.endswith("/" + name) and f"/download/{tag}/" in url):
                continue
            digest = str(a.get("digest") or "")
            sha = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else ""
            cand = {"tag": tag, "name": rel.get("name") or tag, "published": rel.get("published_at"), "asset": name, "url": url,
                    "size": int(a.get("size") or 0), "sha256": sha if re.fullmatch(r"[0-9a-f]{64}", sha) else ""}
            if best is None or str(cand["published"]) > str(best["published"]):
                best = cand
            break
    return best


def check_release():
    """Network call; updates the on-disk cache. Keeps the previous result (marked stale) on failure."""
    now = time.time()
    try:
        rel = _pick_release(_fetch_releases())
        if rel is None:
            raise DriverError("no stable Microsoft-signed release found")
        _update_state(release=rel, checked_at=now, attempted_at=now, error=None)
    except urllib.error.HTTPError as e:
        msg = f"GitHub answered HTTP {e.code}"
        if e.code in (403, 429):
            reset = e.headers.get("X-RateLimit-Reset")
            msg = "GitHub rate limit reached" + (f", resets {datetime.fromtimestamp(int(reset)):%H:%M}" if reset and reset.isdigit() else "")
        _update_state(attempted_at=now, error=msg)
    except (urllib.error.URLError, OSError, ValueError, DriverError) as e:
        reason = getattr(e, "reason", e)
        _update_state(attempted_at=now, error=("offline or unreachable: " if isinstance(e, urllib.error.URLError) else "") + str(reason))


def _maybe_auto_check():
    st = _read_state()
    now = time.time()
    if now - st.get("checked_at", 0) < RELEASE_TTL or now - st.get("attempted_at", 0) < RETRY_AFTER_FAILURE:
        return
    _update_state(attempted_at=now)
    threading.Thread(target=check_release, daemon=True).start()


def _asset_date(rel):
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", rel.get("asset", ""))
    try:
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return datetime.fromisoformat(rel["published"].replace("Z", "+00:00")).date()
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _release_status(installed, rel, versions):
    if not rel:
        return "unchecked"
    if not installed:
        return "not_installed"
    tag = versions.get(installed.get("version"))
    if tag:
        return "current" if tag == rel["tag"] else "update"
    # The installed version is not mapped to a tag (installed outside this app): compare the INF date to the release's.
    try:
        d, rd = date.fromisoformat(installed["date"]), _asset_date(rel)
    except (TypeError, ValueError):
        return "unknown"
    if rd is None:
        return "unknown"
    return "probably_current" if d >= rd - timedelta(days=3) else "update"


def _cached_zips(st=None):
    st = st or _read_state()
    out = []
    for tag, v in (st.get("verified") or {}).items():
        if (ZIP_CACHE / v.get("file", "?")).is_file():
            out.append({"tag": tag, "file": v["file"], "sha256": v["sha256"], "version": v.get("version"), "at": v.get("at")})
    return out


# --------------------------------------------------------------------------------------------- actions

TERMINAL = ("done", "failed")
_actions = {}
_current = None
_action_lock = threading.Lock()


def _brief(a, with_log=True):
    d = {k: a[k] for k in ("id", "kind", "state", "detail", "started", "finished", "dry_run") if k in a}
    if with_log:
        d["log"] = list(a["log"])
    if "result" in a:
        d["result"] = a["result"]
    return d


def _set(a, state, detail="", line=None):
    a["state"], a["detail"] = state, detail
    a["log"].append(f"[{datetime.now():%H:%M:%S}] {line or (state + (': ' + detail if detail else ''))}")
    if state in TERMINAL:
        a["finished"] = time.time()
        _update_state(last_action=_brief(a))
    publish({"type": "driver", "actionId": a["id"], "state": state, "detail": detail, "log": a["log"][-60:]})


def _log(a, line):
    a["log"].append(line)
    publish({"type": "driver", "actionId": a["id"], "state": a["state"], "detail": a["detail"], "log": a["log"][-60:]})


def _start_action(kind, fn):
    global _current
    with _action_lock:
        if _current and _actions[_current]["state"] not in TERMINAL:
            raise Busy()
        a = {"id": secrets.token_hex(6), "kind": kind, "state": "queued", "detail": "", "started": time.time(), "log": [], "dry_run": DRY_RUN}
        _actions[a["id"]] = a
        _current = a["id"]
    threading.Thread(target=_worker, args=(a, fn), daemon=True).start()
    return a["id"]


def _worker(a, fn):
    try:
        fn(a)
    except DriverError as e:
        _set(a, "failed", str(e))
    except Exception as e:
        traceback.print_exc()
        _set(a, "failed", f"unexpected error: {e}")


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url, dest, expect_size, a):
    if not DOWNLOAD_RE.match(url):
        raise DriverError("refusing to download from an unexpected URL")
    req = urllib.request.Request(url, headers={"User-Agent": _UA["User-Agent"]})
    done = 0
    try:
        with _opener.open(req, timeout=30) as r, open(dest, "wb") as out:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                done += len(chunk)
                if done > expect_size or done > MAX_ZIP_BYTES:
                    raise DriverError("download is larger than the size GitHub reported")
                out.write(chunk)
    except (urllib.error.URLError, OSError) as e:
        raise DriverError(f"download failed: {getattr(e, 'reason', e)}")
    if done != expect_size:
        raise DriverError(f"download is {done} bytes, expected {expect_size}")


download_fn = _download  # replaceable in tests


def safe_extract_amd64(zip_path, dest):
    """Extract only AMD64/<flat file> entries; rejects traversal, nesting, symlinks and oversize files."""
    dest = Path(dest).resolve()
    files, total, seen = [], 0, set()
    with zipfile.ZipFile(zip_path) as z:
        for zi in z.infolist():
            name = zi.filename.replace("\\", "/")
            if not name.upper().startswith("AMD64/") or zi.is_dir():
                continue
            parts = name.split("/")
            if len(parts) != 2 or not SAFE_NAME.match(parts[1]) or ".." in parts:
                raise DriverError(f"unexpected entry in zip: {zi.filename!r}")
            if (zi.external_attr >> 16) & 0o170000 == 0o120000:
                raise DriverError(f"symlink in zip: {zi.filename!r}")
            fname = parts[1]
            if fname.lower() in seen:
                raise DriverError(f"duplicate entry in zip: {fname}")
            seen.add(fname.lower())
            if zi.file_size > MAX_FILE_BYTES:
                raise DriverError(f"{fname} is too large")
            target = (dest / fname).resolve()
            if target.parent != dest:
                raise DriverError("path escapes the extraction directory")
            written = 0
            with z.open(zi) as src, open(target, "wb") as out:
                for chunk in iter(lambda: src.read(1 << 16), b""):
                    written += len(chunk)
                    total += len(chunk)
                    if written > MAX_FILE_BYTES or total > 4 * MAX_FILE_BYTES:
                        raise DriverError("zip expands beyond the allowed size")
                    out.write(chunk)
            files.append({"n": fname, "h": _sha256_file(target)})
    return files


_SIG_PS = r"""
Get-ChildItem -LiteralPath $env:MC_DIR -File | Where-Object { $_.Extension -in '.cat','.sys','.dll' } | ForEach-Object {
  $s = Get-AuthenticodeSignature -LiteralPath $_.FullName
  [pscustomobject]@{ file = $_.Name; status = [string]$s.Status; subject = [string]$s.SignerCertificate.Subject }
} | ConvertTo-Json -Compress
"""


def verify_signatures(directory):
    code, out, err = run_ps(_SIG_PS, env={"MC_DIR": str(directory)})
    if code != 0 or not out.strip():
        raise DriverError("could not check signatures: " + (err.strip() or f"exit {code}"))
    sigs = json.loads(out)
    sigs = [sigs] if isinstance(sigs, dict) else sigs
    if not any(s["file"].lower().endswith(".cat") for s in sigs):
        raise DriverError("release has no catalog (.cat) file")
    bad = [f'{s["file"]}: {s["status"]}' for s in sigs if s["status"] != "Valid" or SIGNER not in s["subject"]]
    if bad:
        raise DriverError("signature check failed (" + "; ".join(bad) + ")")
    return sigs


def prepare_release(a, rel, workdir):
    """Download (or reuse the cached verified zip), verify size/sha256, extract AMD64 and check signatures."""
    if not rel.get("sha256"):
        raise DriverError("GitHub published no SHA-256 digest for this asset; refusing to install it")
    ZIP_CACHE.mkdir(exist_ok=True)
    cached = ZIP_CACHE / f'{rel["tag"]}-{rel["sha256"][:12]}-{rel["asset"]}'
    if cached.is_file() and _sha256_file(cached) == rel["sha256"]:
        _set(a, "verifying", "using the cached, previously verified zip")
        zpath = cached
    else:
        _set(a, "downloading", f'{rel["asset"]} ({rel["size"] // 1024} KB)')
        zpath = Path(workdir) / rel["asset"]
        download_fn(rel["url"], zpath, rel["size"], a)
        _set(a, "verifying", "checking SHA-256 against GitHub's digest")
        if _sha256_file(zpath) != rel["sha256"]:
            raise DriverError("SHA-256 of the download does not match GitHub's digest")
    ext = Path(workdir) / "AMD64"
    ext.mkdir()
    files = safe_extract_amd64(zpath, ext)
    infs = [f["n"] for f in files if f["n"].lower().endswith(".inf")]
    if len(infs) != 1:
        raise DriverError("expected exactly one .inf in AMD64/")
    inf_text = (ext / infs[0]).read_text(encoding="utf-8", errors="replace")
    cat = re.search(r"^\s*CatalogFile(?:\.NT\w*)?\s*=\s*(\S+)", inf_text, re.M | re.I)
    if not cat or cat.group(1).lower() not in {f["n"].lower() for f in files}:
        raise DriverError("INF names a catalog that is not in the release")
    _set(a, "verifying", "checking Authenticode signatures (catalog, driver, DLL)")
    sigs = verify_signatures(ext)
    _log(a, "signatures: " + ", ".join(f'{s["file"]}={s["status"]}' for s in sigs))
    d, v = _parse_driver_ver(inf_text)
    if zpath != cached:
        shutil.copyfile(zpath, cached)
    with _state_lock:
        st = _read_state()
        verified = st.get("verified") or {}
        versions = st.get("versions") or {}
        verified[rel["tag"]] = {"file": cached.name, "sha256": rel["sha256"], "version": v, "at": time.time()}
        if v:
            versions[v] = rel["tag"]
        _update_state(verified=verified, versions=versions)
    return {"dir": str(ext), "files": files, "inf": infs[0], "version": v, "date": d}


# ---- elevated script ----

_HEADER = r"""$ErrorActionPreference = 'Stop'
$Id = @@ID@@
$Root = @@ROOT@@
$SkipAcl = @@SKIPACL@@
$Pnp = @@PNPUTIL@@
$Logs = Join-Path $Root 'logs'
$Log = Join-Path $Logs ($Id + '.log')
$Stage = Join-Path $Root ('staging-' + $Id)
function Log([string]$m) { Add-Content -LiteralPath $Log -Value ('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $m) -Encoding UTF8 }
function Done([int]$code, [string]$m) { if ($m) { Log $m }; Log ('RESULT exit=' + $code); exit $code }
function Run-Pnp([string[]]$a) {
  $ErrorActionPreference = 'Continue'  # PowerShell 5.1 turns native stderr into terminating errors under 'Stop'
  $o = (& $Pnp @a 2>&1 | Out-String)
  $script:PnpCode = $LASTEXITCODE
  return $o
}
function Lock-Dir([string]$p, [bool]$usersRead) {
  if ($SkipAcl) { return }
  $g = @('/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F')
  if ($usersRead) { $g += '*S-1-5-32-545:(OI)(CI)RX' }
  $o = & icacls.exe $p @g 2>&1 | Out-String
  if ($LASTEXITCODE -ne 0) { throw ('icacls failed: ' + $o) }
}
function Ensure-Dir([string]$p) {
  if (Test-Path -LiteralPath $p) {
    $i = Get-Item -LiteralPath $p -Force
    if ($i.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw ('unsafe directory (reparse point): ' + $p) }
    $sid = (Get-Acl -LiteralPath $p).GetOwner([Security.Principal.SecurityIdentifier]).Value
    if (-not $SkipAcl -and $sid -notin @('S-1-5-18', 'S-1-5-32-544')) { throw ('unsafe directory (not owned by SYSTEM/Administrators): ' + $p) }
  } else {
    [void][IO.Directory]::CreateDirectory($p)
  }
  Lock-Dir $p $true
}
$ExitCode = 0
try {
  Ensure-Dir $Root
  Ensure-Dir $Logs
  Log ('action @@KIND@@ started by the Magic companion (id ' + $Id + ')')
@@BODY@@
} catch {
  Log ('FAILED: ' + $_.Exception.Message)
  $ExitCode = 4
} finally {
  if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force -ErrorAction SilentlyContinue }
}
Done $ExitCode ''
"""

_STAGE_BODY = r"""  if (Test-Path -LiteralPath $Stage) { throw 'staging directory already exists' }
  [void][IO.Directory]::CreateDirectory($Stage)
  Lock-Dir $Stage $false
  $Src = @@SRC@@
  $Files = @(
@@FILES@@
  )
  foreach ($f in $Files) {
    $d = Join-Path $Stage $f.n
    Copy-Item -LiteralPath (Join-Path $Src $f.n) -Destination $d
    $h = (Get-FileHash -LiteralPath $d -Algorithm SHA256).Hash.ToLower()
    if ($h -ne $f.h) { throw ('hash mismatch after staging: ' + $f.n) }
  }
  Log ('staged and re-hashed ' + $Files.Count + ' files in ' + $Stage)
  foreach ($f in ($Files | Where-Object { $_.n -match '\.(cat|sys|dll)$' })) {
    $sig = Get-AuthenticodeSignature -LiteralPath (Join-Path $Stage $f.n)
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notlike '*@@SIGNER@@*') { throw ('bad signature on ' + $f.n + ': ' + $sig.Status) }
  }
  Log 'signatures valid (@@SIGNER@@)'
@@REINSTALL@@
  $out = Run-Pnp @('/add-driver', (Join-Path $Stage @@INF@@), '/install')
  $code = $PnpCode
  Log $out.Trim()
  Log ('pnputil /add-driver exit code ' + $code)
  if ($code -ne 0 -and $code -ne 3010) { throw ('pnputil /add-driver failed with ' + $code) }
  $pub = [regex]::Match($out, 'oem\d+\.inf').Value
  Log ('published as ' + $pub)
  foreach ($old in @(@@OLD@@)) {
    if ($old -and $old -ne $pub) {
      $o = Run-Pnp @('/delete-driver', $old)
      Log ('pnputil /delete-driver ' + $old + ' exit ' + $PnpCode + ' ' + $o.Trim())
    }
  }
  if ($code -eq 3010) { Log 'Windows asks for a restart to finish.' }
"""

_REINSTALL_PRE = r"""  foreach ($old in @(@@OLD@@)) {
    $o = Run-Pnp @('/delete-driver', $old, '/uninstall', '/force')
    Log ('pnputil /delete-driver ' + $old + ' /uninstall /force exit ' + $PnpCode + ' ' + $o.Trim())
  }"""

_UNINSTALL_BODY = r"""  $o = Run-Pnp @('/delete-driver', @@OEM@@, '/uninstall', '/force')
  $code = $PnpCode
  Log $o.Trim()
  Log ('pnputil /delete-driver exit code ' + $code)
  if ($code -ne 0 -and $code -ne 3010) { throw ('pnputil /delete-driver failed with ' + $code) }
"""

_RESTART_BODY = r"""  $o = Run-Pnp @('/restart-device', @@DEVICE@@)
  $code = $PnpCode
  Log $o.Trim()
  Log ('pnputil /restart-device exit code ' + $code)
  if ($code -ne 0 -and $code -ne 3010) { throw ('pnputil /restart-device failed with ' + $code) }
"""

OEM_RE = re.compile(r"^oem\d{1,5}\.inf$", re.I)


def data_root():
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "MagicPeripherals"


def build_script(kind, action_id, *, prep=None, old_oems=(), oem=None, instance_id=None, reinstall=False, root=None, pnputil="pnputil.exe", skip_acl=False):
    """Text of the elevated PowerShell. Every embedded value is validated and single-quoted."""
    if not re.fullmatch(r"[0-9a-f]{12}", action_id):
        raise DriverError("bad action id")
    for o in list(old_oems) + ([oem] if oem else []):
        if not OEM_RE.match(o):
            raise DriverError(f"bad driver package name {o!r}")
    if kind == "install":
        for f in prep["files"]:
            if not SAFE_NAME.match(f["n"]) or not re.fullmatch(r"[0-9a-f]{64}", f["h"]):
                raise DriverError("bad file manifest")
        files = "\n".join(f"    @{{ n = {psq(f['n'])}; h = {psq(f['h'])} }}" for f in prep["files"])
        old = ",".join(psq(o) for o in old_oems)
        body = (_STAGE_BODY.replace("@@REINSTALL@@", _REINSTALL_PRE if reinstall else "")
                .replace("@@SRC@@", psq(prep["dir"])).replace("@@FILES@@", files).replace("@@SIGNER@@", SIGNER)
                .replace("@@INF@@", psq(prep["inf"])).replace("@@OLD@@", old))
    elif kind == "uninstall":
        body = _UNINSTALL_BODY.replace("@@OEM@@", psq(oem))
    elif kind == "restart":
        if not re.fullmatch(r"[A-Za-z0-9_\\&{}.\- ]+", instance_id or ""):
            raise DriverError("bad device instance id")
        body = _RESTART_BODY.replace("@@DEVICE@@", psq(instance_id))
    else:
        raise DriverError("unknown action")
    return (_HEADER.replace("@@ID@@", psq(action_id)).replace("@@ROOT@@", psq(root or data_root())).replace("@@SKIPACL@@", "$true" if skip_acl else "$false")
            .replace("@@PNPUTIL@@", psq(pnputil)).replace("@@KIND@@", kind).replace("@@BODY@@", body))


LAUNCHER_PS = ("try { $p = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
               "-ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',$env:MC_ELEV_B64); exit $p.ExitCode } "
               "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1223 }")


def _run_elevated(a, script):
    """One UAC prompt. Returns after the elevated script ends; mirrors its log into the action log as it grows."""
    b64 = base64.b64encode(script.encode("utf-16-le")).decode()
    if len(b64) > 28000:
        raise DriverError("elevated script is too large for a command line")
    if DRY_RUN:
        a["result"] = {"dry_run": True, "script": script, "launcher": LAUNCHER_PS, "encoded_chars": len(b64)}
        _set(a, "done", "dry run: stopped before the elevated step (nothing was changed)")
        return None
    log_path = data_root() / "logs" / f"{a['id']}.log"
    _set(a, "awaiting_uac", "approve the Windows prompt (UAC)")
    proc = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", LAUNCHER_PS], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={**os.environ, "MC_ELEV_B64": b64}, creationflags=CREATE_NO_WINDOW)
    shown = 0

    def pump():
        nonlocal shown
        try:
            lines = log_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            return
        if lines and a["state"] == "awaiting_uac":
            _set(a, "installing", "elevated script is running")
        for line in lines[shown:]:
            _log(a, "  | " + line)
        shown = len(lines)

    while proc.poll() is None:
        pump()
        time.sleep(0.5)
    pump()
    err = proc.stderr.read().decode("utf-8", "replace").strip()
    if proc.returncode == 1223:
        raise DriverError("administrator approval was declined or the prompt could not be shown" + (f" ({err})" if err else ""))
    if proc.returncode not in (0,):
        raise DriverError(f"elevated script failed with exit code {proc.returncode} (see the log)")
    return proc.returncode


def _find_release(token):
    st = _read_state()
    rel = st.get("release")
    if not rel or not isinstance(token, dict):
        raise DriverError("no release information cached; run a check first")
    if (token.get("tag"), token.get("asset"), str(token.get("sha256", "")).lower()) != (rel["tag"], rel["asset"], rel["sha256"]) or not rel["sha256"]:
        raise DriverError("request does not match the cached release record; refresh the page and try again")
    return rel


def _our_oems():
    data, _ = get_probe()
    return [p["oem"] for p in (data or {}).get("installed", []) if OEM_RE.match(p["oem"] or "")]


def start_install(token, reinstall=False):
    rel = _find_release(token)
    old = _our_oems()

    def run(a):
        workdir = tempfile.mkdtemp(prefix="mc-driver-")  # fresh per run
        try:
            prep = prepare_release(a, rel, workdir)
            _log(a, f'release {rel["tag"]}, driver {prep["version"]} dated {prep["date"]}, {len(prep["files"])} files verified')
            script = build_script("install", a["id"], prep=prep, old_oems=old, reinstall=reinstall)
            if _run_elevated(a, script) is not None:
                refresh_probe()
                _set(a, "done", f'installed {rel["tag"]}; power-cycle the trackpad (Bluetooth) or re-plug it (USB), then apply driver settings')
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    return _start_action("reinstall" if reinstall else "install", run)


def start_uninstall(oem, confirm):
    if confirm != "uninstall":
        raise DriverError("uninstall needs explicit confirmation")
    if oem not in _our_oems():
        raise DriverError("that is not a Magic driver package on this PC")

    def run(a):
        _set(a, "installing", f"removing {oem}")
        script = build_script("uninstall", a["id"], oem=oem)
        if _run_elevated(a, script) is not None:
            refresh_probe()
            _set(a, "done", "driver package removed; Windows falls back to its inbox driver")
    return _start_action("uninstall", run)


def start_restart(instance_id):
    data, _ = get_probe()
    allowed = {r["instanceId"] for g in (data or {}).get("groups", []) for r in g["rows"] if r["present"]}
    if instance_id not in allowed:
        raise DriverError("unknown or disconnected device")

    def run(a):
        _set(a, "installing", "restarting device")
        script = build_script("restart", a["id"], instance_id=instance_id)
        if _run_elevated(a, script) is not None:
            _set(a, "done", "device restarted (Bluetooth trackpads still need a power-cycle, USB a re-plug, to re-read driver settings)")
    return _start_action("restart", run)


# --------------------------------------------------------------------------------------------- API

def snapshot():
    data, perr = get_probe(block=False)
    if data is None and (_probe_lock.locked() or _probe["t"] == 0):
        # First probe still running: answer at once so the page can show real progress instead of hanging.
        return {"loading": True, "phase": _probe["phase"] or "starting", "label": _probe["label"] or "Starting the driver check…",
                "probe_error": None, "now": time.time()}
    if AUTO_CHECK:
        _maybe_auto_check()
    st = _read_state()
    data = data or {"installed": [], "groups": [], "competitors": [], "environment": []}
    installed = data["installed"][0] if data["installed"] else None
    rel = st.get("release")
    status = _release_status(installed, rel, st.get("versions") or {})
    release = None
    if rel:
        release = {k: rel[k] for k in ("tag", "name", "published", "asset", "size", "sha256")}
        release["installable"] = bool(rel["sha256"])
    action = _actions.get(_current) if _current else None
    last = _brief(action) if action else st.get("last_action")
    warnings = []
    if data["competitors"]:
        warnings.append("Other trackpad software or drivers are present and can conflict with the Magic driver.")
    return {"probe_at": data.get("t"), "probe_error": perr, "installed": installed, "extra_packages": data["installed"][1:],
            "groups": data["groups"], "competitors": data["competitors"], "environment": data["environment"], "warnings": warnings,
            "release": release, "status": status, "checked_at": st.get("checked_at"), "check_error": st.get("error"),
            "cached_zips": _cached_zips(st), "action": last, "busy": bool(action and action["state"] not in TERMINAL),
            "dry_run": DRY_RUN, "now": time.time()}


def api_get(path):
    if path == "/api/drivers":
        return 200, snapshot()
    m = re.fullmatch(r"/api/drivers/action/([0-9a-f]{12})", path)
    if m:
        a = _actions.get(m.group(1))
        return (200, _brief(a)) if a else (404, {"error": "unknown action"})
    return 404, {"error": "not found"}


def api_post(path, body):
    body = body if isinstance(body, dict) else {}
    try:
        if path == "/api/drivers/refresh":
            start_refresh()
            return 202, {"refreshing": True}
        if path == "/api/drivers/check":
            check_release()
            return 200, snapshot()
        if path in ("/api/drivers/install", "/api/drivers/reinstall"):
            return 202, {"actionId": start_install(body, reinstall=path.endswith("reinstall"))}
        if path == "/api/drivers/uninstall":
            return 202, {"actionId": start_uninstall(str(body.get("oem", "")), body.get("confirm"))}
        if path == "/api/drivers/restart":
            return 202, {"actionId": start_restart(str(body.get("instanceId", "")))}
    except Busy:
        return 409, {"error": "another driver action is already running"}
    except DriverError as e:
        return 400, {"error": str(e)}
    return 404, {"error": "not found"}
