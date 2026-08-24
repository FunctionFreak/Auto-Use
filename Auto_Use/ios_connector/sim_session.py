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
            # Generous window: xcodebuild's destination report alone runs to
            # dozens of lines on a Mac with several runtimes, and a short tail
            # would scroll the explanation off and misclassify the failure.
            return "\n".join(lines[-200:])
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
        if "matching the provided destination specifier" in t:
            # xcodebuild prints the eligible/ineligible destinations right after
            # this line, and an "error:" reason on each ineligible one — that
            # block is the only thing that actually explains the failure, so
            # pass it through instead of guessing. Keep the informative rows
            # (iOS Simulator entries, reasons) and drop the macOS/placeholder
            # noise that would otherwise eat the whole budget.
            lines = [ln.strip() for ln in t.splitlines()]
            start = next((i for i, ln in enumerate(lines)
                          if "matching the provided destination specifier" in ln), 0)
            window = lines[start:start + 40]
            keep = [ln for ln in window
                    if ("iOS Simulator" in ln or "error:" in ln or "Ineligible" in ln
                        or "Available destinations" in ln or "could not be found" in ln
                        or ln.startswith("xcodebuild:"))]
            block = "\n    ".join(keep or window)[:1200]
            return {"code": "no_destination",
                    "error": "xcodebuild refused to use this simulator as a test destination",
                    "hint": ("simctl can see the device but Xcode cannot target it — usually the "
                             "selected Xcode has no iOS platform support installed for that "
                             "runtime. Try:  xcodebuild -downloadPlatform iOS   (or Xcode → "
                             "Settings → Components), and check `xcode-select -p` points at the "
                             "Xcode you expect.\n  xcodebuild said:\n    " + block)}
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
                r = _simctl("boot", udid, timeout=180)
                # rc 149 means "already booted", which is fine; anything else
                # non-zero is a real failure worth reporting rather than
                # discovering later as a confusing xcodebuild error.
                if r.returncode not in (0, 149):
                    why = (r.stderr or r.stdout or "").strip().splitlines()
                    return {"ok": False, "state": "error", "code": "boot_failed",
                            "error": f"simctl could not boot {name}: "
                                     f"{why[-1][:200] if why else 'unknown error'}",
                            "hint": f"Try it by hand: xcrun simctl boot {udid}"}
            except subprocess.TimeoutExpired:
                pass  # boot continues inside CoreSimulator
        try:
            # ALWAYS confirm readiness, even when the device already claimed to
            # be "Booted": that state goes stale while a simulator is still
            # coming up or going down, and xcodebuild launching into a
            # half-alive one dies with "Mach error -308 - server died".
            _simctl("bootstatus", udid, "-b", timeout=300)
        except subprocess.TimeoutExpired:
            pass  # fall through to the explicit state check below
        # Show the Simulator window so the run is watchable (best effort).
        try:
            subprocess.Popen(["open", "-a", "Simulator"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        # Don't hand a device that isn't actually up to xcodebuild — on a fresh
        # Mac the first boot of a runtime can outlast bootstatus, and the
        # failure then surfaces as a confusing destination/launch error. Poll
        # rather than sample once: a single reading can catch a transient
        # simctl hiccup (_device_state returns "" on any error).
        deadline = time.time() + 120.0
        state = self._device_state(udid)
        while state != "Booted" and time.time() < deadline:
            time.sleep(2.0)
            state = self._device_state(udid)
        if state != "Booted":
            return {"ok": False, "state": "error", "code": "boot_failed",
                    "error": f"{name} did not finish booting (state: {state or 'unknown'})",
                    "hint": "Open Simulator manually and boot the device once, "
                            "then retry — the first boot of a new runtime is slow."}
        return None

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

        boot_error = self._boot(sim["udid"], f'{sim["name"]} (iOS {sim["version"]})')
        if boot_error:
            # Don't leave a half-booted simulator behind: the caller raises on
            # this without getting a session object to deactivate.
            try:
                _simctl("shutdown", sim["udid"], timeout=60)
            except Exception:
                pass
            with self._lock:
                self._udid = self._name = None
            return boot_error
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
                # A stable, discoverable path beside the build — a first-time
                # user should not have to go spelunking in /var/folders for the
                # one file that explains a failed run.
                self.derived_data.mkdir(parents=True, exist_ok=True)
                self._log_path = str(self.derived_data / f"wda-{self.port}.log")
                log = open(self._log_path, "w")
                # Unsigned simulator build+run — the recipe WebDriverAgent's own
                # Scripts/build.sh uses. `test` leaves WDA serving until we kill it.
                self._xcodebuild = subprocess.Popen(
                    ["xcodebuild",
                     "-project", str(_WDA_PROJECT),
                     "-scheme", "WebDriverAgentRunner",
                     # Fully qualified on purpose: a bare "id=..." leaves the
                     # platform for xcodebuild to infer, which can fail to
                     # match ("Unable to find a destination") on machines
                     # where physical-device destinations are in play.
                     "-destination", f"platform=iOS Simulator,id={sim['udid']}",
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
                failure = str(e)
            else:
                failure = None
        if failure is not None:
            # _stop_locked cleared _udid, so a later deactivate() would leave
            # this simulator booted — shut down the one we just booted here.
            try:
                _simctl("shutdown", sim["udid"], timeout=60)
            except Exception:
                pass
            return {"ok": False, "state": "error", "error": failure}
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
                    # ALWAYS append the log path — the classified hint used to
                    # replace it, which left the failures users actually hit
                    # with no way to see what xcodebuild said.
                    info["hint"] = " ".join(
                        p for p in (info.get("hint"),
                                    f"Full xcodebuild log: {self._log_path}") if p)
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
