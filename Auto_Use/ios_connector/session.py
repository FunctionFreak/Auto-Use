# Copyright 2026 Cursortouch — Auto-Use

"""Paired-device registry + WDA session toggle.

Two halves, both dead simple:

REGISTRY (paired_devices.json, lives in this folder)
    A device gets added when Settings finishes pairing it (the AutoUse runner
    app installed on the phone). If a device is in the list it is paired —
    clicking the Apple logo in the chat box just activates it, no checks, no
    reinstall. Settings can list and delete entries.

SESSION (the Apple-logo toggle)
    activate()  -> fresh WDA session over the cable, exactly two commands:
                     pymobiledevice3 usbmux forward 8100 8100
                     pymobiledevice3 developer dvt xcuitest --userspace <bundle>
                   then the phone answers on http://127.0.0.1:8100.
    status()    -> disconnected / connecting / connected / error (checks the
                   real WDA /status, so "connected" means actually reachable).
    deactivate()-> stop both subprocesses (click the logo off).

No xcodebuild, no reinstall — ever. Pairing (Settings) is the only place that
builds/installs anything.
"""

import os
import sys
import json
import time
import signal
import shutil
import tempfile
import threading
import subprocess
import urllib.request
from pathlib import Path

WDA_BUNDLE_ID = "com.autouse.WebDriverAgentRunner.xctrunner"
WDA_PORT = 8100
WDA_STATUS_URL = f"http://127.0.0.1:{WDA_PORT}/status"

# Which iOS target the current process is driving — "hardware" (paired iPhone)
# or "simulation" (sim_session). Whichever session activates last sets it;
# tools that need non-WDA device access read it (open_app's installed-apps
# scan picks pymobiledevice3 vs simctl from here).
active_target = {"kind": "hardware", "udid": None}


def wda_port() -> int:
    """WDA port for THIS process. 8100 unless AUTOUSE_WDA_PORT says otherwise.

    Parallel simulator tasks each get their own simulator + their own WDA, so
    the parent hands every child process its port through the environment."""
    try:
        return int(os.environ.get("AUTOUSE_WDA_PORT") or WDA_PORT)
    except (TypeError, ValueError):
        return WDA_PORT


def wda_url() -> str:
    """Base URL of the WDA this process talks to (http://localhost:<port>)."""
    return f"http://localhost:{wda_port()}"

_IS_COMPILED = bool(getattr(sys, "frozen", False)) or ("__compiled__" in globals())


# ─────────────────────────── paired-device registry ───────────────────────────
def _registry_file() -> Path:
    """paired_devices.json — package folder in dev, next to the executable in a
    compiled build (the app bundle is read-only there)."""
    if _IS_COMPILED:
        base = Path(sys.executable).parent / "Auto_Use" / "ios_connector"
    else:
        base = Path(__file__).resolve().parent
    base.mkdir(parents=True, exist_ok=True)
    return base / "paired_devices.json"


def _read_registry() -> list:
    f = _registry_file()
    if f.exists():
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("devices"), list):
                return data["devices"]
        except Exception:
            pass
    return []


def _write_registry(devices: list) -> None:
    try:
        with open(_registry_file(), "w", encoding="utf-8") as fh:
            json.dump({"devices": devices}, fh, indent=2, ensure_ascii=False)
    except Exception:
        pass


def paired_devices() -> list:
    """All paired devices: [{udid, name, version, paired_at}], newest first."""
    return sorted(_read_registry(), key=lambda d: d.get("paired_at", 0), reverse=True)


def add_paired(udid, name="iPhone", version="") -> list:
    """Add/refresh one paired device (idempotent on udid)."""
    if not udid:
        return paired_devices()
    devices = [d for d in _read_registry() if d.get("udid") != udid]
    devices.append({"udid": str(udid), "name": str(name or "iPhone"),
                    "version": str(version or ""), "paired_at": int(time.time())})
    _write_registry(devices)
    return paired_devices()


def remove_paired(udid) -> list:
    """Delete one device from the list (Settings' × button)."""
    _write_registry([d for d in _read_registry() if d.get("udid") != udid])
    return paired_devices()


def is_paired(udid) -> bool:
    return any(d.get("udid") == udid for d in _read_registry())


# ───────────────────────────── pymobiledevice3 ────────────────────────────────
def _pmd3_candidates():
    """Every place pymobiledevice3 can legitimately live, in priority order."""
    root = Path(__file__).resolve().parents[2]   # <checkout>/Auto_Use/ios_connector/
    out = []
    override = os.environ.get("AUTOUSE_PMD3")
    if override:
        out.append(Path(override))
    out.append(Path(sys.executable).parent / "pymobiledevice3")
    # THE VENV THE SETUP SCRIPTS INSTALL INTO. Whoever launches the app decides
    # sys.executable — a shell without the venv active, an IDE's interpreter,
    # the venv's own base python — and none of that should make the tooling
    # "missing" when it is sitting right here in the checkout.
    for venv in (".venv", "venv", "env"):
        out.append(root / venv / "bin" / "pymobiledevice3")
    out.append(Path.home() / "Desktop" / "wda_setup" / "venv" / "bin" / "pymobiledevice3")
    return out


def _pmd3_base():
    """argv prefix that runs pymobiledevice3, or None. Forgiving search:
    env override, next-to-executable, the checkout's venv, PATH, dev venv, -m."""
    for cand in _pmd3_candidates():
        try:
            if cand.exists():
                return [str(cand)]
        except OSError:
            continue
    found = shutil.which("pymobiledevice3")
    if found:
        return [found]
    try:
        import pymobiledevice3  # noqa: F401
        return [sys.executable, "-m", "pymobiledevice3"]
    except Exception:
        return None


# ─────────────────────────────── session toggle ───────────────────────────────
class WDASession:
    def __init__(self):
        self._lock = threading.Lock()
        self._forward = None
        self._xctest = None
        self._udid = None
        self._log_path = None

    # -- helpers --
    def _wda_up(self):
        try:
            with urllib.request.urlopen(WDA_STATUS_URL, timeout=3) as r:
                body = json.loads(r.read().decode("utf-8"))
                return r.status == 200 and bool(body.get("value", {}).get("state"))
        except Exception:
            return False

    @staticmethod
    def _alive(p):
        return p is not None and p.poll() is None

    def _free_port(self):
        """Kill only a LEFTOVER pymobiledevice3 forward squatting on the port."""
        try:
            out = subprocess.run(["lsof", "-nP", f"-iTCP:{WDA_PORT}", "-sTCP:LISTEN", "-t"],
                                 capture_output=True, text=True, timeout=8)
            pids = [p for p in out.stdout.split() if p.strip().isdigit()]
        except Exception:
            return
        for pid in pids:
            try:
                cmd = subprocess.run(["ps", "-p", pid, "-o", "command="],
                                     capture_output=True, text=True, timeout=5).stdout
                if "pymobiledevice3" in cmd and "forward" in cmd:
                    os.kill(int(pid), signal.SIGTERM)
            except Exception:
                pass

    def _log_tail(self):
        if not self._log_path or not os.path.exists(self._log_path):
            return ""
        try:
            with open(self._log_path, "r", errors="replace") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
            return "\n".join(lines[-40:])
        except Exception:
            return ""

    @staticmethod
    def _classify(tail):
        t = tail or ""
        if "AppNotInstalledError" in t or "No app with bundle id" in t:
            return {"code": "not_installed",
                    "error": "AutoUse isn't installed on this iPhone",
                    "hint": "Pair the device in Settings → Connect Device first."}
        if "Failed to launch process" in t or "deviceprocesscontrolservice" in t:
            return {"code": "untrusted",
                    "error": "Trust AutoUse on the iPhone",
                    "hint": "iPhone: Settings → General → VPN & Device Management → Trust."}
        if "developer mode" in t.lower() or "DeveloperMode" in t:
            return {"code": "devmode",
                    "error": "Enable Developer Mode on the iPhone",
                    "hint": "iPhone: Settings → Privacy & Security → Developer Mode."}
        last = t.splitlines()[-1][-160:] if t else "Session ended before the server started"
        return {"code": "failed", "error": last}

    # -- public --
    def activate(self, udid=None):
        """Fresh session for `udid` (default: newest paired device)."""
        base = _pmd3_base()
        if not base:
            # Name the interpreter: "not found" is almost always "found, but you
            # launched the app with a different Python", and only this line says so.
            return {"ok": False, "state": "error", "code": "no_pmd3",
                    "error": "pymobiledevice3 not found",
                    "hint": (f"running under {sys.executable} — looked beside it, in the "
                             "checkout's .venv/, and on PATH. Install it with:  bash ios_setup.sh  "
                             "or start the app with the venv's Python:  "
                             "source .venv/bin/activate && python app.py")}
        if not udid:
            devs = paired_devices()
            if not devs:
                return {"ok": False, "state": "error", "code": "not_paired",
                        "error": "No paired device",
                        "hint": "Pair your iPhone in Settings → Connect Device."}
            udid = devs[0]["udid"]
        active_target.update({"kind": "hardware", "udid": udid})
        with self._lock:
            if self._udid == udid and self._wda_up():
                return {"ok": True, "state": "connected", "udid": udid}
            self._stop_locked()
            self._free_port()
            if self._wda_up():
                # Something else already serves WDA on the port (a leftover
                # simulator session) — refuse rather than silently drive it.
                return {"ok": False, "state": "error", "code": "port_busy",
                        "error": f"Port {WDA_PORT} is already serving another WDA "
                                 "(a simulator session?)",
                        "hint": "Close it (quit Simulator / kill xcodebuild), then retry."}
            self._udid = udid
            env = dict(os.environ)
            env["PYMOBILEDEVICE3_UDID"] = udid
            try:
                log = tempfile.NamedTemporaryFile(prefix="autouse_wda_", suffix=".log", delete=False)
                self._log_path = log.name
                self._forward = subprocess.Popen(
                    base + ["usbmux", "forward", str(WDA_PORT), str(WDA_PORT)],
                    stdout=subprocess.DEVNULL, stderr=log, env=env)
                self._xctest = subprocess.Popen(
                    base + ["developer", "dvt", "xcuitest", "--userspace", WDA_BUNDLE_ID],
                    stdout=subprocess.DEVNULL, stderr=log, env=env)
            except Exception as e:
                self._stop_locked()
                return {"ok": False, "state": "error", "error": str(e)}
        return {"ok": True, "state": "connecting", "udid": udid}

    def status(self):
        with self._lock:
            if self._udid is None:
                return {"state": "disconnected"}
            if (not self._alive(self._xctest) or not self._alive(self._forward)) \
                    and not self._wda_up():
                info = self._classify(self._log_tail())
                return {"state": "error", "udid": self._udid, **info}
            state = "connected" if self._wda_up() else "connecting"
            return {"state": state, "udid": self._udid}

    def deactivate(self):
        with self._lock:
            # FIRST tell the runner ON THE PHONE to exit (WDA's /wda/shutdown).
            # That's what makes the "Automation Running" overlay vanish right
            # away — killing only the Mac-side processes leaves the phone
            # session lingering ~30s until testmanagerd times it out. The
            # request never returns: WDA frees its HTTP server while still
            # replying, so the runner segfaults (a use-after-free in the
            # vendored RoutingHTTPServer). On a phone that is invisible and the
            # process was exiting anyway; the local kills below are the
            # guarantee. The SIMULATOR path deliberately does NOT do this —
            # there the same crash pops a macOS dialog (see sim_session.py).
            if self._udid is not None:
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{WDA_PORT}/wda/shutdown", timeout=2)
                except Exception:
                    pass
            self._stop_locked()
        return {"ok": True, "state": "disconnected"}

    def _stop_locked(self):
        # Kill INSTANTLY: xctest first (it owns the phone-side session), tiny
        # grace for a clean teardown, then SIGKILL. The whole stop stays ~1s.
        for attr in ("_xctest", "_forward"):
            p = getattr(self, attr)
            if p is not None:
                try:
                    p.terminate()
                    try:
                        p.wait(timeout=1)
                    except Exception:
                        p.kill()
                except Exception:
                    pass
                setattr(self, attr, None)
        if self._log_path:
            try:
                os.unlink(self._log_path)
            except Exception:
                pass
            self._log_path = None
        self._udid = None


wda_session = WDASession()
