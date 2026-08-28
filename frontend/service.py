# Copyright 2026 Cursortouch — Auto-Use

"""Backend service for the desktop app — the Flask server, every HTTP route, the
agent run, the window.* push callbacks, and the provider/api-key/settings/path
helpers. Split out of the old monolithic app.py; app.py now keeps only the
order-sensitive bootstrap and the pywebview window.

This module is self-contained (it never imports `app`), so `python app.py` —
where app.py is __main__ — can import it once without re-importing a second copy
of app. app.py re-exports the foundational symbols (debug_log, IS_COMPILED,
app_data_dir, debug_exception, …) so `from app import …` keeps working for the
coder/minion/controller/telegram modules. The window is created by app.py and
handed over via set_window(); the callbacks read it back via the module global.
"""

import io
import os
import re
import sys
import json
import uuid
import atexit
import logging
import signal
import platform
import importlib
import threading
import time
import shutil
import subprocess
import traceback
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

# Where the user's data lives (autouse_data/, outside the install folder).
from Auto_Use import api_key_file, skills_dir, data_root

# Resumable chat memory + per-chat token tracker. Platform-agnostic, pure-stdlib.
from Auto_Use.agent_conversation.service import conversation
from Auto_Use.memory_compression.memory_tracker import MemoryTracker

# Markdown -> HTML for everything the agent writes to the user (scratchpad
# notes, done/exit summaries). The single place that formatting happens.
from frontend.markdown import render as md_render, render_notes as md_render_notes

# This file lives at <repo>/frontend/service.py, so __file__-relative paths are
# one directory deeper than app.py. Anchor everything off these.
_THIS_DIR = Path(__file__).resolve().parent     # <repo>/frontend
_REPO_ROOT = _THIS_DIR.parent                    # <repo>

# =============================================================================
# Platform detection
# =============================================================================
# PLATFORM_PKG is the Auto_Use sub-package with the platform-specific code.
IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

if IS_MAC:
    PLATFORM_PKG = "mac"
elif IS_WINDOWS:
    PLATFORM_PKG = "windows"
else:
    raise RuntimeError(f"Unsupported OS: {platform.system()}")

# =============================================================================
# Build / process flags + debug logging (compiled binary only)
# =============================================================================
IS_COMPILED = getattr(sys, 'frozen', False) or '__compiled__' in dir()
IS_CLI_SUBPROCESS = "--cli-mode" in sys.argv
# Any re-exec of AutoUse.exe that must NOT clobber the parent's debug log / wipe
# the parent's scratchpad (--cli-mode / --minion-mode / --banner-mode).
IS_SECONDARY_PROCESS = (
    IS_CLI_SUBPROCESS
    or "--banner-mode" in sys.argv
    or "--minion-mode" in sys.argv
)

# Unique id for this build (gates the once-per-build macOS TCC repair). Absent in dev.
try:
    from _build_stamp import BUILD_STAMP
except Exception:
    BUILD_STAMP = "unknown"

# Bundle id of the packaged macOS app — used to target `tccutil reset`.
# Must match the bundle id stamped by the packaging step.
MACOS_BUNDLE_ID = "com.ashishyadav.autouse"


def app_data_dir() -> Path:
    """Root for cli_agent_result/ + cli_minion_result/ + settings/chats in the
    binary build. Compiled: ~/Library/Application Support/AutoUse (macOS) /
    %LOCALAPPDATA%/AutoUse (Windows). Dev: the repo root (where app.py lives)."""
    if IS_COMPILED:
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "AutoUse"
        elif sys.platform.startswith("win"):
            local = os.environ.get("LOCALAPPDATA")
            base = Path(local) / "AutoUse" if local else Path.home() / "AppData" / "Local" / "AutoUse"
        else:
            base = Path.home() / ".local" / "share" / "AutoUse"
    else:
        base = _REPO_ROOT
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_log_path():
    """Get path for debug log file (only used in compiled mode)"""
    return os.path.join(os.path.dirname(sys.executable), "autouse_debug.log")

DEBUG_LOG_PATH = get_log_path() if IS_COMPILED else None


def debug_log(message, level="INFO"):
    """Write debug message to log file (only in compiled mode)"""
    if not IS_COMPILED or not DEBUG_LOG_PATH:
        return
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log_line = f"[{timestamp}] [{level}] {message}\n"
        with open(DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(log_line)
    except:
        pass


def debug_exception(context):
    """Log full exception traceback (only in compiled mode)"""
    if not IS_COMPILED:
        return
    debug_log(f"EXCEPTION in {context}:", "ERROR")
    debug_log(traceback.format_exc(), "ERROR")

# Initialize the log file on import (compiled mode, not in any secondary
# subprocess — those would clobber the parent's log on every spawn).
if IS_COMPILED and not IS_SECONDARY_PROCESS and DEBUG_LOG_PATH:
    try:
        with open(DEBUG_LOG_PATH, 'w', encoding='utf-8') as f:
            f.write(f"=== Auto Use Debug Log - Started {datetime.now()} ===\n")
            f.write(f"Python: {sys.version}\n")
            f.write(f"Platform: {platform.system()} ({PLATFORM_PKG})\n")
            f.write(f"Executable: {sys.executable}\n")
            f.write("=" * 60 + "\n\n")
    except:
        pass


# =============================================================================
# Embedded resource loader (Nuitka compiled binary)
# =============================================================================
def setup_embedded_resources():
    """Patch builtins.open so file reads resolve embedded resources (compiled
    binary only). Called EARLY from app.py's bootstrap, before any runtime file
    read. Returns True if the patch was installed."""
    import builtins
    import base64

    try:
        from _embedded_resources import RESOURCES  # type: ignore - generated by the binary build script
    except ImportError:
        return False

    _original_open = builtins.open

    def _patched_open(file, mode='r', *args, **kwargs):
        file_str = str(file).replace('\\', '/')

        for res_path, encoded_data in RESOURCES.items():
            if file_str.endswith(res_path) or res_path in file_str:
                file_parts = file_str.split('/')
                res_parts = res_path.split('/')

                if len(file_parts) >= 2 and len(res_parts) >= 2:
                    if file_parts[-1] == res_parts[-1] and file_parts[-2] == res_parts[-2]:
                        pass
                    elif file_str.endswith(res_path):
                        pass
                    else:
                        continue
                elif file_parts[-1] != res_parts[-1]:
                    continue

                content = base64.b64decode(encoded_data)

                if 'b' in mode:
                    return io.BytesIO(content)
                else:
                    encoding = kwargs.get('encoding', 'utf-8')
                    return io.StringIO(content.decode(encoding))

        return _original_open(file, mode, *args, **kwargs)

    builtins.open = _patched_open
    return True


# =============================================================================
# Flask app initialization
# =============================================================================
def get_frontend_path():
    """Get correct frontend path for dev mode (returns None in compiled mode).
    This file lives IN frontend/, so the static folder is its own directory."""
    if IS_COMPILED:
        return None
    return str(_THIS_DIR)

frontend_path = get_frontend_path()
if frontend_path:
    app = Flask(__name__, static_folder=frontend_path, static_url_path='')
else:
    app = Flask(__name__)

# Suppress default Flask logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)


@app.before_request
def _block_source_files():
    """frontend/ is both the static-served folder AND where this service.py lives.
    Flask's static route (static_url_path='') would otherwise serve our own
    source — block any .py/.pyc/__pycache__ path before routing."""
    from flask import request
    p = request.path.lower()
    if p.endswith(('.py', '.pyc', '.pyo')) or '__pycache__' in p:
        return "Not found", 404


# =============================================================================
# Path / startup helpers
# =============================================================================
def get_auto_use_path():
    """Get path to the Auto_Use package root"""
    if IS_COMPILED:
        return Path(sys.executable).parent / "Auto_Use"
    return _REPO_ROOT / "Auto_Use"


def get_platform_use_path(pkg=None):
    """Get path to the active Auto_Use/<platform>_use/ directory.
    pkg overrides the host default for mode-routed runs (e.g. "ios")."""
    return get_auto_use_path() / (pkg or PLATFORM_PKG)


def clean_scratchpad():
    """Clear contents of <platform>_use/scratchpad/ and sandbox_workspace/ on startup"""
    try:
        scratchpad_dir = get_platform_use_path() / "scratchpad"
        if scratchpad_dir.exists():
            for item in scratchpad_dir.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
        else:
            scratchpad_dir.mkdir(parents=True, exist_ok=True)

        # Clean sandbox_workspace on Desktop
        sandbox_dir = Path.home() / "Desktop" / "sandbox_workspace"
        if sandbox_dir.exists():
            for item in sandbox_dir.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

        # Shell-use conversation channel files leak only on an app crash (the
        # run's finally deletes them); durable memory lives in agent_conversation,
        # so sweeping the whole folder at startup is always safe.
        shell_hist = app_data_dir() / "cli_shell_history"
        if shell_hist.exists():
            shutil.rmtree(shell_hist, ignore_errors=True)
    except Exception:
        debug_exception("clean_scratchpad")


def _reset_todo_file(pkg=None):
    """Delete <platform>_use/scratchpad/todo/todo.md so a new agent run starts
    with an empty top-right todo card."""
    try:
        todo_file = get_platform_use_path(pkg) / "scratchpad" / "todo" / "todo.md"
        if todo_file.exists():
            todo_file.unlink()
    except Exception:
        debug_exception("_reset_todo_file")


def _reset_scratchpad_file(pkg=None):
    """Delete <platform>_use/scratchpad/milestone/milestone.md so a new run starts
    with an empty scratchpad."""
    try:
        notes_file = get_platform_use_path(pkg) / "scratchpad" / "milestone" / "milestone.md"
        if notes_file.exists():
            notes_file.unlink()
    except Exception:
        debug_exception("_reset_scratchpad_file")


def set_frontend_flag():
    """Override the FRONTEND flag in Auto_Use.<platform>_use.tree.element to True"""
    try:
        element = importlib.import_module(f"Auto_Use.{PLATFORM_PKG}.tree.element")
        element.FRONTEND = True
    except ImportError:
        pass
    except Exception:
        debug_exception("set_frontend_flag")


def _ax_granted():
    """True if Accessibility is currently granted (no prompt)."""
    try:
        from ApplicationServices import AXIsProcessTrusted
        return bool(AXIsProcessTrusted())
    except Exception:
        return True  # can't tell -> treat as granted so we never reset blindly


def _screen_granted():
    """True if Screen Recording is currently granted (no prompt)."""
    try:
        from Quartz import CGPreflightScreenCaptureAccess
        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return True


def _fda_granted():
    """True if Full Disk Access is granted (probe an FDA-gated path)."""
    try:
        tcc_db = os.path.expanduser("~/Library/Application Support/com.apple.TCC/TCC.db")
        with open(tcc_db, "rb") as _f:
            _f.read(1)
        return True
    except PermissionError:
        return False
    except Exception:
        return True  # missing path / other -> don't reset


# =============================================================================
# macOS permission catalog + setup-wizard state
# =============================================================================
# The four macOS permissions Auto Use needs, in the order the setup wizard walks
# the user through them. Full Disk Access is gated BEFORE Screen Recording on
# purpose: once FDA is granted we can read TCC.db live to detect Screen
# Recording's real state (its in-process CGPreflight result is cached for the
# whole process lifetime, so it can't see a grant made after launch).
PERMISSION_CATALOG = [
    {
        "key": "accessibility",
        "label": "Accessibility",
        "description": "Lets Auto Use move the cursor, click, and type for you.",
        "tcc_service": "Accessibility",
        "settings_deep_link": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        "relaunch_sensitive": False,
        "auto_resettable": True,
    },
    {
        "key": "full_disk_access",
        "label": "Full Disk Access",
        "description": "Lets Auto Use read and save the files your tasks need.",
        "tcc_service": "SystemPolicyAllFiles",
        "settings_deep_link": "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
        "relaunch_sensitive": False,
        "auto_resettable": True,
    },
    {
        "key": "screen_recording",
        "label": "Screen Recording",
        "description": "Lets Auto Use see your screen to decide what to do next.",
        "tcc_service": "ScreenCapture",
        "settings_deep_link": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
        "relaunch_sensitive": True,
        "auto_resettable": True,
    },
    {
        "key": "automation",
        "label": "Automation",
        "description": "Lets Auto Use tell apps like Finder and Safari what to do.",
        "tcc_service": "AppleEvents",
        "settings_deep_link": "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
        "relaunch_sensitive": False,
        "auto_resettable": False,
    },
]

# Cached result of the last System Events probe (Automation has no clean
# no-prompt API; we remember whether the probe last succeeded).
_AUTOMATION_CACHE = "automation_grant.json"


def _automation_marker() -> Path:
    """autouse_data/automation_grant.json — the probe cache lives with the rest
    of the user's data, not loose in the install folder / repo root. Moves a copy
    left at the old app_data_dir() location by an earlier build, so an already
    granted user isn't walked through the Automation step again. Best effort: a
    failed move only costs one extra probe."""
    dest = data_root() / _AUTOMATION_CACHE
    try:
        legacy = app_data_dir() / _AUTOMATION_CACHE
        if not dest.exists() and legacy.is_file() and legacy.resolve() != dest.resolve():
            os.replace(str(legacy), str(dest))
    except Exception:
        debug_exception("automation cache migrate")
    return dest


def _automation_granted():
    """Best-effort Automation (System Events) check. There is no reliable
    no-prompt API, so we read the cached result of the last probe (written by
    _request_automation when it runs). Defaults to False so the wizard shows the
    step rather than silently passing it."""
    try:
        marker = _automation_marker()
        if marker.exists():
            return bool(json.loads(marker.read_text()).get("granted"))
    except Exception:
        pass
    return False


def _screen_granted_live():
    """Live (uncached) Screen Recording grant by reading TCC.db directly.
    Needs Full Disk Access to read the DB and a stable client id (the bundle id),
    so it only works in the packaged app. Returns True/False, or None when we
    can't tell (no FDA / dev mode / schema mismatch). Used only to detect the
    'granted in Settings but not yet effective in this process' relaunch case."""
    if not (IS_COMPILED and IS_MAC):
        return None  # dev: client is the python/terminal path, not the bundle id
    try:
        import sqlite3
        tcc_db = os.path.expanduser("~/Library/Application Support/com.apple.TCC/TCC.db")
        con = sqlite3.connect(f"file:{tcc_db}?mode=ro", uri=True, timeout=1.0)
        try:
            cur = con.execute(
                "SELECT auth_value FROM access WHERE service=? AND client=?",
                ("kTCCServiceScreenCapture", MACOS_BUNDLE_ID),
            )
            row = cur.fetchone()
        finally:
            con.close()
        if row is None:
            return False
        return int(row[0]) >= 2  # auth_value: 0/1 denied, 2 allowed (modern schema)
    except Exception:
        return None


def _needs_relaunch():
    """True if a relaunch-sensitive permission (Screen Recording) is granted on
    disk but the cached in-process preflight still reports it as not granted."""
    if not IS_MAC:
        return False
    try:
        if not _screen_granted() and _screen_granted_live() is True:
            return True
    except Exception:
        pass
    return False


def _permission_states():
    """Per-permission live state (mac). Pure read, never prompts. Single source
    of truth for all_permissions_granted() and the /api/permissions/* routes."""
    checks = {
        "accessibility":    _ax_granted,
        "full_disk_access": _fda_granted,
        "screen_recording": _screen_granted,
        "automation":       _automation_granted,
    }
    states = []
    for spec in PERMISSION_CATALOG:
        try:
            granted = bool(checks[spec["key"]]())
        except Exception:
            granted = False
        states.append({
            "key": spec["key"],
            "label": spec["label"],
            "description": spec["description"],
            "settings_deep_link": spec["settings_deep_link"],
            "relaunch_sensitive": spec["relaunch_sensitive"],
            "granted": granted,
        })
    return states


def all_permissions_granted() -> bool:
    """True iff every required macOS permission is currently granted. Non-mac ->
    True. Used by app.py to decide whether to open the setup wizard or the app.
    Fails open (True) on internal error so a bug can never brick startup."""
    if not IS_MAC:
        return True
    try:
        return all(p["granted"] for p in _permission_states())
    except Exception:
        debug_exception("all_permissions_granted")
        return True


def repair_stale_tcc_entries():
    """Clear orphaned macOS TCC ("ghost") entries for our bundle id, once per build.

    When a previous build's code signature no longer matches the current binary,
    the old permission grant becomes a "ghost" entry: the toggle still shows but
    is bound to a binary that's gone, so the new build silently can't use it AND
    no fresh prompt appears. We `tccutil reset` such entries so
    request_macos_permissions() can re-prompt and rebind to the CURRENT binary.

    Safety: compiled macOS builds only; never reset a working permission
    (preflight skip); at most once per build identity (BUILD_STAMP marker).
    Automation (AppleEvents) is intentionally NOT auto-reset (no reliable
    no-prompt preflight); the System Events re-prompt rebinds it instead.
    """
    if not (IS_COMPILED and IS_MAC):
        return
    try:
        marker = app_data_dir() / "tcc_repair.json"
        already = None
        if marker.exists():
            try:
                already = json.loads(marker.read_text()).get("build_stamp")
            except Exception:
                already = None
        if already == BUILD_STAMP:
            return  # already repaired for this build identity

        # (tccutil service token, preflight returning True when already working)
        services = [
            ("Accessibility", _ax_granted),
            ("ScreenCapture", _screen_granted),
            ("SystemPolicyAllFiles", _fda_granted),
        ]
        for service, is_granted in services:
            try:
                if is_granted():
                    continue  # working — don't touch it
                # user-level reset of our own bundle's TCC entry (no sudo)
                subprocess.run(
                    ["tccutil", "reset", service, MACOS_BUNDLE_ID],
                    capture_output=True, text=True, timeout=10
                )
            except Exception:
                debug_exception(f"tccutil reset {service}")

        try:
            marker.write_text(json.dumps({
                "build_stamp": BUILD_STAMP,
                "repaired_at": datetime.now().isoformat(),
            }))
        except Exception:
            debug_exception("write tcc_repair marker")
    except Exception:
        debug_exception("repair_stale_tcc_entries")


# --- per-permission triggers (each pokes ONE macOS permission) ----------------
def _request_accessibility():
    """Show the Accessibility trust prompt if not already granted."""
    from ApplicationServices import AXIsProcessTrusted, AXIsProcessTrustedWithOptions
    if not AXIsProcessTrusted():
        AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})


def _request_screen_recording():
    """Show the Screen Recording prompt if not already granted (first call only;
    macOS shows nothing on later calls — the wizard opens the pane as fallback)."""
    from Quartz import CGPreflightScreenCaptureAccess, CGRequestScreenCaptureAccess
    if not CGPreflightScreenCaptureAccess():
        CGRequestScreenCaptureAccess()


def _request_automation():
    """Run the System Events probe — this triggers the Automation consent dialog
    AND lets us learn whether Automation works. osascript BLOCKS until the user
    answers the dialog (up to the timeout), so the result usually reflects the
    user's choice. Caches the outcome for _automation_granted()."""
    granted = False
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to return name of first process whose frontmost is true'],
            capture_output=True, text=True, timeout=10
        )
        granted = (result.returncode == 0 and bool(result.stdout.strip()))
    except Exception:
        granted = False
    try:
        _automation_marker().write_text(json.dumps({
            "granted": granted, "checked_at": datetime.now().isoformat(),
        }))
    except Exception:
        debug_exception("automation cache write")
    return granted


# Map of permission key -> programmatic trigger. Full Disk Access has none
# (macOS only lets us open the pane), so it's absent here.
_REQUEST_TRIGGERS = {
    "accessibility": _request_accessibility,
    "screen_recording": _request_screen_recording,
    "automation": _request_automation,
}


def _open_settings_pane(deep_link):
    """Open a specific System Settings privacy pane (best-effort)."""
    try:
        subprocess.run(["open", deep_link], capture_output=True, timeout=10)
    except Exception:
        debug_exception("open settings pane")


def _tccutil_reset(service):
    """Reset our own bundle's TCC entry for ONE service (user-level, no sudo).
    This is the per-permission ghost-entry repair: clears a stale grant left by a
    previous install so a fresh prompt can rebind to the CURRENT binary. No-op in
    dev (the bundle id isn't registered; the Terminal/python owns the grant)."""
    if not (IS_COMPILED and IS_MAC):
        return False
    try:
        subprocess.run(["tccutil", "reset", service, MACOS_BUNDLE_ID],
                       capture_output=True, text=True, timeout=10)
        return True
    except Exception:
        debug_exception(f"tccutil reset {service}")
        return False


def request_permission(key):
    """Drive ONE macOS permission for the setup wizard. Returns
    {ok, key, granted, reset_performed}. Flow: re-check first; if already
    granted, do nothing. Otherwise auto-repair any ghost entry (tccutil reset of
    just this service, when resettable), then trigger the OS prompt / open the
    exact Settings pane, then re-check."""
    spec = next((p for p in PERMISSION_CATALOG if p["key"] == key), None)
    if not IS_MAC or spec is None:
        return {"ok": False, "key": key, "granted": False, "reset_performed": False}
    checks = {
        "accessibility": _ax_granted, "full_disk_access": _fda_granted,
        "screen_recording": _screen_granted, "automation": _automation_granted,
    }
    try:
        if checks[key]():
            return {"ok": True, "key": key, "granted": True, "reset_performed": False}
        reset_performed = False
        if spec["auto_resettable"]:
            reset_performed = _tccutil_reset(spec["tcc_service"])
        trigger = _REQUEST_TRIGGERS.get(key)
        if trigger:
            try:
                trigger()
            except Exception:
                debug_exception(f"trigger {key}")
        _open_settings_pane(spec["settings_deep_link"])
        return {"ok": True, "key": key, "granted": bool(checks[key]()),
                "reset_performed": reset_performed}
    except Exception:
        debug_exception(f"request_permission {key}")
        return {"ok": False, "key": key, "granted": False, "reset_performed": False}


def reset_all_permissions():
    """Escape hatch: clear ALL of our bundle's TCC grants (the same reset the
    uninstaller does, minus removing the app) for a guaranteed clean slate, then
    relaunch. Compiled-mac only for the reset; the relaunch runs everywhere."""
    if IS_MAC and IS_COMPILED:
        try:
            subprocess.run(["tccutil", "reset", "All", MACOS_BUNDLE_ID],
                           capture_output=True, text=True, timeout=15)
        except Exception:
            debug_exception("tccutil reset All")
    try:
        _automation_marker().unlink(missing_ok=True)
    except Exception:
        pass
    request_relaunch()


# --- relaunch (needed so cached preflights re-evaluate) -----------------------
_relaunch_requested = False


def request_relaunch():
    """Ask for a relaunch after the GUI loop exits, and tear down the window so
    webview.start() returns on the main thread. The actual relaunch_app() runs
    from app.py right after webview.start()."""
    global _relaunch_requested
    _relaunch_requested = True
    try:
        w = get_window()
        if w:
            w.destroy()
    except Exception:
        debug_exception("request_relaunch destroy")


def relaunch_app():
    """Relaunch Auto Use cleanly so cached TCC preflights are re-evaluated. MUST
    be called after webview.start() returns (main thread; port 5000 released).
    Compiled: re-open the .app bundle via LaunchServices (rebinds TCC identity to
    the bundle id) then hard-exit. Dev: replace the process image."""
    try:
        if IS_COMPILED and IS_MAC:
            app_bundle = Path(sys.executable).resolve().parents[2]  # .../AutoUse.app
            subprocess.Popen(["open", "-n", str(app_bundle)])
            os._exit(0)
        else:
            python = sys.executable
            script = str(_REPO_ROOT / "app.py")
            os.execv(python, [python, script])
    except Exception:
        debug_exception("relaunch_app")


def request_macos_permissions():
    """Bulk-prompt for required macOS permissions (no-op elsewhere). The setup
    wizard now drives prompts one-by-one via request_permission(); this is kept
    as a fallback for any non-wizard caller."""
    if not IS_MAC:
        return
    try:
        _request_accessibility()
        _request_screen_recording()
        _request_automation()
        if not _fda_granted():
            print(
                "\n⚠️  Full Disk Access not granted. Auto Use needs it so shell commands can\n"
                "    read/write Desktop, Documents and Downloads without macOS permission popups.\n"
                "    Opening System Settings — add to Full Disk Access:\n"
                "      • Packaged app: add 'AutoUse'\n"
                "      • Dev run: add your Terminal / VS Code / the python you launch from\n"
            )
            _open_settings_pane("x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")
    except Exception:
        debug_exception("request_macos_permissions")


# =============================================================================
# Providers / API keys / settings
# =============================================================================
def get_llm_providers():
    """Get list of available LLM providers and their models for the active platform."""
    try:
        base = f"Auto_Use.{PLATFORM_PKG}.llm_provider"
        openrouter_models = importlib.import_module(f"{base}.openrouter.view").MODEL_MAPPINGS
        groq_models       = importlib.import_module(f"{base}.groq.view").MODEL_MAPPINGS
        openai_models     = importlib.import_module(f"{base}.openai.view").MODEL_MAPPINGS
        anthropic_models  = importlib.import_module(f"{base}.anthropic.view").MODEL_MAPPINGS
        google_models     = importlib.import_module(f"{base}.google.view").MODEL_MAPPINGS
        perplexity_models = importlib.import_module(f"{base}.perplexity.view").MODEL_MAPPINGS
        # A missing module must drop just this provider, not the whole list.
        try:
            together_models = importlib.import_module(f"{base}.together.view").MODEL_MAPPINGS
        except ModuleNotFoundError:
            together_models = None

        def format_models(mappings):
            return [{
                'id': model_id,
                'display_name': info.get('display_name', model_id)
            } for model_id, info in mappings.items() if not info.get('hidden', False)]

        providers = [
            {'id': 'openrouter', 'name': 'openrouter', 'models': format_models(openrouter_models)},
            {'id': 'groq',       'name': 'groq',       'models': format_models(groq_models)},
            {'id': 'openai',     'name': 'openai',     'models': format_models(openai_models)},
            {'id': 'anthropic',  'name': 'anthropic',  'models': format_models(anthropic_models)},
            {'id': 'google',     'name': 'google',     'models': format_models(google_models)},
            {'id': 'perplexity', 'name': 'perplexity', 'models': format_models(perplexity_models)},
        ]
        if together_models:
            providers.append({'id': 'together', 'name': 'together', 'models': format_models(together_models)})
        return providers
    except Exception:
        debug_exception("get_llm_providers")
        return []


PROVIDER_KEY_MAP = {
    'openrouter': 'OPENROUTER_API_KEY',
    'groq': 'GROQ_API_KEY',
    'openai': 'OPENAI_API_KEY',
    'anthropic': 'ANTHROPIC_API_KEY',
    'google': 'GOOGLE_API_KEY',
    'perplexity': 'PERPLEXITY_API_KEY',
    'together': 'TOGETHER_API_KEY',
}

# Extra keys stored in the same api_key.txt
EXTRA_KEYS = ['VERTEX_PROJECT_ID', 'VERTEX_LOCATION']


def get_api_key_file():
    """Path to api_key.txt — autouse_data/api_key/, OUTSIDE the install folder so
    an uninstall/reinstall no longer wipes every API key. Shared across
    platforms; resolved in Auto_Use/__init__.py so the Settings panel, the
    Telegram bot and each llm_provider can't disagree about it."""
    return api_key_file()


def read_api_keys():
    """Read api_key.txt and return dict of provider -> key value"""
    key_file = get_api_key_file()
    all_key_names = list(PROVIDER_KEY_MAP.values()) + EXTRA_KEYS
    keys = {k: '' for k in all_key_names}
    if key_file.exists():
        try:
            with open(key_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if '=' in line:
                        name, _, value = line.partition('=')
                        # Keep every key (e.g. TELEGRAM_BOT_TOKEN) so unmanaged
                        # entries survive a read-modify-write cycle.
                        keys[name] = value
        except Exception:
            debug_exception("read_api_keys")
    return keys


def write_api_keys(keys):
    """Write dict of key names -> values to api_key.txt"""
    key_file = get_api_key_file()
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        all_key_names = list(PROVIDER_KEY_MAP.values()) + EXTRA_KEYS
        # Preserve any unmanaged keys (e.g. TELEGRAM_*) written elsewhere.
        extra = [k for k in keys if k not in all_key_names]
        with open(key_file, 'w', encoding='utf-8') as f:
            for name in all_key_names:
                f.write(f"{name}={keys.get(name, '')}\n")
            for name in extra:
                f.write(f"{name}={keys.get(name, '')}\n")
    except Exception:
        debug_exception("write_api_keys")


# Persisted last-used selection (provider + model). Lives alongside the chat
# conversations — same folder the conversation service uses (conversation.root():
# Auto_Use/agent_conversation/ in dev, <exe>/Auto_Use/agent_conversation/ when
# compiled) — so all per-user UI state sits in one place.
def get_settings_file():
    return conversation.root() / "settings.json"


def read_settings():
    """Read settings.json -> {'provider':..., 'model':...} (or {} if missing/invalid)."""
    settings_file = get_settings_file()
    if settings_file.exists():
        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            debug_exception("read_settings")
    return {}


def write_settings(updates):
    """Merge a dict of fields into settings.json (None values written through)."""
    settings_file = get_settings_file()
    try:
        data = read_settings()
        data.update(updates)
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception:
        debug_exception("write_settings")


def get_provider_api_key(provider):
    """Get API key for a specific provider from file"""
    env_name = PROVIDER_KEY_MAP.get(provider)
    if not env_name:
        return None
    keys = read_api_keys()
    return keys.get(env_name, '') or None


# =============================================================================
# Shared state — the window handle + the active-run guard
# =============================================================================
# webview_window is set by app.py after it creates the window (set_window); the
# callbacks read it back here. active_agent_* guard the running agent.
webview_window = None
active_agent_stop_event = None
active_agent_session_id = None


def set_window(win):
    """Hand the pywebview window over from app.py so callbacks can reach it."""
    global webview_window
    webview_window = win


def get_window():
    return webview_window


# =============================================================================
# Embedded file serving (compiled mode)
# =============================================================================
MIME_TYPES = {
    '.html': 'text/html',
    '.css': 'text/css',
    '.js': 'application/javascript',
    '.json': 'application/json',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.ico': 'image/x-icon',
    '.svg': 'image/svg+xml',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
    '.ttf': 'font/ttf',
}


def serve_embedded_file(resource_path):
    """Serve a file from embedded resources (compiled mode only)"""
    try:
        from _embedded_resources import RESOURCES  # type: ignore - generated by the binary build script
        import base64
        from flask import Response, request

        resource_path = resource_path.replace('\\', '/')

        for res_key, encoded_data in RESOURCES.items():
            if res_key.endswith(resource_path) or resource_path in res_key:
                if res_key.split('/')[-1] == resource_path.split('/')[-1]:
                    content = base64.b64decode(encoded_data)
                    ext = os.path.splitext(resource_path)[1].lower()
                    mime_type = MIME_TYPES.get(ext, 'application/octet-stream')

                    # WKWebView's media player (macOS) requires HTTP Range support
                    # (206) to play <video>/<audio>; honour the Range header.
                    range_header = request.headers.get('Range')
                    if range_header and mime_type.startswith(('video/', 'audio/')):
                        size = len(content)
                        import re as _re
                        m = _re.match(r'bytes=(\d*)-(\d*)', range_header)
                        start, end = 0, size - 1
                        if m:
                            if m.group(1):
                                start = int(m.group(1))
                            if m.group(2):
                                end = int(m.group(2))
                        start = max(0, start)
                        end = min(end, size - 1)
                        chunk = content[start:end + 1]
                        resp = Response(chunk, status=206, mimetype=mime_type)
                        resp.headers['Content-Range'] = f'bytes {start}-{end}/{size}'
                        resp.headers['Accept-Ranges'] = 'bytes'
                        resp.headers['Content-Length'] = str(len(chunk))
                        return resp

                    resp = Response(content, mimetype=mime_type)
                    resp.headers['Accept-Ranges'] = 'bytes'
                    return resp

        return None
    except ImportError:
        return None


# =============================================================================
# Flask routes — static / assets
# =============================================================================
@app.route('/')
def index():
    if IS_COMPILED:
        response = serve_embedded_file('frontend/index.html')
        if response:
            return response
        return "index.html not found in embedded resources", 500
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/<path:filename>')
def serve_static(filename):
    """Serve static files - from embedded resources in compiled mode, filesystem in dev mode"""
    if IS_COMPILED:
        response = serve_embedded_file('frontend/' + filename)
        if response:
            return response
        response = serve_embedded_file(filename)
        if response:
            return response
        return "Not found", 404

    if app.static_folder:
        return send_from_directory(app.static_folder, filename)
    return "Not found", 404


@app.route('/telegram/telergam_animation.html')
def serve_telegram_orb():
    """Serve the Telegram banner orb so the floating banner's webview can iframe it
    from http://127.0.0.1:5000/ — single source of truth across dev/compiled."""
    rel = f"Auto_Use/{PLATFORM_PKG}/remote_connection/telegram/telergam_animation.html"
    if IS_COMPILED:
        response = serve_embedded_file(rel)
        if response:
            return response
        return "Not found", 404
    tg_dir = get_platform_use_path() / 'remote_connection' / 'telegram'
    return send_from_directory(str(tg_dir), 'telergam_animation.html')


@app.route('/logo.png')
def serve_logo():
    """Serve the Auto Use brand logo (left bar + setup screen).

    logo.png is the ONLY brand image left in Auto_Use/logo/. The old
    auto_use.png / cursor.png were deleted in f9067f1 and logo_rounded.png in
    60183a9, but the routes (and the left bar's <img>) kept pointing at them —
    so the left-bar mark and the setup logo were both silently broken 404s.
    One file, one route, and everything brands from it."""
    if IS_COMPILED:
        response = serve_embedded_file('Auto_Use/logo/logo.png')
        if response:
            return response
        return "Logo not found", 404
    return send_from_directory(str(get_auto_use_path() / 'logo'), 'logo.png')


# =============================================================================
# Flask routes — macOS permission setup wizard
# =============================================================================
@app.route('/setup')
def setup_page():
    """Serve the permission setup wizard (mirrors index())."""
    if IS_COMPILED:
        response = serve_embedded_file('frontend/setup/setup.html')
        if response:
            return response
        return "setup.html not found in embedded resources", 500
    return send_from_directory(app.static_folder, 'setup/setup.html')


@app.route('/api/permissions/status', methods=['GET'])
def api_permissions_status():
    """Live per-permission status. The wizard polls this ~every 1.5s. Stateless:
    re-reads the OS every call so it can never desync from System Settings."""
    if not IS_MAC:
        return jsonify({
            "platform": "other", "is_compiled": IS_COMPILED,
            "permissions": [], "all_granted": True, "needs_relaunch": False,
        })
    states = _permission_states()
    return jsonify({
        "platform": "mac",
        "is_compiled": IS_COMPILED,
        "permissions": states,
        "all_granted": all(p["granted"] for p in states),
        "needs_relaunch": _needs_relaunch(),
    })


@app.route('/api/permissions/request', methods=['POST'])
def api_permissions_request():
    """Drive ONE permission: auto-repair any ghost entry, trigger the prompt /
    open the right Settings pane, re-check. Body: {permission_key|key}."""
    from flask import request
    data = request.get_json(silent=True) or {}
    key = data.get("permission_key") or data.get("key")
    result = request_permission(key)
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route('/api/permissions/reset_all', methods=['POST'])
def api_permissions_reset_all():
    """Escape hatch — reset every TCC grant for our bundle, then relaunch."""
    reset_all_permissions()
    return jsonify({"ok": True})


@app.route('/api/app/relaunch', methods=['POST'])
def api_app_relaunch():
    """Relaunch the app (so cached Screen Recording / Accessibility grants apply)."""
    request_relaunch()
    return jsonify({"ok": True})


@app.route('/api/app/quit', methods=['POST'])
def api_app_quit():
    """Close the window so the user is never trapped in the wizard."""
    try:
        w = get_window()
        if w:
            w.destroy()
    except Exception:
        debug_exception("api_app_quit")
    return jsonify({"ok": True})


# =============================================================================
# Flask route — iOS setup UI (Settings → Connect Device → iPhone)
# Runs the existing ios_connector/setup.py server (the full sign/build/install/
# trust tool with Add Apple Account, team + device select, build console) and
# hands its URL to the frontend, which shows setup.py's index.html in an iframe.
# We do NOT reimplement any of that flow.
# =============================================================================
@app.route('/api/ios/setup-server', methods=['POST'])
def api_ios_setup_server():
    """Ensure the WDA setup server is running; return its URL for the iframe."""
    try:
        from Auto_Use.ios_connector.setup_server import ensure_running
        return jsonify(ensure_running())
    except Exception:
        debug_exception("api_ios_setup_server")
        return jsonify({"ok": False, "ready": False, "error": "could not start setup server"}), 500


# =============================================================================
# Flask routes — iOS paired devices + WDA session toggle
# paired_devices.json (ios_connector) is the source of truth: a device in the
# list IS paired. The Apple logo in the chat box toggles a WDA session on the
# newest paired device — fresh session on, stopped on off. No reinstall, ever.
# =============================================================================
@app.route('/api/ios/paired', methods=['GET'])
def api_ios_paired():
    """List paired devices (for Settings and the activation toggle)."""
    try:
        from Auto_Use.ios_connector.session import paired_devices
        return jsonify({"devices": paired_devices()})
    except Exception:
        debug_exception("api_ios_paired")
        return jsonify({"devices": []})


@app.route('/api/ios/paired/add', methods=['POST'])
def api_ios_paired_add():
    """Record a device as paired (called by Settings when pairing completes)."""
    from flask import request
    try:
        data = request.get_json(silent=True) or {}
        from Auto_Use.ios_connector.session import add_paired
        return jsonify({"ok": True,
                        "devices": add_paired(data.get("udid"), data.get("name"),
                                              data.get("version"))})
    except Exception:
        debug_exception("api_ios_paired_add")
        return jsonify({"ok": False}), 500


@app.route('/api/ios/paired/remove', methods=['POST'])
def api_ios_paired_remove():
    """Delete a device from the paired list (Settings' × button)."""
    from flask import request
    try:
        data = request.get_json(silent=True) or {}
        from Auto_Use.ios_connector.session import remove_paired
        return jsonify({"ok": True, "devices": remove_paired(data.get("udid"))})
    except Exception:
        debug_exception("api_ios_paired_remove")
        return jsonify({"ok": False}), 500


@app.route('/api/ios/activate', methods=['POST'])
def api_ios_activate():
    """Apple logo ON: fresh WDA session on the paired device (no reinstall)."""
    from flask import request
    try:
        data = request.get_json(silent=True) or {}
        from Auto_Use.ios_connector.session import wda_session
        return jsonify(wda_session.activate(data.get("udid")))
    except Exception:
        debug_exception("api_ios_activate")
        return jsonify({"ok": False, "state": "error", "error": "activate failed"}), 500


@app.route('/api/ios/session-status', methods=['GET'])
def api_ios_session_status():
    """Session state — 'connected' means WDA really answers on port 8100."""
    try:
        from Auto_Use.ios_connector.session import wda_session
        return jsonify(wda_session.status())
    except Exception:
        debug_exception("api_ios_session_status")
        return jsonify({"state": "error", "error": "status failed"})


@app.route('/api/ios/deactivate', methods=['POST'])
def api_ios_deactivate():
    """Apple logo OFF: stop the session."""
    try:
        from Auto_Use.ios_connector.session import wda_session
        return jsonify(wda_session.deactivate())
    except Exception:
        debug_exception("api_ios_deactivate")
        return jsonify({"ok": False}), 500


# =============================================================================
# Flask routes — providers / keys / settings / vertex
# =============================================================================
@app.route('/api/providers', methods=['GET'])
def get_providers():
    try:
        return jsonify(get_llm_providers())
    except Exception:
        debug_exception("get_providers API")
        return jsonify([])


@app.route('/api/keys/status', methods=['GET'])
def get_keys_status():
    """Return which providers have keys set (never returns actual keys)"""
    try:
        keys = read_api_keys()
        status = {}
        for provider_id, env_name in PROVIDER_KEY_MAP.items():
            status[provider_id] = bool(keys.get(env_name, ''))
        return jsonify(status)
    except Exception:
        debug_exception("get_keys_status")
        return jsonify({})


@app.route('/api/keys/save', methods=['POST'])
def save_api_key():
    """Save a single provider's API key to file"""
    from flask import request
    try:
        data = request.get_json()
        provider = data.get('provider')
        key_value = data.get('key', '')

        env_name = PROVIDER_KEY_MAP.get(provider)
        if not env_name:
            return jsonify({'error': 'Unknown provider'}), 400

        keys = read_api_keys()
        keys[env_name] = key_value
        write_api_keys(keys)
        return jsonify({'status': 'saved'})
    except Exception:
        debug_exception("save_api_key")
        return jsonify({'error': 'Failed to save'}), 500


@app.route('/api/keys/delete', methods=['POST'])
def delete_api_key():
    """Delete a single provider's API key from file"""
    from flask import request
    try:
        data = request.get_json()
        provider = data.get('provider')

        env_name = PROVIDER_KEY_MAP.get(provider)
        if not env_name:
            return jsonify({'error': 'Unknown provider'}), 400

        keys = read_api_keys()
        keys[env_name] = ''
        write_api_keys(keys)
        return jsonify({'status': 'deleted'})
    except Exception:
        debug_exception("delete_api_key")
        return jsonify({'error': 'Failed to delete'}), 500


@app.route('/api/last-selection', methods=['GET'])
def get_last_selection():
    """Return the user's last-used {provider, model} so the app auto-loads it on launch."""
    try:
        return jsonify(read_settings())
    except Exception:
        debug_exception("get_last_selection")
        return jsonify({})


@app.route('/api/last-selection', methods=['POST'])
def save_last_selection():
    """Persist the user's selected provider and/or model (partial merge)."""
    from flask import request
    try:
        data = request.get_json(silent=True) or {}
        updates = {}
        if 'provider' in data:
            updates['provider'] = data['provider']
        if 'model' in data:
            updates['model'] = data['model']
        if updates:
            write_settings(updates)
        return jsonify({'status': 'saved'})
    except Exception:
        debug_exception("save_last_selection")
        return jsonify({'error': 'Failed to save'}), 500


@app.route('/api/vertex/status', methods=['GET'])
def get_vertex_status():
    """Return current Vertex AI config (project_id and location)"""
    try:
        keys = read_api_keys()
        return jsonify({
            'project_id': keys.get('VERTEX_PROJECT_ID', ''),
            'location': keys.get('VERTEX_LOCATION', '') or 'global'
        })
    except Exception:
        debug_exception("get_vertex_status")
        return jsonify({'project_id': '', 'location': 'global'})


@app.route('/api/vertex/save', methods=['POST'])
def save_vertex_config():
    """Save Vertex AI project ID and location to api_key.txt"""
    from flask import request
    try:
        data = request.get_json()
        project_id = data.get('project_id', '')
        location = data.get('location', 'global')

        keys = read_api_keys()
        keys['VERTEX_PROJECT_ID'] = project_id
        keys['VERTEX_LOCATION'] = location
        write_api_keys(keys)
        return jsonify({'status': 'saved'})
    except Exception:
        debug_exception("save_vertex_config")
        return jsonify({'error': 'Failed to save'}), 500


@app.route('/api/screenshot')
def get_screenshot():
    return jsonify({'image': None})


# =============================================================================
# Frontend push callbacks (agent -> webview)
# =============================================================================
def send_image_to_frontend(base64_image):
    global webview_window
    if webview_window:
        try:
            js_code = f"window.updateAgentImage('{base64_image}')"
            webview_window.evaluate_js(js_code)
        except Exception:
            debug_exception("send_image_to_frontend")


def send_text_to_frontend(text):
    global webview_window
    if webview_window:
        try:
            escaped_text = text.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r')
            js_code = f"window.streamAgentText('{escaped_text}')"
            webview_window.evaluate_js(js_code)
        except Exception:
            debug_exception("send_text_to_frontend")


def send_milestone_to_frontend(text):
    """Push ONE scratchpad line to the live 'tracking progress' stream.

    Rendered through frontend/markdown.py, exactly like the run-end notes, so
    the same entry looks the same while it streams and after it lands on the
    notes stage. render_notes() also strips the leading 'N. ' — the stream
    draws its own circle bullet."""
    global webview_window
    if webview_window:
        try:
            entries = md_render_notes(text)
            if not entries:
                return
            escaped_text = _js_escape(entries[0])
            js_code = f"window.streamMilestone('{escaped_text}')"
            webview_window.evaluate_js(js_code)
        except Exception:
            debug_exception("send_milestone_to_frontend")


def _parse_todo_md(content):
    """Parse the agent's todo.md into {objective, tasks:[{text, done}]}.

    Format written by TaskTrackerService:
        Objective: <goal>
        #1. - [ ] task one
        #2. - [x] task two
    The "#N." prefix is stripped; "- [x]" => done, "- [ ]" => pending.
    """
    objective = ""
    tasks = []
    for raw in (content or "").split('\n'):
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            dot = line.find('. ')
            if dot != -1 and line[1:dot].isdigit():
                line = line[dot + 2:].strip()
        low = line.lower()
        if low.startswith('- [x]'):
            tasks.append({"text": line[5:].strip(), "done": True})
        elif low.startswith('- [ ]'):
            tasks.append({"text": line[5:].strip(), "done": False})
        elif low.startswith('objective:'):
            objective = line.split(':', 1)[1].strip()
        elif not tasks and not objective:
            objective = line
    return {"objective": objective, "tasks": tasks}


def send_todo_to_frontend(payload):
    """Push the main agent's parsed todo list to the top-right card."""
    global webview_window
    if not webview_window:
        return
    try:
        escaped = _js_escape(json.dumps(payload))
        webview_window.evaluate_js(
            f"window.updateTodoList && window.updateTodoList('{escaped}')"
        )
    except Exception:
        debug_exception("send_todo_to_frontend")


def _render_exchange_html(exchanges):
    """Add task_html/done_html (rendered Markdown) to each exchange row, in
    place. The shape showAgentHistory / cliShellHistory consume."""
    for x in (exchanges or []):
        if isinstance(x, dict):
            x['task_html'] = md_render(x.get('task', ''))
            x['done_html'] = md_render(x.get('done_message', ''))
    return exchanges or []


def send_agent_notes(content, session_id=None):
    """Show the agent's scratchpad as 'Agent Notes' on the notes stage (called
    when a run ends — completed or stopped).

    Entries arrive as rendered HTML — frontend/markdown.py turns each note's
    Markdown into real bold/code/links/line breaks and escapes everything else,
    so showAgentNotes can assign it with innerHTML.

    Empty-scratchpad fallback: a run that never wrote a note would leave the
    stage on a bare "No notes". Instead we push the chat's full request/outcome
    transcript — the SAME view reopening the chat gives, and it already
    includes the run that just ended, because save_run() appends this run's
    exchange before we get here. Platform-agnostic: every agent (macOS /
    Windows / iOS) ends through this one call."""
    global webview_window
    if not webview_window:
        return
    try:
        entries = md_render_notes(content)
        if entries:
            escaped = _js_escape(json.dumps(entries))
            webview_window.evaluate_js(
                f"window.showAgentNotes && window.showAgentNotes('{escaped}')"
            )
            return

        rows = []
        if session_id:
            try:
                data = conversation.get_session(session_id) or {}
                rows = _render_exchange_html(data.get('exchanges'))
            except Exception:
                debug_exception("send_agent_notes exchanges")
        if rows:
            escaped = _js_escape(json.dumps(rows))
            webview_window.evaluate_js(
                f"window.showAgentHistory && window.showAgentHistory('{escaped}')"
            )
            return

        # Nothing written AND no transcript yet — still swap the stage on so it
        # replaces the screenshot; the empty state is correct here.
        webview_window.evaluate_js(
            "window.showAgentNotes && window.showAgentNotes('[]')"
        )
    except Exception:
        debug_exception("send_agent_notes")


def send_web_status_to_frontend(status):
    global webview_window
    if webview_window:
        try:
            if status == "start":
                webview_window.evaluate_js("window.webSearchStart()")
            elif status == "end":
                webview_window.evaluate_js("window.webSearchEnd()")
        except Exception:
            debug_exception("send_web_status_to_frontend")


def _js_escape(text):
    """Escape a string for safe interpolation into a single-quoted JS literal."""
    if text is None:
        return ""
    return (
        str(text)
        .replace('\\', '\\\\')
        .replace("'", "\\'")
        .replace('\n', '\\n')
        .replace('\r', '\\r')
    )


def send_cli_event_to_frontend(event_type, *args):
    """Forward CLI agent streaming events to the frontend (window.cli*())."""
    global webview_window
    if not webview_window:
        return
    try:
        if event_type == "await_start":
            reason = _js_escape(args[0] if args else "")
            webview_window.evaluate_js(f"window.cliAwaitStart && window.cliAwaitStart('{reason}')")
        elif event_type == "await_end":
            webview_window.evaluate_js("window.cliAwaitEnd && window.cliAwaitEnd()")
        elif event_type == "task_start":
            task_id = _js_escape(args[0])
            desc = _js_escape(args[1] if len(args) > 1 else "")
            webview_window.evaluate_js(
                f"window.cliTaskStart && window.cliTaskStart('{task_id}', '{desc}')"
            )
        elif event_type == "task_line":
            task_id = _js_escape(args[0])
            line = _js_escape(args[1] if len(args) > 1 else "")
            stream = _js_escape(args[2] if len(args) > 2 else "out")
            webview_window.evaluate_js(
                f"window.cliTaskLine && window.cliTaskLine('{task_id}', '{line}', '{stream}')"
            )
        elif event_type == "task_end":
            task_id = _js_escape(args[0])
            status = _js_escape(args[1] if len(args) > 1 else "complete")
            summary = _js_escape(args[2] if len(args) > 2 else "")
            webview_window.evaluate_js(
                f"window.cliTaskEnd && window.cliTaskEnd('{task_id}', '{status}', '{summary}')"
            )
        elif event_type == "todo_update":
            task_id = _js_escape(args[0] if len(args) > 0 else "")
            todo_payload = _js_escape(json.dumps(_parse_todo_md(args[1] if len(args) > 1 else "")))
            webview_window.evaluate_js(
                f"window.cliTaskTodo && window.cliTaskTodo('{task_id}', '{todo_payload}')"
            )
        elif event_type == "minion_start":
            parent_task_id = _js_escape(args[0] if len(args) > 0 else "")
            task_id = _js_escape(args[1] if len(args) > 1 else "")
            query = _js_escape(args[2] if len(args) > 2 else "")
            webview_window.evaluate_js(
                f"window.cliMinionStart && window.cliMinionStart('{parent_task_id}', '{task_id}', '{query}')"
            )
        elif event_type == "minion_end":
            task_id = _js_escape(args[0] if len(args) > 0 else "")
            status = _js_escape(args[1] if len(args) > 1 else "complete")
            summary = _js_escape(args[2] if len(args) > 2 else "")
            webview_window.evaluate_js(
                f"window.cliMinionEnd && window.cliMinionEnd('{task_id}', '{status}', '{summary}')"
            )
        elif event_type == "minion_line":
            task_id = _js_escape(args[0] if len(args) > 0 else "")
            line = _js_escape(args[1] if len(args) > 1 else "")
            stream = _js_escape(args[2] if len(args) > 2 else "out")
            webview_window.evaluate_js(
                f"window.cliMinionLine && window.cliMinionLine('{task_id}', '{line}', '{stream}')"
            )
        elif event_type == "pill_web_loading_start":
            task_id = _js_escape(args[0] if len(args) > 0 else "")
            webview_window.evaluate_js(
                f"window.cliPillWebLoadingStart && window.cliPillWebLoadingStart('{task_id}')"
            )
        elif event_type == "pill_web_loading_end":
            task_id = _js_escape(args[0] if len(args) > 0 else "")
            webview_window.evaluate_js(
                f"window.cliPillWebLoadingEnd && window.cliPillWebLoadingEnd('{task_id}')"
            )
    except Exception:
        debug_exception(f"send_cli_event_to_frontend({event_type})")


def send_shell_status_to_frontend(event, data=None, label=None):
    """Send shell / AppleScript execution status to frontend for terminal animation."""
    global webview_window
    if webview_window:
        try:
            if event == "start":
                escaped_cmd = (data or "").replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r')
                escaped_label = (label or "Shell").replace("'", "\\'")
                webview_window.evaluate_js(f"window.shellStart('{escaped_cmd}', '{escaped_label}')")
            elif event == "result":
                status = (data or {}).get("status", "success")
                output = (data or {}).get("output", "")
                escaped_output = output.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r')
                webview_window.evaluate_js(f"window.shellResult('{status}', '{escaped_output}')")
            elif event == "end":
                webview_window.evaluate_js("window.shellEnd()")
        except Exception:
            debug_exception("send_shell_status_to_frontend")


def send_flow_to_frontend(event, payload=None):
    """Drive the bottom 'Tool response' tool-flow chain (window.toolFlow.onFlow)."""
    global webview_window
    if webview_window:
        try:
            ev = str(event).replace("'", "\\'")
            if payload is None:
                webview_window.evaluate_js(f"window.toolFlow && window.toolFlow.onFlow('{ev}')")
            else:
                pj = json.dumps(payload, ensure_ascii=False)
                pj = pj.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r')
                webview_window.evaluate_js(f"window.toolFlow && window.toolFlow.onFlow('{ev}', '{pj}')")
        except Exception:
            debug_exception("send_flow_to_frontend")


# =============================================================================
# Agent run
# =============================================================================
@app.route('/api/start-agent', methods=['POST'])
def start_agent():
    """Start the agent with the provided provider, model, and task"""
    from flask import request
    global active_agent_stop_event, active_agent_session_id

    try:
        data = request.get_json()
        provider = data.get('provider')
        model = data.get('model')
        task = data.get('task')
        req_session_id = data.get('session_id')   # None / "new" / existing chat id

        if not all([provider, model, task]):
            return jsonify({'error': 'Missing provider, model, or task'}), 400

        # ── Agent mode: mobile+ios runs the iOS agent; anything else = host desktop ──
        agent_mode = (data.get('mode') or 'computer').strip().lower()
        device_os = (data.get('os') or '').strip().lower()
        speed = (data.get('speed') or 'quality').strip().lower()   # ⚡/✨ toggle
        run_pkg = 'ios' if (agent_mode == 'mobile' and device_os == 'ios') else PLATFORM_PKG
        # The run's package folder — the todo/milestone watchers and resets below
        # must follow the agent that actually runs, not the host desktop package.
        run_use_path = get_platform_use_path(run_pkg)

        api_key = get_provider_api_key(provider)

        # ── Resolve the CHAT session via the conversation service ────────────
        # A freshly minted chat is stamped with its mode right away, so the
        # per-chat lock is correct even if this run never reaches save_run.
        chat_session_id, prior_history = conversation.start_or_resume(
            req_session_id, task,
            agent_mode=('mobile' if run_pkg == 'ios' else 'computer'))

        # ── Memory bar: current memory fullness for the MAIN agent, shown against
        # the fixed 300k budget (MemoryTracker.MEMORY_CAP — headroom for the future
        # memory-compression system). Seed from the chat's last saved context size
        # so a reopened chat restores where memory was.
        _sess = conversation.get_session(chat_session_id) or {}

        # Per-chat mode lock, shell axis FIRST: a chat owned by Shell use never
        # runs the main agent. run_pkg alone can't catch this — shell chats
        # share the desktop PLATFORM_PKG — hence the dedicated agent_mode marker.
        if (_sess.get("agent_mode") or "") == "shell":
            return jsonify({'error': 'This chat is locked to Shell use — open a new chat to switch mode'}), 400

        # Per-chat mode lock: a chat that already ran in one mode only accepts
        # that mode (the UI greys the other option; this is the backstop).
        locked_pkg = _sess.get("run_pkg") or ""
        if locked_pkg and locked_pkg != run_pkg:
            locked_label = 'Mobile use' if locked_pkg == 'ios' else 'Computer use'
            return jsonify({'error': f'This chat is locked to {locked_label} — open a new chat to switch mode'}), 400

        # Backstop for legacy UNTAGGED sessions (saved before run_pkg existed —
        # host-desktop runs by construction): never replay their memory into a
        # different agent; start fresh on the same chat thread instead.
        saved_pkg = locked_pkg or (PLATFORM_PKG if _sess else "")
        if prior_history is not None and saved_pkg and saved_pkg != run_pkg:
            prior_history = None

        token_tracker = MemoryTracker(initial_context=_sess.get("context_tokens", 0))

        # Memory-bar push — shared factory (Shell use builds the identical
        # sender around its own tracker).
        send_token_to_frontend = _make_token_sender(token_tracker)

        active_agent_stop_event = threading.Event()
        active_agent_session_id = str(time.time())   # per-RUN guard
        current_session_id = active_agent_session_id

        def run_agent():
            stop_event = active_agent_stop_event

            # Clear stale todo/scratchpad and blank the top-right card immediately;
            # the watchers below repopulate it as soon as the agent writes its plan.
            _reset_todo_file(run_pkg)
            _reset_scratchpad_file(run_pkg)
            send_todo_to_frontend({"objective": "", "tasks": []})

            def run_is_current():
                # False once a newer run started OR the user clicked New chat
                # (/api/new-chat nulls active_agent_session_id) — a stale run's
                # watcher/final pushes must not repaint the freshly reset UI.
                return current_session_id == active_agent_session_id

            def only_if_current(push):
                # EVERY push this run makes to the UI goes through here. The
                # instant Stop (or New chat) is pressed the run id is cleared,
                # but the run itself may be parked in an LLM call and keep
                # emitting for seconds afterwards — screenshots, agent text,
                # tool-chain steps, the memory bar. Once retired, none of it
                # reaches the screen: the user sees nothing more from a session
                # they already ended, and nothing lands on the run they start
                # next.
                def guarded(*args, **kwargs):
                    if run_is_current():
                        push(*args, **kwargs)
                return guarded

            def cli_push(event_type, *args):
                # CLI events need one exception to that rule: the teardown
                # events CLOSE the coder card and unpin the stage, so they must
                # land even for a retired run - otherwise the terminal is stuck
                # mid-run forever. Keyed by task_id, so a stale one is a no-op.
                if event_type in ("task_end", "await_end", "minion_end") or run_is_current():
                    send_cli_event_to_frontend(event_type, *args)

            def monitor_milestones():
                milestone_path = run_use_path / "scratchpad" / "milestone" / "milestone.md"
                last_pos = 0

                while not milestone_path.exists() and not stop_event.is_set():
                    time.sleep(0.5)

                while not stop_event.is_set() and run_is_current():
                    if milestone_path.exists():
                        try:
                            with open(milestone_path, 'r', encoding='utf-8') as f:
                                f.seek(last_pos)
                                new_content = f.read()
                                if new_content:
                                    last_pos = f.tell()
                                    lines = new_content.strip().split('\n')
                                    for line in lines:
                                        if line.strip():
                                            send_milestone_to_frontend(line.strip())
                        except Exception:
                            debug_exception("monitor_milestones")
                    time.sleep(1)

                # Final read to stream any remaining milestones after agent stopped
                if milestone_path.exists() and run_is_current():
                    try:
                        with open(milestone_path, 'r', encoding='utf-8') as f:
                            f.seek(last_pos)
                            new_content = f.read()
                            if new_content:
                                lines = new_content.strip().split('\n')
                                for line in lines:
                                    if line.strip():
                                        send_milestone_to_frontend(line.strip())
                    except Exception:
                        debug_exception("monitor_milestones final read")

            def monitor_todo():
                # todo.md is REWRITTEN on every update (not appended), so re-read
                # the whole file and push it whenever the content changes.
                todo_path = run_use_path / "scratchpad" / "todo" / "todo.md"
                last_content = None
                while not stop_event.is_set() and run_is_current():
                    try:
                        if todo_path.exists():
                            content = todo_path.read_text(encoding='utf-8')
                            if content != last_content:
                                last_content = content
                                send_todo_to_frontend(_parse_todo_md(content))
                    except Exception:
                        debug_exception("monitor_todo")
                    time.sleep(0.3)
                # Final read so the terminal state (e.g. all tasks complete) shows.
                try:
                    if todo_path.exists() and run_is_current():
                        content = todo_path.read_text(encoding='utf-8')
                        if content != last_content:
                            send_todo_to_frontend(_parse_todo_md(content))
                except Exception:
                    debug_exception("monitor_todo final read")

            # Outcome reported back to the web UI.
            run_outcome = {"status": "success", "message": ""}
            agent = None  # bound below; kept defined so the finally save is safe
            try:
                AgentService = importlib.import_module(
                    f"Auto_Use.{run_pkg}.agent.main_driver.service"
                ).AgentService

                agent = AgentService(
                    provider=provider,
                    model=model,
                    frontend_callback=only_if_current(send_image_to_frontend),
                    text_callback=only_if_current(send_text_to_frontend),
                    web_callback=only_if_current(send_web_status_to_frontend),
                    shell_callback=only_if_current(send_shell_status_to_frontend),
                    cli_callback=cli_push,
                    tool_callback=only_if_current(send_flow_to_frontend),
                    token_callback=only_if_current(send_token_to_frontend),
                    api_key=api_key,
                    stop_event=stop_event,
                    prior_history=prior_history,   # None for a fresh chat
                    speed=speed,                   # ⚡ fast / ✨ quality (all platforms)
                )

                monitor_thread = threading.Thread(target=monitor_milestones)
                monitor_thread.daemon = True
                monitor_thread.start()

                todo_thread = threading.Thread(target=monitor_todo)
                todo_thread.daemon = True
                todo_thread.start()

                run_outcome = agent.process_request(task)
                if not isinstance(run_outcome, dict):
                    run_outcome = {"status": "success", "message": ""}

            except Exception as agent_exc:
                debug_exception("run_agent")
                run_outcome = {"status": "error", "message": str(agent_exc)}
            finally:
                stop_event.set()

                # ── Persist this run via the conversation service ───────────
                # Runs for BOTH completion and stop, and BEFORE the window guard,
                # so memory is saved even if the window closed or a newer run
                # superseded this one. agent may be None if construction failed.
                try:
                    conversation.save_run(
                        chat_session_id,
                        getattr(agent, "assistant_messages", None),
                        getattr(agent, "tool_responses", None),
                        run_outcome.get("status", "success"),
                        run_outcome.get("message", "") or "",
                        task,
                        getattr(agent, "last_messages", None),  # exact payload -> true memory log
                        context_tokens=token_tracker.current,   # latest context size for the bar
                        context_cap=token_tracker.cap,          # fixed 300k memory budget
                        run_pkg=run_pkg,                        # which agent produced this memory
                        agent_mode=('mobile' if run_pkg == 'ios' else 'computer'),  # per-chat lock
                    )
                except Exception:
                    debug_exception("persist chat session on finish")

                if webview_window and current_session_id == active_agent_session_id:
                    try:
                        status = run_outcome.get("status", "success")
                        message = run_outcome.get("message", "") or ""
                        if status == "success":
                            webview_window.evaluate_js("window.agentComplete()")
                        else:
                            # Surface the real reason instead of a silent "complete".
                            prefix = "❌ Error: " if status == "error" else "⚠️ Stopped without completing: "
                            reason = prefix + message if message else prefix.strip()
                            webview_window.evaluate_js(
                                f"window.agentError('{_js_escape(reason)}')"
                            )
                    except Exception:
                        debug_exception("signaling agent completion")

                    # Replace the screenshot with the "Agent Notes" view now the run
                    # has ended (completed OR stopped) — ALWAYS, even if empty.
                    try:
                        notes_path = run_use_path / "scratchpad" / "milestone" / "milestone.md"
                        content = notes_path.read_text(encoding='utf-8') if notes_path.exists() else ""
                        # session id: lets an empty scratchpad fall back to this
                        # chat's done-message transcript instead of "No notes".
                        send_agent_notes(content, chat_session_id)
                    except Exception:
                        debug_exception("send agent notes on finish")

        thread = threading.Thread(target=run_agent)
        thread.daemon = True
        thread.start()

        # Return the chat session id so a brand-new chat's id reaches the frontend.
        return jsonify({'status': 'started', 'session_id': chat_session_id})

    except Exception as e:
        debug_exception("start_agent API")
        return jsonify({'error': str(e)}), 500


def _make_token_sender(token_tracker):
    """Build the per-run memory-bar push around one MemoryTracker — the closure
    moved verbatim from /api/start-agent so Shell use drives the EXACT same
    updateMemoryBar pipe with its own tracker."""
    def send_token_to_frontend(usage):
        # Cosmetic gauge ONLY — updates the visual memory bar each LLM call.
        # It never gates the agent: the run does not stop when the bar fills
        # (the agent keeps working past 300k / 1M); the bar just reads full.
        global webview_window
        if not webview_window:
            return
        try:
            # Memory-compression indicator events ride the same pipe as the
            # token usage: {"memory_compression": "start"|"end"} blinks the
            # Memory logo red while the background handoff compression runs.
            mc = (usage or {}).get("memory_compression")
            if mc:
                fn = "memoryCompressionStart" if mc == "start" else "memoryCompressionEnd"
                webview_window.evaluate_js(f"window.{fn} && window.{fn}()")
                return
            p = token_tracker.record(usage)
            webview_window.evaluate_js(
                f"window.updateMemoryBar && window.updateMemoryBar({p['used']}, {p['cap']})"
            )
        except Exception:
            debug_exception("send_token_to_frontend")
    return send_token_to_frontend


# ── Shell-use conversation channel ───────────────────────────────────────────
# The coder runs as a SUBPROCESS (argv-only), so a shell chat's conversation
# crosses the process boundary through ONE JSON file: the seed is written here
# before the spawn, the coder rewrites the file atomically after every step,
# and run_shell reads it back for conversation.save_run. It lives OUTSIDE any
# scratchpad/ folder — those are wiped on app start AND on every main-agent
# construction, and this must survive both for the life of the run.
def _shell_history_dir() -> Path:
    return app_data_dir() / "cli_shell_history"


def _new_shell_history_file() -> Path:
    d = _shell_history_dir()
    d.mkdir(parents=True, exist_ok=True)
    # uuid suffix: a double-send landing in the same millisecond must never
    # share (and cross-clobber) one channel file.
    return d / f"history_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.json"


def _write_shell_seed(path: Path, task: str, prior_history, manual_block: str = "") -> dict:
    """Write the coder's seed (the chat's saved history, or empty lists for a
    fresh chat). Returns the seed dict — run_shell's fallback if the file can't
    be read back (e.g. the run died before its first snapshot)."""
    seed = {
        "task": (prior_history or {}).get("task") or task,
        "assistant_messages": list((prior_history or {}).get("assistant_messages") or []),
        "tool_responses": list((prior_history or {}).get("tool_responses") or []),
        "last_messages": None,
    }
    if manual_block:
        # consumed once by the coder's _load_seed; its snapshots never echo the
        # key back, so a record can't be delivered twice
        seed["manual_mode"] = manual_block
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(seed, fh, ensure_ascii=False)
    except Exception:
        debug_exception("write shell history seed")
    return seed


def _read_shell_history(path: Path):
    """The coder's latest snapshot, or None. The coder rewrites via tmp +
    os.replace, so a torn read can't happen — None means no snapshot landed."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# PID of the coder subprocess driving the LIVE Shell-use run. /api/stop-agent
# kills this tree the instant Stop is pressed - it has to happen while the coder
# is still alive, because a process tree is resolved THROUGH the parent.
_active_shell_pids = []


def _live_cli_pids(controller) -> list:
    """PIDs of the coder subprocesses the controller is tracking right now."""
    pids = []
    try:
        for entry in list(getattr(controller, "_cli_tasks", None) or []):
            proc = entry.get("subprocess")
            if proc is not None and proc.poll() is None:
                pids.append(proc.pid)
    except Exception:
        debug_exception("read cli pids")
    return pids


def _kill_process_trees(pids) -> None:
    """Kill each pid AND everything it spawned.

    The coder runs minions as its OWN child processes, so terminating the coder
    leaves them orphaned and still working. taskkill /T walks the tree on
    Windows; killpg does the same on POSIX (the coder is started in its own
    process group - mac/controller/view.py passes start_new_session=True).
    Best-effort and idempotent: on a normal finish the tree is already gone and
    every call below is a no-op.

    NEVER signals our own process group. That is not paranoia: the coder used to
    be spawned WITHOUT start_new_session, so it inherited our group, getpgid()
    resolved to the app's own pgid, and pressing Stop SIGKILLed the entire
    application instead of the agent. The spawn is fixed, but a SIGKILL aimed at
    ourselves is unrecoverable and untraceable - so it is refused here too, and
    the caller is left with a dead-obvious log line rather than a dead app.
    """
    own_pgid = None if IS_WINDOWS else os.getpgrp()
    own_pid = os.getpid()
    for pid in pids or []:
        try:
            if not pid or int(pid) <= 0 or int(pid) == own_pid:
                debug_log(f"refusing to kill pid {pid!r} - not a child", "ERROR")
                continue
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=10)
            else:
                pgid = os.getpgid(pid)
                if pgid == own_pgid:
                    # Shares OUR group -> killpg would take the app with it. Fall
                    # back to killing just this process; any children it spawned
                    # are in the same group and cannot be reached safely.
                    debug_log(f"pid {pid} shares our process group ({pgid}) - "
                              f"killing it alone, NOT the group", "ERROR")
                    os.kill(pid, signal.SIGKILL)
                    continue
                os.killpg(pgid, signal.SIGKILL)
        except Exception:
            pass


@atexit.register
def _kill_shell_trees_on_exit() -> None:
    """Quitting the app mid-run must not leave the coder running.

    The coder is spawned into its OWN session so Stop can killpg it without
    taking us down - which also means it no longer dies incidentally with us
    (a terminal-launched app used to SIGHUP its whole group on quit). Nothing
    else covers this: run_shell_task's finally only fires if the run unwinds,
    and app.py's closing hook is Windows-only and just shuts banners.

    Covers a clean quit and Ctrl+C; a SIGKILL of the app skips atexit entirely,
    same as any other cleanup.
    """
    try:
        pids = list(_active_shell_pids)
        if pids:
            debug_log(f"app exiting - killing {len(pids)} live coder tree(s)")
            _kill_process_trees(pids)
            _active_shell_pids.clear()
    except Exception:
        pass
    # Quitting the app must also release the iPhone: nothing else covers the
    # window being closed mid-session, and an orphaned WDA session leaves the
    # "Automation Running" overlay on the phone until testmanagerd times it
    # out. Deactivate is a no-op when no session is active.
    _release_iphone()


def _shell_outcome(result) -> dict:
    """Map run_shell_task's cli_await result onto the {status, message} shape
    run_agent persists — so shell exchanges read identically across modes."""
    if not isinstance(result, dict) or result.get("status") == "stopped":
        return {"status": "incomplete", "message": "Stopped by user"}
    if result.get("status") == "error":
        # route_action's catch-all: surface the real reason, don't swallow it
        return {"status": "error", "message": str(result.get("message", "") or "Shell run failed")}
    completed = result.get("completed") or []
    if not completed:
        return {"status": "incomplete", "message": "Shell run ended without a result"}
    last = completed[-1]
    status = "success" if last.get("status") == "complete" else "incomplete"
    return {"status": status, "message": last.get("summary", "") or ""}


def run_shell_task(task, provider, model, api_key, run_pkg,
                   cli_callback=None, stop_event=None,
                   history_file=None, request_no=None):
    """Shell use — a DIRECT line from the composer to the coder (CLI) agent.

    Runs ONE task on the coder and BLOCKS until it finishes; call from a
    background thread. No main agent, no chat session, no conversation memory,
    no todo/milestone watchers — the CLI stage (frontend/cli/) is the whole UI,
    showing the same terminal / tool-chain / tracking-progress card the coder
    shows when the MAIN agent dispatches one, because it is the same event
    stream.

    That reuse is the point. Rather than re-implement the subprocess spawn, the
    stdout reader threads, the `__MINION_UI_EVENT__` bridge and the cli_*
    lifecycle, this drives the platform ControllerView's existing `cli_agent`
    (dispatch) + `cli_await` (block) actions — the two the main agent itself
    uses. macOS and Windows go through the same two calls; only run_pkg differs,
    and the coder subprocess command is already branched inside ControllerView.

    Args:
        task: the user's typed request, handed to the coder verbatim.
        provider/model/api_key: same LLM selection the main agent would use.
        run_pkg: 'mac' | 'windows' (PLATFORM_PKG).
        cli_callback: send_cli_event_to_frontend — drives the CLI stage.
        stop_event: threading.Event shared with /api/stop-agent, so the
            composer's stop orb kills the coder subprocess mid-run.

    Returns:
        The cli_await result dict: {"status", "completed": [...]}, or
        {"status": "stopped"} if the user stopped the run.
    """
    ControllerView = importlib.import_module(
        f"Auto_Use.{run_pkg}.controller.view"
    ).ControllerView

    # cli_mode=True with NO session_id: keeps this run off the MAIN agent's
    # scratchpad/todo files (it writes to the shared cli_milestone/ folder
    # instead) without minting a per-run sandbox folder on the Desktop.
    # cli_report_usage: the coder emits per-LLM-call token usage markers, so
    # the memory bar tracks a Shell-use run the way it tracks Computer use.
    # cli_history_file / cli_request_no: the chat's conversation channel — the
    # coder seeds from the file, snapshots back into it, and tags its task
    # <user_request=N>.
    controller = ControllerView(
        provider=provider,
        model=model,
        cli_mode=True,
        cli_callback=cli_callback,
        api_key=api_key,
        stop_event=stop_event,
        cli_report_usage=True,
        cli_history_file=history_file,
        cli_request_no=request_no,
    )

    logging.getLogger(__name__).info(f"Shell use — dispatching coder agent for: {task[:120]}")
    coder_pids = []   # bound before the try: the finally below always reads it
    try:
        # 1. Dispatch: spawns the coder subprocess, emits task_start and starts
        #    streaming its stdout/stderr into the card.
        controller.route_action([{"type": "cli_agent", "value": task}])
        # Remember the coder's pid NOW, while the controller still tracks it:
        # stop_cli_agent() clears its task list, so after the await there is
        # nothing left to read it from.
        coder_pids = _live_cli_pids(controller)
        # Publish it so /api/stop-agent can kill the tree IMMEDIATELY, while the
        # coder is still alive. cli_await terminates the coder itself the moment
        # it sees the stop event, and once the parent is dead its children can no
        # longer be resolved from its pid - they would survive as orphans.
        _active_shell_pids[:] = coder_pids
        # 2. Await: emits await_start (stage slides up), blocks until the coder
        #    writes its result file, then emits task_end + await_end.
        return controller.route_action([{"type": "cli_await", "value": "Shell use"}])
    finally:
        # Tree FIRST, while the coder is still alive: stop_cli_agent() terminates
        # only the coder process, and the minions it spawned are its own
        # children - neither TerminateProcess nor SIGTERM walks a tree, so
        # killing the parent first would strand them (still running, still
        # calling the API). Harmless no-op on a normal finish.
        try:
            _kill_process_trees(coder_pids)
        except Exception:
            debug_exception("shell run tree kill")
        _active_shell_pids.clear()
        # Safety net for an early exception — a no-op on the normal path, where
        # cli_await already drained the task list.
        try:
            controller.stop_cli_agent()
        except Exception:
            debug_exception("shell run cleanup")


@app.route('/api/start-shell', methods=['POST'])
def start_shell():
    """Agent mode → Shell use: send the task STRAIGHT to the coder agent.

    Slim next to /api/start-agent — no main agent, no todo/milestone watchers,
    no Agent Notes overlay (the CLI stage is the whole UI) — but a shell run IS
    a chat: the session is resolved/minted via the conversation service, the
    coder is seeded with the chat's saved history (--history file channel) and
    tags its task <user_request=N>, its token usage drives the memory bar, and
    every ending is persisted with save_run. The chat locks to Shell use the
    same way Computer/Mobile chats lock to theirs.

    Shares active_agent_stop_event / active_agent_session_id with the normal
    run so the stop orb (/api/stop-agent) and New chat work unchanged.
    """
    from flask import request
    global active_agent_stop_event, active_agent_session_id

    try:
        data = request.get_json() or {}
        provider = data.get('provider')
        model = data.get('model')
        task = data.get('task')
        req_session_id = data.get('session_id')   # None / "new" / existing chat id

        if not all([provider, model, task]):
            return jsonify({'error': 'Missing provider, model, or task'}), 400

        api_key = get_provider_api_key(provider)

        # ── Resolve the CHAT session (same service as /api/start-agent). A
        # freshly minted chat is stamped agent_mode='shell' at mint time, so the
        # per-chat lock is right even if this run never reaches save_run.
        chat_session_id, prior_history = conversation.start_or_resume(
            req_session_id, task, agent_mode='shell')
        _sess = conversation.get_session(chat_session_id) or {}

        # ── Per-chat mode lock backstop (mirror of start_agent's) ───────────
        locked_pkg = _sess.get("run_pkg") or ""
        locked_mode = _sess.get("agent_mode") or ""
        if not locked_mode and (locked_pkg or prior_history is not None
                                or (_sess.get("exchanges") or [])):
            # Legacy chats (saved before agent_mode existed) are computer/mobile
            # by construction — shell chats didn't exist yet. The exchanges
            # signal also catches a legacy chat whose conversation.json was
            # truncated by a crash (prior_history None, pkg untagged): its
            # transcript still proves it ran, so it must not convert to shell.
            locked_mode = 'mobile' if locked_pkg == 'ios' else 'computer'
        if locked_mode and locked_mode != 'shell':
            label = 'Mobile use' if locked_mode == 'mobile' else 'Computer use'
            return jsonify({'error': f'This chat is locked to {label} — open a new chat to switch mode'}), 400
        # Cross-OS: a shell chat's memory never seeds the other platform's coder.
        if locked_pkg and locked_pkg != PLATFORM_PKG:
            return jsonify({'error': 'This chat was created on a different OS — open a new chat'}), 400

        # Which request of the conversation this is — numbers the coder's
        # <user_request=N> tag. Exchanges append one entry per finished run.
        request_no = len(_sess.get("exchanges") or []) + 1

        # ── Memory bar: same tracker + push pipe as Computer use, seeded from
        # the chat's last saved context size so a resumed chat restores its bar.
        token_tracker = MemoryTracker(initial_context=_sess.get("context_tokens", 0))
        send_token_to_frontend = _make_token_sender(token_tracker)

        # Teardown events are ALWAYS delivered, even for a retired run: they are
        # what CLOSES this run's card and unpins the stage. Swallowing them
        # (Stop retires the run, then stop_cli_agent immediately emits task_end)
        # freezes the terminal mid-run forever. They are keyed by task_id, so a
        # stale one is a no-op for whatever run is on screen now.
        _CLI_TEARDOWN_EVENTS = ("task_end", "await_end", "minion_end")

        def shell_cli_callback(event_type, *args):
            # Everything else follows the main agent's rule: once the run is
            # retired (Stop / New chat clears the id) its output does not reach
            # the screen. The coder keeps streaming until it actually dies, so
            # without this its lines would land on whatever the user started
            # next. Read at call time - the thread only runs after the id below
            # is assigned.
            if (event_type not in _CLI_TEARDOWN_EVENTS
                    and current_session_id != active_agent_session_id):
                return
            if event_type == "token_usage":
                send_token_to_frontend(args[0] if args and isinstance(args[0], dict) else {})
                return
            send_cli_event_to_frontend(event_type, *args)

        # ── Conversation file channel to/from the coder subprocess ──────────
        history_file = _new_shell_history_file()
        # Hand-typed terminal commands since the last run ride the seed as a
        # <manual_mode> block — the coder replays it as conversation, so the
        # agent resumes knowing what the user tried by hand.
        manual_records = _drain_manual_records(chat_session_id, request_no == 1)
        manual_block = _render_manual_block(manual_records)
        seed = _write_shell_seed(history_file, task, prior_history, manual_block)

        active_agent_stop_event = threading.Event()
        active_agent_session_id = str(time.time())   # per-RUN guard
        current_session_id = active_agent_session_id
        stop_event = active_agent_stop_event

        def run_shell():
            outcome = {"status": "incomplete", "message": ""}
            try:
                result = run_shell_task(
                    task=task,
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    run_pkg=PLATFORM_PKG,
                    cli_callback=shell_cli_callback,
                    stop_event=stop_event,
                    history_file=history_file,
                    request_no=request_no,
                )
                outcome = _shell_outcome(result)
            except Exception as shell_exc:
                debug_exception("run_shell")
                outcome = {"status": "error", "message": str(shell_exc)}
            finally:
                stop_event.set()

                # ── Persist this run via the conversation service — BEFORE the
                # window guard, same as run_agent, so memory survives a mid-run
                # New chat / closed window. The coder snapshots per step, so a
                # hard-killed run still reads back to its last finished step.
                hist = _read_shell_history(history_file) or seed

                # <manual_mode> delivery ack: the coder stamps every snapshot
                # with manual_delivered once its seed carried the block — so a
                # snapshot proves the model's transcript contained it (length /
                # substring heuristics break under mid-run compression). No
                # snapshot (died before step 1) or an unreadable seed leaves
                # the stamp absent — put the records back so the chat's next
                # run delivers them instead of dropping them.
                if manual_records and not hist.get("manual_delivered"):
                    _requeue_manual_records(manual_records)

                try:
                    conversation.save_run(
                        chat_session_id,
                        hist.get("assistant_messages"),
                        hist.get("tool_responses"),
                        outcome.get("status", "incomplete"),
                        outcome.get("message", "") or "",
                        task,
                        hist.get("last_messages"),      # exact payload -> true memory log
                        context_tokens=token_tracker.current,
                        context_cap=token_tracker.cap,
                        run_pkg=PLATFORM_PKG,
                        agent_mode="shell",             # keeps the chat locked to Shell use
                        request_no=request_no,          # zero-step runs still record their request
                    )
                except Exception:
                    debug_exception("persist shell chat on finish")
                try:
                    history_file.unlink(missing_ok=True)
                except Exception:
                    pass

                # Same completion signal as a normal run: rolls the composer's
                # orb, placeholder and input box back to idle.
                if webview_window and current_session_id == active_agent_session_id:
                    try:
                        status = outcome.get("status", "incomplete")
                        if status == "success":
                            webview_window.evaluate_js("window.agentComplete()")
                        else:
                            prefix = "❌ Error: " if status == "error" else "⚠️ Stopped without completing: "
                            message = outcome.get("message", "") or ""
                            reason = prefix + message if message else prefix.strip()
                            # agentError writes to the milestone stream, which lives in a
                            # zone body.cli-stage hides — so ALSO put it on the terminal,
                            # the only surface visible in Shell use. The chat id rides
                            # along: if the user has since opened a DIFFERENT chat, the
                            # frontend drops the note instead of writing this run's
                            # outcome onto that chat's terminal.
                            webview_window.evaluate_js(
                                f"window.cliShellNote && window.cliShellNote('{_js_escape(reason)}', '{_js_escape(chat_session_id)}')"
                            )
                            webview_window.evaluate_js(
                                f"window.agentError('{_js_escape(reason)}')"
                            )
                    except Exception:
                        debug_exception("signaling shell run completion")

        thread = threading.Thread(target=run_shell)
        thread.daemon = True
        thread.start()

        # Return the chat session id so a brand-new chat's id reaches the frontend.
        return jsonify({'status': 'started', 'session_id': chat_session_id})

    except Exception as e:
        debug_exception("start_shell API")
        return jsonify({'error': str(e)}), 500


# One Sandbox for the hand-typed terminal, kept for the life of the app so its
# working directory survives between commands (that's what makes `cd` stick).
_manual_shell = None


def _get_manual_shell():
    global _manual_shell
    if _manual_shell is None:
        Sandbox = importlib.import_module(f"Auto_Use.{PLATFORM_PKG}.sandbox").Sandbox
        _manual_shell = Sandbox()
    return _manual_shell


# ── Manual-mode record: what the user typed, shown to the coder ──────────────
# Every hand-typed command is captured as {cmd, cwd, status, exit_code, output}
# and buffered here until the chat's NEXT shell run drains it into the coder's
# seed as a <manual_mode> block — so the agent resumes knowing what the user
# tried by hand and what it produced. Records are tagged with the chat's
# session id (None = typed before any chat existed) so one chat's commands
# never surface in another chat's run.
_manual_records = []
_manual_records_lock = threading.Lock()
_MANUAL_KEEP = 50           # records buffered between runs
_MANUAL_HEAD_TAIL = 150     # live-capture lines kept per side of a long output
_MANUAL_BLOCK_CMDS = 12     # newest commands shown per injection
_MANUAL_BLOCK_LINES = 60    # output lines per record in the rendered block
_MANUAL_BLOCK_CHARS = 6000  # hard char cap per record's output

# A command (or its output) may itself contain the transcript's control tags —
# e.g. the user greps this repo, or `type`s a saved conversation. Left intact,
# an embedded <user_request=N> pair would hijack the coder's compression
# regex and a stray </manual_mode> would fake-close the block. Defuse just
# those openers; everything else passes through verbatim.
_MANUAL_TAG_RE = re.compile(r"<(?=/?(?:user_request=|manual_mode>))")


def _queue_manual_record(record):
    with _manual_records_lock:
        _manual_records.append(record)
        del _manual_records[:-_MANUAL_KEEP]


def _finish_manual_record(rec, exit_code):
    """Close a live command's capture (head/tail line windows) into a final
    record and queue it for the next agent run."""
    omitted = rec["total"] - len(rec["head"]) - len(rec["tail"])
    lines = rec["head"] + ([f"... [{omitted} line(s) omitted] ..."] if omitted > 0 else []) + rec["tail"]
    if rec.get("interrupted"):
        status = "interrupted"
    else:
        status = "success" if exit_code == 0 else "error"
    _queue_manual_record({
        "cmd": rec["cmd"], "cwd": rec["cwd"], "session_id": rec.get("session_id"),
        "status": status, "exit_code": exit_code, "output": "\n".join(lines),
    })


def _drain_manual_records(chat_session_id, first_request):
    """Hand this chat its pending records and leave other chats' in the
    buffer. Untagged records (typed while no chat id existed yet) only go to
    a chat's FIRST request — so commands typed in a brand-new chat can never
    leak into an older chat the user switches to before sending anything."""
    take, keep = [], []
    with _manual_records_lock:
        for r in _manual_records:
            sid = r.get("session_id")
            match = sid == chat_session_id or (not sid and first_request)
            (take if match else keep).append(r)
        _manual_records[:] = keep
    return take


def _requeue_manual_records(records):
    """Delivery failed (the run died before its first snapshot) — put the
    records back at the FRONT so ordering survives for the next run."""
    with _manual_records_lock:
        _manual_records[:0] = records
        del _manual_records[:-_MANUAL_KEEP]


def _render_manual_block(records) -> str:
    """The <manual_mode> block the coder replays in its transcript: one JSON
    object per hand-typed command. Output is capped hard — the block becomes a
    permanent part of the saved conversation and of every later prompt prefix."""
    if not records:
        return ""
    shown = records[-_MANUAL_BLOCK_CMDS:]
    parts = []
    if len(records) > len(shown):
        parts.append(f"[{len(records) - len(shown)} earlier command(s) omitted]")
    for r in shown:
        out = r.get("output") or ""
        ls = out.split("\n")
        if len(ls) > _MANUAL_BLOCK_LINES:
            half = _MANUAL_BLOCK_LINES // 2
            ls = ls[:half] + [f"... [{len(ls) - 2 * half} line(s) omitted] ..."] + ls[-half:]
            out = "\n".join(ls)
        if len(out) > _MANUAL_BLOCK_CHARS:
            out = (out[:_MANUAL_BLOCK_CHARS // 2] + "\n... [output truncated] ...\n"
                   + out[-_MANUAL_BLOCK_CHARS // 2:])
        parts.append(json.dumps(
            {"cmd": _MANUAL_TAG_RE.sub("&lt;", r.get("cmd") or ""),
             "cwd": r.get("cwd") or "",
             "status": r.get("status") or "", "exit_code": r.get("exit_code"),
             "output": _MANUAL_TAG_RE.sub("&lt;", out)},
            ensure_ascii=False, indent=2))
    header = ("Commands the user ran BY HAND at the terminal's `>` prompt since the "
              "previous run. They executed in the user's OWN shell (cwd shown per "
              "command) — the agent's shell and cwd are untouched. Treat their "
              "effects on files and processes as already applied.")
    return "<manual_mode>\n" + header + "\n\n" + "\n".join(parts) + "\n</manual_mode>"


class _ManualTerminal:
    """The live process behind Shell use's hand-typed `>` prompt.

    Deliberately NOT Sandbox.run(): that blocks until the command exits and hands
    back one blob, which is fine for an agent tool call but useless at a prompt —
    `ping 8.8.8.8` would show nothing until it died. This spawns the command in its
    OWN process group, streams stdout/stderr to the terminal as they arrive, and can
    deliver a real SIGINT to that group, which is what Ctrl+C actually does.

    One command at a time, because there is one prompt. Output is flushed in small
    batches (~80ms) rather than per line, so a chatty command can't drown the UI
    thread in evaluate_js calls.
    """

    FLUSH_MS = 0.08

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self._buf = []
        self._buf_lock = threading.Lock()
        # The RUNNING command's record — used ONLY by interrupt() to flag it.
        # Everything else (reader, finalize) holds the record by closure: a
        # stale reader kept alive by an orphaned child that still owns the
        # pipe must keep writing into ITS OWN command's record, never into
        # whatever command runs next.
        self._rec = None

    def is_running(self):
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    # ---- output pump -------------------------------------------------------
    def _emit(self, line, rec=None):
        with self._buf_lock:
            self._buf.append(line)
            if rec is not None:
                # head+tail windows, so an endless `ping` can't grow the record
                # unbounded while still keeping the start and the ending
                rec["total"] += 1
                if rec["total"] <= _MANUAL_HEAD_TAIL:
                    rec["head"].append(line)
                else:
                    rec["tail"].append(line)
                    if len(rec["tail"]) > _MANUAL_HEAD_TAIL:
                        rec["tail"].pop(0)

    def _flush(self):
        with self._buf_lock:
            if not self._buf:
                return
            lines, self._buf = self._buf, []
        if not webview_window:
            return
        try:
            payload = json.dumps(lines, ensure_ascii=False)
            payload = _js_escape(payload)
            webview_window.evaluate_js(
                f"window.shellTermLines && window.shellTermLines('{payload}')"
            )
        except Exception:
            debug_exception("shell term flush")

    def _pump(self, proc):
        while proc.poll() is None:
            time.sleep(self.FLUSH_MS)
            self._flush()
        self._flush()   # drain whatever landed in the final moments

    def _read(self, pipe, rec=None):
        try:
            for raw in iter(pipe.readline, b""):
                if not raw:
                    break
                self._emit(raw.decode("utf-8", errors="replace").rstrip("\r\n"), rec)
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    # ---- lifecycle ---------------------------------------------------------
    def start(self, command, cwd, session_id=None):
        """Spawn `command`. Returns (started, error_message)."""
        if self.is_running():
            return False, "a command is already running — press Ctrl+C to stop it"

        if IS_WINDOWS:
            argv = ["powershell", "-NoProfile", "-Command", command]
            # own group so CTRL_BREAK_EVENT reaches it and not us
            kwargs = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        else:
            argv = ["/bin/zsh", "-c", command]
            kwargs = {"start_new_session": True}   # setsid -> killpg targets only the child

        try:
            proc = subprocess.Popen(
                argv, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, **kwargs
            )
        except Exception as e:
            return False, str(e)

        # one record per command: everything the user saw, plus how it ended —
        # harvested into <manual_mode> for the next agent run. The reader and
        # _wait hold it by CLOSURE (identity-safe); the slot only serves
        # interrupt()'s flag, guarded by `is` checks.
        rec = {"cmd": command, "cwd": cwd, "session_id": session_id,
               "head": [], "tail": [], "total": 0, "interrupted": False}
        with self._lock:
            self._proc = proc
        with self._buf_lock:
            self._rec = rec

        reader = threading.Thread(target=self._read, args=(proc.stdout, rec), daemon=True)
        reader.start()
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()

        def _wait():
            code = proc.wait()
            # Give the reader a moment to drain the pipe's tail. Short on
            # purpose: an orphaned child holding the pipe would make a longer
            # join delay shellTermEnd (the prompt unlock) by its full timeout.
            reader.join(timeout=0.5)
            self._flush()
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            with self._buf_lock:
                if self._rec is rec:
                    self._rec = None
            _finish_manual_record(rec, code)
            if webview_window:
                try:
                    webview_window.evaluate_js(
                        f"window.shellTermEnd && window.shellTermEnd({int(code)})"
                    )
                except Exception:
                    debug_exception("shell term end")

        threading.Thread(target=_wait, daemon=True).start()
        return True, ""

    def interrupt(self):
        """Ctrl+C: SIGINT the process GROUP (so pipelines and children go too),
        then SIGKILL anything still alive a moment later."""
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        with self._buf_lock:
            # flag before signalling — the process may die instantly
            if self._rec is not None:
                self._rec["interrupted"] = True
        try:
            if IS_WINDOWS:
                proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

        def _sigkill():
            time.sleep(1.5)
            if proc.poll() is None:
                try:
                    if IS_WINDOWS:
                        proc.kill()
                    else:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass

        threading.Thread(target=_sigkill, daemon=True).start()
        return True


_manual_terminal = None


def _manual_term():
    global _manual_terminal
    if _manual_terminal is None:
        _manual_terminal = _ManualTerminal()
    return _manual_terminal


def _short_cwd(path):
    """Prompt-friendly cwd: the home prefix collapsed to ~ (…/a/b/c stays as-is)."""
    try:
        home = str(Path.home())
        p = str(path or "")
        return "~" + p[len(home):] if p.startswith(home) else p
    except Exception:
        return str(path or "")


@app.route('/api/shell-cwd', methods=['GET'])
def shell_cwd():
    """Where the hand-typed terminal currently is — the frontend shows this in the
    prompt while it's focused, so `cd` is obvious."""
    try:
        cwd = _get_manual_shell().get_cwd()
        return jsonify({'cwd': cwd, 'short': _short_cwd(cwd)})
    except Exception as e:
        debug_exception('shell_cwd API')
        return jsonify({'error': str(e)}), 500


@app.route('/api/shell-exec', methods=['POST'])
def shell_exec():
    """Shell use → a command the USER typed at the `>` prompt. No agent, no LLM.

    Runs through the same Sandbox the coder's shell tool uses, so it's zsh on
    macOS / PowerShell on Windows, the cwd persists between commands, and the
    macOS TCC-popup watcher still applies. trusted=True skips the agent-oriented
    blocked-path guard: the person typing is the one running it.
    """
    from flask import request
    try:
        data = request.get_json() or {}
        command = (data.get('command') or '').strip()
        # which CHAT the terminal belongs to right now — tags the manual-mode
        # record so another chat's next run never sees this command
        session_id = data.get('session_id') or None
        if not command:
            return jsonify({'error': 'Missing command'}), 400

        sh = _get_manual_shell()

        # `cd` has to move the Sandbox's own working dir — run as a subprocess it
        # would change directory, exit, and leave nothing behind.
        if command == 'cd' or command.startswith('cd '):
            # Sandbox._validate_path resolves relative to the cwd and does NOT expand `~`,
            # so `cd ~/Desktop` would look for "<cwd>/~/Desktop". Expand it here, and drop
            # any quotes the user wrapped a spaced path in.
            target = command[2:].strip().strip('"\'') or '~'
            res = sh.cd(os.path.expanduser(target))
            ok = bool(res.get('success'))
            output = '' if ok else (res.get('error') or 'Directory not found')
            # recorded too — a `cd` changes what the user's later commands mean
            # (the record's cwd is the RESULTING directory)
            _queue_manual_record({
                'cmd': command, 'cwd': sh.get_cwd(), 'session_id': session_id,
                'status': 'success' if ok else 'error', 'exit_code': None,
                'output': output,
            })
            return jsonify({
                'output': output,
                'status': 'success' if ok else 'error',
                'cwd': sh.get_cwd(),
                'short': _short_cwd(sh.get_cwd()),
            })

        # Everything else runs LIVE: the route returns as soon as the process is
        # spawned and its output is pushed to the terminal line by line, so `ping`
        # scrolls as it happens instead of appearing only once it dies.
        started, err = _manual_term().start(command, sh.get_cwd(), session_id)
        if not started:
            _queue_manual_record({
                'cmd': command, 'cwd': sh.get_cwd(), 'session_id': session_id,
                'status': 'error', 'exit_code': None, 'output': err,
            })
            return jsonify({'output': err, 'status': 'error',
                            'cwd': sh.get_cwd(), 'short': _short_cwd(sh.get_cwd())}), 200
        return jsonify({'status': 'started',
                        'cwd': sh.get_cwd(), 'short': _short_cwd(sh.get_cwd())})

    except Exception as e:
        debug_exception('shell_exec API')
        return jsonify({'error': str(e)}), 500


@app.route('/api/shell-kill', methods=['POST'])
def shell_kill():
    """Ctrl+C at the `>` prompt — interrupt whatever is running, exactly as a
    terminal would: SIGINT to the whole process group first, SIGKILL only if it
    refuses to die."""
    try:
        killed = _manual_term().interrupt()
        return jsonify({'status': 'killed' if killed else 'idle'})
    except Exception as e:
        debug_exception('shell_kill API')
        return jsonify({'error': str(e)}), 500


@app.route('/api/stop-agent', methods=['POST'])
def stop_agent():
    """Stop the currently running agent.

    The run is RETIRED here, not when it actually unwinds. It may be parked in
    an LLM call and only notice the stop when that call returns seconds later —
    by then the user may already have sent the next task. Clearing the run id
    (same as New chat) makes every late push it still makes — chain events,
    watcher reads, its own complete/error — fail run_is_current() and be
    dropped, so a discarded run can never repaint the run that replaced it.
    Its memory is still saved: save_run runs before that guard in run_agent's
    finally."""
    global active_agent_stop_event, active_agent_session_id
    if active_agent_stop_event:
        active_agent_stop_event.set()
        active_agent_session_id = None
        # Kill the Shell-use coder's whole process tree RIGHT NOW. Waiting for
        # the run to unwind is too late: cli_await terminates the coder itself
        # within a second of seeing the stop event, and once that parent is dead
        # the minions it spawned can no longer be resolved from its pid - they
        # would keep running and keep burning API calls. Killing here, while the
        # coder is still alive, takes the minions down with it.
        try:
            _kill_process_trees(list(_active_shell_pids))
            _active_shell_pids.clear()
        except Exception:
            debug_exception("stop_agent tree kill")
        return jsonify({'status': 'stopped'})
    return jsonify({'status': 'no_agent_running'})


@app.route('/api/new-chat', methods=['POST'])
def new_chat():
    """The user abandoned the live view (New chat). Stop any running agent and
    invalidate its run id so late pushes — the todo/milestone watchers' final
    reads and the run-end agentComplete/Agent-Notes — can't repaint the freshly
    reset UI. The run's memory is still persisted: save_run executes before the
    session-id push guard in run_agent's finally."""
    global active_agent_stop_event, active_agent_session_id
    active_agent_session_id = None
    if active_agent_stop_event:
        active_agent_stop_event.set()
    # Release the iPhone too. Switching modes kills the WDA session, but New
    # chat resets the mode picker with a SILENT set — deliberately no
    # agentmode:changed, so no side effects — and the phone disconnect was one
    # of the side effects it skipped: the UI showed Computer use while the
    # "Automation Running" overlay stayed on the phone. Deactivate is
    # idempotent (no session -> no-op) and phone-first (~1s), but that second
    # belongs to a background thread, not this endpoint's response.
    threading.Thread(target=_release_iphone, daemon=True).start()
    return jsonify({'status': 'ok'})


def _release_iphone():
    """Best-effort WDA teardown — safe to call whether or not a session exists."""
    try:
        from Auto_Use.ios_connector.session import wda_session
        wda_session.deactivate()
    except Exception:
        debug_exception("release iphone")


@app.route('/api/open-github', methods=['POST'])
def open_github():
    """Open the project's GitHub repo in the SYSTEM browser (a plain link would
    navigate the pywebview window itself). Fixed URL — no arbitrary-URL opener."""
    try:
        import webbrowser
        webbrowser.open("https://github.com/FunctionFreak/Auto-Use")
        return jsonify({'status': 'ok'})
    except Exception:
        debug_exception("open_github")
        return jsonify({'error': 'failed'}), 500


# =============================================================================
# Flask routes — skills (the Skills stage's Computer-use tab: list / preview /
# delete the active platform's autouse_data/skills/<platform>/*.md, so
# the same code serves windows on Windows and mac on Mac)
# =============================================================================
def _skills_dir():
    """The active platform's skill-markdown folder — autouse_data/skills/
    <windows|mac|ios>/, OUTSIDE the install folder so uninstalling never
    deletes the user's edited skills. `?platform=ios` selects the iOS folder
    (iOS is driven from a Mac, so it is never the host default)."""
    from flask import request
    plat = (request.args.get("platform") or "").strip().lower() if request else ""
    return skills_dir("ios" if plat == "ios" else None)


def _safe_skill_path(name):
    """Resolve a skill filename inside the skills dir, or None. Only bare
    '<something>.md' basenames are accepted — no separators, no traversal.
    Extension check is case-insensitive to match Windows' case-insensitive
    glob in list_skills (a listed FOO.MD must also preview/delete)."""
    if (not name or not name.lower().endswith('.md')
            or '/' in name or '\\' in name or name != os.path.basename(name)
            or name.startswith('.')):
        return None
    p = _skills_dir() / name
    return p if p.is_file() else None


@app.route('/api/skills', methods=['GET'])
def list_skills():
    """List the platform's skill .md files (sorted, names only) for the
    Skills stage's default list view."""
    try:
        d = _skills_dir()
        files = sorted((f.name for f in d.glob('*.md')), key=str.lower) if d.is_dir() else []
        return jsonify({'skills': files})
    except Exception:
        debug_exception("list_skills")
        return jsonify({'skills': []})


@app.route('/api/skills/<name>', methods=['GET'])
def get_skill(name):
    """A single skill file's raw markdown, for the preview view."""
    try:
        p = _safe_skill_path(name)
        if not p:
            return jsonify({'error': 'Not found'}), 404
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            return jsonify({'name': name, 'content': f.read()})
    except Exception:
        debug_exception("get_skill")
        return jsonify({'error': 'Failed'}), 500


@app.route('/api/skills/<name>', methods=['PUT'])
def save_skill(name):
    """Overwrite an EXISTING skill .md with edited content from the preview's
    Edit mode. Atomic write (temp + replace) so a crash can't truncate the
    skill; the .tmp never matches list_skills' *.md glob."""
    from flask import request
    try:
        p = _safe_skill_path(name)
        if not p:
            return jsonify({'error': 'Not found'}), 404
        data = request.get_json(silent=True) or {}
        content = data.get('content')
        if not isinstance(content, str):
            return jsonify({'error': 'Bad content'}), 400
        tmp = p.parent / (p.name + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp, p)
        return jsonify({'status': 'saved'})
    except Exception:
        debug_exception("save_skill")
        return jsonify({'error': 'Failed to save'}), 500


@app.route('/api/skills/<name>', methods=['DELETE'])
def delete_skill(name):
    """Delete a skill .md (idempotent) and scrub any skills.json entries that
    pointed at it, so the agent's site/app→skill index never dangles."""
    try:
        p = _safe_skill_path(name)
        if p:
            p.unlink()
        try:
            idx = _skills_dir() / 'skills.json'
            if idx.is_file():
                with open(idx, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                changed = False
                for mapping in data.values():
                    if isinstance(mapping, dict):
                        for key in [k for k, v in mapping.items() if v == name]:
                            del mapping[key]
                            changed = True
                if changed:
                    # Atomic rewrite: never leave skills.json truncated if we
                    # die mid-dump (the agent falls back to empty mappings on a
                    # broken index, silently disabling all skill injection).
                    tmp = idx.with_suffix('.json.tmp')
                    with open(tmp, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=2)
                    os.replace(tmp, idx)
        except Exception:
            debug_exception("delete_skill_index")
        return jsonify({'status': 'deleted'})
    except Exception:
        debug_exception("delete_skill")
        return jsonify({'error': 'Failed to delete'}), 500


# =============================================================================
# Flask routes — chat history
# =============================================================================
@app.route('/api/chats', methods=['GET'])
def list_chats():
    """List saved chat sessions (newest-first) for the left-bar history list."""
    try:
        return jsonify(conversation.list_sessions())
    except Exception:
        debug_exception("list_chats")
        return jsonify([])


@app.route('/api/chats/<chat_id>', methods=['GET'])
def get_chat(chat_id):
    """Reopen payload: name + last done message + context_tokens/context_cap (the
    memory bar's last context size and its fixed 300k budget) for the chat.

    Each exchange also carries `task_html`/`done_html` — the Markdown rendered
    by frontend/markdown.py, same as the live run-end path — so a reopened chat
    reads identically to the notes that were on screen when the run finished.
    The raw `task`/`done_message` stay in the payload for any consumer that
    wants plain text."""
    try:
        data = conversation.get_session(chat_id)
        if not data:
            return jsonify({'error': 'Not found'}), 404
        _render_exchange_html(data.get('exchanges'))
        data['last_done_message_html'] = md_render(data.get('last_done_message', ''))
        return jsonify(data)
    except Exception:
        debug_exception("get_chat")
        return jsonify({'error': 'Failed'}), 500


@app.route('/api/chats/<chat_id>', methods=['DELETE'])
def delete_chat(chat_id):
    """Delete a saved chat session (folder + index entry). Idempotent."""
    try:
        conversation.delete_session(chat_id)
        return jsonify({'status': 'deleted'})
    except Exception:
        debug_exception("delete_chat")
        return jsonify({'error': 'Failed to delete'}), 500


def _read_main_system_prompt():
    """Best-effort read of the active platform's main_driver system prompt, so a
    downloaded conversation log shows the same SYSTEM PROMPT block as a main.py run."""
    try:
        p = get_platform_use_path() / "agent" / "main_driver" / "system_prompt.md"
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


@app.route('/api/chats/<chat_id>/download', methods=['POST'])
def download_chat(chat_id):
    """Debug: write a session's saved memory as a human-readable conversation log
    (.txt) to the user's Downloads folder and return the saved path."""
    try:
        path = conversation.export_to_downloads(chat_id, system_prompt=_read_main_system_prompt())
        if not path:
            return jsonify({'error': 'Nothing saved for this chat yet'}), 404
        return jsonify({'status': 'saved', 'path': path})
    except Exception:
        debug_exception("download_chat")
        return jsonify({'error': 'Failed to export'}), 500


def _evict_port_squatter(host, port):
    """Best-effort: if the port is genuinely unbindable (stale AutoUse instance,
    macOS AirPlay Receiver holding it), kill the squatter so AutoUse can take it.
    Does nothing when the port is free or co-bindable, and never kills ourselves."""
    import socket
    import signal
    import subprocess

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
        return  # port is usable as-is — nothing to evict
    except OSError:
        pass
    finally:
        probe.close()

    try:
        me = os.getpid()
        if IS_WINDOWS:
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                                 capture_output=True, text=True, timeout=8).stdout
            pids = set()
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
                    pids.add(int(parts[4]))
            pids.discard(me)
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=8)
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=8).stdout
            pids = {int(p) for p in out.split() if p.strip()}
            pids.discard(me)

            def alive(pid):
                try:
                    os.kill(pid, 0)
                    return True
                except OSError:
                    return False

            for pid in pids:
                os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 2.0
            remaining = set(pids)
            while remaining and time.time() < deadline:
                time.sleep(0.1)
                remaining = {p for p in remaining if alive(p)}
            for pid in remaining:
                os.kill(pid, signal.SIGKILL)
        if pids:
            print(f"[port] evicted process(es) {sorted(pids)} holding :{port} so AutoUse can start")
    except Exception:
        debug_exception("evict_port_squatter")


def start_server():
    # Windows build exposes the Flask server on 0.0.0.0 so the Telegram
    # remote-pairing flow can reach it from other devices on the LAN.
    # macOS sticks to localhost since it doesn't ship Telegram yet.
    host = '0.0.0.0' if IS_WINDOWS else '127.0.0.1'
    _evict_port_squatter(host, 5000)
    app.run(host=host, port=5000, debug=False, use_reloader=False)
