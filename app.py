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
Thin entry point. Run directly for the GUI:

    python app.py

This file keeps ONLY the order-sensitive bootstrap (stdio fixup, the embedded
`open()` patch, banner-mode) and the pywebview window creation. The Flask server,
routes, callbacks, agent run, and helpers live in frontend/service.py. The
foundational symbols (`debug_log`, `debug_exception`, `IS_COMPILED`,
`app_data_dir`, …) are re-exported below so the existing `from app import …`
consumers (coder, minions, controller, telegram) keep working unchanged.

For binary builds, see windows_binary_build.py / mac_binary_build.py — both use
this file as the single Nuitka entry point (Nuitka follows imports, so service.py
compiles in automatically).
"""

import sys
import io
import os
import time
import threading
import importlib

import webview

# The backend lives in frontend/service.py. Importing it defines the env flags +
# debug logger + paths and builds the Flask app. service.py never imports `app`,
# so there is no circular import and no second copy of this module.
from frontend import service
from frontend.service import (
    IS_MAC, IS_WINDOWS, IS_COMPILED, PLATFORM_PKG, IS_SECONDARY_PROCESS,
    BUILD_STAMP, MACOS_BUNDLE_ID, DEBUG_LOG_PATH,
    debug_log, debug_exception, app_data_dir,
    get_auto_use_path, get_platform_use_path, get_frontend_path,
)

# =============================================================================
# Order-sensitive bootstrap (runs at import time — keep the original ordering)
# =============================================================================

# -----------------------------------------------------------------------------
# Banner subprocess stdio reconnection (MUST run before the std-fixup below)
# -----------------------------------------------------------------------------
# When AutoUse.exe is re-exec'd as a banner subprocess via --banner-mode, the
# parent's subprocess.Popen wires fd 0/1 to the pipes it drives the wizard with.
# But the Windows GUI-subsystem binary sets sys.stdin/stdout to None even though
# the OS-level fds are valid pipe handles inherited from the parent. Wrap those
# fds here BEFORE the `if sys.stdout is None` block replaces stdin/stdout with
# /dev/null and severs the JSON-stdio protocol with the parent.
if "--banner-mode" in sys.argv:
    try:
        sys.stdin = os.fdopen(0, "r", encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        # buffering=1 → line-buffered, so each JSON line reaches the parent at once.
        sys.stdout = os.fdopen(1, "w", encoding="utf-8", errors="replace", buffering=1)
    except Exception:
        pass
    if sys.stderr is None:
        try:
            sys.stderr = os.fdopen(2, "w", encoding="utf-8", errors="replace", buffering=1)
        except Exception:
            try:
                sys.stderr = open(os.devnull, "w", encoding="utf-8")
            except Exception:
                pass

# -----------------------------------------------------------------------------
# Fix for bundled app (MUST be before any print statements).
# Skip when run from main.py / cli.py so terminal output is not buffered. Also
# skip in --banner-mode: the subprocess already wired its stdio above.
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Embedded resource loader (Nuitka) — patch open() before any runtime file read.
# -----------------------------------------------------------------------------
if IS_COMPILED:
    service.setup_embedded_resources()

# =============================================================================
# Native window styling + centering (the "critical bit" — stays in app.py)
# =============================================================================

# Soft off-white with a hint of grey (light greige), used to tint the native
# title bar on BOTH macOS and Windows. KEEP IN SYNC with the app/splash
# background in frontend (style.css `body` + `.splash-overlay`, and the two
# *_animation.html bodies) so the bar and content read as one unified surface.
TITLEBAR_COLOR = "#D0CFC9"


def _style_macos_titlebar():
    """Tint the native macOS window titlebar off-white instead of stark white.

    The bright bar at the top of the window is the native NSWindow titlebar
    (pywebview exposes it as `window.native`). We make it transparent so the
    window's own background color shows through, set to a soft off-white. macOS-only.
    """
    win = service.get_window()
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
            # Pin the titlebar to Aqua so title text + traffic lights stay dark on
            # the off-white bar even in Dark Mode.
            nswindow.setAppearance_(
                NSAppearance.appearanceNamed_(NSAppearanceNameAqua)
            )
            # pywebview pins the titlebar's own background view to white at window
            # creation; recolor that exact view (NSTitlebarContainerView).
            try:
                titlebar_view = nswindow.contentView().superview().subviews().lastObject()
                titlebar_view.setBackgroundColor_(color)
            except Exception:
                debug_exception("titlebar_view_color")
        except Exception:
            debug_exception("titlebar_color_apply")

    # `shown` fires on a pywebview worker thread, but AppKit mutations must run on
    # the main thread — marshal the work over.
    AppHelper.callAfter(apply)


def _style_windows_titlebar():
    """Tint the native Windows title bar (caption) to the same off-white as macOS.

    Windows 11 (build 22000+) exposes DWM attributes to set the caption background
    and title-text colors; on Windows 10 these are unsupported and the calls
    simply no-op. Mirrors `_style_macos_titlebar`.
    """
    win = service.get_window()
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

        # Force light-mode caption (dark glyphs) so they stay readable on the
        # off-white bar even in Dark Mode, then set the caption + title-text colors.
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


def minimize_main_window():
    """Minimise the AutoUse pywebview window. No-op if the window isn't up yet or
    pywebview's minimise call fails. Safe to call from any thread."""
    win = service.get_window()
    if win is None:
        return
    try:
        win.minimize()
    except Exception:
        debug_exception("minimize_main_window")


# =============================================================================
# main()
# =============================================================================
def main():
    # --banner-mode / --cli-mode / --minion-mode are handled first — these
    # re-exec'd children must not boot a second webview / Flask / Telegram bot.
    if "--banner-mode" in sys.argv and IS_WINDOWS:
        # In dev the banner spawns via `python -m …banner`, but the Nuitka binary
        # has no `-m`, so StatusBanner.show() re-execs AutoUse.exe with this flag.
        sys.argv.remove("--banner-mode")
        try:
            from Auto_Use.windows_use.remote_connection.banner import _run_subprocess_banner
            _run_subprocess_banner()
        except Exception:
            debug_exception("Banner mode")
        return

    # Wire the Telegram remote-control bot. Windows mounts a Flask blueprint plus a
    # polling bot; macOS just starts the polling bot. Only the real GUI process
    # runs it — the cli/minion children dispatch + return below.
    if not IS_SECONDARY_PROCESS:
        if IS_WINDOWS:
            try:
                from Auto_Use.windows_use.remote_connection.telegram.view import telegram_bp, start_bot
                service.app.register_blueprint(telegram_bp)
                start_bot()
            except Exception:
                debug_exception("telegram_blueprint_init")
        elif IS_MAC:
            try:
                from Auto_Use.macOS_use.remote_connection.telegram.view import telegram_bp
                from Auto_Use.macOS_use.remote_connection.telegram.service import start_bot as start_telegram_bot
                service.app.register_blueprint(telegram_bp)
                start_telegram_bot()
            except Exception as _tg_e:
                import traceback as _tg_tb
                print(f"[telegram] IMPORT/INIT FAILED: {_tg_e!r}", file=sys.stderr, flush=True)
                _tg_tb.print_exc(file=sys.stderr)
                debug_exception("telegram_bot_init")

    if "--cli-mode" in sys.argv:
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
        # Required from the compiled binary, where the controller re-execs AutoUse
        # with --minion-mode instead of `python -m ...minions`.
        sys.argv.remove("--minion-mode")
        try:
            minion_main = importlib.import_module(
                f"Auto_Use.{PLATFORM_PKG}.agent.minions.__main__"
            ).main
            minion_main()
        except Exception:
            debug_exception("Minion mode")
        return

    # Startup sequence (no-ops where not applicable).
    service.clean_scratchpad()
    service.set_frontend_flag()
    # Clear orphaned macOS TCC "ghost" entries from a previous build (once per
    # build identity; no-op on Windows / dev).
    service.repair_stale_tcc_entries()
    # macOS permissions are now driven one-by-one by the setup wizard (served at
    # /setup and gated via the initial window URL below), which also auto-repairs
    # stale ghost entries per-permission. The old bulk request_macos_permissions()
    # shotgun is intentionally NOT called here anymore.

    # Start Flask in a daemon thread, then wait until it's actually ready.
    t = threading.Thread(target=service.start_server)
    t.daemon = True
    t.start()

    import urllib.request
    for _ in range(40):  # up to ~10 seconds
        try:
            urllib.request.urlopen('http://127.0.0.1:5000', timeout=0.5)
            break
        except Exception:
            time.sleep(0.25)

    # Create the webview window. 1140 = ~900 content + the 240px left bar (see
    # --left-bar-w in frontend/css/style.css). Don't pass x/y: pywebview's Edge
    # backend double-scales them on HiDPI; omitting position uses CenterScreen.
    win_w, win_h = 1140, 700
    # Gate the main app behind the permission setup wizard: if any required macOS
    # permission is missing, open /setup instead of / (the wizard navigates to /
    # once everything is granted). No-op on Windows / when all are granted.
    start_path = '/setup' if (IS_MAC and not service.all_permissions_granted()) else '/'
    win = webview.create_window(
        'Auto use',
        f'http://127.0.0.1:5000{start_path}',
        width=win_w,
        height=win_h,
    )
    service.set_window(win)

    # Dismiss any floating helper banner the INSTANT the user closes the app.
    # Return None (NOT False — False would cancel the close). Windows-only.
    if IS_WINDOWS:
        try:
            import atexit
            from Auto_Use.windows_use.remote_connection.banner import close_all_banners

            def _on_app_closing():
                close_all_banners()

            win.events.closing += _on_app_closing
            atexit.register(close_all_banners)  # backstop for teardown paths that skip `closing`
        except Exception:
            debug_exception("banner_close_hook")

    # Recolor the native titlebar once the native window/handle exists (`shown`).
    if IS_MAC:
        try:
            win.events.shown += _style_macos_titlebar
        except Exception:
            debug_exception("titlebar_color_hook")
    elif IS_WINDOWS:
        try:
            win.events.shown += _style_windows_titlebar
        except Exception:
            debug_exception("titlebar_color_hook_win")

    # macOS needs pynput keyboard pre-initialized on the main thread (Carbon
    # APIs require the main dispatch queue).
    if IS_MAC:
        try:
            from Auto_Use.macOS_use.controller.hotkey.service import _get_keyboard
            _get_keyboard()
        except Exception:
            pass

    webview.start()

    # If the setup wizard (Restart-to-finish) or the "Reset everything" action
    # asked for a relaunch, do it now that the GUI loop has fully exited — main
    # thread, with port 5000 released — so cached TCC preflights re-evaluate.
    if getattr(service, '_relaunch_requested', False):
        service.relaunch_app()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        debug_exception("main entry point")
        raise
