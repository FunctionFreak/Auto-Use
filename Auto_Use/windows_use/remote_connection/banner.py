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

"""Universal floating pill/orb banner — surface-agnostic.

This module lives OUTSIDE any single remote-connection surface
(telegram/, discord/, whatsapp/) on purpose: the pill + orb are the
*visual engine*, written once and reused by every surface. The
surface-specific behaviour (what text to show, the setup wizard steps,
the agent stream) lives in each surface's own folder and drives this
banner over the JSON-over-stdio protocol below. Telegram is the first
caller; Discord/WhatsApp can import `StatusBanner` unchanged.

Like the old telegram banner, this module plays two roles in one file:

  1. **Imported** (from a surface's setup.py / service.py) — exposes the
     `StatusBanner` class. Side-effect-free: PySide6 is NOT imported at
     module load, only inside `_run_subprocess_banner`, which the parent
     never calls.

  2. **Run as `python -m …banner`** (spawned by `StatusBanner.show()`
     via `subprocess.Popen`, or re-exec'd as `AutoUse.exe --banner-mode`
     in the compiled build) — falls through `if __name__ == "__main__"`
     into `_run_subprocess_banner`, which boots a PySide6
     `QWebEngineView` hosting the pill HTML and parks on `app.exec()`.
     Reads JSON commands from stdin, emits JSON events on stdout.

Why a subprocess? Running a second webview window inside the already-
running AutoUse process landed the pill off-screen on DPI-scaled
displays; a fresh interpreter (the subprocess) dodges that confusion.

Why Qt WebEngine (not pywebview)? pywebview cannot create a transparent
window on Windows, so the previous banner fell back to Win32 layered
windows + Pillow. Qt's WebEngine CAN host a transparent page in a
frameless translucent window, so the pill's smooth rounded edges and the
animated CSS orb render straight against the desktop — live HTML/CSS,
the same model macOS uses with its WKWebView.

Wire protocol (one JSON message per line):

  → stdin   {"cmd": "MSG"|"SHOW_NEXT"|"HIDE_NEXT"|"SHOW_CHOICE"|
                    "SHOW_INPUT"|"CLEAR"|"CLOSE", ...}
  ← stdout  {"event": "READY"|"NEXT"|"CHOICE"|"SAVE"|"CLOSED", ...}
"""
import ctypes
import datetime
import json
import logging
import os
import subprocess
import sys
import threading
from queue import Queue, Empty

logger = logging.getLogger(__name__)

# True when this module is running inside the Nuitka-compiled AutoUse.exe
# (i.e. sys.executable is the exe, not a Python interpreter). In that case
# `python -m …banner` is meaningless — the binary has no -m loader — so
# StatusBanner.show() must re-exec AutoUse.exe with --banner-mode, which
# app.py's main() picks up and routes to _run_subprocess_banner() directly.
_IS_COMPILED = getattr(sys, "frozen", False) or "__compiled__" in globals()


# ── Pill geometry ─────────────────────────────────────────────────────────
# Setup pill: a fixed-WIDTH white pill (orb on the left cap + streaming wizard
# text + controls). Its HEIGHT is dynamic — the message wraps to multiple
# lines and the window is animated taller from Python (see _animate_height)
# so the pill grows smoothly DOWNWARD and the whole message stays visible.
SETUP_WIN_W = 440
SETUP_MIN_H = 56    # single-line height (orb + vertical padding)
SETUP_MAX_H = 170   # cap — an unusually long message clips rather than fill the screen
# Compact task pill: starts as a 50×50 white circle (orb only) at the top-
# right, then grows LEFT into a stadium as the agent streams text. The
# window is a fixed COMPACT_WIN_W×COMPACT_WIN_H transparent canvas anchored
# top-right; the pill itself is right-anchored inside it and the CSS width
# transition does the circle⇄stadium animation (the window never resizes —
# it's click-through, so the empty transparent area is harmless).
COMPACT_WIN_W = 580
COMPACT_WIN_H = 50
SCREEN_MARGIN = 20

# Win32 extended-window-style constants for click-through (compact pill).
# WS_EX_TRANSPARENT removes the window from mouse hit-testing → clicks fall
# through to whatever app is behind the pill; the pill is a passive cue.
GWL_EXSTYLE       = -20
WS_EX_LAYERED     = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE  = 0x08000000
WS_EX_TOOLWINDOW  = 0x00000080


# ── active-banner registry (parent-side) ──────────────────────────────────
# Every live StatusBanner (one whose subprocess is running) registers itself
# here so the app can dismiss them all the instant the user closes the main
# window — see close_all_banners(), wired to pywebview's `closing` event +
# atexit in app.py. Without this, a banner whose owning thread is a daemon
# (e.g. the Telegram setup wizard) only learns to quit via stdin-EOF, which
# fires only AFTER the parent's slow webview/Qt teardown — leaving the pill
# painted on screen for a few seconds.
_ACTIVE_BANNERS: "set[StatusBanner]" = set()
_ACTIVE_BANNERS_LOCK = threading.Lock()


def close_all_banners() -> None:
    """Close every live banner now. Safe to call from any thread and more
    than once (StatusBanner.close() is idempotent). Snapshot under the lock so
    a banner deregistering mid-iteration can't mutate the set we're walking."""
    with _ACTIVE_BANNERS_LOCK:
        banners = list(_ACTIVE_BANNERS)
    for banner in banners:
        try:
            banner.close()
        except Exception:
            pass


# ── stdio helpers (parent + subprocess) ───────────────────────────────────

def _stderr(msg: str) -> None:
    """Loud print to whichever stderr we're attached to. Used both by
    the parent (for `[banner] spawned subprocess pid=…` etc.) and by
    the subprocess (which inherits the parent's stderr so the messages
    land in the same terminal)."""
    print(f"[banner] {msg}", file=sys.stderr, flush=True)


def _emit(event: str, **kwargs) -> None:
    """Subprocess → parent: write a JSON event to stdout (one line)."""
    try:
        payload = {"event": event, **kwargs}
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _log(msg: str) -> None:
    """File-based event log for the subprocess, at
    %LOCALAPPDATA%\\AutoUse\\banner_debug.log so it survives whatever happens
    to the subprocess's stdio. Best effort: any failure to write is
    swallowed."""
    try:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "AutoUse", "banner_debug.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                f"[{datetime.datetime.now().isoformat()}] pid={os.getpid()} {msg}\n"
            )
    except Exception:
        pass


def _js_escape(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "")
    )


# ── shared orb (pure CSS — the default AutoUse orb) ───────────────────────
# A soft lavender (#9292d8) disc with a bright inset white rim-glow, fixed
# pink (top-right) / blue (bottom-left) accents, and a white icon that
# cross-fades between a PC monitor (with blinking eyes) and the Telegram
# plane. This is the project's standard orb — identical to the frontend's
# .stop-agent-button and the macOS pill. Reused by both pills below.
#
# `.stop-agent-button` positioning differs per pill (flex child in setup,
# absolutely pinned to the pill's left cap in compact), so it is set in each
# document; everything below is shared and inlined verbatim into both.
_ORB_CSS = r"""
  .stop-orb { position: relative; width: 100%; height: 100%;
    display: flex; align-items: center; justify-content: center; pointer-events: none; }
  .stop-circle-1 { width: 42px; height: 42px; border-radius: 50%; position: absolute;
    background: transparent; animation: stop-pulse 4.2s ease-in-out infinite 0.3s; z-index: 1; }
  .stop-circle-1::before, .stop-circle-1::after {
    content: ""; position: absolute; border-radius: 50%; filter: blur(8px); width: 30%; height: 30%; }
  .stop-circle-1::before { background: #ff0073; top: 30%; right: 30%; }
  .stop-circle-1::after  { background: #00baff; bottom: 10%; left: 30%; }
  .stop-circle-2 { width: 32px; height: 32px; border-radius: 50%; position: absolute;
    inset: 0; margin: auto; background-color: white; z-index: 9;
    animation: stop-pulse2 4.2s ease-in-out infinite; }
  .stop-bg { position: absolute; inset: 0; border-radius: 50%;
    box-shadow: inset 0 0 5px 2px rgba(255,255,255,0.8), 0 0 2px 2px rgba(255,255,255,0.9);
    background-color: #9292d8; animation: stop-bgRotate 2.5s linear infinite; }
  .stop-bg::before { content: ""; position: absolute; inset: 0; border-radius: inherit;
    animation: stop-bgColor 4s linear infinite;
    box-shadow: inset 0 0 5px 2px rgba(255,255,255,0.8); opacity: 0.2; }
  .icon-stack { position: absolute; inset: 0; margin: auto;
    width: 32px; height: 32px; z-index: 10;
    display: flex; align-items: center; justify-content: center; }
  .icon-layer { position: absolute; inset: 0;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 1px; box-sizing: border-box;
    will-change: opacity; transform: translateZ(0); backface-visibility: hidden; }
  .icon-pc { animation: icon-cycle-pc 6s ease-in-out infinite; }
  .icon-tg { animation: icon-cycle-tg 6s ease-in-out infinite; color: white; }
  .stop-monitor { width: 12px; height: 10px; border: 1px solid white; box-sizing: border-box; }
  /* Even, integer layout (2px eye + 2px gap + 2px eye = 6px in the 10px screen
     → 2px clearance each side). The clearance keeps both eyes off the monitor
     border even when the expanded/streaming pill renders the orb at a slightly
     different sub-pixel position than the idle circle (otherwise the tighter
     right eye rounds into the border and looks like it's touching it). */
  .stop-screen { width: 100%; height: 100%; display: flex;
    justify-content: center; align-items: center; gap: 2px; }
  .stop-eye { width: 2px; height: 3px; border-radius: 1px; background: white;
    position: relative; top: -1px;
    animation: stop-blink 4s infinite; }
  .stop-base { width: 16px; height: 1px; background: white; border-radius: 0.5px; }
  @keyframes stop-pulse  { 0%{transform:scale(.97)} 15%{transform:scale(1)} 30%{transform:scale(.98)} 45%{transform:scale(1)} 60%{transform:scale(.97)} 85%{transform:scale(1)} 100%{transform:scale(.97)} }
  @keyframes stop-pulse2 { 0%{transform:scale(1)} 15%{transform:scale(1.03)} 30%{transform:scale(.98)} 45%{transform:scale(1.04)} 60%{transform:scale(.97)} 85%{transform:scale(1.03)} 100%{transform:scale(1)} }
  @keyframes stop-bgRotate { 0%{transform:rotate(0)} 20%{transform:rotate(90deg)} 40%{transform:rotate(180deg) scale(.95,1)} 60%,100%{transform:rotate(360deg)} }
  @keyframes stop-bgColor  { 20%{background-color:red} 40%{background-color:#5eff7e} 60%{background-color:#2cb5ff} 80%{background-color:#fc63ff} }
  @keyframes stop-blink    { 0%,85%,100%{transform:scaleY(1)} 92%{transform:scaleY(.1)} }
  @keyframes icon-cycle-pc { 0%, 40% { opacity: 1 } 50%, 90% { opacity: 0 } 100% { opacity: 1 } }
  @keyframes icon-cycle-tg { 0%, 40% { opacity: 0 } 50%, 90% { opacity: 1 } 100% { opacity: 0 } }
"""

_ORB_MARKUP = r"""
  <div class="stop-agent-button">
    <div class="stop-orb">
      <div class="stop-circle-1"></div>
      <div class="stop-circle-2"><div class="stop-bg"></div></div>
      <div class="icon-stack">
        <div class="icon-layer icon-pc">
          <div class="stop-monitor"><div class="stop-screen"><div class="stop-eye"></div><div class="stop-eye"></div></div></div>
          <div class="stop-base"></div>
        </div>
        <div class="icon-layer icon-tg">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M21.5 4.5L2.5 12l5.5 2 2 6 3-3.5 5.5 4 3-16zM10 14l8.5-7L11 14.5l-1 4.5L10 14z"/></svg>
        </div>
      </div>
    </div>
  </div>
"""

# The QWebChannel bootstrap (test.py pattern): the qwebchannel.js shim is
# served by Qt at qrc:///, so the page must be loaded with QUrl("qrc:///")
# as its base for this <script> to resolve. window.bridge gets the exposed
# Bridge QObject; every JS→Python call guards on `window.bridge` because the
# channel connects a frame or two after the inline scripts run.
_CHANNEL_BOOT = r"""
  <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
"""
_CHANNEL_CONNECT = r"""
    if (window.qt && qt.webChannelTransport) {
      new QWebChannel(qt.webChannelTransport, function (channel) {
        window.bridge = channel.objects.bridge;
      });
    }
"""


# ── HTML: interactive setup pill (orb + streaming text + Next/Choice/Input) ─
BANNER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
""" + _CHANNEL_BOOT + r"""
<style>
  html, body {
    margin: 0; padding: 0; height: 100%; width: 100%;
    background: transparent; overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    -webkit-user-select: none; user-select: none;
  }
  /* The white pill FILLS the window (so its rounded corners always match the
     window edge); the window's height is animated from Python to fit the
     content, so the pill grows downward as the message wraps. A fixed
     border-radius keeps a grown pill a clean rounded-rectangle, not an oval. */
  .banner {
    width: 100%; height: 100%; background: #ffffff; border-radius: 28px;
    overflow: hidden; box-sizing: border-box;
  }
  /* Natural-height content row (orb + text/controls). align-items:flex-start
     TOP-anchors the content so the first line keeps its position when the
     message wraps or the Next button drops to a second line — extra lines grow
     DOWNWARD instead of re-centring the whole block upward. .body's padding-top
     drops that first line onto the orb's axis for the common single-line case. */
  .measure {
    display: flex; align-items: flex-start; gap: 9px;
    padding: 6px 18px 8px 7px; box-sizing: border-box;
  }
  /* Orb slot reserves the 42px the layout expects; the actual orb is the
     pc_button.html file embedded in an iframe (single source of truth). The
     iframe is a touch bigger and centred so the orb's glow isn't clipped, and
     click-through so it never eats the wizard's button clicks. */
  .stop-agent-button { position: relative; flex-shrink: 0;
    width: 42px; height: 42px; background: transparent; }
  .orb-frame { position: absolute; top: 50%; left: 50%;
    width: 50px; height: 50px; transform: translate(-50%, -50%);
    border: 0; background: transparent; pointer-events: none; }

  /* Text + controls flow inline and WRAP. padding-top drops the first line onto
     the orb's axis (matching the old centred single-line look); because the row
     is now top-anchored (see .measure), wrapped/extra lines grow downward and
     the first line never moves — so the prompt sits on line 1 and the choice
     buttons / token field flow onto line 2 below it. */
  .body { flex: 1; min-width: 0; line-height: 20px; padding-top: 11px; }
  .banner-text { color: #333333; font-size: 13px; font-weight: 600;
    line-height: 20px; overflow-wrap: break-word; }
  /* Purple pill button, sized to match the macOS banner (#5e6ad2, ~28px tall)
     so both surfaces look identical. Inline after the wizard line; if a long
     line leaves no trailing room it simply wraps below and the pill grows. */
  .next-btn {
    display: inline-block; vertical-align: middle; margin-left: 6px;
    background: #5e6ad2; color: #ffffff; border: none; font-family: inherit;
    font-size: 12px; font-weight: 600; padding: 4px 14px; border-radius: 999px;
    cursor: pointer; transition: background 0.15s ease;
  }
  .next-btn:hover  { background: #6e7ce3; }
  .next-btn:active { background: #4e5ac2; }
  .choice-row { display: none; }
  .choice-row .next-btn { margin-left: 0; margin-right: 6px; }
  .input-row { display: none; align-items: center; gap: 6px; }
  #token-input {
    flex: 1; height: 28px; border: 1px solid #d1d5db; border-radius: 14px;
    padding: 0 12px; font-size: 12px; font-family: inherit; color: #374151;
    background: #ffffff; outline: none;
  }
  #token-input:focus { border-color: #5e6ad2; }
</style>
</head>
<body>
  <div class="banner">
    <div class="measure">
      <div class="stop-agent-button">
        <iframe id="orbFrame" class="orb-frame" src="http://127.0.0.1:5000/pc_button.html"
                scrolling="no" frameborder="0"></iframe>
      </div>
      <div class="body">
        <span class="banner-text" id="msg">Starting…</span><button class="next-btn" id="next" style="display:none"
              onclick="if(window.bridge) bridge.next_clicked()">Next</button>
        <div class="choice-row" id="choice-row">
          <button class="next-btn" id="choice-left"
                  onclick="if(window.bridge) bridge.choice_clicked('left')">Left</button>
          <button class="next-btn" id="choice-right"
                  onclick="if(window.bridge) bridge.choice_clicked('right')">Right</button>
        </div>
        <div class="input-row" id="input-row">
          <input type="text" id="token-input" placeholder="Paste your BotFather token here" />
          <button class="next-btn" id="save-btn"
                  onclick="(function(){var v=document.getElementById('token-input').value;
                           if(window.bridge) bridge.save_clicked(v);})()">Save</button>
        </div>
      </div>
    </div>
  </div>

  <script>
""" + _CHANNEL_CONNECT + r"""
    // The orb is pc_button.html in an iframe; it starts hidden and shows when
    // told, so nudge it visible once the iframe has loaded.
    var __orb = document.getElementById('orbFrame');
    if (__orb) __orb.addEventListener('load', function () {
      try { __orb.contentWindow.postMessage('pcbtn:show', '*'); } catch (e) {}
    });
    // Multi-line streaming pill. The message reveals letter-by-letter and
    // WRAPS naturally; as it grows past a line the pill expands downward (the
    // window height is animated from Python on the height the observer below
    // reports). No paging — the whole message stays visible.
    const _CHAR_DELAY_MS = 8;     // per-letter cadence — fast typewriter feel
    const _FADE_MS = 60;          // per-letter fade-in duration
    let _revealTimer = null;

    function setMsg(fullText) {
      if (_revealTimer) { clearTimeout(_revealTimer); _revealTimer = null; }
      const el = document.getElementById('msg');
      if (!el) return;
      const text = (fullText || '').toString();
      el.textContent = '';
      if (!text) return;

      const chars = Array.from(text);   // split by code point so emoji stay intact
      let i = 0;

      const step = () => {
        if (i >= chars.length) { _revealTimer = null; return; }
        const span = document.createElement('span');
        span.textContent = chars[i];
        span.style.opacity = '0';
        span.style.transition = 'opacity ' + _FADE_MS + 'ms ease-out';
        el.appendChild(span);
        requestAnimationFrame(() => { span.style.opacity = '1'; });
        i++;
        _revealTimer = setTimeout(step, _CHAR_DELAY_MS);
      };
      step();
    }
    function showNext()  {
      clearAll();
      document.getElementById('next').style.display = 'inline-block';
      document.getElementById('msg').style.display = 'inline';
      reportHitRects();
    }
    function hideNext() {
      document.getElementById('next').style.display = 'none';
      reportHitRects();
    }
    function setChoice(leftLabel, rightLabel) {
      // Keep the prompt (e.g. "How do you want to set up the bot?") visible
      // ABOVE the buttons, like the macOS banner — clearAll already shows msg.
      clearAll();
      document.getElementById('choice-left').textContent = leftLabel;
      document.getElementById('choice-right').textContent = rightLabel;
      document.getElementById('choice-row').style.display = 'block';
      reportHitRects();
    }
    function setInput(saveLabel) {
      // Keep the prompt (e.g. "Paste your BotFather token…") visible above the
      // field, like the macOS banner — clearAll already shows msg.
      clearAll();
      document.getElementById('save-btn').textContent = saveLabel || 'Save';
      document.getElementById('input-row').style.display = 'flex';
      var inp = document.getElementById('token-input');
      inp.value = '';
      setTimeout(function(){ inp.focus(); }, 30);
      reportHitRects();
    }
    function clearAll() {
      document.getElementById('next').style.display = 'none';
      document.getElementById('choice-row').style.display = 'none';
      document.getElementById('input-row').style.display = 'none';
      document.getElementById('msg').style.display = 'inline';
      reportHitRects();
    }
    // Report the window-local rects of the currently-VISIBLE interactive controls
    // so Python keeps the pill click-through everywhere except over them. CSS px
    // == window-local px (the view fills the frameless window with 0 margins).
    function reportHitRects() {
      if (!window.bridge) return;
      var rects = [];
      var inputVisible = false;
      function add(el) {
        if (!el) return;
        var r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
          rects.push({ x: r.left, y: r.top, w: r.width, h: r.height });
        }
      }
      var next = document.getElementById('next');
      if (next && next.style.display !== 'none') add(next);
      var choiceRow = document.getElementById('choice-row');
      if (choiceRow && choiceRow.style.display !== 'none') {
        add(document.getElementById('choice-left'));
        add(document.getElementById('choice-right'));
      }
      var inputRow = document.getElementById('input-row');
      if (inputRow && inputRow.style.display !== 'none') {
        inputVisible = true;
        add(document.getElementById('token-input'));
        add(document.getElementById('save-btn'));
      }
      try { bridge.set_hit_rects(JSON.stringify({ rects: rects, input: inputVisible })); } catch (e) {}
    }
    window.setMsg = setMsg;
    window.showNext = showNext;
    window.hideNext = hideNext;
    window.setChoice = setChoice;
    window.setInput = setInput;
    window.clearAll = clearAll;

    document.getElementById('token-input').addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && window.bridge) {
        bridge.save_clicked(this.value);
      }
    });

    // Report the content's natural height to Python whenever it changes (text
    // wraps as it streams, or controls toggle); Python animates the window to fit
    // so the pill grows/shrinks smoothly from the bottom. Also re-report the
    // control hit-rects each fire (they shift when the pill height changes).
    // Height is last-value debounced so identical reports don't churn.
    (function () {
      var last = -1;
      var m = document.querySelector('.measure');
      function report() {
        if (!window.bridge || !m) return;
        var h = Math.ceil(m.getBoundingClientRect().height);
        if (h !== last) {
          last = h;
          try { bridge.height_changed(h); } catch (e) {}
        }
        reportHitRects();
      }
      window.addEventListener('load', function () { setTimeout(report, 30); });
      try { var ro = new ResizeObserver(report); ro.observe(m); } catch (e) {}
    })();
  </script>
</body>
</html>
"""


# ── HTML: compact task pill (orb circle that expands into a streaming pill) ─
COMPACT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
""" + _CHANNEL_BOOT + r"""
<style>
  html, body {
    margin: 0; padding: 0; height: 100%; width: 100%;
    background: transparent; overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    -webkit-user-select: none; user-select: none;
  }
  /* PILL: right edge fixed to the window's right (top-right of screen);
     width animates 50px → 580px so it grows LEFT. Empty state = a 50×50
     circle (orb only); has-text = a stadium with streaming text. */
  .pill {
    position: absolute; right: 0; top: 0;
    height: 50px; width: 50px; border-radius: 25px;
    background: #ffffff; overflow: hidden;
    transition: width 0.42s cubic-bezier(.22,1,.36,1);
  }
  body.has-text .pill { width: 580px; }
  /* Orb pinned to the pill's left cap (centre = cap centre); it rides left
     as the pill grows. */
  .stop-agent-button { position: absolute; left: 4px; top: 4px;
    width: 42px; height: 42px; background: transparent; }
  .orb-frame { position: absolute; top: 50%; left: 50%;
    width: 50px; height: 50px; transform: translate(-50%, -50%);
    border: 0; background: transparent; pointer-events: none; }
  /* Vertically centred via top/translateY — NOT display:flex. With flex, each
     streamed per-character <span> becomes a flex item and a space-only item
     collapses to zero width, eating the spaces between words. Plain inline
     flow preserves spaces. */
  .msg {
    position: absolute; left: 56px; right: 16px; top: 50%;
    transform: translateY(-50%);
    font-size: 13px; color: #374151; line-height: 1.4;
    white-space: nowrap; overflow: hidden;
  }
</style>
</head>
<body>
  <div class="pill">
    <div class="stop-agent-button">
      <iframe class="orb-frame" src="http://127.0.0.1:5000/telegram/telergam_animation.html"
              scrolling="no" frameborder="0"></iframe>
    </div>
    <span class="msg" id="msg"></span>
  </div>

  <script>
""" + _CHANNEL_CONNECT + r"""
    // Stream `text` letter-by-letter into .msg, paging long lines. On the
    // first MSG of a task, body gets .has-text (the pill expands leftward via
    // CSS) and streaming waits out the expansion before any letter appears;
    // subsequent MSGs start immediately. When the final message finishes and
    // nothing new arrives, the pill collapses back to the circle.
    const _CHAR_DELAY_MS = 8;      // per-letter cadence
    const _FADE_MS = 60;           // per-letter fade-in duration
    const _PAGE_HOLD_MS = 400;     // full line lingers before paging
    const _DONE_HOLD_MS = 1400;    // idle hold before collapsing to the circle
    let _revealTimer = null;

    function setMsg(fullText) {
      if (_revealTimer) { clearTimeout(_revealTimer); _revealTimer = null; }
      const el = document.getElementById('msg');
      if (!el) return;
      const text = (fullText || '').toString();
      const wasEmpty = !document.body.classList.contains('has-text');
      document.body.classList.toggle('has-text', !!text);
      el.textContent = '';
      if (!text) return;

      const chars = Array.from(text);
      let i = 0;

      const streamChar = () => {
        if (i >= chars.length) {
          // Whole message shown → hold, then collapse back to the circle.
          _revealTimer = setTimeout(() => {
            el.textContent = '';
            document.body.classList.remove('has-text');
            _revealTimer = null;
          }, _DONE_HOLD_MS);
          return;
        }
        const span = document.createElement('span');
        span.textContent = chars[i];
        span.style.opacity = '0';
        span.style.transition = 'opacity ' + _FADE_MS + 'ms ease-out';
        el.appendChild(span);

        if (el.scrollWidth > el.clientWidth + 0.5) {
          if (el.children.length === 1) {
            requestAnimationFrame(() => { span.style.opacity = '1'; });
            i++;
            _revealTimer = setTimeout(streamChar, _CHAR_DELAY_MS);
            return;
          }
          el.removeChild(span);
          _revealTimer = setTimeout(() => {
            el.textContent = '';
            while (i < chars.length && /\s/.test(chars[i])) i++;
            streamChar();
          }, _PAGE_HOLD_MS);
          return;
        }
        requestAnimationFrame(() => { span.style.opacity = '1'; });
        i++;
        _revealTimer = setTimeout(streamChar, _CHAR_DELAY_MS);
      };

      const startDelay = wasEmpty ? 480 : 0;   // wait out the CSS expansion
      _revealTimer = setTimeout(streamChar, startDelay);
    }
    window.setMsg = setMsg;
  </script>
</body>
</html>
"""


# ── stdin reader thread (subprocess-side only) ───────────────────────────

def _stdin_reader(emit_js, on_close, on_focus=None) -> None:
    """Loop reading JSON commands from stdin and dispatching them.

    Runs on its own thread so it never blocks the Qt GUI loop. Each command
    is turned into a JS string and handed to `emit_js(code)` — a thread-safe
    Signal.emit that marshals the actual runJavaScript onto the Qt GUI
    thread (QtWebEngine JS calls MUST run there). CLOSE / stdin-EOF call
    `on_close()` (also marshaled) so the window tears down on the GUI thread.
    `on_focus()` (setup pill only) is fired on SHOW_INPUT so the token field
    grabs OS focus and typing/paste land without the user clicking first.
    """
    _log("stdin_reader: thread started")
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                _log(f"stdin_reader: skip unparseable line {line!r}")
                continue
            cmd = msg.get("cmd")
            _log(f"stdin_reader: cmd={cmd!r}")
            try:
                if cmd == "MSG":
                    esc = _js_escape(msg.get("text", ""))
                    emit_js(f"if(window.setMsg) setMsg('{esc}');")
                elif cmd == "SHOW_NEXT":
                    emit_js("if(window.showNext) showNext();")
                elif cmd == "HIDE_NEXT":
                    emit_js("if(window.hideNext) hideNext();")
                elif cmd == "SHOW_CHOICE":
                    left = _js_escape(msg.get("left", ""))
                    right = _js_escape(msg.get("right", ""))
                    emit_js(f"if(window.setChoice) setChoice('{left}', '{right}');")
                elif cmd == "SHOW_INPUT":
                    label = _js_escape(msg.get("label", "Save"))
                    emit_js(f"if(window.setInput) setInput('{label}');")
                    if on_focus is not None:
                        on_focus()
                elif cmd == "CLEAR":
                    emit_js("if(window.clearAll) clearAll();")
                elif cmd == "CLOSE":
                    _log("stdin_reader: CLOSE received, closing window")
                    on_close()
                    return
            except Exception:
                import traceback
                _log(
                    f"stdin_reader: cmd={cmd!r} dispatch raised:\n"
                    + traceback.format_exc()
                )
    except Exception:
        import traceback
        _log("stdin_reader: outer loop raised:\n" + traceback.format_exc())
    _log("stdin_reader: thread exiting (stdin EOF or pipe break)")
    # EOF / pipe break without a CLOSE means the parent process is gone (e.g.
    # the frontend UI was closed). Close the window so this subprocess exits
    # with it instead of lingering as an orphan tied to nothing.
    try:
        on_close()
    except Exception:
        pass


# ── subprocess entry point (PySide6 QWebEngineView pill) ──────────────────

def _run_subprocess_banner() -> None:
    """Subprocess body. Imports PySide6 lazily so the parent (which only uses
    StatusBanner) doesn't pay Qt's startup cost when it imports this module.

    Builds ONE QApplication and a single transparent frameless pill window
    hosting the setup or compact HTML, then parks on app.exec(). Reads JSON
    commands from stdin (via _stdin_reader) and emits JSON events on stdout.
    """
    _log(f"subprocess start (sys.executable={sys.executable!r})")
    try:
        from PySide6.QtCore import (
            Qt, QUrl, QObject, Slot, Signal, QVariantAnimation, QEasingCurve,
        )
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWebEngineCore import QWebEngineSettings
        from PySide6.QtWebChannel import QWebChannel
    except Exception:
        import traceback
        _log("banner: PySide6 import failed:\n" + traceback.format_exc())
        raise

    compact = "--compact" in sys.argv[1:]

    class Bridge(QObject):
        """Exposed to JS as window.bridge. Each slot maps a control event to a
        stdout event the parent's StatusBanner already consumes. QWebChannel
        delivers these on the GUI thread (the Bridge's thread), so height_changed
        can resize the window directly."""

        def __init__(self, win):
            super().__init__()
            self._win = win

        @Slot()
        def next_clicked(self):
            _emit("NEXT")

        @Slot(str)
        def choice_clicked(self, value=""):
            _emit("CHOICE", value=value or "left")

        @Slot(str)
        def save_clicked(self, value=""):
            _emit("SAVE", value=(value or "").strip())

        @Slot(int)
        def height_changed(self, h=0):
            # Setup pill only — grow/shrink the window to fit the content.
            try:
                self._win._animate_height(h)
            except Exception:
                pass

        @Slot(str)
        def set_hit_rects(self, payload=""):
            # Setup pill only — JS reports the window-local rects of the visible
            # interactive controls so the pill can be click-through everywhere
            # except over them. Runs on the GUI thread (QWebChannel slot).
            try:
                self._win._on_hit_rects(payload)
            except Exception:
                pass

    class _PillWindow(QWidget):
        """Transparent frameless top-right pill window hosting the HTML.

        run_js / do_close are cross-thread channels: the stdin reader runs on
        a worker thread but QtWebEngine JS and window teardown must happen on
        the GUI thread, so the reader emits these Signals (auto-queued onto
        the GUI event loop)."""

        run_js = Signal(str)
        do_close = Signal()
        do_focus = Signal()

        def __init__(self, compact: bool):
            super().__init__()
            self._compact = compact
            self._ready = False
            self._target_h = COMPACT_WIN_H if compact else SETUP_MIN_H
            self._anim = None
            # Setup-pill click-through state. The setup pill is click-through by
            # default and becomes interactive only while a wizard control is
            # visible (see _apply_click_through / _on_hit_rects).
            self._exstyle_state = None  # cached (transparent, noactivate) → skip redundant syscalls

            flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
            if compact:
                # Passive indicator: never clickable / focusable; clicks pass
                # straight through to whatever app is behind the pill.
                flags |= Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus
            self.setWindowFlags(flags)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            # Neither pill activates on show. The setup pill manages focus via the
            # Win32 WS_EX_NOACTIVATE ex-style (cleared only for the token-input
            # step), not a static Qt flag, so it never steals focus from Edge.
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            if compact:
                self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

            if compact:
                self.setFixedSize(COMPACT_WIN_W, COMPACT_WIN_H)
            else:
                # Width is fixed; height is dynamic (animated downward as the
                # message wraps — see _animate_height).
                self.setFixedWidth(SETUP_WIN_W)
                self.setFixedHeight(SETUP_MIN_H)
            self._move_to_top_right()

            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)

            self.view = QWebEngineView(self)
            self.view.setAttribute(Qt.WA_TranslucentBackground, True)
            if compact:
                self.view.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.view.setStyleSheet("background: transparent;")
            self.view.page().setBackgroundColor(QColor(0, 0, 0, 0))  # transparent page
            self.view.setContextMenuPolicy(Qt.NoContextMenu)
            # The pill page loads as local (qrc:///) content but embeds the orb
            # from http://127.0.0.1:5000 (pc_button.html / telergam_animation.html),
            # so let local content load that remote URL.
            _wa = (QWebEngineSettings.WebAttribute
                   if hasattr(QWebEngineSettings, "WebAttribute") else QWebEngineSettings)
            self.view.settings().setAttribute(_wa.LocalContentCanAccessRemoteUrls, True)
            layout.addWidget(self.view)

            self.channel = QWebChannel()
            self.bridge = Bridge(self)
            self.channel.registerObject("bridge", self.bridge)
            self.view.page().setWebChannel(self.channel)

            # cross-thread marshaling (worker thread → GUI thread)
            self.run_js.connect(self._on_run_js)
            self.do_close.connect(self._on_close)
            self.do_focus.connect(self._on_focus)
            # READY (and the stdin reader) wait for the page to finish loading
            # so window.setMsg / the bridge exist before the first command.
            self.view.loadFinished.connect(self._on_load_finished)

            html = COMPACT_HTML if compact else BANNER_HTML
            # qrc:/// base URL so the qwebchannel.js <script> resolves.
            self.view.setHtml(html, QUrl("qrc:///"))

        def _move_to_top_right(self):
            screen = QApplication.primaryScreen().availableGeometry()
            x = screen.right() - self.width() + 1 - SCREEN_MARGIN
            y = screen.top() + SCREEN_MARGIN
            self.move(x, y)

        def _animate_height(self, content_h):
            """Smoothly grow/shrink the setup pill to fit `content_h` px of
            content. Width and top-left stay fixed, so the pill expands
            downward. Runs on the GUI thread (called from the bridge)."""
            if self._compact:
                return
            target = max(SETUP_MIN_H, min(SETUP_MAX_H, int(content_h)))
            start = self.height()
            if target == self._target_h and target == start:
                return
            self._target_h = target
            if target == start:
                return
            if self._anim is not None:
                try:
                    self._anim.stop()
                except Exception:
                    pass
            anim = QVariantAnimation(self)
            anim.setDuration(180)
            anim.setStartValue(start)
            anim.setEndValue(target)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.valueChanged.connect(lambda v: self.setFixedHeight(int(v)))
            self._anim = anim
            anim.start()

        def showEvent(self, event):
            super().showEvent(event)
            # Re-pin (DPI / geometry may resolve only after show).
            self._move_to_top_right()
            if self._compact:
                self._make_click_through()
            else:
                # Setup pill: start click-through so it never blocks the agent's
                # clicks/keystrokes. It flips to interactive only while a wizard
                # control (Next / choice / token field) is visible — see
                # _on_hit_rects. No control is ever shown while the agent
                # automates Edge, so automation always passes through.
                self._apply_click_through(True)

        def _make_click_through(self):
            # Compact pill: permanently click-through + non-activating.
            self._apply_click_through(True)

        def _apply_click_through(self, transparent):
            """Set/clear WS_EX_TRANSPARENT (mouse pass-through) on the top-level
            HWND. WS_EX_LAYERED|TOOLWINDOW are always kept. WS_EX_NOACTIVATE is
            kept only WHILE click-through (text-only / agent automation) so we
            never steal focus then; when a control is visible the window may
            activate so clicks fully register and the token field can take
            keyboard focus. No-op when the ex-style state is unchanged."""
            if sys.platform != "win32":
                return
            noactivate = bool(transparent)
            state = (bool(transparent), noactivate)
            if state == self._exstyle_state:
                return
            try:
                user32 = ctypes.windll.user32
                hwnd = int(self.winId())
                ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                ex |= WS_EX_LAYERED | WS_EX_TOOLWINDOW
                ex = (ex | WS_EX_TRANSPARENT) if transparent else (ex & ~WS_EX_TRANSPARENT)
                ex = (ex | WS_EX_NOACTIVATE) if noactivate else (ex & ~WS_EX_NOACTIVATE)
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
                self._exstyle_state = state
            except Exception:
                pass

        def _on_hit_rects(self, payload):
            """JS → Python (GUI thread): whether any interactive control is
            currently visible. While ANY control (Next / choice / token field) is
            showing, the pill is interactive so the controls reliably register
            clicks; otherwise (streaming text only — which is the ONLY state
            while the agent automates Edge) it is fully click-through. No control
            is ever visible during automation, so going interactive then is safe."""
            try:
                data = json.loads(payload or "{}")
            except Exception:
                data = {}
            has_controls = len(data.get("rects") or []) > 0
            self._apply_click_through(not has_controls)
            if has_controls and data.get("input"):
                # Token-input step: pull focus so typing/paste land immediately,
                # without the user having to click the field first.
                try:
                    self.activateWindow()
                    self.raise_()
                except Exception:
                    pass

        @Slot(bool)
        def _on_load_finished(self, ok):
            if self._ready:
                return
            self._ready = True
            _emit("READY")
            _log(f"banner: READY (loadFinished ok={ok})")
            on_focus = None if self._compact else self.do_focus.emit
            threading.Thread(
                target=_stdin_reader,
                args=(self.run_js.emit, self.do_close.emit, on_focus),
                daemon=True,
            ).start()

        @Slot(str)
        def _on_run_js(self, code):
            try:
                self.view.page().runJavaScript(code)
            except Exception:
                pass

        @Slot()
        def _on_focus(self):
            # Setup pill only: pull the window to the foreground so the token
            # <input> receives keystrokes / paste without a click first.
            try:
                self.activateWindow()
                self.raise_()
            except Exception:
                pass

        @Slot()
        def _on_close(self):
            _log("banner: close requested")
            # Unmap the window THIS frame so it vanishes instantly, before the
            # (slightly slower) close()/app.quit()/Chromium teardown. Both the
            # CLOSE command and the stdin-EOF path converge here on the GUI
            # thread, so this covers every close trigger.
            try:
                self.hide()
            except Exception:
                pass
            try:
                self.close()
            finally:
                app = QApplication.instance()
                if app is not None:
                    app.quit()

    try:
        app = QApplication.instance() or QApplication(sys.argv)
        win = _PillWindow(compact)
        win.show()
        rc = app.exec()
        _log(f"banner: app.exec() returned rc={rc}")
    except Exception:
        import traceback
        _log("_run_subprocess_banner crashed:\n" + traceback.format_exc())
        raise

    _emit("CLOSED")
    _log("subprocess exit (CLOSED emitted)")


# ── parent-side wrapper ──────────────────────────────────────────────────


class StatusBanner:
    """Universal pill driver, backed by a subprocess that runs the Qt
    WebEngine pill independently. Surface-agnostic — Telegram/Discord/
    WhatsApp all use the same class and wire protocol."""

    # Module path the subprocess runs in dev mode (`python -m <module>`).
    # The compiled build re-execs AutoUse.exe --banner-mode instead (no -m).
    _PROC_MODULE = "Auto_Use.windows_use.remote_connection.banner"

    def __init__(self, compact: bool = False):
        self._compact = compact
        self._proc: subprocess.Popen | None = None
        self._stdout_thread: threading.Thread | None = None
        self._closed = threading.Event()
        self._ready = threading.Event()
        self._next_event = threading.Event()
        # Distinguishes a real NEXT click from a subprocess-close that also
        # has to unblock _next_event so waiters don't deadlock. Only the
        # "NEXT" stdout event flips this to True; close-cleanup leaves it
        # False so callers can tell the user dismissed the banner.
        self._next_clicked = False
        self._choice_q: Queue = Queue()
        self._input_q: Queue = Queue()

    # ── public API ───────────────────────────────────────────────────────

    def show(self) -> None:
        if self._proc is not None or self._closed.is_set():
            return

        # In the Nuitka build, sys.executable is AutoUse.exe — a compiled C
        # binary with no `-m` module loader. Running it with `-m …banner`
        # silently re-execs the whole AutoUse app (Flask + main webview +
        # bot), giving the user a second main window instead of the pill.
        # Re-exec AutoUse.exe with --banner-mode so app.py's main() can route
        # directly to _run_subprocess_banner. In dev (`python app.py`)
        # sys.executable IS a python interpreter, so the old -m invocation
        # still works and is preferred — it avoids the cost of bootstrapping
        # app.py just to reach the banner.
        # cwd: pin the subprocess to the binary's install dir in the compiled
        # build so native DLL loaders resolve from the install folder
        # regardless of what cwd the parent inherited. In dev mode cwd=None
        # inherits the parent's (the repo root).
        cwd = None
        if _IS_COMPILED:
            exe_dir = os.path.dirname(sys.executable)
            main_exe = os.path.join(exe_dir, "AutoUse.exe")
            args = [main_exe, "--banner-mode"]
            cwd = exe_dir
        else:
            args = [sys.executable, "-m", self._PROC_MODULE]
        if self._compact:
            args.append("--compact")

        try:
            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # stderr is left attached so the subprocess can write
                # diagnostics to our terminal (useful for debugging,
                # never gets parsed).
                stderr=None,
                text=True,
                bufsize=1,  # line-buffered
                cwd=cwd,
                # On Windows, hide the extra console window subprocess
                # would otherwise spawn.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            _stderr(
                f"spawned banner subprocess pid={self._proc.pid} "
                f"compact={self._compact}"
            )
        except Exception as e:
            _stderr(f"banner subprocess spawn failed: {e!r}")
            self._proc = None
            return

        # Register so close_all_banners() (app-window-close hook / atexit) can
        # dismiss this pill the instant the user closes the app, instead of it
        # lingering until stdin-EOF fires after the parent's slow teardown.
        with _ACTIVE_BANNERS_LOCK:
            _ACTIVE_BANNERS.add(self)

        self._stdout_thread = threading.Thread(
            target=self._stdout_reader,
            daemon=True,
            name="banner-stdout-reader",
        )
        self._stdout_thread.start()

        # Block until the subprocess emits READY (banner is visible).
        # 15 s ceiling covers a cold Python interpreter start; under
        # normal conditions READY arrives in well under a second.
        if not self._ready.wait(timeout=15):
            _stderr("banner subprocess never emitted READY")

    def update(self, text: str) -> None:
        # Both modes accept MSG — the compact pill renders the thinking-stream
        # text in its msg span and grows to fit; the setup pill pages it.
        self._send({"cmd": "MSG", "text": text or ""})

    def wait_for_next(self, timeout: float | None = None) -> bool:
        if self._compact:
            return True
        if self._proc is None:
            return True
        # Banner already dismissed (subprocess gone) — don't pretend the
        # user clicked Next. Callers use the False return to short-circuit
        # the wizard instead of opening Edge / advancing steps.
        if self._closed.is_set():
            return False
        self._next_clicked = False
        self._next_event.clear()
        self._send({"cmd": "SHOW_NEXT"})
        self._next_event.wait(timeout=timeout)
        self._send({"cmd": "HIDE_NEXT"})
        return self._next_clicked

    def wait_for_choice(
        self, left_label: str, right_label: str, timeout=None
    ):
        if self._compact or self._proc is None:
            return None
        self._drain(self._choice_q)
        self._send({
            "cmd": "SHOW_CHOICE",
            "left": left_label,
            "right": right_label,
        })
        try:
            value = self._choice_q.get(timeout=timeout if timeout else 600)
        except Empty:
            value = None
        self._send({"cmd": "CLEAR"})
        return value

    def wait_for_input(self, save_label: str = "Save"):
        if self._compact or self._proc is None:
            return None
        self._drain(self._input_q)
        self._send({"cmd": "SHOW_INPUT", "label": save_label})
        try:
            value = self._input_q.get(timeout=600)
        except Empty:
            value = None
        self._send({"cmd": "CLEAR"})
        return value

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with _ACTIVE_BANNERS_LOCK:
            _ACTIVE_BANNERS.discard(self)
        # Unblock anything still parked on a Queue/Event before we tear
        # the subprocess down.
        self._next_event.set()
        try:
            self._choice_q.put_nowait(None)
        except Exception:
            pass
        try:
            self._input_q.put_nowait(None)
        except Exception:
            pass

        if self._proc is None:
            return

        # Ask the subprocess to close gracefully (it hides the window this
        # frame — see _PillWindow._on_close), then reap the process OFF this
        # thread. close() is called from pywebview's `closing` event, which
        # runs handlers synchronously and blocks the window teardown, so it
        # must return immediately; the wait/terminate/kill escalation runs on
        # a daemon reaper instead of stalling shutdown for up to 3 s.
        self._send({"cmd": "CLOSE"})
        proc = self._proc
        self._proc = None  # detach so a racing _send / double-close can't touch it

        def _reap(p):
            try:
                p.wait(timeout=3)
            except Exception:
                try:
                    p.terminate()
                    p.wait(timeout=2)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass

        threading.Thread(
            target=_reap, args=(proc,), daemon=True, name="banner-reaper"
        ).start()

    # ── internals ────────────────────────────────────────────────────────

    def _send(self, msg: dict) -> None:
        """Write a JSON command to the subprocess stdin. Silent on
        broken-pipe errors so a dead subprocess doesn't crash callers."""
        if self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
        except Exception:
            pass

    def _stdout_reader(self) -> None:
        """Read JSON events from the subprocess and route to local
        Event / Queue primitives so wait_for_* unblock at the right time."""
        if self._proc is None or self._proc.stdout is None:
            return
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            event = msg.get("event")
            if event == "READY":
                _stderr("banner subprocess READY — pill visible")
                self._ready.set()
            elif event == "NEXT":
                # Flag must be set BEFORE the Event so a waiter that wakes
                # on _next_event.wait() reads the True value, not the
                # default False left by the close-cleanup path below.
                self._next_clicked = True
                self._next_event.set()
            elif event == "CHOICE":
                self._choice_q.put(msg.get("value", "left"))
            elif event == "SAVE":
                self._input_q.put(msg.get("value", ""))
            elif event == "CLOSED":
                _stderr("banner subprocess CLOSED")
                break

        # Subprocess exited (whether via CLOSED or pipe break). Unblock
        # any pending waiters so callers don't deadlock, and drop ourselves
        # from the active registry (the proc is gone — nothing left to close).
        self._closed.set()
        with _ACTIVE_BANNERS_LOCK:
            _ACTIVE_BANNERS.discard(self)
        self._ready.set()
        self._next_event.set()
        try:
            self._choice_q.put_nowait(None)
        except Exception:
            pass
        try:
            self._input_q.put_nowait(None)
        except Exception:
            pass

    @staticmethod
    def _drain(q: Queue) -> None:
        try:
            while True:
                q.get_nowait()
        except Empty:
            pass


# ── module entry: run as subprocess if invoked via `python -m …banner` ──

if __name__ == "__main__":
    _run_subprocess_banner()
