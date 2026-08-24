# Copyright 2026 Ashish Yadav — Auto-Use

"""iOS Simulator session — the simulator twin of session.WDASession.

Same three-call contract the launcher and any UI polls:

    activate(ios_version=None) -> boot a simulator (picked by iOS version,
                                  newest installed runtime when omitted) and
                                  start WebDriverAgent on it via one command:
                                    xcodebuild test -destination id=<sim udid>
                                  (unsigned — simulators need no team id, no
                                  pairing, no pymobiledevice3). WDA then
                                  answers on http://127.0.0.1:8100, the same
                                  URL the whole agent already talks to.
    status()                   -> disconnected / connecting / connected / error.
    deactivate()               -> stop WDA (xcodebuild child), then SHUT THE
                                  SIMULATOR DOWN and quit the Simulator app —
                                  it's not a real device, nothing should
                                  linger on screen after the run.

First run compiles WebDriverAgent for the simulator (a few minutes); after
that the cached build boots + attaches in well under a minute.
"""

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from Auto_Use.ios_connector.session import WDA_PORT, active_target

_WDA_PROJECT = Path(__file__).resolve().parent / "WebDriverAgent" / "WebDriverAgent.xcodeproj"
_DERIVED_DATA = Path(__file__).resolve().parent / "build-sim"  # separate from the hardware build/


def _simctl(*args, timeout=30):
    return subprocess.run(["xcrun", "simctl", *args],
                          capture_output=True, text=True, timeout=timeout)


def _runtime_version(runtime_key):
    """'com.apple.CoreSimulator.SimRuntime.iOS-26-5' -> '26.5' (None if not iOS)."""
    tail = runtime_key.rsplit(".", 1)[-1]
    if not tail.startswith("iOS-"):
        return None
    return tail[len("iOS-"):].replace("-", ".")


def _version_tuple(v):
    return tuple(int(p) for p in str(v).split(".") if p.isdigit())


def _named_simulator(udid):
    """Look one specific simulator up by udid. Returns (device, error)."""
    try:
        out = _simctl("list", "devices", "-j")
        for runtime_key, devices in json.loads(out.stdout).get("devices", {}).items():
            for d in devices:
                if d.get("udid") == udid:
                    return {"udid": udid, "name": d.get("name", "Simulator"),
                            "version": _runtime_version(runtime_key) or "?"}, None
    except Exception as e:
        return None, {"error": f"simctl failed: {e}"}
    return None, {"error": f"No simulator with udid {udid}",
                  "hint": "Check `xcrun simctl list devices`."}


def resolve_simulator(ios_version=None, exclude=()):
    """Pick (udid, name, version) for the run.

    Prefers an already-Booted device; otherwise an iPhone on the newest
    runtime matching ios_version (prefix match, so "26" finds 26.5).
    `exclude` skips udids already claimed by other parallel tasks.
    Returns (device_dict, error_dict) — exactly one is None.
    """
    try:
        out = _simctl("list", "devices", "available", "-j")
        devices_by_runtime = json.loads(out.stdout).get("devices", {})
    except Exception as e:
        return None, {"error": f"simctl failed: {e}",
                      "hint": "Simulation needs full Xcode (xcode-select -p should point at Xcode.app)."}

    want = str(ios_version).strip() if ios_version else ""
    candidates = []  # (version_tuple, version_str, device)
    for runtime_key, devices in devices_by_runtime.items():
        version = _runtime_version(runtime_key)
        if version is None:
            continue
        if want and not (version == want or version.startswith(want + ".")):
            continue
        for d in devices:
            if d.get("udid") in exclude:
                continue  # already claimed by another parallel task
            candidates.append((_version_tuple(version), version, d))

    if not candidates:
        installed = sorted({v for k in devices_by_runtime
                            if (v := _runtime_version(k))}) or ["none"]
        what = f'iOS {want}' if want else "any iOS runtime"
        if exclude:
            return None, {"error": f"Not enough simulators for {what} — "
                                   f"{len(exclude)} already in use by other tasks",
                          "hint": "Add devices in Xcode → Window → Devices and "
                                  "Simulators, or run fewer tasks at once."}
        return None, {"error": f"No simulator found for {what}",
                      "hint": f"Installed runtimes: {', '.join(installed)}. "
                              "Add more in Xcode → Settings → Components."}

    booted = [c for c in candidates if c[2].get("state") == "Booted"]
    if booted:
        _vt, version, d = max(booted, key=lambda c: c[0])
    else:
        newest = max(c[0] for c in candidates)
        pool = [c for c in candidates if c[0] == newest]
        iphones = [c for c in pool if c[2].get("name", "").startswith("iPhone")]
        _vt, version, d = (iphones or pool)[0]
    return {"udid": d["udid"], "name": d.get("name", "Simulator"), "version": version}, None


class SimulatorSession:
    def __init__(self, port=None, slot=1):
        self._lock = threading.Lock()
        self._xcodebuild = None
        self._udid = None
        self._name = None
        self._log_path = None
        # Parallel tasks each own a simulator + a WDA port (8100, 8101, ...).
        self.port = int(port or WDA_PORT)
        # ...and their own build folder. Concurrent xcodebuilds sharing one
        # derived-data dir re-sign the same .app underneath each other, which
        # kills the other run's runner ("crashed with signal term before
        # establishing connection"). Slot 1 keeps the default folder, so a
        # single-task run reuses the build it already has.
        self.derived_data = (_DERIVED_DATA if int(slot) <= 1
                             else Path(f"{_DERIVED_DATA}-{int(slot)}"))

    # -- helpers (same shapes as WDASession) --
    def _wda_up(self):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/status", timeout=3) as r:
                body = json.loads(r.read().decode("utf-8"))
                return r.status == 200 and bool(body.get("value", {}).get("state"))
        except Exception:
            return False

    def _port_owner(self):
        """Command line of whatever LISTENs on this session's WDA port ('' if free).

        Identifies WHO answers there: a pymobiledevice3 forward (paired
        iPhone), or a simulator WDA runner — whose binary path contains
        CoreSimulator/Devices/<udid>, so it names its simulator."""
        try:
            out = subprocess.run(["lsof", "-nP", f"-iTCP:{self.port}", "-sTCP:LISTEN", "-t"],
                                 capture_output=True, text=True, timeout=8)
            for pid in out.stdout.split():
                cmd = subprocess.run(["ps", "-p", pid, "-o", "command="],
                                     capture_output=True, text=True, timeout=5).stdout
                if cmd.strip():
                    return cmd.strip()
        except Exception:
            pass
        return ""

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
        # CoreSimulator occasionally wedges (usually after killed runs). Both
        # of these mean "the simulator service is unhealthy", not "your code or
        # your task is wrong" — and both clear with the same reset.
        if ("timed out while preparing to run tests" in t
                or "server died" in t
                or "Failed to install or launch the test runner" in t):
            return {"code": "sim_wedged",
                    "error": "The simulator's test service is wedged (a known flake)",
                    "hint": "Run: xcrun simctl shutdown all && pkill -9 -f CoreSimulatorService — then retry."}
        if "Unable to find a destination" in t:
            return {"code": "no_destination",
                    "error": "xcodebuild could not find the simulator",
                    "hint": "Check `xcrun simctl list devices` — the picked device may have been deleted."}
        if "xcodebuild: error" in t or "BUILD FAILED" in t or "TEST FAILED" in t:
            last = next((ln for ln in reversed(t.splitlines()) if "error" in ln.lower()), t.splitlines()[-1])
            return {"code": "build_failed", "error": last[-200:]}
        last = t.splitlines()[-1][-160:] if t else "xcodebuild exited before WDA started"
        return {"code": "failed", "error": last}

    @staticmethod
    def _device_state(udid):
        try:
            out = _simctl("list", "devices", "-j")
            for devs in json.loads(out.stdout).get("devices", {}).values():
                for d in devs:
                    if d.get("udid") == udid:
                        return d.get("state", "")
        except Exception:
            pass
        return ""

    def _boot(self, udid, name):
        state = self._device_state(udid)
        # A device still winding down from a previous run can't be booted yet
        # ("Unable to boot device in current state: Shutting Down").
        waited = 0.0
        while state == "Shutting Down" and waited < 60.0:
            time.sleep(1.0)
            waited += 1.0
            state = self._device_state(udid)
        if state != "Booted":
            print(f"📱 Booting {name}...")
            try:
                _simctl("boot", udid, timeout=180)  # rc 149 = already booted, fine
            except subprocess.TimeoutExpired:
                pass  # boot continues inside CoreSimulator
        try:
            # ALWAYS confirm readiness, even when the device already claimed to
            # be "Booted": that state goes stale while a simulator is still
            # coming up or going down, and xcodebuild launching into a
            # half-alive one dies with "Mach error -308 - server died".
            _simctl("bootstatus", udid, "-b", timeout=180)
        except subprocess.TimeoutExpired:
            pass  # xcodebuild will wait on the device itself if needed
        # Show the Simulator window so the run is watchable (best effort).
        try:
            subprocess.Popen(["open", "-a", "Simulator"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    # -- public --
    def activate(self, ios_version=None, udid=None, exclude=()):
        """Boot a simulator and start WDA on this session's port.

        ios_version picks the runtime (newest installed when omitted); udid
        pins an exact device and exclude skips devices other parallel tasks
        already claimed."""
        if not _WDA_PROJECT.exists():
            return {"ok": False, "state": "error",
                    "error": "WebDriverAgent project not found",
                    "hint": "Run ios_setup.sh once — it fetches Auto_Use/ios_connector/WebDriverAgent."}
        owner = self._port_owner()
        if "pymobiledevice3" in owner and "forward" in owner:
            return {"ok": False, "state": "error", "code": "hardware_active",
                    "error": f"A paired-iPhone session is using port {self.port}",
                    "hint": 'Disconnect the phone session first, or run with device="hardware".'}

        if udid:
            sim, err = _named_simulator(udid)
        else:
            sim, err = resolve_simulator(ios_version, exclude=exclude)
        if err:
            return {"ok": False, "state": "error", **err}

        with self._lock:
            if (self._udid == sim["udid"] and self._alive(self._xcodebuild)
                    and self._wda_up()):
                active_target.update({"kind": "simulation", "udid": sim["udid"]})
                return {"ok": True, "state": "connected", **sim}
            self._stop_locked()
            self._udid, self._name = sim["udid"], sim["name"]

        self._boot(sim["udid"], f'{sim["name"]} (iOS {sim["version"]})')
        active_target.update({"kind": "simulation", "udid": sim["udid"]})

        # Something already serves WDA on the port. Reuse it ONLY if it is a
        # runner living inside OUR simulator (its binary path names the udid);
        # anything else would silently drive the wrong device — refuse instead.
        if self._wda_up():
            owner = self._port_owner()
            if f"CoreSimulator/Devices/{sim['udid']}" in owner:
                return {"ok": True, "state": "connected", **sim}
            return {"ok": False, "state": "error", "code": "port_busy",
                    "error": f"Port {self.port} is serving a WDA that is not on "
                             f"{sim['name']} (iOS {sim['version']})",
                    "hint": f"Kill the leftover process (`lsof -nP -iTCP:{self.port}"
                            "` shows its pid), then retry."}

        with self._lock:
            try:
                log = tempfile.NamedTemporaryFile(prefix="autouse_sim_wda_",
                                                  suffix=".log", delete=False)
                self._log_path = log.name
                # Unsigned simulator build+run — the recipe WebDriverAgent's own
                # Scripts/build.sh uses. `test` leaves WDA serving until we kill it.
                self._xcodebuild = subprocess.Popen(
                    ["xcodebuild",
                     "-project", str(_WDA_PROJECT),
                     "-scheme", "WebDriverAgentRunner",
                     "-destination", f"id={sim['udid']}",
                     "-derivedDataPath", str(self.derived_data),
                     "CODE_SIGN_IDENTITY=", "CODE_SIGNING_REQUIRED=NO",
                     "CODE_SIGNING_ALLOWED=NO",
                     "test"],
                    stdout=log, stderr=subprocess.STDOUT,
                    # WDA reads USE_PORT and binds exactly that port — this is
                    # what lets parallel tasks each own a simulator + a port.
                    env={**os.environ, "USE_PORT": str(self.port)})
            except Exception as e:
                self._stop_locked()
                return {"ok": False, "state": "error", "error": str(e)}
        return {"ok": True, "state": "connecting", **sim}

    @staticmethod
    def _alive(p):
        return p is not None and p.poll() is None

    def status(self):
        with self._lock:
            if self._udid is None:
                return {"state": "disconnected"}
            if not self._alive(self._xcodebuild) and not self._wda_up():
                info = self._classify(self._log_tail())
                if self._log_path:
                    info.setdefault("hint", f"Full xcodebuild log: {self._log_path}")
                return {"state": "error", "udid": self._udid, "name": self._name, **info}
            state = "connected" if self._wda_up() else "connecting"
            return {"state": state, "udid": self._udid, "name": self._name}

    def deactivate(self):
        with self._lock:
            udid = self._udid
            # NO /wda/shutdown here (the hardware session uses it to clear the
            # on-phone overlay). On a simulator it CRASHES the runner —
            # WDA tears its HTTP server down while still writing the reply to
            # that very request, and the SIGSEGV pops macOS's "WebDriverAgent
            # Runner-Runner quit unexpectedly" dialog. Killing xcodebuild
            # instead lets XCTest end the session with a plain SIGTERM, which
            # generates no crash report.
            self._stop_locked()
        # Not a real device — shut it down so nothing lingers after the run,
        # and close the Simulator app unless another simulator is still up.
        if udid:
            try:
                _simctl("shutdown", udid, timeout=60)
            except Exception:
                pass
            try:
                out = _simctl("list", "devices", "booted")
                if "(Booted)" not in out.stdout:
                    subprocess.run(["osascript", "-e", 'quit app "Simulator"'],
                                   capture_output=True, text=True, timeout=10)
            except Exception:
                pass
        return {"ok": True, "state": "disconnected"}

    def _stop_locked(self):
        p = self._xcodebuild
        if p is not None:
            try:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except Exception:
                    p.kill()
            except Exception:
                pass
            self._xcodebuild = None
        # The log file is deliberately KEPT (unlike the hardware session) —
        # error hints point users at it, and xcodebuild logs are the only
        # way to debug a failed simulator build.
        self._log_path = None
        self._udid = None
        self._name = None


sim_session = SimulatorSession()
