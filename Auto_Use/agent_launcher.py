# Copyright 2026 Cursortouch — Auto-Use

# Picks the right AgentService for the requested mode:
#   mode = "computer use"     -> desktop agent for the host OS (mac / windows)
#   mode = "shell use"        -> CLI/coder agent for the host OS, straight to the
#                                terminal (skips the main agent layer)
#   mode = "mobile use, ios"  -> iOS agent (iPhone or iPad). By default it runs on an iOS
#                                SIMULATOR (device="simulation", ios_version
#                                picks the runtime); pass device="hardware" to
#                                drive the paired iPhone/iPad instead. Either way the
#                                WDA connection is opened before the run and
#                                closed after it (ios_connector).
#   mode = "web use"          -> browser agent driving CDP-controlled Chrome
#                                (Auto_Use/web). Host OS independent.
import inspect
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# Repo root (the folder holding main.py and the Auto_Use package) — parallel
# children run with cwd inside ./parallel/task_N/, so they need this on
# PYTHONPATH to import Auto_Use.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _parse_mode(mode, os=None):
    """Split a mode string into (kind, device), e.g. "mobile use, ios" -> ("mobile", "ios")."""
    words = str(mode).strip().lower().replace(",", " ").replace("_", " ").split()
    words = [w for w in words if w != "use"]
    kind = words[0] if words else ""
    device = words[1] if len(words) > 1 else str(os or "").strip().lower()
    return kind, device


def resolve_agent_service(mode, os=None):
    """Return the AgentService class for the given mode.

    mode: "computer use" / "shell use" / "web use" (host OS auto-detected or
          irrelevant, nothing else needed) or "mobile use, ios" /
          "mobile use, android" — the device os rides along in the same string.
          Underscores/commas/case don't matter.
    os:   optional separate device os ("ios"/"android") for callers that
          don't embed it in mode; the embedded one wins if both are given.
    """
    kind, os = _parse_mode(mode, os)

    if kind == "computer":
        host = platform.system()
        if host == "Darwin":
            from Auto_Use.mac.agent.main_driver.service import AgentService
        elif host == "Windows":
            from Auto_Use.windows.agent.main_driver.service import AgentService
        else:
            raise RuntimeError(f"Unsupported OS for computer use: {host}")
        return AgentService

    if kind == "shell":
        host = platform.system()
        if host == "Darwin":
            from Auto_Use.mac.agent.coder import AgentService
        elif host == "Windows":
            from Auto_Use.windows.agent.coder import AgentService
        else:
            raise RuntimeError(f"Unsupported OS for shell use: {host}")
        return AgentService

    if kind == "web":
        # Browser agent — no host-OS branch: it drives Chrome over CDP, which
        # is the same on every platform. The implementation is the Rust
        # extension in Auto_Use/web/agent (agent_native), compiled lazily by
        # that package's __init__ on first import.
        from Auto_Use.web.agent import AgentService
        return AgentService

    if kind == "mobile":
        device = str(os or "").strip().lower()
        if device == "ios":
            from Auto_Use.ios.agent.main_driver.service import AgentService
            return AgentService
        if device == "android":
            raise NotImplementedError("Android support is not available yet")
        raise ValueError('mobile use needs a device — "mobile use, ios" or "mobile use, android"')

    raise ValueError(
        f'Unknown mode "{mode}" — use "computer use", "shell use", "web use" '
        'or "mobile use, ios"'
    )


def _supported_kwargs(AgentService, kwargs):
    """Keep only the kwargs this AgentService actually takes.

    One call site serves every mode, but the services differ: the mobile agent
    has no external_terminal, the shell-use (coder) agent has no speed. Anything
    the target doesn't declare is dropped instead of raising TypeError.
    """
    params = inspect.signature(AgentService.__init__).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}


def _connect_iphone(poll_every=2.0, deadline=90.0):
    """Open the iPhone WDA session and wait until it answers (the app's mode-dial flow).

    Returns the live wda_session; raises RuntimeError if the phone can't be reached.
    """
    from Auto_Use.ios_connector.session import wda_session

    res = wda_session.activate()
    if not res.get("ok"):
        detail = " — ".join(
            p for p in (res.get("error"), res.get("hint")) if p) or res.get("code") or res
        raise RuntimeError(f"📱 iPhone connection failed: {detail}")
    if res.get("state") == "connected":
        print(f"📱 iPhone already connected ({res.get('udid', '')})")
        return wda_session

    print("📱 Connecting iPhone...")
    end = time.time() + deadline
    while time.time() < end:
        time.sleep(poll_every)
        try:
            st = wda_session.status()
        except Exception:
            continue  # transient probe hiccup, keep polling (matches the UI)
        state = st.get("state")
        if state == "connected":
            print(f"📱 Paired device connected ({st.get('udid', '')})")
            return wda_session
        if state in ("error", "disconnected"):
            wda_session.deactivate()  # reap half-started processes, like the UI's fail path
            detail = st.get("hint") or st.get("error") or st.get("code") or state
            raise RuntimeError(f"📱 iPhone connection failed: {detail}")

    wda_session.deactivate()
    raise RuntimeError(f"📱 iPhone connection timed out after {int(deadline)}s")


def _connect_simulator(ios_version=None, poll_every=2.0, deadline=600.0, sim_device=None):
    """Boot an iOS Simulator (sim_device: "iphone" / "ipad" / exact name) and
    start WDA on it, then wait until it answers.

    Mirrors _connect_iphone (same activate/status/deactivate contract). The
    deadline is generous because the FIRST run compiles WebDriverAgent for the
    simulator (a few minutes); after that it attaches in seconds.
    """
    from Auto_Use.ios_connector.sim_session import sim_session

    res = sim_session.activate(ios_version, device=sim_device)
    if not res.get("ok"):
        detail = " — ".join(
            p for p in (res.get("error"), res.get("hint")) if p) or res.get("code") or res
        raise RuntimeError(f"📱 Simulator connection failed: {detail}")
    label = f"{res.get('name', 'Simulator')} (iOS {res.get('version', '?')})"
    if res.get("state") == "connected":
        print(f"📱 Simulator already connected — {label}")
        return sim_session

    print(f"📱 Starting WebDriverAgent on {label} — first run builds it, a few minutes...")
    end = time.time() + deadline
    try:
        while time.time() < end:
            time.sleep(poll_every)
            try:
                st = sim_session.status()
            except Exception:
                continue  # transient probe hiccup, keep polling
            state = st.get("state")
            if state == "connected":
                print(f"📱 Simulator connected — {label}")
                return sim_session
            if state in ("error", "disconnected"):
                sim_session.deactivate()
                detail = " — ".join(
                    p for p in (st.get("error"), st.get("hint")) if p) or st.get("code") or state
                raise RuntimeError(f"📱 Simulator connection failed: {detail}")
    except KeyboardInterrupt:
        # Ctrl+C mid-connect — don't leave the xcodebuild/WDA child running.
        sim_session.deactivate()
        raise

    sim_session.deactivate()
    raise RuntimeError(f"📱 Simulator connection timed out after {int(deadline)}s")


def _ios_device_target(device):
    """Normalise the device flag to "simulation" (default) or "hardware"."""
    target = str(device or "simulation").strip().lower()
    if target == "simulator":
        target = "simulation"
    if target not in ("simulation", "hardware"):
        raise ValueError(f'device must be "simulation" or "hardware" — got "{device}"')
    return target


def run_agent(mode, provider, model, task, os=None,
              device=None, ios_version=None, sim_device=None,
              save_conversation=False, external_terminal=False,
              extra_tasks=None, **agent_kwargs):
    """Create the AgentService for mode/os, run the task, return its response dict.

    device ("mobile use, ios" only): "simulation" (the default) runs on an iOS
    Simulator — ios_version picks the runtime (e.g. "26.5"; omitted = newest
    installed) and sim_device picks the simulator: "iphone" (default), "ipad",
    or an exact name such as "iPad Pro 11-inch (M5)" (an already-booted
    simulator is reused only if it is that kind). "hardware" drives the paired
    device - iPhone or iPad, whichever is paired - with whatever iOS it runs
    (ios_version and sim_device are ignored). All three are ignored by every
    other mode.

    extra_tasks ("web use" only): optional list of additional task strings
    (main.py's task_2, task_3, ...). Any non-empty entries make ALL tasks —
    including `task` — run in parallel, each as its own web agent sharing one
    Chrome, each pinned to its own single tab.

    For "mobile use, ios" the device connection is opened first and always
    closed when the agent terminates (success, error, or Ctrl+C).
    """
    kind, mobile_os = _parse_mode(mode, os)

    extra = [str(t).strip() for t in (extra_tasks or []) if t and str(t).strip()]
    if extra:
        if kind == "web":
            return run_parallel_web_agents(
                [task] + extra,
                provider=provider,
                model=model,
                save_conversation=save_conversation,
                speed=agent_kwargs.get("speed"),
                headless=bool(agent_kwargs.get("headless")),
                browser_port=agent_kwargs.get("browser_port"),
            )
        if (kind, mobile_os) == ("mobile", "ios"):
            if _ios_device_target(device) == "hardware":
                raise ValueError(
                    'extra tasks (task_2, task_3, ...) need one device per task — '
                    'you only have one paired device. Use device="simulation" (the '
                    'default), which gives each task its own simulator.'
                )
            return run_parallel_sim_agents(
                [task] + extra,
                provider=provider,
                model=model,
                save_conversation=save_conversation,
                speed=agent_kwargs.get("speed"),
                ios_version=ios_version,
                sim_device=sim_device,
            )
        raise ValueError(
            f'extra tasks (task_2, task_3, ...) are only supported in "web use" '
            f'and "mobile use, ios" (device="simulation") — got "{mode}"'
        )

    AgentService = resolve_agent_service(mode, os)

    kwargs = dict(provider=provider, model=model,
                  save_conversation=save_conversation,
                  external_terminal=external_terminal, **agent_kwargs)

    phone = None
    if (kind, mobile_os) == ("mobile", "ios"):
        target = _ios_device_target(device)
        if target == "hardware":
            if sim_device:
                print('📱 sim_device is ignored with device="hardware" — the paired '
                      'device is whatever it is (iPhone or iPad).')
            if ios_version:
                print('📱 ios_version is ignored with device="hardware" — '
                      'the phone runs whatever iOS it has')
            phone = _connect_iphone()
        else:
            phone = _connect_simulator(ios_version, sim_device=sim_device)
    try:
        agent = AgentService(**_supported_kwargs(AgentService, kwargs))
        return agent.process_request(task)
    finally:
        if phone is not None:
            phone.deactivate()
            print("📱 iOS connection closed")


def run_parallel_sim_agents(tasks, provider, model, save_conversation=False,
                            speed=None, ios_version=None, connect_deadline=900.0,
                            sim_device=None):
    """Run N iOS tasks in parallel — one SIMULATOR per task, one agent each.

    The simulator counterpart of run_parallel_web_agents. Where web shares one
    Chrome and gives each agent a tab, a phone screen can't be shared: every
    task gets its own simulator, its own WebDriverAgent (ports 8100, 8101, ...)
    and its own child process in ./parallel/task_N/ (so its conversation/,
    raw_reasoning/ and debug/ never touch another task's). The parent boots the
    simulators and starts every WDA before any agent runs, exactly like the
    web path pre-launches Chrome, and always shuts them down afterwards.

    Returns {"status", "message", "results": [{"task", "status", "message"}, ...]}.
    """
    from Auto_Use.ios_connector.session import WDA_PORT
    from Auto_Use.ios_connector.sim_session import SimulatorSession

    n = len(tasks)
    print(f"Running {n} iOS tasks in parallel — one simulator each")

    sessions = []  # (index, SimulatorSession, sim_info)
    boot_threads = []
    try:
        # 1. Claim a distinct simulator + port per task. Resolving is quick
        #    (a simctl listing), so it runs in order and each pick excludes the
        #    udids already taken - that is what keeps the claims distinct.
        from Auto_Use.ios_connector.sim_session import resolve_simulator
        claimed = []
        for i in range(1, n + 1):
            port = WDA_PORT + i - 1
            sim, err = resolve_simulator(ios_version, exclude=tuple(claimed), device=sim_device)
            if err:
                detail = " — ".join(p for p in (err.get("error"), err.get("hint")) if p)
                raise RuntimeError(f"📱 task {i}: {detail}")
            claimed.append(sim["udid"])
            # Track it BEFORE activating: activate() boots a simulator and can
            # still fail afterwards, and only sessions in this list get shut
            # down by the finally below.
            sessions.append((i, SimulatorSession(port=port, slot=i), sim))
            print(f"[task {i}] {sim['name']} (iOS {sim['version']}) on port {port}")

        # 1b. Boot them ALL AT ONCE. activate() blocks until its simulator has
        #     finished booting (tens of seconds each, minutes on a cold
        #     runtime), so doing it in sequence made a 3-task run wait for
        #     three boots back to back. Each session has its own udid, port
        #     and derived-data folder, so the boots and xcodebuilds don't
        #     touch each other; the pinned-udid path skips re-resolution.
        print(f"📱 Booting {n} simulators in parallel...")
        outcomes = {}

        def _activate(index, session, sim):
            try:
                outcomes[index] = session.activate(udid=sim["udid"])
            except BaseException as e:            # never lose a thread's failure
                outcomes[index] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

        boot_threads[:] = [threading.Thread(target=_activate, args=(i, session, sim), daemon=True)
                           for i, session, sim in sessions]
        for t in boot_threads:
            t.start()
        for t in boot_threads:
            t.join()   # Ctrl+C lands here; the finally cancels the boots
        for i, _session, _sim in sessions:
            res = outcomes.get(i) or {"ok": False, "error": "activate() returned nothing"}
            if not res.get("ok"):
                detail = " — ".join(p for p in (res.get("error"), res.get("hint")) if p) or res.get("code")
                raise RuntimeError(f"📱 task {i}: {detail}")

        # 2. Wait for all of them to answer (first run builds WDA — minutes).
        print("📱 Starting WebDriverAgent on each simulator...")
        pending = [(i, s) for i, s, _r in sessions]
        end = time.time() + connect_deadline
        while pending and time.time() < end:
            time.sleep(2.0)
            still = []
            for i, session in pending:
                try:
                    st = session.status()
                except Exception:
                    still.append((i, session))
                    continue
                if st.get("state") == "connected":
                    print(f"[task {i}] simulator ready")
                elif st.get("state") in ("error", "disconnected"):
                    detail = " — ".join(p for p in (st.get("error"), st.get("hint")) if p)
                    raise RuntimeError(f"📱 task {i} failed to start: {detail}")
                else:
                    still.append((i, session))
            pending = still
        if pending:
            raise RuntimeError(
                f"📱 {len(pending)} simulator(s) not ready after {int(connect_deadline)}s"
            )

        # 3. One child process per task, each pinned to its own simulator.
        run_root = Path.cwd() / "parallel"
        shutil.rmtree(run_root, ignore_errors=True)

        base_env = os.environ.copy()
        base_env["PYTHONUNBUFFERED"] = "1"
        base_env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + base_env.get("PYTHONPATH", "")

        procs = []  # (index, task_text, Popen, result_file)
        for (i, session, sim), task_text in zip(sessions, tasks):
            task_dir = run_root / f"task_{i}"
            task_dir.mkdir(parents=True, exist_ok=True)
            result_file = task_dir / "result.json"
            cmd = [sys.executable, "-m", "Auto_Use.ios.agent",
                   "--task", str(task_text),
                   "--provider", provider,
                   "--model", model,
                   "--session-id", f"task_{i}",
                   "--wda-port", str(session.port),
                   "--sim-udid", str(sim["udid"]),
                   "--result", str(result_file)]
            if speed:
                cmd += ["--speed", str(speed)]
            if save_conversation:
                cmd += ["--save-conversation"]
            # The port MUST be in the child's environment before the process
            # starts: `python -m Auto_Use.ios.agent` imports the package (and
            # ios/tree/element.py, which resolves the WDA URL at import) before
            # __main__ runs, so a value set inside main() would arrive too late
            # and the scanner would read task 1's screen. One env per child.
            env = dict(base_env)
            env["AUTOUSE_WDA_PORT"] = str(session.port)
            env["AUTOUSE_IOS_SESSION"] = f"task_{i}"
            # Deliberately NO start_new_session: children stay in this
            # terminal's process group, so one Ctrl+C interrupts every agent.
            proc = subprocess.Popen(cmd, cwd=str(task_dir), env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            procs.append((i, str(task_text), proc, result_file))
            head = " ".join(str(task_text).split())[:70]
            print(f"[task {i}] started: {head}")

        def pump(index, stream):
            for line in stream:
                print(f"[task {index}] {line}", end="", flush=True)

        pumps = []
        for i, _task_text, proc, _rf in procs:
            t = threading.Thread(target=pump, args=(i, proc.stdout), daemon=True)
            t.start()
            pumps.append(t)

        try:
            for _i, _task_text, proc, _rf in procs:
                proc.wait()
        except KeyboardInterrupt:
            # The terminal already delivered SIGINT to the whole group; give
            # each child a grace period to write its result file, then escalate.
            print("\nStopping all tasks...")
            for _i, _task_text, proc, _rf in procs:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()

        for t in pumps:
            t.join(timeout=2)

        results = []
        ok = 0
        for i, task_text, proc, result_file in procs:
            try:
                result = json.loads(result_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result = {"status": "error",
                          "message": f"child exited with code {proc.returncode} "
                                     f"without writing a result"}
            if result.get("status") == "success":
                ok += 1
            results.append({"task": task_text, **result})

        print("=" * 60)
        print(f"PARALLEL RUN SUMMARY — {ok}/{n} succeeded")
        for i, r in enumerate(results, start=1):
            message = " ".join(str(r.get("message", "")).split())
            print(f"[task {i}] {r.get('status', '?'):<10} {message[:80]}")
        print(f"Artifacts: ./parallel/task_N/  (result.json, conversation/, "
              f"debug/, raw_reasoning/)")
        print("=" * 60)

        return {
            "status": "success" if ok == n else "error",
            "message": f"{ok}/{n} tasks succeeded",
            "results": results,
        }
    finally:
        # Every simulator this run booted gets shut down — success, error or
        # Ctrl+C — the same guarantee the single-task path gives. deactivate()
        # also CANCELS a boot still running in its thread (activate re-checks
        # ownership and starts no runner); wait for those threads, then
        # deactivate once more so a runner spawned in the last instant before
        # the cancel is reaped too.
        def _close_all():
            for i, session, _sim in sessions:
                try:
                    session.deactivate()
                except Exception:
                    pass
        _close_all()
        for t in boot_threads:
            if t.is_alive():
                t.join(timeout=15)
        if any(t.is_alive() for t in boot_threads) or boot_threads:
            _close_all()
        if sessions:
            print("📱 simulators closed")


def run_parallel_web_agents(tasks, provider, model, save_conversation=False,
                            speed=None, headless=False, browser_port=None):
    """Run N web tasks in parallel — one child process per task, one shared Chrome.

    Each child gets its own working directory ./parallel/task_N/ (so its
    conversation/, raw_reasoning/ and debug/ never touch another task's), its
    own scratchpad session, and ONE dedicated browser tab. Live output is
    prefixed [task N]; a summary prints when every task has finished.

    Returns {"status", "message", "results": [{"task", "status", "message"}, ...]}.
    """
    # Importing the package builds the Rust extension ONCE here in the parent,
    # so N children never race the compiler — they find a fresh .so.
    from Auto_Use.web.agent import launch_chrome, CHROME_PORT
    from Auto_Use import browser_profile_dir

    port = int(browser_port or CHROME_PORT)
    n = len(tasks)
    print(f"Running {n} web tasks in parallel — one Chrome, port {port}")
    if not headless:
        print(f"Note: {n} agents share one visible Chrome window — expect tab "
              f"switching on screen. Set headless = True for a calmer run.")

    # One browser for everyone: launch (or attach) before any child starts, so
    # every child's constructor takes the clean attach path.
    launch_chrome(port, headless, str(browser_profile_dir()))

    run_root = Path.cwd() / "parallel"
    shutil.rmtree(run_root, ignore_errors=True)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    procs = []  # (index, task_text, Popen, result_file)
    for i, task_text in enumerate(tasks, start=1):
        task_dir = run_root / f"task_{i}"
        task_dir.mkdir(parents=True, exist_ok=True)
        result_file = task_dir / "result.json"
        cmd = [sys.executable, "-m", "Auto_Use.web.agent",
               "--task", str(task_text),
               "--provider", provider,
               "--model", model,
               "--session-id", f"task_{i}",
               "--browser-port", str(port),
               "--result", str(result_file)]
        if speed:
            cmd += ["--speed", str(speed)]
        if headless:
            cmd += ["--headless"]
        if save_conversation:
            cmd += ["--save-conversation"]
        # Deliberately NO start_new_session: children stay in this terminal's
        # process group, so one Ctrl+C interrupts every agent at once.
        proc = subprocess.Popen(cmd, cwd=str(task_dir), env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        procs.append((i, str(task_text), proc, result_file))
        head = " ".join(str(task_text).split())[:70]
        print(f"[task {i}] started: {head}")

    def pump(index, stream):
        for line in stream:
            print(f"[task {index}] {line}", end="", flush=True)

    pumps = []
    for i, _task_text, proc, _rf in procs:
        t = threading.Thread(target=pump, args=(i, proc.stdout), daemon=True)
        t.start()
        pumps.append(t)

    try:
        for _i, _task_text, proc, _rf in procs:
            proc.wait()
    except KeyboardInterrupt:
        # The terminal already delivered SIGINT to the whole group; give each
        # child a grace period to write its result file, then escalate.
        print("\nStopping all tasks...")
        for _i, _task_text, proc, _rf in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

    for t in pumps:
        t.join(timeout=2)

    results = []
    ok = 0
    for i, task_text, proc, result_file in procs:
        try:
            result = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = {"status": "error",
                      "message": f"child exited with code {proc.returncode} "
                                 f"without writing a result"}
        if result.get("status") == "success":
            ok += 1
        results.append({"task": task_text, **result})

    print("=" * 60)
    print(f"PARALLEL RUN SUMMARY — {ok}/{n} succeeded")
    for i, r in enumerate(results, start=1):
        message = " ".join(str(r.get("message", "")).split())
        print(f"[task {i}] {r.get('status', '?'):<10} {message[:80]}")
    print(f"Artifacts: ./parallel/task_N/  (result.json, conversation/, "
          f"debug/, raw_reasoning/)")
    print("=" * 60)

    return {
        "status": "success" if ok == n else "error",
        "message": f"{ok}/{n} tasks succeeded",
        "results": results,
    }
