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

"""Banner — both the StatusBanner wrapper used by callers AND the
subprocess that hosts the pywebview pill.

The same module is invoked two ways:

  1. **Imported** from setup.py / service.py — exposes the
     `StatusBanner` class that drives the wizard. Side-effect-free:
     pywebview is NOT imported at module load, only inside
     `_run_subprocess_banner` which the parent never calls.

  2. **Run as `python -m …banner`** (spawned by `StatusBanner.show()`
     via `subprocess.Popen`) — falls through `if __name__ == "__main__"`
     into `_run_subprocess_banner`, which boots pywebview and parks on
     `webview.start()`. Reads JSON commands from stdin, emits JSON
     events on stdout.

Why two roles, one file? Running pywebview's second window from a
worker thread inside the already-running AutoUse process kept landing
the pill off-screen on DPI-scaled displays. A fresh Python interpreter
(the subprocess) was the only way to dodge that DPI confusion —
`banner_test.py` standalone works perfectly on the same machine. The
subprocess body used to live in a separate `banner_proc.py` but it
doesn't need to: a single module's `__main__` guard does the same job
with one fewer file to keep in sync.

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
import tempfile
import threading
import time
import uuid
from queue import Queue, Empty

logger = logging.getLogger(__name__)

# True when this module is running inside the Nuitka-compiled AutoUse.exe
# (i.e. sys.executable is the exe, not a Python interpreter). In that case
# `python -m …banner` is meaningless — the binary has no -m loader — so
# StatusBanner.show() must re-exec AutoUse.exe with --banner-mode, which
# app.py's main() picks up and routes to _run_subprocess_banner() directly.
# Mirrors the detection in app.py:71 and the same pattern already used for
# --minion-mode in Auto_Use/windows_use/controller/view.py:697.
_IS_COMPILED = getattr(sys, "frozen", False) or "__compiled__" in globals()


# ── Pill geometry ─────────────────────────────────────────────────────────

# Setup-wizard banner. A clean white stadium pill that matches the
# standalone pill.py reference exactly — 350 × 42, no orb, no grow
# animation: it simply appears at full size and streams status text.
# (The colourful animated orb still lives on the compact task-progress
# pill below — only the setup wizard was asked to drop it "for now".)
# The pill grows taller only when a wizard message wraps to multiple
# lines (see height_changed); a single-line message stays exactly 42 px.
PILL_WIDTH = 350
PILL_HEIGHT = 42
# Compact pill geometry. Starts as a 50×50 white circle (orb only). When
# the bot streams text into the .msg span the pill grows rightward into
# a 580×50 stadium — a single-line ticker. Height never changes. Long
# messages page through one line at a time (fill, pause, clear, resume).
# WinForms imposes an OS-level minimum width (~SM_CXMINTRACK = 132+
# logical pixels) on freshly created Forms, but a programmatic
# window.resize() AFTER the form is alive bypasses that clamp (see
# _on_shown). COMPACT_MAX_W is the compact pill's own ceiling — it is no
# longer tied to PILL_WIDTH, which now tracks the smaller setup pill.
COMPACT_MIN_W = 50   # square → circle when only the orb is visible
COMPACT_MIN_H = 50
COMPACT_MAX_W = 580  # compact task-progress pill's max width
COMPACT_MAX_H = 50   # single-line height — pill never grows taller
SCREEN_MARGIN = 20


# ── Win32 region clip + click-through (subprocess-side, but ctypes is
#    stdlib so importing it at the top costs nothing for the parent) ──

class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


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


# File-based event log for the subprocess. Lives at
# %LOCALAPPDATA%\AutoUse\banner_debug.log so it survives whatever happens
# to the subprocess's stdio. We log subprocess start, on_shown, events.closing,
# events.closed, exceptions, and webview.start() return — enough to point at
# the exact proximate cause if the pill ever vanishes mid-flow again. Best
# effort: any failure to write is swallowed.
def _log(msg: str) -> None:
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


def _find_hwnd(title: str) -> int:
    """Locate the OS HWND for our pywebview window by title. Polls
    briefly because events.shown can fire one frame before the OS lets
    FindWindowW see the new window."""
    user32 = ctypes.windll.user32
    hwnd = 0
    for _ in range(40):
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            return hwnd
        time.sleep(0.025)
    return 0


def _make_click_through(title: str) -> None:
    """Make the window pass mouse clicks to whatever is underneath it.

    Achieved by adding WS_EX_LAYERED | WS_EX_TRANSPARENT to the
    extended window style. SetLayeredWindowAttributes with alpha=255
    is required after the LAYERED flag goes on or Windows treats the
    window as fully invisible — we want fully visible but unclickable.

    Used by the compact "telegram task in progress" indicator pill so
    it never blocks the user from clicking the desktop / other apps
    beneath it; the pill is a passive visual cue, never interactive.
    Matches macOS's `setIgnoresMouseEvents_(True)` on the compact
    NSPanel."""
    user32 = ctypes.windll.user32
    hwnd = _find_hwnd(title)
    if not hwnd:
        return
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    LWA_ALPHA = 0x00000002
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(
        hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT
    )
    # WS_EX_LAYERED windows render nothing until SetLayeredWindowAttributes
    # (or UpdateLayeredWindow) is called. alpha=255 → fully opaque so the
    # orb still paints normally; only mouse input is what we want to drop.
    user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)


def _apply_rounded_region(title: str) -> None:
    """Clip the window with the given title into a stadium pill.

    Uses FindWindowW on the unique title to locate the HWND,
    GetWindowRect for the actual DPI-aware size, then SetWindowRgn for
    the clip. Polls briefly because events.shown can fire one frame
    before the OS lets FindWindowW see the new window."""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    hwnd = 0
    for _ in range(40):
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            break
        time.sleep(0.025)
    if not hwnd:
        return

    rect = _RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return

    # Pill: full-height end caps via corner ellipse = h × h.
    rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, h, h)
    user32.SetWindowRgn(hwnd, rgn, True)


# ── HTML (subprocess-side only — parent never touches these strings) ──

BANNER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<style>
  html, body {
    margin: 0;
    padding: 0;
    height: 100%;
    width: 100%;
    background: transparent;     /* window is transparent — only the .banner
                                    pill below is opaque, so its CSS rounded
                                    corners become the real window shape. */
    overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    -webkit-user-select: none;
    user-select: none;
  }

  /* The white stadium pill itself. border-radius: 999px clamps to half the
     height → full-semicircle end caps (a true pill like pill.py, NOT a
     rounded rectangle); the corners outside it stay transparent. This is
     what actually draws the shape — WebView2 renders it anti-aliased on the
     pixels, the way pill.py draws its alpha rounded rectangle. No orb; the
     Next/Save/choice controls sit to the right of the streaming text. */
  .banner {
    width: 100%;
    height: 100%;
    background: #ffffff;
    border-radius: 999px;
    overflow: hidden;            /* clip streamed text to the rounded edge */
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 0 20px;             /* clears the rounded end caps */
    box-sizing: border-box;
  }

  .banner-text {
    flex: 1;
    min-width: 0;                /* lets flex shrink so the next-btn keeps
                                    its place; text streams within the
                                    remaining width and pages, never wraps. */
    color: #333333;              /* matches pill.py's text fill */
    font-size: 13px;             /* matches pill.py's 13px label */
    font-weight: 600;
    line-height: 1.4;
    white-space: nowrap;         /* single line — the pill stays an
                                    elongated stadium, never grows taller. */
    overflow: hidden;            /* the streamer pages before text spills. */
  }

  .next-btn {
    background: #6366f1;
    color: #ffffff;
    border: none;
    font-family: inherit;
    font-size: 12px;
    font-weight: 600;
    padding: 6px 14px;
    border-radius: 999px;
    cursor: pointer;
    transition: background 0.15s ease;
    flex-shrink: 0;
  }
  .next-btn:hover  { background: #4f46e5; }
  .next-btn:active { background: #4338ca; }

  .choice-row { display: none; flex-shrink: 0; gap: 6px; }
  .choice-row .next-btn { padding: 6px 12px; font-size: 12px; }

  .input-row { display: none; flex: 1; align-items: center; gap: 6px; }
  #token-input {
    flex: 1;
    height: 28px;
    border: 1px solid #d1d5db;
    border-radius: 14px;
    padding: 0 12px;
    font-size: 12px;
    font-family: inherit;
    color: #374151;
    background: #ffffff;
    outline: none;
  }
  #token-input:focus { border-color: #6366f1; }
</style>
</head>
<body>
  <div class="banner">
    <div class="banner-text" id="msg">Starting…</div>

    <button class="next-btn" id="next" style="display:none"
            onclick="if(window.pywebview&&window.pywebview.api) window.pywebview.api.next_clicked()">Next</button>

    <div class="choice-row" id="choice-row">
      <button class="next-btn" id="choice-left"
              onclick="if(window.pywebview&&window.pywebview.api) window.pywebview.api.choice_clicked('left')">Left</button>
      <button class="next-btn" id="choice-right"
              onclick="if(window.pywebview&&window.pywebview.api) window.pywebview.api.choice_clicked('right')">Right</button>
    </div>

    <div class="input-row" id="input-row">
      <input type="text" id="token-input" placeholder="Paste your BotFather token here" />
      <button class="next-btn" id="save-btn"
              onclick="(function(){var v=document.getElementById('token-input').value;
                       if(window.pywebview&&window.pywebview.api) window.pywebview.api.save_clicked(v);})()">Save</button>
    </div>
  </div>

  <script>
    // Single-line streaming pill (mirrors the compact task pill). The
    // pill is fixed at 350×42 — text never wraps. setMsg reveals the
    // message letter-by-letter; when a line fills the available width it
    // holds briefly, clears, and continues on a fresh line (paging), so
    // a long wizard message flows through one line at a time while the
    // stadium shape stays exactly the same size.
    const _CHAR_DELAY_MS = 8;     // per-letter cadence — fast typewriter feel
    const _FADE_MS = 60;          // per-letter fade-in duration
    const _PAGE_HOLD_MS = 1600;   // how long a full line lingers before paging
    let _revealTimer = null;

    function setMsg(fullText) {
      if (_revealTimer) { clearTimeout(_revealTimer); _revealTimer = null; }
      const el = document.getElementById('msg');
      if (!el) return;
      const text = (fullText || '').toString();
      el.textContent = '';
      if (!text) return;

      // Array.from splits by code point so emoji stay intact.
      const chars = Array.from(text);
      let i = 0;

      const streamChar = () => {
        if (i >= chars.length) {
          // End of message — leave the final line up; the wizard is
          // waiting on the user to read it and click. A fresh setMsg()
          // replaces it (and cancels this stream at its top).
          _revealTimer = null;
          return;
        }

        const span = document.createElement('span');
        span.textContent = chars[i];
        span.style.opacity = '0';
        span.style.transition = 'opacity ' + _FADE_MS + 'ms ease-out';
        el.appendChild(span);

        if (el.scrollWidth > el.clientWidth + 0.5) {
          // This letter overflows the line. If it's the only one, the
          // line is narrower than a single glyph — keep it (overflow
          // clips) and advance so we don't loop. Otherwise yank it, hold
          // the visible line briefly, clear, and continue on a new line.
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

      streamChar();
    }
    function showNext()  {
      clearAll();
      document.getElementById('next').style.display = 'inline-block';
      document.getElementById('msg').style.display = 'block';
    }
    function hideNext() { document.getElementById('next').style.display = 'none'; }
    function setChoice(leftLabel, rightLabel) {
      clearAll();
      document.getElementById('msg').style.display = 'none';
      document.getElementById('choice-left').textContent = leftLabel;
      document.getElementById('choice-right').textContent = rightLabel;
      document.getElementById('choice-row').style.display = 'flex';
    }
    function setInput(saveLabel) {
      clearAll();
      document.getElementById('msg').style.display = 'none';
      document.getElementById('save-btn').textContent = saveLabel || 'Save';
      document.getElementById('input-row').style.display = 'flex';
      var inp = document.getElementById('token-input');
      inp.value = '';
      setTimeout(function(){ inp.focus(); }, 30);
    }
    function clearAll() {
      document.getElementById('next').style.display = 'none';
      document.getElementById('choice-row').style.display = 'none';
      document.getElementById('input-row').style.display = 'none';
      document.getElementById('msg').style.display = 'block';
    }
    window.setMsg = setMsg;
    window.showNext = showNext;
    window.hideNext = hideNext;
    window.setChoice = setChoice;
    window.setInput = setInput;
    window.clearAll = clearAll;

    document.getElementById('token-input').addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && window.pywebview && window.pywebview.api) {
        window.pywebview.api.save_clicked(this.value);
      }
    });

    // The setup pill is a fixed 350×42 stadium — it never resizes, so
    // there is no height/width reporting back to Python (unlike the
    // compact pill, which grows to fit). Long text fits by streaming and
    // paging within the fixed single line, handled entirely in setMsg
    // above. The rounded pill shape is the .banner CSS border-radius on a
    // transparent window, plus a SetWindowRgn clip on the opaque form
    // behind it (both done from Python — see create_window / _on_shown).
  </script>
</body>
</html>
"""


COMPACT_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
  /* Compact pill visual model:
     - Empty state: 50×50 white circle, just the orb.
     - has-text state: 580×50 single-line stadium pill. Orb on left,
       text streams to its right; height never changes. Long messages
       page through one line at a time (fill, pause, clear, continue). */
  html { margin: 0; padding: 0; background: #ffffff;
    overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    -webkit-user-select: none; user-select: none; }
  body { margin: 0; padding: 4px; box-sizing: border-box;
    background: #ffffff;
    display: flex; align-items: center; gap: 10px;
    width: 50px; height: 50px; overflow: hidden; }
  body.has-text { width: 580px; }

  .stop-agent-button { position: relative; width: 42px; height: 42px;
    flex-shrink: 0; background: transparent;
    display: flex; align-items: center; justify-content: center; }
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
  /* Both icons share this stack frame — they sit at the same position
     and cross-fade via opposing opacity keyframes. */
  .icon-stack { position: absolute; inset: 0; margin: auto;
    width: 32px; height: 32px; z-index: 10;
    display: flex; align-items: center; justify-content: center; }
  .icon-layer { position: absolute; inset: 0;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 1px; box-sizing: border-box;
    /* Promote each layer to its own compositor layer up-front so the
       opacity cross-fade is GPU-only — without this the first fade
       triggers a one-frame layer-promotion artifact (a tiny square
       flash) on EdgeChromium. */
    will-change: opacity;
    transform: translateZ(0);
    backface-visibility: hidden; }
  .icon-pc { animation: icon-cycle-pc 6s ease-in-out infinite; }
  .icon-tg { animation: icon-cycle-tg 6s ease-in-out infinite; color: white; }

  .stop-monitor { width: 12px; height: 10px; border: 1px solid white; box-sizing: border-box; }
  .stop-screen { width: 100%; height: 100%; display: flex;
    justify-content: center; align-items: center; gap: 2px; }
  .stop-eye { width: 1.5px; height: 2.5px; border-radius: 1px; background: white;
    animation: stop-blink 4s infinite; }
  .stop-base { width: 16px; height: 1px; background: white; border-radius: 0.5px; }

  @keyframes stop-pulse  { 0%{transform:scale(.97)} 15%{transform:scale(1)} 30%{transform:scale(.98)} 45%{transform:scale(1)} 60%{transform:scale(.97)} 85%{transform:scale(1)} 100%{transform:scale(.97)} }
  @keyframes stop-pulse2 { 0%{transform:scale(1)} 15%{transform:scale(1.03)} 30%{transform:scale(.98)} 45%{transform:scale(1.04)} 60%{transform:scale(.97)} 85%{transform:scale(1.03)} 100%{transform:scale(1)} }
  @keyframes stop-bgRotate { 0%{transform:rotate(0)} 20%{transform:rotate(90deg)} 40%{transform:rotate(180deg) scale(.95,1)} 60%,100%{transform:rotate(360deg)} }
  @keyframes stop-bgColor  { 20%{background-color:red} 40%{background-color:#5eff7e} 60%{background-color:#2cb5ff} 80%{background-color:#fc63ff} }
  @keyframes stop-blink    { 0%,85%,100%{transform:scaleY(1)} 92%{transform:scaleY(.1)} }
  /* 6 s total cycle = 3 s per icon. 0-40 % = first icon fully visible,
     40-50 % = cross-fade, 50-90 % = second icon fully visible, 90-100 %
     = cross-fade back. ease-in-out timing makes the swap feel soft. */
  @keyframes icon-cycle-pc { 0%, 40% { opacity: 1 } 50%, 90% { opacity: 0 } 100% { opacity: 1 } }
  @keyframes icon-cycle-tg { 0%, 40% { opacity: 0 } 50%, 90% { opacity: 1 } 100% { opacity: 0 } }

  /* Single-line streaming text. white-space: nowrap keeps tokens
     flowing left-to-right with no wrapping; overflow: hidden clips
     anything past max-width. Pager watches scrollWidth and starts a
     new page before content actually overflows.
       4 padding-l + 42 orb + 10 gap + 520 msg + 4 padding-r = 580.   */
  .msg { font-size: 13px; color: #374151; line-height: 1.4;
    white-space: nowrap; overflow: hidden;
    max-width: 520px; }
  .msg:empty { display: none; }
</style></head>
<body>
  <div class="stop-agent-button">
    <div class="stop-orb">
      <div class="stop-circle-1"></div>
      <div class="stop-circle-2"><div class="stop-bg"></div></div>
      <div class="icon-stack">
        <div class="icon-layer icon-pc">
          <div class="stop-monitor">
            <div class="stop-screen">
              <div class="stop-eye"></div>
              <div class="stop-eye"></div>
            </div>
          </div>
          <div class="stop-base"></div>
        </div>
        <div class="icon-layer icon-tg">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M21.5 4.5L2.5 12l5.5 2 2 6 3-3.5 5.5 4 3-16zM10 14l8.5-7L11 14.5l-1 4.5L10 14z"/></svg>
        </div>
      </div>
    </div>
  </div>
  <span class="msg" id="msg"></span>
  <script>
    // Stream `text` LETTER-BY-LETTER into .msg, paging through one line
    // at a time. After each letter, sync-read scrollWidth — if it
    // exceeds clientWidth (max-width on .msg), this letter overflows:
    // yank it, hold the visible line briefly, clear, continue streaming
    // the rest on a fresh line. Loops until every letter has shown.
    //
    // First setMsg() of a task waits 350 ms so the WinForms window can
    // finish its 50→580 width expansion before any text appears (no
    // per-letter jitter). Subsequent calls start immediately.
    const _CHAR_DELAY_MS = 8;      // per-letter cadence — fast typewriter feel
    const _FADE_MS = 60;           // per-letter fade-in duration
    const _PAGE_HOLD_MS = 400;     // how long a full line lingers before clearing
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

      // Array.from splits by code point so 🧠 / 🎯 / etc. stay intact.
      const chars = Array.from(text);
      let i = 0;

      const streamChar = () => {
        if (i >= chars.length) {
          // End of message — hold the final page briefly so the user
          // can read it, then clear and drop the has-text class so body
          // shrinks back to the 50×50 circle. A new setMsg() during the
          // hold cancels this timer (cleared at the top of setMsg).
          _revealTimer = setTimeout(() => {
            el.textContent = '';
            document.body.classList.remove('has-text');
            _revealTimer = null;
          }, _PAGE_HOLD_MS);
          return;
        }

        const span = document.createElement('span');
        span.textContent = chars[i];
        span.style.opacity = '0';
        span.style.transition = 'opacity ' + _FADE_MS + 'ms ease-out';
        el.appendChild(span);

        if (el.scrollWidth > el.clientWidth + 0.5) {
          // Defensive: a single letter too wide for the whole line —
          // keep it (overflow:hidden clips) and advance so we don't loop.
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

      const startDelay = wasEmpty ? 350 : 0;
      _revealTimer = setTimeout(streamChar, startDelay);
    }
    window.setMsg = setMsg;

    // Report body size to Python on the empty ↔ has-text toggle (the
    // only time dimensions change — both states are fixed). Last-value
    // debounce avoids a resize feedback loop.
    (function () {
      let lastW = -1, lastH = -1;
      const report = () => {
        if (!window.pywebview || !window.pywebview.api) return;
        const w = Math.ceil(document.body.getBoundingClientRect().width);
        const h = Math.ceil(document.body.getBoundingClientRect().height);
        if (w === lastW && h === lastH) return;
        lastW = w; lastH = h;
        try { window.pywebview.api.size_changed(w, h); } catch (e) {}
      };
      window.addEventListener('load', () => setTimeout(report, 30));
      window.addEventListener('pywebviewready', () => setTimeout(report, 30));
      try {
        const ro = new ResizeObserver(report);
        ro.observe(document.body);
      } catch (e) {}
    })();
  </script>
</body>
</html>
"""


# ── JS↔Python bridge (subprocess-side only) ──────────────────────────────


# Hard cap on pill height so a freakishly long message can't push it
# into a wall-of-text rectangle. Matches the macOS banner's MAX_H.
_MAX_PILL_HEIGHT = 200


class _BannerState:
    """Mutable state shared between the resize-handler and the rest of
    the subprocess body.

    Deliberately NOT used as `js_api` — see _make_js_handlers."""

    def __init__(self, title: str, width: int, min_h: int, compact: bool,
                 screen_w: int = 1920, top_margin: int = SCREEN_MARGIN,
                 right_margin: int = SCREEN_MARGIN):
        self.window = None
        self.title = title
        self.width = width
        self.min_h = min_h
        self.compact = compact
        self.last_h = min_h
        # Last reported width — only relevant for compact mode where both
        # axes grow. Standard mode keeps a fixed width so this stays at
        # init value.
        self.last_w = width
        # Screen geometry the resize handler uses to anchor the pill's
        # top-right corner. Without this the pill would drift leftward
        # across the screen as it grew wider.
        self.screen_w = screen_w
        self.top_margin = top_margin
        self.right_margin = right_margin


def _make_js_handlers(state: _BannerState):
    """Return JS-exposed handlers as a 4-tuple of plain local functions.

    We register these via `window.expose(*funcs)` instead of the old
    `js_api=_Api(...)` pattern because pywebview's util.py:get_functions
    filters attributes via `inspect.ismethod(attr)` — which returns
    False for bound methods of Nuitka-compiled classes. In the
    compiled binary that silently drops every method on _Api, so the
    JS-side `window.pywebview.api.next_clicked()` resolves to nothing
    and clicks become no-ops. `window.expose()` stores functions
    directly in `window._functions`, which the dispatcher checks
    BEFORE falling back to js_api reflection."""

    def next_clicked(_value=None):
        _emit("NEXT")
        return None

    def choice_clicked(value=None):
        _emit("CHOICE", value=str(value) if value is not None else "left")
        return None

    def save_clicked(value=None):
        _emit("SAVE", value=value.strip() if isinstance(value, str) else "")
        return None

    def height_changed(h=0):
        """Resize the window to fit the reported body height, then
        re-clip the (possibly taller) window into a stadium so the end
        caps follow the new height. Used by the STANDARD banner only —
        the compact pill posts {w, h} to size_changed instead, which
        animates both axes."""
        if state.compact or state.window is None:
            return None
        try:
            target = max(state.min_h, min(_MAX_PILL_HEIGHT, int(h)))
            if target == state.last_h:
                return None
            state.last_h = target
            state.window.resize(state.width, target)
            # SetWindowRgn's saved region is anchored to the OLD height,
            # so without re-clipping the bottom of the now-taller window
            # would render as a hard rectangle below the pill ends.
            _apply_rounded_region(state.title)
        except Exception:
            pass
        return None

    def size_changed(w=0, h=0):
        """Resize the compact pill in both axes to fit its natural body
        size, then re-position so the top-right corner stays anchored to
        its screen position (without this, growing wider would push the
        pill leftward across the screen). Compact mode only — the
        standard banner has a fixed width and uses height_changed."""
        if not state.compact or state.window is None:
            return None
        try:
            new_w = max(COMPACT_MIN_W, min(COMPACT_MAX_W, int(w)))
            new_h = max(COMPACT_MIN_H, min(COMPACT_MAX_H, int(h)))
            if new_w == state.last_w and new_h == state.last_h:
                return None
            state.last_w = new_w
            state.last_h = new_h
            # window.move BEFORE resize: when we shrink the pill, resizing
            # first leaves a brief 1-frame gap on the right; moving first
            # closes that gap. Both APIs schedule on the GUI thread so the
            # ordering is honoured by WinForms.
            new_x = max(0, state.screen_w - new_w - state.right_margin)
            new_y = state.top_margin
            try:
                state.window.move(new_x, new_y)
            except Exception:
                pass
            state.window.resize(new_w, new_h)
            # Region clip is sized in absolute pixels — recompute for the
            # new dimensions or the pill renders with hard rectangle
            # corners on its excess area.
            _apply_rounded_region(state.title)
        except Exception:
            pass
        return None

    return next_clicked, choice_clicked, save_clicked, height_changed, size_changed


# ── stdin reader thread (subprocess-side only) ───────────────────────────


def _stdin_reader(window) -> None:
    """Loop reading JSON commands from stdin and dispatching to the window.

    Runs on its own thread so we don't block the pywebview GUI thread."""
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
                    window.evaluate_js(f"if(window.setMsg) setMsg('{esc}');")
                elif cmd == "SHOW_NEXT":
                    window.evaluate_js("if(window.showNext) showNext();")
                elif cmd == "HIDE_NEXT":
                    window.evaluate_js("if(window.hideNext) hideNext();")
                elif cmd == "SHOW_CHOICE":
                    left = _js_escape(msg.get("left", ""))
                    right = _js_escape(msg.get("right", ""))
                    window.evaluate_js(
                        f"if(window.setChoice) setChoice('{left}', '{right}');"
                    )
                elif cmd == "SHOW_INPUT":
                    label = _js_escape(msg.get("label", "Save"))
                    window.evaluate_js(
                        f"if(window.setInput) setInput('{label}');"
                    )
                elif cmd == "CLEAR":
                    window.evaluate_js("if(window.clearAll) clearAll();")
                elif cmd == "CLOSE":
                    _log("stdin_reader: CLOSE received, destroying window")
                    try:
                        window.destroy()
                    except Exception:
                        import traceback
                        _log(
                            "stdin_reader: window.destroy() raised:\n"
                            + traceback.format_exc()
                        )
                    return
            except Exception:
                # Window may have been destroyed mid-flight — log and
                # keep the reader alive so the process exits cleanly.
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
    # the frontend UI was closed). Destroy the window so this subprocess exits
    # with it instead of lingering as an orphan tied to nothing.
    try:
        window.destroy()
    except Exception:
        pass


# ── layered-window setup pill (pill.py technique, no WebView2) ────────────


def _run_layered_setup_banner() -> None:
    """Setup-wizard pill rendered exactly like pill.py: a Win32 WS_EX_LAYERED
    window painted via UpdateLayeredWindow with a real 32-bit alpha channel.
    The pill is drawn by Pillow at 4x and downsampled with LANCZOS, so the
    rounded edges blend smoothly into the wallpaper — no border, no halo, no
    aliasing (which is what the WebView2 + region-clip approach could never do,
    because WebView2 composites separately and a GDI region is a hard 1-bit
    mask).

    Speaks the same JSON-over-stdio wire protocol as the pywebview path so
    StatusBanner / setup.py drive it unchanged: reads MSG / SHOW_NEXT /
    HIDE_NEXT / SHOW_CHOICE / SHOW_INPUT / CLEAR / CLOSE on stdin; emits
    READY / NEXT / CHOICE / SAVE / CLOSED on stdout.
    """
    _log("layered setup banner: start")
    try:
        import math
        import ctypes as C
        from ctypes import wintypes
        from PIL import Image, ImageDraw, ImageChops, ImageFont, ImageFilter
    except Exception:
        import traceback
        _log("layered banner: import failed:\n" + traceback.format_exc())
        raise

    user32 = C.windll.user32
    gdi32 = C.windll.gdi32
    kernel32 = C.windll.kernel32

    # DPI-aware so the bitmap is rendered at native resolution (crisp); the
    # actual scale factor is read after the GDI signatures are set, below.
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

    # ----- Win32 plumbing (mirrors pill.py) -----
    WNDPROC = C.WINFUNCTYPE(
        C.c_ssize_t, C.c_void_p, C.c_uint, C.c_size_t, C.c_ssize_t)

    class WNDCLASS(C.Structure):
        _fields_ = [
            ("style", C.c_uint), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", C.c_int), ("cbWndExtra", C.c_int),
            ("hInstance", C.c_void_p), ("hIcon", C.c_void_p),
            ("hCursor", C.c_void_p), ("hbrBackground", C.c_void_p),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR)]

    class POINT(C.Structure):
        _fields_ = [("x", C.c_long), ("y", C.c_long)]

    class SIZE(C.Structure):
        _fields_ = [("cx", C.c_long), ("cy", C.c_long)]

    class BLENDFUNCTION(C.Structure):
        _fields_ = [("BlendOp", C.c_ubyte), ("BlendFlags", C.c_ubyte),
                    ("SourceConstantAlpha", C.c_ubyte), ("AlphaFormat", C.c_ubyte)]

    class BITMAPINFOHEADER(C.Structure):
        _fields_ = [
            ("biSize", C.c_uint32), ("biWidth", C.c_int32),
            ("biHeight", C.c_int32), ("biPlanes", C.c_uint16),
            ("biBitCount", C.c_uint16), ("biCompression", C.c_uint32),
            ("biSizeImage", C.c_uint32), ("biXPelsPerMeter", C.c_int32),
            ("biYPelsPerMeter", C.c_int32), ("biClrUsed", C.c_uint32),
            ("biClrImportant", C.c_uint32)]

    class BITMAPINFO(C.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", C.c_uint32 * 3)]

    class MSG(C.Structure):
        _fields_ = [("hwnd", C.c_void_p), ("message", C.c_uint),
                    ("wParam", C.c_size_t), ("lParam", C.c_ssize_t),
                    ("time", wintypes.DWORD), ("pt", POINT)]

    P = C.POINTER
    kernel32.GetModuleHandleW.restype = C.c_void_p
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.RegisterClassW.argtypes = [P(WNDCLASS)]
    user32.LoadCursorW.restype = C.c_void_p
    user32.LoadCursorW.argtypes = [C.c_void_p, C.c_void_p]
    user32.CreateWindowExW.restype = C.c_void_p
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        C.c_int, C.c_int, C.c_int, C.c_int,
        C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p]
    user32.DefWindowProcW.restype = C.c_ssize_t
    user32.DefWindowProcW.argtypes = [C.c_void_p, C.c_uint, C.c_size_t, C.c_ssize_t]
    user32.GetDC.restype = C.c_void_p
    user32.GetDC.argtypes = [C.c_void_p]
    user32.ReleaseDC.argtypes = [C.c_void_p, C.c_void_p]
    gdi32.CreateCompatibleDC.restype = C.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [C.c_void_p]
    gdi32.CreateDIBSection.restype = C.c_void_p
    gdi32.CreateDIBSection.argtypes = [
        C.c_void_p, P(BITMAPINFO), C.c_uint, P(C.c_void_p), C.c_void_p, wintypes.DWORD]
    gdi32.SelectObject.restype = C.c_void_p
    gdi32.SelectObject.argtypes = [C.c_void_p, C.c_void_p]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL
    user32.UpdateLayeredWindow.argtypes = [
        C.c_void_p, C.c_void_p, P(POINT), P(SIZE),
        C.c_void_p, P(POINT), wintypes.DWORD, P(BLENDFUNCTION), wintypes.DWORD]
    user32.SetWindowPos.argtypes = [
        C.c_void_p, C.c_void_p, C.c_int, C.c_int, C.c_int, C.c_int, C.c_uint]
    user32.SetCapture.restype = C.c_void_p
    user32.SetCapture.argtypes = [C.c_void_p]
    user32.GetCursorPos.argtypes = [P(POINT)]
    user32.DestroyWindow.argtypes = [C.c_void_p]
    user32.PostMessageW.argtypes = [C.c_void_p, C.c_uint, C.c_size_t, C.c_ssize_t]
    user32.GetSystemMetrics.restype = C.c_int
    user32.SetTimer.restype = C.c_size_t
    user32.SetTimer.argtypes = [C.c_void_p, C.c_size_t, C.c_uint, C.c_void_p]
    user32.GetMessageW.argtypes = [P(MSG), C.c_void_p, C.c_uint, C.c_uint]
    user32.GetMessageW.restype = C.c_int
    user32.DispatchMessageW.restype = C.c_ssize_t
    user32.DispatchMessageW.argtypes = [P(MSG)]
    user32.TranslateMessage.argtypes = [P(MSG)]
    user32.ShowWindow.argtypes = [C.c_void_p, C.c_int]
    user32.ReleaseCapture.restype = wintypes.BOOL
    user32.PostQuitMessage.argtypes = [C.c_int]
    user32.KillTimer.argtypes = [C.c_void_p, C.c_size_t]
    user32.SetFocus.restype = C.c_void_p
    user32.SetFocus.argtypes = [C.c_void_p]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [C.c_void_p]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.OpenClipboard.argtypes = [C.c_void_p]
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.restype = C.c_void_p
    user32.GetClipboardData.argtypes = [C.c_uint]
    gdi32.DeleteObject.argtypes = [C.c_void_p]
    gdi32.DeleteDC.argtypes = [C.c_void_p]
    gdi32.GetDeviceCaps.restype = C.c_int
    gdi32.GetDeviceCaps.argtypes = [C.c_void_p, C.c_int]
    kernel32.GlobalLock.restype = C.c_void_p
    kernel32.GlobalLock.argtypes = [C.c_void_p]
    kernel32.GlobalUnlock.argtypes = [C.c_void_p]

    # Now that GetDC / GetDeviceCaps signatures are set (so the 64-bit DC
    # handle isn't truncated), read the DPI scale and derive every pixel
    # dimension from it.
    _hdc0 = user32.GetDC(None)
    try:
        dpi = gdi32.GetDeviceCaps(_hdc0, 88) or 96   # LOGPIXELSX
    except Exception:
        dpi = 96
    user32.ReleaseDC(None, _hdc0)
    scale = (dpi or 96) / 96.0

    def S(v):
        return max(1, int(round(v * scale)))

    PILL_W, MARGIN = S(435), S(10)
    PILL_H_MIN = S(44)            # default (single-line) pill height
    MAX_PILL_H = S(200)          # grows downward up to here for long messages
    CW = PILL_W + 2 * MARGIN
    MIN_CH = PILL_H_MIN + 2 * MARGIN
    MAX_CH = MAX_PILL_H + 2 * MARGIN
    SS = 4
    PAD_R, GAP = S(16), S(10)
    BTN_H, BTN_PAD_X = S(26), S(14)
    CORNER = PILL_H_MIN / 2.0    # fixed radius: stadium at MIN, rounded-rect grown
    # Orb (animated colourful indicator) — bigger than before and CENTRED on
    # the pill's left semicircular cap so it nests perfectly inside the curve
    # (its centre = the cap's centre). It's clipped to a feathered circle so
    # its square sprite corners never poke past the pill's rounded edge.
    ORB = S(40)
    ORB_GAP = S(10)             # gap between the orb and the text column
    ORB_CX = MARGIN + CORNER    # orb centre x = centre of the left cap
    ORB_CY = MARGIN + PILL_H_MIN / 2.0
    TEXT_X0 = int(round(ORB_CX + ORB / 2.0)) + ORB_GAP   # text column left (canvas px)
    # The FIRST text line is vertically centred on the orb (= pill centre at
    # MIN height). Extra lines flow BELOW it and the pill grows downward, so
    # the first line never shifts. LINE_Y0 is that first-line centre, in
    # pill-relative px (LINE_H is added once fonts are loaded).
    CTRL_GAP = S(8)             # gap between wrapped text and the control row
    N_ORB_FRAMES = 36
    ORB_TICKS_PER_FRAME = 5     # advance one orb frame every ~80 ms
    SCREEN_M = S(SCREEN_MARGIN)
    TEXT_COL = (51, 51, 51, 255)
    MSG_COL = (107, 107, 117, 255)   # macOS message grey #6b6b75
    PURPLE = (94, 106, 210, 255)     # macOS button #5e6ad2
    PURPLE_TXT = (255, 255, 255, 255)
    FIELD_BORDER = (212, 212, 220, 255)
    PLACEHOLDER = (156, 163, 175, 255)

    WS_POPUP = 0x80000000
    WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW = 0x80000, 0x8, 0x80
    SW_SHOW, ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 5, 2, 0, 1
    BI_RGB, DIB_RGB_COLORS = 0, 0
    WM_DESTROY, WM_MOUSEMOVE = 0x2, 0x200
    WM_LBUTTONDOWN, WM_LBUTTONUP, WM_TIMER, WM_CHAR = 0x201, 0x202, 0x113, 0x102
    WM_APP_CLOSE, WM_APP_REPAINT, WM_APP_FOCUS = 0x8000 + 1, 0x8000 + 2, 0x8000 + 3
    SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
    IDC_ARROW = 32512

    def _font(size, bold=True):
        names = ("segoeuib.ttf", "segoeui.ttf") if bold else ("segoeui.ttf",)
        for n in names:
            try:
                return ImageFont.truetype(n, size)
            except Exception:
                pass
        return ImageFont.load_default()

    FONT_TEXT = _font(S(13), bold=False)
    FONT_BTN = _font(S(12), bold=True)
    _NEXT_LABEL = "Next"
    _next_btn_w = int(FONT_BTN.getlength(_NEXT_LABEL)) + 2 * BTN_PAD_X

    _asc, _desc = FONT_TEXT.getmetrics()
    LINE_H = _asc + _desc + S(3)
    LINE_Y0 = CORNER             # first-line centre (pill-relative) = pill centre
    TEXT_RIGHT = MARGIN + PILL_W - PAD_R

    def _text_width():
        # Full text column width (orb column → right padding). In 'next' mode
        # the button flows inline AFTER the text, so we don't reserve a column
        # for it — it only pushes to a new line if it can't fit after the last
        # word (see _layout).
        return max(S(40), TEXT_RIGHT - TEXT_X0)

    def _wrap(text, max_w):
        lines = []
        for para in (text or "").split("\n"):
            cur = ""
            for word in para.split(" "):
                trial = (cur + " " + word).strip()
                if not cur or FONT_TEXT.getlength(trial) <= max_w:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = word
            lines.append(cur)
        return lines or [""]

    def _layout(mode, text):
        # Returns (lines, btn_newline). In 'next' mode the Next button sits
        # right AFTER the last word. To keep "word [Next]" together, if they
        # don't fit on the last line we push the last word down to its own
        # line (with the button after it). btn_newline is set only if even a
        # lone word + button can't fit, so the button drops below by itself.
        lines = _wrap(text, _text_width())
        btn_newline = False
        if mode == "next":
            last_w = FONT_TEXT.getlength(lines[-1])
            if TEXT_X0 + last_w + GAP + _next_btn_w > TEXT_RIGHT:
                words = lines[-1].split(" ")
                if len(words) > 1:
                    lines[-1] = " ".join(words[:-1])
                    lines.append(words[-1])
                    last_w = FONT_TEXT.getlength(lines[-1])
                if TEXT_X0 + last_w + GAP + _next_btn_w > TEXT_RIGHT:
                    btn_newline = True
        return lines, btn_newline

    def _lerp(a, b, t):
        return tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))

    def _orb_frame(p):
        # One animation frame of the orb — a Pillow recreation of the
        # frontend's .stop-agent-button (css/style.css): a soft lavender
        # (#9292d8) disc with a bright INSET white rim-glow, FIXED pink
        # (top-right) / blue (bottom-left) accents bleeding at the edge, a
        # faint colour shimmer, and a white PC-monitor icon with blinking
        # eyes. Gentle pulse only — matches the pearly, mostly-static look of
        # the real button (NOT the over-saturated cycling gradient before).
        ss = 3
        sz = ORB * ss
        c = sz / 2.0
        pulse = 1.0 + 0.04 * math.sin(2 * math.pi * p)
        R = sz * 0.44 * pulse
        img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))

        # Outer pink/blue glow halo behind the disc (stop-circle-1 ::before /
        # ::after): pink fixed top-right, blue fixed bottom-left, blurred.
        glow = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gb, off = R * 0.5, R * 0.62
        gd.ellipse([c + off - gb, c - off - gb, c + off + gb, c - off + gb],
                   fill=(255, 0, 115, 160))            # top-right pink
        gd.ellipse([c - off - gb, c + off - gb, c - off + gb, c + off + gb],
                   fill=(0, 186, 255, 160))            # bottom-left blue
        glow = glow.filter(ImageFilter.GaussianBlur(R * 0.5))
        img = Image.alpha_composite(img, glow)

        d = ImageDraw.Draw(img)
        # Lavender base disc (#9292d8 — the stop-bg colour).
        d.ellipse([c - R, c - R, c + R, c + R], fill=(146, 146, 216, 255))

        # Faint colour shimmer (stop-bgColor @ ~0.2 opacity), clipped to disc.
        pal = [(255, 40, 40), (94, 255, 126), (44, 181, 255), (252, 99, 255)]
        n = len(pal)
        fp = (p % 1.0) * n
        i0 = int(fp) % n
        tcol = _lerp(pal[i0], pal[(i0 + 1) % n], fp - int(fp))
        tint = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        ImageDraw.Draw(tint).ellipse([c - R, c - R, c + R, c + R], fill=tcol + (52,))
        img = Image.alpha_composite(img, tint)

        # Inset white rim-glow (the stop-bg box-shadow inset white) — the
        # bright pearly sheen that defines the look. A thick white ring at the
        # disc edge, blurred inward, clipped to the disc.
        mask = Image.new("L", (sz, sz), 0)
        ImageDraw.Draw(mask).ellipse([c - R, c - R, c + R, c + R], fill=255)
        ring = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse(
            [c - R, c - R, c + R, c + R], outline=(255, 255, 255, 255),
            width=max(1, int(R * 0.24)))
        ring = ring.filter(ImageFilter.GaussianBlur(R * 0.14))
        ring.putalpha(ImageChops.multiply(ring.split()[3], mask))
        img = Image.alpha_composite(img, ring)

        # White PC-monitor icon + blinking eyes (stop-pc).
        d2 = ImageDraw.Draw(img)
        mw, mh = sz * 0.34, sz * 0.27
        mx1, my1 = c - mw / 2, c - mh / 2 - sz * 0.03
        mx2, my2 = mx1 + mw, my1 + mh
        d2.rounded_rectangle([mx1, my1, mx2, my2], radius=sz * 0.04,
                             outline=(255, 255, 255, 255),
                             width=max(1, int(sz * 0.035)))
        ew, eh = sz * 0.05, sz * 0.10
        eh2 = eh * (0.18 if 0.90 <= p <= 0.97 else 1.0)
        ey = (my1 + my2) / 2 - eh2 / 2
        for ex in (c - ew * 1.4, c + ew * 0.4):
            d2.rounded_rectangle([ex, ey, ex + ew, ey + eh2], radius=ew / 2,
                                 fill=(255, 255, 255, 255))
        bw = sz * 0.46
        by = my2 + sz * 0.07
        d2.rounded_rectangle([c - bw / 2, by, c + bw / 2, by + max(1.0, sz * 0.025)],
                             radius=sz * 0.02, fill=(255, 255, 255, 255))

        # Clip the whole orb to a clean circle so the blurred glow can't tint
        # the square corners of the frame.
        circ = Image.new("L", (sz, sz), 0)
        ImageDraw.Draw(circ).ellipse([0, 0, sz - 1, sz - 1], fill=255)
        img.putalpha(ImageChops.multiply(img.split()[3], circ))
        return img.resize((ORB, ORB), Image.LANCZOS)

    def _load_orb_sprite():
        # Prefer the REAL frontend orb: orb_sprite.png next to this module is a
        # static sprite sheet of the actual .stop-agent-button (from
        # frontend/css/style.css) on a TRANSPARENT background, so each frame
        # composites straight onto the pill. Each square frame is downscaled to
        # the orb size. Falls back to the hand-drawn orb below if the sprite is
        # missing (e.g. not bundled in a build).
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "orb_sprite.png")
            sheet = Image.open(path).convert("RGBA")
            fw = sheet.width
            count = sheet.height // fw
            if count < 1:
                return None
            return [sheet.crop((0, k * fw, fw, (k + 1) * fw))
                    .resize((ORB, ORB), Image.LANCZOS) for k in range(count)]
        except Exception:
            return None

    ORB_FRAMES = _load_orb_sprite() or [_orb_frame(k / N_ORB_FRAMES)
                                        for k in range(N_ORB_FRAMES)]

    # The sprite frames already have a TRANSPARENT background (the white was
    # flood-filled out at generation time, leaving only the orb), and the
    # hand-drawn fallback is drawn on transparent too — so the orb composites
    # straight onto the white pill with no white fill / circle edge to leave a
    # grey line. Nothing to clip here.
    N_ORB_FRAMES = len(ORB_FRAMES)

    def _read_clipboard():
        try:
            CF_UNICODETEXT = 13
            if not user32.OpenClipboard(None):
                return ""
            try:
                h = user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return ""
                kernel32.GlobalLock.restype = C.c_void_p
                ptr = kernel32.GlobalLock(C.c_void_p(h))
                if not ptr:
                    return ""
                try:
                    return C.wstring_at(ptr)
                finally:
                    kernel32.GlobalUnlock(C.c_void_p(h))
            finally:
                user32.CloseClipboard()
        except Exception:
            return ""

    class Pill:
        def __init__(self):
            sw = user32.GetSystemMetrics(0)
            pill_x = sw - SCREEN_M - PILL_W      # pill's left edge (screen)
            self.x = pill_x - MARGIN             # canvas left
            self.y = SCREEN_M - MARGIN           # canvas top
            self.mode = "msg"
            self.text = "Starting…"
            self.left_label = ""
            self.right_label = ""
            self.save_label = "Save"
            self.input_buf = ""
            self.btn_rects = {}                  # name -> (x1,y1,x2,y2) canvas
            self.dragging = False
            self.drag_cursor = (0, 0)
            self.drag_win = (0, 0)
            self.pending = None
            self.dirty = True
            self.lock = threading.Lock()
            self.cur_h = MIN_CH                  # current canvas height
            self.target_h = MIN_CH
            self.orb_i = 0
            self.orb_counter = 0
            self.orb_x = int(round(ORB_CX - ORB / 2.0))
            self.orb_y = int(round(ORB_CY - ORB / 2.0))
            self._base = None

            self._make_window()
            self._make_dib()
            self.target_h = self._compute_target_h()
            self.cur_h = self.target_h
            self._build_base()
            self._blit()
            user32.SetTimer(self.hwnd, 1, 16, None)
            _emit("READY")
            _log("layered setup banner: READY")
            threading.Thread(target=self._stdin, daemon=True).start()
            self._loop()

        # ----- window / bitmap -----
        def _make_window(self):
            self.hinst = kernel32.GetModuleHandleW(None)
            self._wndproc = WNDPROC(self._on_message)   # strong ref
            wc = WNDCLASS()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = self.hinst
            wc.hCursor = user32.LoadCursorW(None, C.c_void_p(IDC_ARROW))
            wc.lpszClassName = "AutoUseLayeredPill"
            self._wc = wc
            user32.RegisterClassW(C.byref(wc))
            ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
            self.hwnd = user32.CreateWindowExW(
                ex, "AutoUseLayeredPill", "AutoUseBanner", WS_POPUP,
                self.x, self.y, CW, MIN_CH, None, None, self.hinst, None)
            user32.ShowWindow(self.hwnd, SW_SHOW)

        def _make_dib(self):
            # DIB is allocated at the MAX height once; each blit renders the
            # pill into the top `cur_h` rows and UpdateLayeredWindow uses only
            # that many rows, so the pill can grow/shrink without realloc.
            self.screen_dc = user32.GetDC(None)
            self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = C.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = CW
            bmi.bmiHeader.biHeight = -MAX_CH     # top-down, max height
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = BI_RGB
            self._bmi = bmi
            self.bits = C.c_void_p()
            self.hbmp = gdi32.CreateDIBSection(
                self.screen_dc, C.byref(bmi), DIB_RGB_COLORS,
                C.byref(self.bits), None, 0)
            self.old_obj = gdi32.SelectObject(self.mem_dc, self.hbmp)

        # ----- rendering -----
        def _compute_target_h(self):
            """Canvas height to show the full message (wrapped) plus any
            control row, clamped to [MIN_CH, MAX_CH]. The first line is
            centred on the orb at MIN height; each extra line adds LINE_H and
            the pill grows downward (the first line never moves)."""
            with self.lock:
                mode, text = self.mode, self.text
            lines, btn_newline = _layout(mode, text)
            n = len(lines) + (1 if btn_newline else 0)
            if mode in ("choice", "input"):
                pill_h = PILL_H_MIN + (len(lines) - 1) * LINE_H + CTRL_GAP + BTN_H
            else:
                pill_h = PILL_H_MIN + (n - 1) * LINE_H
            pill_h = max(PILL_H_MIN, min(MAX_PILL_H, pill_h))
            return pill_h + 2 * MARGIN

        def _pill_bg(self, h):
            # White rounded-rect pill of canvas height h. Corner radius is
            # FIXED (CORNER) so it's a stadium at MIN_CH and a clean rounded-
            # rectangle when grown — never a fat oval (matches macOS).
            pill_h = h - 2 * MARGIN
            big = Image.new("RGBA", (CW * SS, h * SS), (0, 0, 0, 0))
            ImageDraw.Draw(big).rounded_rectangle(
                [MARGIN * SS, MARGIN * SS,
                 (MARGIN + PILL_W) * SS - 1, (MARGIN + pill_h) * SS - 1],
                radius=CORNER * SS, fill=(255, 255, 255, 255))
            return big.resize((CW, h), Image.LANCZOS)

        def _aa_rrect(self, img, x1, y1, x2, y2, radius, fill=None,
                      outline=None, width=1):
            # Anti-aliased rounded rectangle: drawn at 4x on its own layer,
            # downscaled with LANCZOS, then composited. Pillow's
            # rounded_rectangle is otherwise 1-bit/aliased — that's the
            # staircase on the buttons' curved ends. (The pill body is already
            # supersampled in _pill_bg; this fixes the inner content only.)
            w = max(1, int(round(x2 - x1)))
            h = max(1, int(round(y2 - y1)))
            s = 4
            layer = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
            ImageDraw.Draw(layer).rounded_rectangle(
                [0, 0, w * s - 1, h * s - 1], radius=max(0.0, radius) * s,
                fill=fill, outline=outline, width=max(1, int(round(width * s))))
            img.alpha_composite(layer.resize((w, h), Image.LANCZOS),
                                (int(round(x1)), int(round(y1))))

        def _draw_button(self, img, x1, y1, x2, y2, label, name, font=FONT_BTN):
            self._aa_rrect(img, x1, y1, x2, y2, (y2 - y1) / 2.0, fill=PURPLE)
            d = ImageDraw.Draw(img)
            tw = d.textlength(label, font=font)
            bb = d.textbbox((0, 0), label, font=font)
            d.text(((x1 + x2) / 2 - tw / 2,
                    (y1 + y2) / 2 - (bb[3] + bb[1]) / 2),
                   label, font=font, fill=PURPLE_TXT)
            self.btn_rects[name] = (x1, y1, x2, y2)

        def _vtext(self, d, x, cy, text, font, fill):
            bb = d.textbbox((0, 0), text or "Ag", font=font)
            d.text((x, cy - (bb[3] + bb[1]) / 2), text, font=font, fill=fill)

        def _build_base(self):
            """Render pill + wrapped text + controls (everything EXCEPT the
            orb, which is composited per-frame in _blit) into self._base at
            the current canvas height. Records button hit-rects."""
            with self.lock:
                mode = self.mode
                text = self.text
                left_label, right_label = self.left_label, self.right_label
                save_label, input_buf = self.save_label, self.input_buf
            h = self.cur_h
            img = self._pill_bg(h)
            d = ImageDraw.Draw(img)
            self.btn_rects = {}
            right = TEXT_RIGHT

            # Message text, wrapped. The FIRST line is centred on the orb
            # (LINE_Y0 = pill centre at MIN height); each extra line flows
            # below at LINE_H spacing, so the first line stays put as the pill
            # grows downward — to the right of the orb.
            lines, btn_newline = _layout(mode, text)
            for i, ln in enumerate(lines):
                self._vtext(d, TEXT_X0, MARGIN + LINE_Y0 + i * LINE_H,
                            ln, FONT_TEXT, MSG_COL)
            last_cy = MARGIN + LINE_Y0 + (len(lines) - 1) * LINE_H
            ctrl_cy = last_cy + LINE_H / 2 + CTRL_GAP + BTN_H / 2

            if mode == "next":
                # Button right AFTER the last word (same line); drops to its
                # own line below only if it can't fit there.
                last_w = FONT_TEXT.getlength(lines[-1])
                if btn_newline:
                    bx1 = TEXT_X0
                    bcy = MARGIN + LINE_Y0 + len(lines) * LINE_H
                else:
                    bx1 = TEXT_X0 + last_w + GAP
                    bcy = last_cy
                self._draw_button(img, bx1, bcy - BTN_H / 2,
                                  bx1 + _next_btn_w, bcy + BTN_H / 2,
                                  _NEXT_LABEL, "next")
            elif mode == "choice":
                rw = int(d.textlength(right_label, font=FONT_BTN)) + 2 * BTN_PAD_X
                lw = int(d.textlength(left_label, font=FONT_BTN)) + 2 * BTN_PAD_X
                rx1 = right - rw
                lx2 = rx1 - GAP
                lx1 = lx2 - lw
                self._draw_button(img, lx1, ctrl_cy - BTN_H / 2, lx2,
                                  ctrl_cy + BTN_H / 2, left_label, "left")
                self._draw_button(img, rx1, ctrl_cy - BTN_H / 2, right,
                                  ctrl_cy + BTN_H / 2, right_label, "right")
            elif mode == "input":
                sw_ = int(d.textlength(save_label, font=FONT_BTN)) + 2 * BTN_PAD_X
                sx1 = right - sw_
                self._draw_button(img, sx1, ctrl_cy - BTN_H / 2, right,
                                  ctrl_cy + BTN_H / 2, save_label, "save")
                fx1 = TEXT_X0
                fx2 = sx1 - GAP
                fy1, fy2 = ctrl_cy - S(14), ctrl_cy + S(14)
                self._aa_rrect(img, fx1, fy1, fx2, fy2, S(13),
                               outline=FIELD_BORDER, width=max(1, S(1)),
                               fill=(255, 255, 255, 255))
                inner_l = fx1 + S(12)
                inner_w = fx2 - inner_l - S(8)
                if input_buf:
                    shown = input_buf
                    while shown and d.textlength(shown, font=FONT_TEXT) > inner_w:
                        shown = shown[1:]            # scroll to keep the tail
                    self._vtext(d, inner_l, ctrl_cy, shown, FONT_TEXT, TEXT_COL)
                    caret_x = inner_l + d.textlength(shown, font=FONT_TEXT) + S(1)
                    d.line([(caret_x, ctrl_cy - S(8)), (caret_x, ctrl_cy + S(8))],
                           fill=TEXT_COL, width=max(1, S(1)))
                else:
                    self._vtext(d, inner_l, ctrl_cy, "Paste your token…",
                                FONT_TEXT, PLACEHOLDER)
            self._base = img

        def _blit(self):
            if self._base is None:
                self._build_base()
            frame = self._base.copy()
            frame.alpha_composite(ORB_FRAMES[self.orb_i], (self.orb_x, self.orb_y))
            r, g, b, a = frame.split()
            out = Image.merge("RGBA", (ImageChops.multiply(b, a),
                                       ImageChops.multiply(g, a),
                                       ImageChops.multiply(r, a), a))
            data = out.tobytes()
            C.memmove(self.bits, data, len(data))
            ptDst = POINT(self.x, self.y)
            size = SIZE(CW, self.cur_h)
            ptSrc = POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            user32.UpdateLayeredWindow(
                self.hwnd, self.screen_dc, C.byref(ptDst), C.byref(size),
                self.mem_dc, C.byref(ptSrc), 0, C.byref(blend), ULW_ALPHA)

        # ----- hit-testing -----
        def _hit(self):
            p = POINT()
            user32.GetCursorPos(C.byref(p))
            rx, ry = p.x - self.x, p.y - self.y
            for name, (x1, y1, x2, y2) in self.btn_rects.items():
                if x1 <= rx <= x2 and y1 <= ry <= y2:
                    return name
            return None

        # ----- message handling -----
        def _on_message(self, hwnd, msg, wparam, lparam):
            if msg == WM_TIMER:
                self._tick()
                return 0
            if msg == WM_APP_REPAINT:
                # Used by token-input typing: input_buf changed, height is
                # unchanged, so just rebuild the base (new text) and blit.
                self._build_base()
                self._blit()
                return 0
            if msg == WM_APP_CLOSE:
                user32.DestroyWindow(self.hwnd)
                return 0
            if msg == WM_APP_FOCUS:
                # Token-input step needs keyboard focus so typing / Ctrl+V
                # land in the pill. Best-effort: foreground may be denied by
                # the OS if we're not the active app, in which case a click on
                # the pill (which calls SetFocus below) still focuses it.
                try:
                    user32.SetForegroundWindow(self.hwnd)
                    user32.SetFocus(self.hwnd)
                except Exception:
                    pass
                return 0
            if msg == WM_LBUTTONDOWN:
                user32.SetFocus(self.hwnd)        # clicking focuses the pill
                self.pending = self._hit()
                if self.pending is None:
                    self.dragging = True
                    user32.SetCapture(self.hwnd)
                    p = POINT()
                    user32.GetCursorPos(C.byref(p))
                    self.drag_cursor = (p.x, p.y)
                    self.drag_win = (self.x, self.y)
                return 0
            if msg == WM_MOUSEMOVE and self.dragging:
                p = POINT()
                user32.GetCursorPos(C.byref(p))
                self.x = self.drag_win[0] + (p.x - self.drag_cursor[0])
                self.y = self.drag_win[1] + (p.y - self.drag_cursor[1])
                user32.SetWindowPos(self.hwnd, None, self.x, self.y, 0, 0,
                                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                return 0
            if msg == WM_LBUTTONUP:
                if self.dragging:
                    self.dragging = False
                    user32.ReleaseCapture()
                    return 0
                name = self._hit()
                if name and name == self.pending:
                    self._fire(name)
                self.pending = None
                return 0
            if msg == WM_CHAR:
                with self.lock:
                    is_input = self.mode == "input"
                if is_input:
                    ch = wparam
                    if ch == 0x08:                       # backspace
                        with self.lock:
                            self.input_buf = self.input_buf[:-1]
                    elif ch in (0x0D, 0x0A):             # enter -> save
                        with self.lock:
                            val = self.input_buf
                        _emit("SAVE", value=val.strip())
                    elif ch == 0x16:                     # Ctrl+V -> paste
                        pasted = _read_clipboard()
                        if pasted:
                            with self.lock:
                                self.input_buf += pasted.replace("\r", "").replace("\n", "")
                    elif ch >= 0x20:                     # printable
                        with self.lock:
                            self.input_buf += chr(ch)
                    self._post_repaint()
                return 0
            if msg == WM_DESTROY:
                self._cleanup()
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        def _fire(self, name):
            if name == "next":
                _emit("NEXT")
            elif name in ("left", "right"):
                _emit("CHOICE", value=name)
            elif name == "save":
                with self.lock:
                    val = self.input_buf
                _emit("SAVE", value=val.strip())

        def _post_repaint(self):
            user32.PostMessageW(self.hwnd, WM_APP_REPAINT, 0, 0)

        # ----- per-frame tick: orb animation + smooth height growth -----
        def _tick(self):
            # Advance the orb animation (cycle pre-rendered frames).
            self.orb_counter += 1
            orb_changed = False
            if self.orb_counter >= ORB_TICKS_PER_FRAME:
                self.orb_counter = 0
                self.orb_i = (self.orb_i + 1) % N_ORB_FRAMES
                orb_changed = True

            # Pick up state changes (new text / mode) → recompute target size.
            relayout = False
            with self.lock:
                if self.dirty:
                    self.dirty = False
                    relayout = True
            if relayout:
                self.target_h = self._compute_target_h()

            # Smoothly animate the canvas height toward the target. Top edge
            # is anchored (self.y fixed), so the pill grows/shrinks downward.
            height_changed = False
            if self.cur_h != self.target_h:
                diff = self.target_h - self.cur_h
                step = max(S(6), abs(diff) // 3)
                if abs(diff) <= step:
                    self.cur_h = self.target_h
                else:
                    self.cur_h += step if diff > 0 else -step
                height_changed = True

            if relayout or height_changed:
                self._build_base()
                self._blit()
            elif orb_changed:
                self._blit()

        # ----- stdin command reader -----
        def _set(self, **kw):
            with self.lock:
                for k, v in kw.items():
                    setattr(self, k, v)
                self.dirty = True

        def _stdin(self):
            _log("layered setup banner: stdin reader started")
            try:
                for line in sys.stdin:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        m = json.loads(line)
                    except Exception:
                        continue
                    cmd = m.get("cmd")
                    if cmd == "MSG":
                        self._set(text=m.get("text", "") or "")
                    elif cmd == "SHOW_NEXT":
                        self._set(mode="next")
                    elif cmd == "HIDE_NEXT":
                        self._set(mode="msg")
                    elif cmd == "SHOW_CHOICE":
                        self._set(mode="choice",
                                  left_label=m.get("left", ""),
                                  right_label=m.get("right", ""))
                    elif cmd == "SHOW_INPUT":
                        self._set(mode="input",
                                  save_label=m.get("label", "Save"),
                                  input_buf="")
                        user32.PostMessageW(self.hwnd, WM_APP_FOCUS, 0, 0)
                    elif cmd == "CLEAR":
                        self._set(mode="msg")
                    elif cmd == "CLOSE":
                        user32.PostMessageW(self.hwnd, WM_APP_CLOSE, 0, 0)
                        return
            except Exception:
                import traceback
                _log("layered setup banner: stdin raised:\n"
                     + traceback.format_exc())
            # stdin reached EOF (or broke) without a CLOSE command — the
            # parent process is gone (e.g. the frontend UI was closed, which
            # kills the app that spawned us). Tear the banner down so it's
            # tied to the UI's lifetime and never lingers as an orphan.
            _log("layered setup banner: stdin EOF — parent gone, closing")
            try:
                user32.PostMessageW(self.hwnd, WM_APP_CLOSE, 0, 0)
            except Exception:
                pass

        def _cleanup(self):
            try:
                user32.KillTimer(self.hwnd, 1)
            except Exception:
                pass
            try:
                if getattr(self, "old_obj", None):
                    gdi32.SelectObject(self.mem_dc, self.old_obj)
                if getattr(self, "hbmp", None):
                    gdi32.DeleteObject(self.hbmp)
                if getattr(self, "mem_dc", None):
                    gdi32.DeleteDC(self.mem_dc)
                if getattr(self, "screen_dc", None):
                    user32.ReleaseDC(None, self.screen_dc)
            except Exception:
                pass

        def _loop(self):
            msg = MSG()
            while user32.GetMessageW(C.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(C.byref(msg))
                user32.DispatchMessageW(C.byref(msg))

    try:
        Pill()
    except Exception:
        import traceback
        _log("layered setup banner: crashed:\n" + traceback.format_exc())
        raise
    _emit("CLOSED")
    _log("layered setup banner: exit (CLOSED)")


# ── subprocess entry point ────────────────────────────────────────────────


def _run_subprocess_banner() -> None:
    """Subprocess body. Imports webview lazily so the parent (which only
    uses StatusBanner) doesn't pay its startup cost when it imports this
    module.

    Mirrors `banner_test.py` byte-for-byte except for the JSON-stdio
    protocol that lets the parent drive the wizard state machine."""
    _log(f"subprocess start (sys.executable={sys.executable!r})")
    # Top-level guard: any exception escaping the GUI setup or webview.start()
    # must land in the debug log — otherwise the user sees the pill flash and
    # vanish with nothing to point at. Each step is also wrapped individually
    # so we know exactly which one died.
    try:
        compact = "--compact" in sys.argv[1:]
        if not compact:
            # Setup-wizard pill: rendered with pill.py's layered-window
            # technique (Win32 UpdateLayeredWindow + per-pixel alpha, no
            # WebView2) for a clean, borderless, anti-aliased pill that blends
            # into the wallpaper. Only the compact task-progress pill below
            # still uses pywebview.
            _run_layered_setup_banner()
            return

        import webview
        _log("webview imported")

        # Primary-screen width via Win32. GetSystemMetrics(SM_CXSCREEN=0)
        # returns the DPI-virtualised value in this freshly spawned,
        # still-DPI-unaware subprocess — identical to what tkinter's
        # winfo_screenwidth() returned before, without dragging the
        # tcl/tk runtime into the Nuitka binary (tkinter is listed in
        # nofollow_third_party in windows_binary_build.py:533 and is
        # therefore not bundled in the compiled exe).
        try:
            screen_w = ctypes.windll.user32.GetSystemMetrics(0) or 1920
        except Exception:
            screen_w = 1920

        w = COMPACT_MIN_W if compact else PILL_WIDTH
        h = COMPACT_MIN_H if compact else PILL_HEIGHT
        x = max(0, screen_w - w - SCREEN_MARGIN)
        y = SCREEN_MARGIN
        html = COMPACT_HTML if compact else BANNER_HTML
        title = f"AutoUseBanner_{uuid.uuid4().hex[:8]}"
        state = _BannerState(
            title=title, width=w, min_h=h, compact=compact,
            screen_w=screen_w, top_margin=SCREEN_MARGIN,
            right_margin=SCREEN_MARGIN,
        )
        (next_clicked, choice_clicked, save_clicked,
         height_changed, size_changed) = _make_js_handlers(state)

        # No js_api here — methods on a Nuitka-compiled class fail pywebview's
        # `inspect.ismethod` filter and never get exposed to JS. We register
        # the handlers via window.expose() below instead.
        window = webview.create_window(
            title,
            html=html,
            width=w,
            height=h,
            min_size=(w, h),
            x=x,
            y=y,
            frameless=True,
            on_top=True,
            easy_drag=True,
            resizable=False,
            # Setup pill: a TRANSPARENT WebView2 window so the CSS
            # border-radius can draw the rounded pill on the actual pixels
            # (the web equivalent of pill.py's alpha-drawn rounded rect).
            # This alone is NOT enough: pywebview makes the WebView2 *content*
            # transparent but leaves the WinForms *form* opaque white behind
            # it (that white form was the "container"). So _on_shown also
            # clips the form into the matching stadium with SetWindowRgn —
            # transparency rounds the content, the region clip rounds the
            # form, and together they give a clean pill with the desktop
            # showing through the corners. The compact pill stays opaque
            # (transparent=False) + SetWindowRgn.
            transparent=(not compact),
        )
        state.window = window
        window.expose(next_clicked, choice_clicked, save_clicked,
                      height_changed, size_changed)
        _log("window created and handlers exposed")

        def _on_shown():
            _log("on_shown: entered")
            # Compact mode: WinForms stretches our small create_window
            # request to its OS-imposed minimum width (~132+ logical px).
            # A programmatic window.resize() AFTER the form is alive
            # bypasses that minimum. We size to COMPACT_MIN_W × COMPACT_MIN_H
            # initially; once the page loads and the ResizeObserver fires,
            # size_changed will resize the window to fit the natural
            # content (orb-only until text streams in).
            if compact:
                try:
                    window.resize(COMPACT_MIN_W, COMPACT_MIN_H)
                    # Reposition to anchor top-right based on the actual
                    # initial size — without this WinForms may have placed
                    # the wider initial window further left than we want.
                    new_x = max(
                        0, state.screen_w - COMPACT_MIN_W - state.right_margin
                    )
                    try:
                        window.move(new_x, state.top_margin)
                    except Exception:
                        pass
                    # Give WinForms one frame to actually realise the new
                    # rect before _apply_rounded_region reads it — without
                    # this the region clip runs against the old wide-pill
                    # geometry.
                    time.sleep(0.1)
                except Exception:
                    pass
            else:
                # Setup pill: do NOT resize after show. Resizing a
                # transparent WebView2 window after its page has loaded does
                # not reflow the content — the pill keeps rendering at the
                # pre-resize size while the OS window is the new size, and the
                # mismatch clips the pill into a square-edged RECTANGLE
                # (reproduced + verified visually). The window is created at
                # PILL_WIDTH × PILL_HEIGHT with min_size=(PILL_WIDTH,
                # PILL_HEIGHT), which bypasses the WinForms min-size clamp at
                # CREATE time, so it is already the right size and positioned
                # top-right by create_window's x/y.
                pass
            # Clip the window into a stadium with SetWindowRgn — for BOTH
            # modes, but for different reasons:
            #   • Compact pill: its body is opaque white, so the region clip
            #     rounds the visible pill directly.
            #   • Setup pill: transparent=True makes the WebView2 *content*
            #     transparent (so the CSS border-radius pill shows), but
            #     pywebview leaves the WinForms *form* opaque white behind it
            #     — that white form is the "container" you saw. SetWindowRgn
            #     CAN clip the GDI form (it just can't clip WebView2's
            #     composited content), so the region carves the white form
            #     into the same stadium the CSS draws → the corners show the
            #     desktop, container gone. (Verified: transparent + region =
            #     clean stadium; transparent alone = white rounded-rect.)
            _apply_rounded_region(title)
            if compact:
                _make_click_through(title)
            else:
                # WinForms can reset a raw SetWindowRgn during its post-show
                # layout passes, which would bring the white form-corners
                # back. Re-apply a few times over the first ~2 s so the clip
                # sticks. (The compact pill re-clips via size_changed; the
                # fixed-size setup pill has no such follow-up.)
                def _reclip_setup():
                    for delay in (0.3, 0.7, 1.3, 2.2):
                        time.sleep(delay)
                        try:
                            _apply_rounded_region(title)
                        except Exception:
                            pass
                threading.Thread(target=_reclip_setup, daemon=True).start()
            _log("on_shown: about to emit READY")
            _emit("READY")
            _log("on_shown: READY emitted")
            # Spawn the stdin reader once the window is up.
            threading.Thread(
                target=_stdin_reader, args=(window,), daemon=True
            ).start()
            _log("on_shown: stdin reader thread spawned, exiting handler")

        window.events.shown += _on_shown

        # Lifecycle observability: log if the window starts closing or has been
        # closed by anything other than our own CLOSE command. `events.closing`
        # handlers must return a truthy value to allow the close; the tuple-idiom
        # logs first and then yields True.
        window.events.closing += lambda: (_log("event: closing"), True)[1]
        window.events.closed += lambda: _log("event: closed")

        # Give the subprocess's WebView2 environment its own UserDataFolder.
        # pywebview's default is %APPDATA%\pywebview ([winforms.py:704]) — shared
        # process-wide. In the compiled exe the parent (main AutoUse window) and
        # this subprocess are both AutoUse.exe and would otherwise contend on the
        # same folder, which can cause WebView2 to tear down our renderer process
        # seconds into operation. A per-PID temp folder is invisible to dev mode
        # (each python interpreter already has its own folder) and isolates the
        # banner subprocess cleanly in the binary build.
        storage_path = os.path.join(
            tempfile.gettempdir(), f"autouse_banner_{os.getpid()}"
        )
        _log(f"webview.start(storage_path={storage_path!r})")

        # webview.start() runs the GUI loop in this subprocess's main thread.
        # Blocks until window.destroy() — which the CLOSE command triggers.
        try:
            webview.start(storage_path=storage_path)
            _log("webview.start() returned normally")
        except Exception:
            import traceback
            _log("webview.start() raised:\n" + traceback.format_exc())
            raise

        _emit("CLOSED")
        _log("subprocess exit (CLOSED emitted)")
    except Exception:
        # Catches anything escaping the GUI setup so we have a footprint
        # in the log instead of just "subprocess vanished".
        import traceback
        _log("_run_subprocess_banner crashed:\n" + traceback.format_exc())
        raise


# ── parent-side wrapper ──────────────────────────────────────────────────


class StatusBanner:
    """Drop-in Windows mirror of the macOS Cocoa banner, backed by a
    subprocess that runs the pywebview pill independently."""

    # Module path the subprocess runs. After merging banner_proc.py
    # into this file, the subprocess re-executes THIS module with the
    # `if __name__ == "__main__"` guard firing into
    # _run_subprocess_banner().
    _PROC_MODULE = "Auto_Use.windows_use.remote_connection.telegram.banner"

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
        # Telegram bot), giving the user a second main window instead of
        # the pill. Re-exec AutoUse.exe with --banner-mode so app.py's
        # main() can route directly to _run_subprocess_banner. In dev
        # (`python app.py`) sys.executable IS a python interpreter, so
        # the old -m invocation still works and is preferred — it avoids
        # the cost of bootstrapping app.py just to reach the banner.
        # cwd: pin the subprocess to the binary's install dir in the compiled
        # build so WebView2's native DLL loader resolves WebView2Loader.dll,
        # WebBrowserInterop.x64.dll, etc. from the install folder regardless
        # of what cwd the parent inherited (a Start-menu launch leaves cwd
        # at the user's home dir; a shortcut can leave it anywhere). In dev
        # mode cwd=None inherits the parent's, which is the repo root —
        # matches the working behaviour.
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
        # Both modes accept MSG now — the compact pill renders the
        # thinking-stream text in its msg span and grows to fit.
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

        # Ask the subprocess to close gracefully; fall back to terminate.
        self._send({"cmd": "CLOSE"})
        try:
            self._proc.wait(timeout=3)
        except Exception:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None

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
        # any pending waiters so callers don't deadlock.
        self._closed.set()
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
