# Copyright 2026 Autouse AI — https://github.com/auto-use/Auto-Use
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# If you build on this project, please keep this header and credit
# Autouse AI (https://github.com/auto-use/Auto-Use) in forks and derivative works.
# A small attribution goes a long way toward a healthy open-source
# community — thank you for contributing.

"""
Auto Use — unified entry point (Windows + macOS)
================================================
Single app.py that self-detects the host OS and routes to the matching
Auto_Use.<platform>_use package. Run directly for the GUI:

    python app.py

For binary builds, see windows_binary_build.py (produces .exe) or
mac_binary_build.py (produces .dmg / .app). Both build scripts use this
file as the single Nuitka entry point.
"""

import sys
import io
import os
import json
import platform
import subprocess
import traceback
import logging
import threading
import time
import shutil
import importlib
from datetime import datetime
from pathlib import Path

import webview
from flask import Flask, jsonify, send_from_directory

# Resumable chat memory (UI path only). Platform-agnostic, pure-stdlib module —
# safe to import at top level. main.py / cli.py never import this, so they stay
# direct one-shot entry points.
from Auto_Use.agent_conversation.service import conversation

# =============================================================================
# Platform detection
# =============================================================================
# PLATFORM_PKG is the name of the Auto_Use sub-package that contains the
# platform-specific implementation (controller, agent, llm_provider, ...).
# On Windows: Auto_Use.windows_use.*  — On macOS: Auto_Use.macOS_use.*
IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

if IS_MAC:
    PLATFORM_PKG = "macOS_use"
elif IS_WINDOWS:
    PLATFORM_PKG = "windows_use"
else:
    raise RuntimeError(f"Unsupported OS: {platform.system()}")

# =============================================================================
# DEBUG LOGGING - Only for compiled binary (not in dev mode)
# =============================================================================

# Check if running as compiled binary (Nuitka)
IS_COMPILED = getattr(sys, 'frozen', False) or '__compiled__' in dir()
IS_CLI_SUBPROCESS = "--cli-mode" in sys.argv
# Any re-exec of AutoUse.exe that should NOT overwrite the parent's debug log
# or wipe the parent's scratchpad. --banner-mode pops the floating Telegram
# pill (compiled-binary path — see banner.py:_IS_COMPILED branch). Treated
# identically to --cli-mode at the bootstrap-suppression layer below.
IS_SECONDARY_PROCESS = (
    IS_CLI_SUBPROCESS
    or "--banner-mode" in sys.argv
    or "--minion-mode" in sys.argv
)

# Unique id for this build, injected at build time. Used to gate the launch-time
# macOS TCC repair so it runs at most once per build identity. Absent in dev runs
# (repair is gated to compiled builds anyway).
try:
    from _build_stamp import BUILD_STAMP
except Exception:
    BUILD_STAMP = "unknown"

# Bundle id of the packaged macOS app. Used to target `tccutil reset` at our own
# TCC entries.
MACOS_BUNDLE_ID = "com.ashishyadav.autouse"


def app_data_dir() -> Path:
    """Root folder for cli_agent_result/ and cli_minion_result/ in the binary build.

    Compiled binary: ~/Library/Application Support/AutoUse on macOS,
    %LOCALAPPDATA%/AutoUse on Windows. Keeps user data out of /Applications/
    (or wherever the binary's CWD ends up at launch).

    Dev mode: project root (where app.py lives), so `python app.py` keeps
    writing these folders into the repo as before.
    """
    if IS_COMPILED:
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "AutoUse"
        elif sys.platform.startswith("win"):
            local = os.environ.get("LOCALAPPDATA")
            base = Path(local) / "AutoUse" if local else Path.home() / "AppData" / "Local" / "AutoUse"
        else:
            base = Path.home() / ".local" / "share" / "AutoUse"
    else:
        base = Path(__file__).resolve().parent
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

# Initialize log file on startup (only in compiled mode, not in any
# secondary subprocess — those would clobber the parent's log on every spawn)
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
# Banner subprocess stdio reconnection (MUST run before the std-fixup below)
# =============================================================================
# When AutoUse.exe is re-exec'd as a banner subprocess via --banner-mode, the
# parent's subprocess.Popen wires fd 0 (stdin) and fd 1 (stdout) to the pipes
# it uses to drive the wizard. But the binary is built as a Windows
# GUI-subsystem app (--windows-console-mode=disable in windows_binary_build.py)
# which means Python startup sets sys.stdin/sys.stdout to None — even though
# the OS-level fds are valid pipe handles inherited from the parent. We have
# to wrap those fds as text streams here, BEFORE the `if sys.stdout is None`
# block below silently replaces stdin/stdout with /dev/null and permanently
# severs the JSON-stdio protocol with the parent. Without this, the parent
# never sees READY/NEXT/CHOICE/SAVE/CLOSED events, the subprocess's
# _stdin_reader crashes on `for line in None`, and the entire banner wizard
# auto-completes in milliseconds when the eventual subprocess crash unblocks
# every wait_for_* event in the parent at once. (Symptom: pill flashes for a
# few seconds, Edge opens, empty token gets persisted, AutoUse restarts.)
if "--banner-mode" in sys.argv:
    try:
        # line_buffering on stdin doesn't really matter (we're the reader),
        # but the explicit encoding stops a UTF-8/cp1252 mismatch from
        # silently dropping non-ASCII wizard text.
        sys.stdin = os.fdopen(0, "r", encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        # buffering=1 → line-buffered, so each `_emit()` JSON line reaches
        # the parent immediately instead of sitting in a 4 KB block buffer.
        sys.stdout = os.fdopen(1, "w", encoding="utf-8", errors="replace", buffering=1)
    except Exception:
        pass
    if sys.stderr is None:
        # sys.stderr is None in a Nuitka GUI-subsystem child. pywebview's
        # webview/http.py has a self-heal shim, but it only runs after
        # `import webview` — anything that writes to stderr before that
        # (a stray print, an uncaught traceback) would crash the
        # subprocess. Try the inherited fd 2; fall back to devnull so the
        # attribute is never None.
        try:
            sys.stderr = os.fdopen(2, "w", encoding="utf-8", errors="replace", buffering=1)
        except Exception:
            try:
                sys.stderr = open(os.devnull, "w", encoding="utf-8")
            except Exception:
                pass

# =============================================================================
# Fix for bundled app (MUST be before any print statements)
# Skip when run from main.py / cli.py so terminal output is not buffered.
# Also skip in --banner-mode: the subprocess already wired its stdio above and
# re-wrapping orphans the original TextIOWrapper — its eventual GC closes
# fd 1 in the subprocess (silently breaking the JSON protocol with the parent
# after a few seconds), and the new wrapper also drops the line-buffering
# we deliberately set with buffering=1.
# =============================================================================

def _entry_is_cli_script():
    """True when the process was started with python main.py or python cli.py."""
    if '__main__' not in sys.modules:
        return False
    main_file = getattr(sys.modules['__main__'], '__file__', None) or ''
    return os.path.basename(main_file) in ('main.py', 'cli.py')

if not _entry_is_cli_script() and "--banner-mode" not in sys.argv:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    elif hasattr(sys.stdout, 'buffer'):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        except:
            pass

    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')
    elif hasattr(sys.stderr, 'buffer'):
        try:
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
        except:
            pass

# =============================================================================
# EMBEDDED RESOURCE LOADER (for Nuitka compiled binary)
# =============================================================================

def _setup_embedded_resources():
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

if IS_COMPILED:
    _setup_embedded_resources()

# =============================================================================
# Flask app initialization
# =============================================================================

def get_frontend_path():
    """Get correct frontend path for dev mode (returns None in compiled mode)"""
    if IS_COMPILED:
        return None
    else:
        return os.path.join(os.path.dirname(__file__), 'frontend')

frontend_path = get_frontend_path()
if frontend_path:
    app = Flask(__name__, static_folder=frontend_path, static_url_path='')
else:
    app = Flask(__name__)

# Suppress default Flask logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# =============================================================================
# Helper functions
# =============================================================================

def get_auto_use_path():
    """Get path to the Auto_Use package root"""
    if IS_COMPILED:
        return Path(sys.executable).parent / "Auto_Use"
    else:
        return Path(__file__).parent / "Auto_Use"

def get_platform_use_path():
    """Get path to the active Auto_Use/<platform>_use/ directory"""
    return get_auto_use_path() / PLATFORM_PKG

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
    except Exception:
        debug_exception("clean_scratchpad")

def _reset_todo_file():
    """Delete <platform>_use/scratchpad/todo/todo.md so a new agent run starts
    with an empty top-right todo card (the previous run's plan would otherwise
    linger until the agent writes a fresh one)."""
    try:
        todo_file = get_platform_use_path() / "scratchpad" / "todo" / "todo.md"
        if todo_file.exists():
            todo_file.unlink()
    except Exception:
        debug_exception("_reset_todo_file")

def _reset_scratchpad_file():
    """Delete <platform>_use/scratchpad/milestone/milestone.md so a new run starts
    with an empty scratchpad — otherwise the end-of-run "Agent Notes" view would
    show the previous run's entries accumulated on top of the new ones."""
    try:
        notes_file = get_platform_use_path() / "scratchpad" / "milestone" / "milestone.md"
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


def repair_stale_tcc_entries():
    """Clear orphaned macOS TCC ("ghost") entries for our bundle id, once per build.

    When a previous build's code signature no longer matches the current binary
    (the classic ad-hoc-signing churn: an unstable signature changes every
    build), the old permission grant becomes a "ghost" entry in System
    Settings: the toggle still shows but is bound to a binary that's gone,
    so the new build silently can't use it AND no fresh prompt appears. We
    `tccutil reset` such entries so request_macos_permissions() can re-prompt and
    rebind the grant to the CURRENT binary ("delete the old reference when
    triggering the new one").

    Safety guarantees:
      - compiled macOS builds only (dev runs sign Terminal/Python — leave alone)
      - never reset a permission that currently works (preflight skip)
      - at most once per build identity (BUILD_STAMP marker) -> no reset-loop,
        even if the user keeps declining the prompt

    Automation (AppleEvents) is intentionally NOT auto-reset: it has no reliable
    no-prompt preflight, so a blind once-per-build reset would nuke a working
    grant on every rebuild. The System Events re-prompt in
    request_macos_permissions() already rebinds it, and the uninstaller clears it
    via `tccutil reset All`.
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


def request_macos_permissions():
    """Prompt user for required macOS permissions on first launch (no-op elsewhere)"""
    if not IS_MAC:
        return
    try:
        from ApplicationServices import AXIsProcessTrusted

        # Accessibility — prompt if not already granted
        if not AXIsProcessTrusted():
            from ApplicationServices import AXIsProcessTrustedWithOptions
            AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})

        # Screen Recording — prompt if not already granted
        from Quartz import CGPreflightScreenCaptureAccess, CGRequestScreenCaptureAccess
        if not CGPreflightScreenCaptureAccess():
            CGRequestScreenCaptureAccess()

        # Automation (Apple Events) — trigger System Events prompt at first launch
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to return name of first process whose frontmost is true'],
                capture_output=True, text=True, timeout=10
            )
        except Exception:
            pass

        # Full Disk Access — macOS has no API to request it, only to open its pane.
        # FDA stops the Desktop/Documents/Downloads popups that block coder/minion
        # shell commands, and lets the auto-clicker work. Probe by reading an
        # FDA-gated, always-present path; PermissionError ⇒ not granted.
        try:
            tcc_db = os.path.expanduser("~/Library/Application Support/com.apple.TCC/TCC.db")
            has_fda = True
            try:
                with open(tcc_db, "rb") as _f:
                    _f.read(1)
            except PermissionError:
                has_fda = False
            except Exception:
                has_fda = True  # missing path / other error — don't nag
            if not has_fda:
                print(
                    "\n⚠️  Full Disk Access not granted. Auto Use needs it so shell commands can\n"
                    "    read/write Desktop, Documents and Downloads without macOS permission popups.\n"
                    "    Opening System Settings — add to Full Disk Access:\n"
                    "      • Packaged app: add 'AutoUse'\n"
                    "      • Dev run: add your Terminal / VS Code / the python you launch from\n"
                )
                subprocess.run(
                    ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"],
                    capture_output=True, timeout=10
                )
        except Exception:
            pass

    except Exception:
        debug_exception("request_macos_permissions")

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

        def format_models(mappings):
            return [{
                'id': model_id,
                'display_name': info.get('display_name', model_id),
                'reasoning_support': info.get('reasoning_support', False)
            } for model_id, info in mappings.items() if not info.get('hidden', False)]

        return [
            {'id': 'openrouter', 'name': 'openrouter', 'models': format_models(openrouter_models)},
            {'id': 'groq',       'name': 'groq',       'models': format_models(groq_models)},
            {'id': 'openai',     'name': 'openai',     'models': format_models(openai_models)},
            {'id': 'anthropic',  'name': 'anthropic',  'models': format_models(anthropic_models)},
            {'id': 'google',     'name': 'google',     'models': format_models(google_models)},
            {'id': 'perplexity', 'name': 'perplexity', 'models': format_models(perplexity_models)},
        ]
    except Exception:
        debug_exception("get_llm_providers")
        return []

# =============================================================================
# API Key File Management
# =============================================================================

PROVIDER_KEY_MAP = {
    'openrouter': 'OPENROUTER_API_KEY',
    'groq': 'GROQ_API_KEY',
    'openai': 'OPENAI_API_KEY',
    'anthropic': 'ANTHROPIC_API_KEY',
    'google': 'GOOGLE_API_KEY',
    'perplexity': 'PERPLEXITY_API_KEY',
}

def get_api_key_file():
    """Get path to api_key.txt (lives at Auto_Use/api_key/, shared across platforms)"""
    return get_auto_use_path() / "api_key" / "api_key.txt"

# Extra keys stored in the same api_key.txt
EXTRA_KEYS = ['VERTEX_PROJECT_ID', 'VERTEX_LOCATION']

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
                        # Keep every key, not just managed ones, so unmanaged
                        # entries (e.g. TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_CHAT_ID)
                        # survive a read-modify-write cycle instead of being dropped.
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
        # Preserve any unmanaged keys (e.g. TELEGRAM_BOT_TOKEN,
        # TELEGRAM_OWNER_CHAT_ID) that the Telegram surface writes — without
        # this they'd be wiped every time a provider key is saved.
        extra = [k for k in keys if k not in all_key_names]
        with open(key_file, 'w', encoding='utf-8') as f:
            for name in all_key_names:
                f.write(f"{name}={keys.get(name, '')}\n")
            for name in extra:
                f.write(f"{name}={keys.get(name, '')}\n")
    except Exception:
        debug_exception("write_api_keys")

# ── Last-used selection (provider + model) ──────────────────────────────────
# Persisted so the app auto-loads the user's last choice on launch instead of
# making them re-pick every time. Selection ONLY — API keys stay in api_key.txt.
def get_settings_file():
    """Path to settings.json (the user's last selection), in the per-user app-data dir."""
    return app_data_dir() / "settings.json"

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
    """Merge a dict of fields (e.g. {'provider':..,'model':..}) into settings.json.
    A field whose value is None is written through, so the frontend can CLEAR the
    model (provider changed) by sending {'model': None}."""
    settings_file = get_settings_file()
    try:
        data = read_settings()
        data.update(updates)
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception:
        debug_exception("write_settings")

# Resumable chat memory (UI path only) lives entirely in
# Auto_Use.agent_conversation.service — the `conversation` singleton imported at
# the top of this file. app.py only calls start_or_resume / save_run /
# list_sessions / get_session / delete_session; it owns no memory logic itself.

def get_provider_api_key(provider):
    """Get API key for a specific provider from file"""
    env_name = PROVIDER_KEY_MAP.get(provider)
    if not env_name:
        return None
    keys = read_api_keys()
    return keys.get(env_name, '') or None

# =============================================================================
# Global state
# =============================================================================

webview_window = None
active_agent_stop_event = None
active_agent_session_id = None

# =============================================================================
# Embedded file serving (for compiled mode)
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

                    # WKWebView's media player (macOS) requires HTTP Range
                    # support (206) to play <video>/<audio>; a plain 200 with the
                    # whole body makes media fail to start. Honour the Range
                    # header for any embedded media served from the bundle.
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
# Flask routes
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
    """Serve the Telegram banner orb (the PC⇄Telegram flip) so the floating
    banner's webview can iframe it from http://127.0.0.1:5000/ — single source
    of truth, so edits to telergam_animation.html show up in the banner.
    Resolves the platform-correct copy (windows_use vs macOS_use) and works in
    both dev (filesystem) and compiled (embedded resources) builds."""
    rel = f"Auto_Use/{PLATFORM_PKG}/remote_connection/telegram/telergam_animation.html"
    if IS_COMPILED:
        response = serve_embedded_file(rel)
        if response:
            return response
        return "Not found", 404
    tg_dir = os.path.join(os.path.dirname(__file__), 'Auto_Use', PLATFORM_PKG,
                          'remote_connection', 'telegram')
    return send_from_directory(tg_dir, 'telergam_animation.html')

@app.route('/logo.png')
def serve_logo():
    """Serve the Auto Use logo for the splash screen"""
    if IS_COMPILED:
        response = serve_embedded_file('Auto_Use/logo/auto_use.png')
        if response:
            return response
        return "Logo not found", 404
    return send_from_directory(os.path.join(os.path.dirname(__file__), 'Auto_Use', 'logo'), 'auto_use.png')

@app.route('/cursor.png')
def serve_cursor():
    """Serve the cursor image for the splash animation"""
    if IS_COMPILED:
        response = serve_embedded_file('Auto_Use/logo/cursor.png')
        if response:
            return response
        return "Cursor not found", 404
    return send_from_directory(os.path.join(os.path.dirname(__file__), 'Auto_Use', 'logo'), 'cursor.png')

@app.route('/api/providers', methods=['GET'])
def get_providers():
    try:
        providers = get_llm_providers()
        return jsonify(providers)
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
    """Persist the user's selected provider and/or model (partial merge; only the
    keys present in the body are updated, so provider and model can be saved separately)."""
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
    global webview_window
    if webview_window:
        try:
            escaped_text = text.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r')
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
    The "#N." auto-numbering prefix is stripped; "- [x]" => done, "- [ ]" =>
    pending. The first non-task / "Objective:" line becomes the objective.
    """
    objective = ""
    tasks = []
    for raw in (content or "").split('\n'):
        line = raw.strip()
        if not line:
            continue
        # Strip an optional "#N." numbering prefix ("#3. - [ ] ..." -> "- [ ] ...").
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
            # A stray leading line with no "Objective:" prefix — treat as the goal.
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

def _parse_scratchpad_md(content):
    """Parse milestone.md (the agent scratchpad: '1. note\\n2. note\\n…') into a
    list of entry strings, with the leading 'N.' numbering stripped (the frontend
    re-numbers them)."""
    entries = []
    for raw in (content or "").split('\n'):
        line = raw.strip()
        if not line:
            continue
        dot = line.find('. ')
        if dot != -1 and line[:dot].isdigit():
            line = line[dot + 2:].strip()
        if line:
            entries.append(line)
    return entries

def send_agent_notes(content):
    """Show the agent's scratchpad as 'Agent Notes' in the top-left container
    (called when a run ends — completed or stopped)."""
    global webview_window
    if not webview_window:
        return
    try:
        entries = _parse_scratchpad_md(content)
        escaped = _js_escape(json.dumps(entries))
        webview_window.evaluate_js(
            f"window.showAgentNotes && window.showAgentNotes('{escaped}')"
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
    """Forward CLI agent streaming events to the frontend.

    Event types:
      - "await_start", reason            -> window.cliAwaitStart(reason)
      - "await_end"                      -> window.cliAwaitEnd()
      - "task_start", task_id, desc      -> window.cliTaskStart(task_id, desc)
      - "task_line",  task_id, line, s   -> window.cliTaskLine(task_id, line, stream)
      - "task_end",   task_id, status, summary -> window.cliTaskEnd(task_id, status, summary)
    """
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
            # The coder agent's own todo list (cli_todo/todo.md) changed. Parse it the
            # same way as the main agent's todo and forward to the live coder card.
            task_id = _js_escape(args[0] if len(args) > 0 else "")
            todo_payload = _js_escape(json.dumps(_parse_todo_md(args[1] if len(args) > 1 else "")))
            webview_window.evaluate_js(
                f"window.cliTaskTodo && window.cliTaskTodo('{task_id}', '{todo_payload}')"
            )
        elif event_type == "minion_start":
            # parent_task_id is the spawning CLI agent's task_id; task_id is the minion's own.
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
            # Live stdout/stderr from a running minion — streams into its pill body.
            task_id = _js_escape(args[0] if len(args) > 0 else "")
            line = _js_escape(args[1] if len(args) > 1 else "")
            stream = _js_escape(args[2] if len(args) > 2 else "out")
            webview_window.evaluate_js(
                f"window.cliMinionLine && window.cliMinionLine('{task_id}', '{line}', '{stream}')"
            )
        elif event_type == "pill_web_loading_start":
            # Web tool started inside a piped CLI subprocess — show clean dots-loading
            # visual on the parent CLI pill (replaces the ugly "🌐 Web..." stream).
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
    """Send shell / AppleScript execution status to frontend for terminal animation.
    `label` lets callers tag the terminal card ("Shell", "AppleScript", ...);
    defaults to 'Shell' when omitted (Windows callers don't pass it)."""
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
    """Drive the bottom 'Tool response' tool-flow chain.
    event: run_start | turn | received | tool | done | run_end.
    payload: a small JSON-serializable dict (or None)."""
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

        api_key = get_provider_api_key(provider)

        # ── Resolve the CHAT session via the conversation service ────────────
        # Continuation -> loads the saved optimized history into prior_history;
        # fresh start -> mints a new id (prior_history is None). All memory logic
        # lives in Auto_Use.agent_conversation.service, not here.
        chat_session_id, prior_history = conversation.start_or_resume(req_session_id, task)

        active_agent_stop_event = threading.Event()
        active_agent_session_id = str(time.time())   # per-RUN guard (unchanged)
        current_session_id = active_agent_session_id

        def run_agent():
            stop_event = active_agent_stop_event

            # Clear any stale todo from a previous run in this session and blank
            # the top-right card immediately; the watcher below repopulates it as
            # soon as the agent writes its plan. Also clear the scratchpad so the
            # end-of-run "Agent Notes" view only shows this run's entries.
            _reset_todo_file()
            _reset_scratchpad_file()
            send_todo_to_frontend({"objective": "", "tasks": []})

            def monitor_milestones():
                milestone_path = get_platform_use_path() / "scratchpad" / "milestone" / "milestone.md"
                last_pos = 0

                while not milestone_path.exists() and not stop_event.is_set():
                    time.sleep(0.5)

                while not stop_event.is_set():
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
                if milestone_path.exists():
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
                # todo.md is REWRITTEN on every update (not appended), so we
                # re-read the whole file and push it whenever the content changes.
                todo_path = get_platform_use_path() / "scratchpad" / "todo" / "todo.md"
                last_content = None
                while not stop_event.is_set():
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
                    if todo_path.exists():
                        content = todo_path.read_text(encoding='utf-8')
                        if content != last_content:
                            send_todo_to_frontend(_parse_todo_md(content))
                except Exception:
                    debug_exception("monitor_todo final read")

            # Outcome reported back to the web UI. Defaults to success; the
            # process_request return value or an exception overrides it so the
            # UI shows the real result instead of always signaling completion.
            run_outcome = {"status": "success", "message": ""}
            agent = None  # bound below; kept defined so the finally save is safe
            try:
                AgentService = importlib.import_module(
                    f"Auto_Use.{PLATFORM_PKG}.agent.main_driver.service"
                ).AgentService

                agent = AgentService(
                    provider=provider,
                    model=model,
                    thinking=True,
                    frontend_callback=send_image_to_frontend,
                    text_callback=send_text_to_frontend,
                    web_callback=send_web_status_to_frontend,
                    shell_callback=send_shell_status_to_frontend,
                    cli_callback=send_cli_event_to_frontend,
                    tool_callback=send_flow_to_frontend,
                    api_key=api_key,
                    stop_event=stop_event,
                    prior_history=prior_history,   # None for a fresh chat
                )

                monitor_thread = threading.Thread(target=monitor_milestones)
                monitor_thread.daemon = True
                monitor_thread.start()

                todo_thread = threading.Thread(target=monitor_todo)
                todo_thread.daemon = True
                todo_thread.start()

                run_outcome = agent.process_request(task)
                if not isinstance(run_outcome, dict):
                    # Older return shape (a bare string) → treat as success.
                    run_outcome = {"status": "success", "message": ""}

            except Exception as agent_exc:
                debug_exception("run_agent")
                run_outcome = {"status": "error", "message": str(agent_exc)}
            finally:
                stop_event.set()

                # ── Persist this run via the conversation service ───────────
                # Runs for BOTH completion and stop, and BEFORE the window guard
                # below, so memory is saved even if the window closed or a newer
                # run superseded this one. The service optimizes the agent's final
                # lists + writes a clean terminal "done message" (success OR any
                # abnormal end). agent may be None if construction failed.
                try:
                    conversation.save_run(
                        chat_session_id,
                        getattr(agent, "assistant_messages", None),
                        getattr(agent, "tool_responses", None),
                        run_outcome.get("status", "success"),
                        run_outcome.get("message", "") or "",
                        task,
                        getattr(agent, "last_messages", None),  # exact payload -> true memory log
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

                    # Replace the screenshot with the "Agent Notes" view now the
                    # run has ended (completed OR stopped) — ALWAYS, even if the
                    # scratchpad is empty (empty content still shows the notes view,
                    # the image is gone).
                    try:
                        notes_path = get_platform_use_path() / "scratchpad" / "milestone" / "milestone.md"
                        content = notes_path.read_text(encoding='utf-8') if notes_path.exists() else ""
                        send_agent_notes(content)
                    except Exception:
                        debug_exception("send agent notes on finish")

        thread = threading.Thread(target=run_agent)
        thread.daemon = True
        thread.start()

        # Return the chat session id so a brand-new chat's id reaches the
        # frontend (it adopts it for the run-end save + future continuations).
        return jsonify({'status': 'started', 'session_id': chat_session_id})

    except Exception as e:
        debug_exception("start_agent API")
        return jsonify({'error': str(e)}), 500

@app.route('/api/stop-agent', methods=['POST'])
def stop_agent():
    """Stop the currently running agent"""
    global active_agent_stop_event
    if active_agent_stop_event:
        active_agent_stop_event.set()
        return jsonify({'status': 'stopped'})
    return jsonify({'status': 'no_agent_running'})

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
    """Reopen payload: the session's name + last done message (the only thing the
    reopen view shows, in the top-left container). No transcript is returned."""
    try:
        data = conversation.get_session(chat_id)
        if not data:
            return jsonify({'error': 'Not found'}), 404
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
    downloaded conversation log shows the same SYSTEM PROMPT block as a main.py
    run. open() is patched in compiled builds to resolve embedded resources."""
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

def start_server():
    # Windows build exposes the Flask server on 0.0.0.0 so the Telegram
    # remote-pairing flow can reach it from other devices on the LAN.
    # macOS build sticks to localhost since it doesn't ship Telegram yet.
    host = '0.0.0.0' if IS_WINDOWS else '127.0.0.1'
    app.run(host=host, port=5000, debug=False, use_reloader=False)

def minimize_main_window():
    """Minimise the AutoUse pywebview window. No-op if the window isn't up yet
    (e.g. someone calls this before main() has created it) or pywebview's
    minimise call fails for any reason. Safe to call from any thread —
    pywebview routes the call to its own UI loop internally."""
    win = globals().get('webview_window')
    if win is None:
        return
    try:
        win.minimize()
    except Exception:
        debug_exception("minimize_main_window")


# Soft off-white with a hint of grey (light greige), used to tint the native
# title bar on BOTH macOS and Windows. KEEP IN SYNC with the app/splash
# background in frontend (style.css `body` + `.splash-overlay`, and the two
# *_animation.html bodies) so the bar and content read as one unified surface.
#   greyer → #CAC9C3  |  warmer → #D6D5CF  |  flatter neutral grey → #D2D2CF
TITLEBAR_COLOR = "#D0CFC9"


def _style_macos_titlebar():
    """Tint the native macOS window titlebar off-white instead of stark white.

    There is no HTML header — the bright bar at the top of the window is the
    native NSWindow titlebar. pywebview exposes that NSWindow as
    `webview_window.native`. We make the titlebar transparent so the window's
    own background color shows through the titlebar strip, and set that color to
    a soft off-white. The opaque WKWebView still fills the content area below, so
    only the titlebar picks up the tint; the title text and traffic-light
    buttons are left untouched. macOS-only (Windows has its own chrome).
    """
    win = globals().get('webview_window')
    nswindow = getattr(win, 'native', None) if win is not None else None
    if nswindow is None:
        return
    try:
        from AppKit import NSColor, NSAppearance, NSAppearanceNameAqua
        from PyObjCTools import AppHelper
    except Exception:
        debug_exception("titlebar_color_import")
        return

    def apply():
        try:
            h = TITLEBAR_COLOR.lstrip('#')
            r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
            color = NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)
            nswindow.setBackgroundColor_(color)
            nswindow.setTitlebarAppearsTransparent_(True)
            # Pin the titlebar to the light (Aqua) appearance so the title text
            # and traffic lights stay dark/contrasty on the off-white bar even
            # when the system is in Dark Mode.
            nswindow.setAppearance_(
                NSAppearance.appearanceNamed_(NSAppearanceNameAqua)
            )
            # pywebview pins the titlebar's own background view to the system
            # windowBackgroundColor (white) at window creation, which paints over
            # the window background color in the titlebar region. Recolor that
            # exact view (NSTitlebarContainerView) to our off-white.
            try:
                titlebar_view = nswindow.contentView().superview().subviews().lastObject()
                titlebar_view.setBackgroundColor_(color)
            except Exception:
                debug_exception("titlebar_view_color")
        except Exception:
            debug_exception("titlebar_color_apply")

    # The `shown` event fires on a pywebview worker thread, but AppKit mutations
    # must happen on the main thread — marshal the work over.
    AppHelper.callAfter(apply)


def _style_windows_titlebar():
    """Tint the native Windows title bar (caption) to the same off-white as macOS.

    There is no HTML header — the bar is the native window caption drawn by the
    Desktop Window Manager. Windows 11 (build 22000+) exposes DWM attributes to
    set the caption background and title-text colors; on Windows 10 these are
    unsupported and the calls simply no-op (the bar stays default). Mirrors
    `_style_macos_titlebar` for cross-platform parity. DWM calls are not
    thread-affine, so running from the `shown` worker thread is fine.
    """
    win = globals().get('webview_window')
    native = getattr(win, 'native', None) if win is not None else None
    try:
        hwnd = int(native.Handle.ToInt32()) if native is not None else 0
    except Exception:
        debug_exception("titlebar_color_hwnd")
        return
    if not hwnd:
        return
    try:
        import ctypes
        from ctypes import wintypes

        def _colorref(hex_str):
            h = hex_str.lstrip('#')
            r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
            return (b << 16) | (g << 8) | r  # COLORREF is 0x00BBGGRR

        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD
        ]
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        DWMWA_CAPTION_COLOR = 35
        DWMWA_TEXT_COLOR = 36

        def _set(attr, value):
            v = wintypes.DWORD(value)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        # Force the light-mode caption (dark button glyphs) so they stay readable
        # on the off-white bar even when Windows is in Dark Mode, then set the
        # caption (bar) background and the title-text color.
        _set(DWMWA_USE_IMMERSIVE_DARK_MODE, 0)
        _set(DWMWA_CAPTION_COLOR, _colorref(TITLEBAR_COLOR))
        _set(DWMWA_TEXT_COLOR, _colorref("#2A2A2A"))
    except Exception:
        debug_exception("titlebar_color_apply_win")


def _compute_window_center(win_w, win_h):
    """Return (x, y) to center a (win_w, win_h) window on the main display.
    Falls back to a sensible default if the native APIs are unavailable."""
    try:
        if IS_MAC:
            from AppKit import NSScreen
            frame = NSScreen.mainScreen().frame()
            screen_w = frame.size.width
            screen_h = frame.size.height
            return int((screen_w - win_w) / 2), int((screen_h - win_h) / 2)

        if IS_WINDOWS:
            import ctypes

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
            work_rect = RECT()
            # SPI_GETWORKAREA = 0x0030 — excludes the taskbar
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_rect), 0)
            area_w = work_rect.right - work_rect.left
            area_h = work_rect.bottom - work_rect.top
            cx = work_rect.left + (area_w - win_w) // 2
            cy = work_rect.top + (area_h - win_h) // 2
            return cx, cy
    except Exception:
        debug_exception("_compute_window_center")

    return 600, 30

def main():
    # --banner-mode MUST be handled before anything else in main() — Flask,
    # webview, Telegram bot, scratchpad cleanup, etc. all need to stay
    # untouched in the banner subprocess. In dev (`python app.py`) the
    # banner spawns via `python -m …banner`, but the Nuitka binary has no
    # `-m` mode, so StatusBanner.show() re-execs AutoUse.exe with this
    # flag instead. Without an early exit here, the banner subprocess
    # would boot a second AutoUse webview, start a second Telegram bot,
    # and race the parent for port 5000 + the milestone scratchpad. We
    # check at the very top so even one stray scratchpad wipe / Flask
    # bind can't happen. --compact is left in argv on purpose — it's
    # read inside _run_subprocess_banner via `"--compact" in sys.argv`.
    if "--banner-mode" in sys.argv and IS_WINDOWS:
        sys.argv.remove("--banner-mode")
        try:
            from Auto_Use.windows_use.remote_connection.banner import (
                _run_subprocess_banner,
            )
            _run_subprocess_banner()
        except Exception:
            debug_exception("Banner mode")
        return

    # Wire the Telegram remote-control bot. Windows mounts a Flask blueprint
    # plus a polling bot; macOS just starts the polling bot (no blueprint yet —
    # token is read from .env / api_key.txt directly).
    #
    # Only the REAL GUI process runs the bot. Re-exec'd secondary processes
    # (--cli-mode / --minion-mode / --banner-mode) must NOT start a bot: in the
    # compiled binary the controller spawns sub-agents by re-exec'ing
    # AutoUse.exe, which re-enters main() and would otherwise boot a *second*
    # Telegram bot per child. That duplicates the "AutoUse online" announcement,
    # fights the parent's getUpdates long-poll (HTTP 409), and prints
    # "[telegram] starting bot …" to the child's stderr — which the parent
    # streams onto the on-screen pill (token leak + looping line). The cli/minion
    # dispatch blocks below return before Flask/start_server is ever reached, so
    # skipping the blueprint registration here is a no-op for those children.
    if not IS_SECONDARY_PROCESS:
        if IS_WINDOWS:
            try:
                from Auto_Use.windows_use.remote_connection.telegram.view import telegram_bp, start_bot
                app.register_blueprint(telegram_bp)
                start_bot()
            except Exception:
                debug_exception("telegram_blueprint_init")
        elif IS_MAC:
            try:
                from Auto_Use.macOS_use.remote_connection.telegram.view import telegram_bp
                from Auto_Use.macOS_use.remote_connection.telegram.service import start_bot as start_telegram_bot
                app.register_blueprint(telegram_bp)
                start_telegram_bot()
            except Exception as _tg_e:
                import traceback as _tg_tb
                print(f"[telegram] IMPORT/INIT FAILED: {_tg_e!r}", file=sys.stderr, flush=True)
                _tg_tb.print_exc(file=sys.stderr)
                debug_exception("telegram_bot_init")

    if "--cli-mode" in sys.argv:
        # CLI mode - delegate to the platform-specific CLI agent
        sys.argv.remove("--cli-mode")
        try:
            cli_main = importlib.import_module(
                f"Auto_Use.{PLATFORM_PKG}.agent.coder.__main__"
            ).main
            cli_main()
        except Exception:
            debug_exception("CLI mode")
        return

    if "--minion-mode" in sys.argv:
        # Minion mode - delegate to the platform-specific minion sub-agent.
        # Required when running from the compiled binary, where the controller
        # re-execs AutoUse with --minion-mode instead of `python -m ...minions`.
        sys.argv.remove("--minion-mode")
        try:
            minion_main = importlib.import_module(
                f"Auto_Use.{PLATFORM_PKG}.agent.minions.__main__"
            ).main
            minion_main()
        except Exception:
            debug_exception("Minion mode")
        return

    # Clean scratchpad on startup
    clean_scratchpad()

    # Set the frontend flag
    set_frontend_flag()

    # Clear orphaned macOS TCC "ghost" entries left by a previous build whose
    # signature no longer matches this binary, so the prompt below rebinds to the
    # current build (once per build identity; no-op on Windows / dev runs).
    repair_stale_tcc_entries()

    # Prompt for required macOS permissions on first launch (no-op on Windows)
    request_macos_permissions()

    # Start Flask in a daemon thread
    t = threading.Thread(target=start_server)
    t.daemon = True
    t.start()

    # Wait until Flask is actually ready (not a fixed sleep)
    import urllib.request
    for _ in range(40):  # up to ~10 seconds
        try:
            urllib.request.urlopen('http://127.0.0.1:5000', timeout=0.5)
            break
        except Exception:
            time.sleep(0.25)

    # Create webview window
    global webview_window

    # 1140 = ~900 of content area + the 240px left bar (see --left-bar-w in
    # frontend/css/style.css) so the sidebar doesn't cramp the main content.
    win_w, win_h = 1140, 700

    # Don't pass x/y: pywebview's Edge backend multiplies them by the DPI scale
    # factor (winforms.py), but a manual center computed in physical pixels is
    # already scaled — so on any display scaled >100% the window lands
    # off-centre (double-scaled). Omitting the position makes pywebview use
    # FormStartPosition.CenterScreen, which centres correctly at any DPI.
    webview_window = webview.create_window(
        'Auto use',
        'http://127.0.0.1:5000',
        width=win_w,
        height=win_h,
    )

    # Dismiss any floating helper banner the INSTANT the user closes the app.
    # pywebview fires `closing` synchronously before the (slow) window/Qt
    # teardown, so closing banners here makes the pill vanish immediately
    # instead of lingering until stdin-EOF fires after teardown. The banner's
    # own close() is now non-blocking, so this handler returns at once. Return
    # None (NOT False — False would cancel the close). Windows-only: the
    # banner module lives under windows_use.
    if IS_WINDOWS:
        try:
            import atexit
            from Auto_Use.windows_use.remote_connection.banner import (
                close_all_banners,
            )

            def _on_app_closing():
                close_all_banners()

            webview_window.events.closing += _on_app_closing
            atexit.register(close_all_banners)  # backstop for teardown paths that skip `closing`
        except Exception:
            debug_exception("banner_close_hook")

    # Recolor the native titlebar from stark white to a soft off-white. Done on
    # `shown` (once the native window/handle exists). macOS marshals its AppKit
    # calls onto the main thread; Windows uses DWM (Win11+) which is fine on the
    # worker thread.
    if IS_MAC:
        try:
            webview_window.events.shown += _style_macos_titlebar
        except Exception:
            debug_exception("titlebar_color_hook")
    elif IS_WINDOWS:
        try:
            webview_window.events.shown += _style_windows_titlebar
        except Exception:
            debug_exception("titlebar_color_hook_win")

    # macOS needs pynput keyboard pre-initialized on the main thread
    # because Carbon APIs require the main dispatch queue.
    if IS_MAC:
        try:
            from Auto_Use.macOS_use.controller.hotkey.service import _get_keyboard
            _get_keyboard()
        except Exception:
            pass

    webview.start()

if __name__ == '__main__':
    try:
        main()
    except Exception:
        debug_exception("main entry point")
        raise