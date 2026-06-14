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

Lives at remote_connection/ (outside any single surface folder) so the pill +
orb visual engine is written once and reused by every surface — telegram/ today,
discord/ and whatsapp/ later. Each surface's own folder holds the surface-specific
behaviour (setup wizard steps, agent stream) and drives this banner over the same
`StatusBanner` API; new surfaces import it unchanged. Telegram is the first caller.

A small always-on-top pill at the top-right of the screen that contains:
  - the animated stop-orb on the left,
  - a status message in the middle (multi-line capable; pill grows downward),
  - a clickable "Next" button on the right (only visible when the script is
    waiting for the user — hidden during processing steps).

setup.py calls show() once, then alternates update("…") + wait_for_next()
to pace the user. close() tears it down. The Next button is shown
automatically inside wait_for_next() and hidden as soon as it returns, so
callers don't have to manage visibility manually.

The pill default height is the original 44px. When a long status message
wraps to multiple lines a ResizeObserver in JS posts the new body height
back to Python via a second WKScriptMessageHandler, and Python resizes the
NSWindow (top edge anchored, height grows downward).

Everything runs inside the existing Python process. pywebview's main-thread
NSApplication run loop (started by webview.start() in app.py) is reused —
AppKit work is dispatched onto it via PyObjCTools.AppHelper.callAfter so the
Flask worker thread that runs setup.py never touches Cocoa directly.

If Cocoa/PyObjC isn't importable for any reason the class becomes a no-op
so the automation still completes without a banner.
"""
import logging
import threading

logger = logging.getLogger(__name__)

try:
    from Cocoa import (
        NSPanel, NSColor, NSScreen,
        NSBackingStoreBuffered, NSMakeRect,
    )
    from Foundation import NSObject
    from WebKit import WKWebView, WKWebViewConfiguration
    from PyObjCTools.AppHelper import callAfter
    _COCOA_OK = True
except Exception as e:
    logger.warning(f"banner: Cocoa unavailable, popup disabled ({e})")
    _COCOA_OK = False

# Non-activating panel: clicks inside the WebView do NOT activate the Python
# process, so the AutoUse main pywebview window can't pop over Safari while
# the wizard is running. The panel still becomes key when a text input needs
# keyboard focus (setBecomesKeyOnlyIfNeeded_).
NSWindowStyleMaskNonactivatingPanel = 1 << 7  # 128
NSStatusWindowLevel = 25


BANNER_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
html, body { margin: 0; padding: 0; width: 100%; background: transparent;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
html { height: 100%; }
/* The orb is absolute-positioned (top-left, anchored), and the body has
   extra left padding (= orb-width 36 + gap 8 = 44) so flex content starts
   to the right of the orb. This decouples orb position from message
   height: no matter how many lines the message wraps to, the orb stays
   exactly where it started — first line of text stays next to it,
   additional lines flow below. */
body { display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  padding: 6px 10px 6px 54px; box-sizing: border-box;
  min-height: 44px; overflow: hidden; position: relative; }

.orb-wrap { position: absolute; top: 6px; left: 10px;
  width: 36px; height: 36px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center; }
/* The orb itself is pc_button.html embedded in an iframe (single source of
   truth); centred + click-through, a touch bigger so the glow isn't clipped. */
.orb-frame { position: absolute; top: 50%; left: 50%;
  width: 50px; height: 50px; transform: translate(-50%, -50%);
  border: 0; background: transparent; pointer-events: none; }
.stop-circle-1 {
  width: 36px; height: 36px; border-radius: 50%; position: absolute; background: transparent;
  display: flex; align-items: center; justify-content: center;
  animation: stop-pulse 4.2s ease-in-out infinite 0.3s; z-index: 1;
}
.stop-circle-1::before, .stop-circle-1::after {
  content: ""; position: absolute; border-radius: 50%; filter: blur(7px); width: 30%; height: 30%;
}
.stop-circle-1::before { background: #ff0073; top: 30%; right: 30%; }
.stop-circle-1::after  { background: #00baff; bottom: 10%; left: 30%; }
.stop-circle-2 {
  width: 28px; height: 28px; border-radius: 50%; position: absolute; inset: 0; margin: auto;
  background-color: white; z-index: 9;
  animation: stop-pulse2 4.2s ease-in-out infinite;
}
.stop-circle-2::before, .stop-circle-2::after {
  content: ""; position: absolute; border-radius: 50%; filter: blur(5px); z-index: 1;
}
.stop-circle-2::before { background: #ff0073; width: 30%; height: 30%; top: 20%; right: 20%; }
.stop-circle-2::after  { background: #00bbff; width: 20%; height: 20%; bottom: 10%; left: 40%; }
.stop-bg {
  position: absolute; inset: 0; border-radius: 50%;
  box-shadow: inset 0 0 5px 2px rgba(255,255,255,0.8), 0 0 2px 2px rgba(255,255,255,0.9);
  background-color: #9292d8; animation: stop-bgRotate 2.5s linear infinite;
}
.stop-bg::before {
  content: ""; position: absolute; inset: 0; border-radius: inherit;
  animation: stop-bgColor 4s linear infinite;
  box-shadow: inset 0 0 5px 2px rgba(255,255,255,0.8); opacity: 0.2;
}
.stop-pc {
  position: absolute; inset: 0; margin: auto; width: 28px; height: 28px; z-index: 10;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  box-sizing: border-box; gap: 1px;
}
.stop-monitor { width: 11px; height: 9px; background: transparent; border-radius: 1px; padding: 0;
  border: 1px solid white; box-sizing: border-box; }
.stop-screen { width: 100%; height: 100%; display: flex; justify-content: center; align-items: center; gap: 1px; }
.stop-eye { width: 2px; height: 3px; border-radius: 1px; background: white; position: relative; top: -1px; animation: stop-blink 4s infinite; }
.stop-base { width: 14px; height: 1px; background: white; border-radius: 0.5px; }

/* min-width: 0 is the flexbox shrink-below-content-size fix — without it a
   long message refuses to shrink and pushes the Next button off the pill.
   align-self + padding-top pin the first line to the same vertical spot it
   sits at when single-line — so when the text wraps, the first line stays
   put and the new line flows below it instead of the whole block sliding
   down to stay centered. */
.msg { flex: 1 1 auto; min-width: 0; font-size: 12.5px; color: #6b6b75;
  padding: 10px 4px 0; line-height: 1.35;
  word-wrap: break-word; overflow-wrap: break-word;
  align-self: flex-start; }

.next-btn { flex-shrink: 0; height: 28px; padding: 0 14px; border: none; border-radius: 14px;
  background: #5e6ad2; color: white; font-size: 12px; font-weight: 600; cursor: pointer;
  font-family: inherit; transition: background 0.15s ease; align-self: center; }
.next-btn:hover  { background: #6e7ce3; }
.next-btn:active { background: #4e5ac2; }

.choice-row { display: none; flex-shrink: 0; gap: 6px; align-self: center; }
.input-row { display: none; flex-basis: 100%; flex-direction: column; gap: 4px;
  padding: 2px 4px 0; order: 1; }
.input-line { display: flex; gap: 6px; align-items: center; }
#token-input { flex: 1 1 auto; height: 28px; border: 1px solid #d4d4dc; border-radius: 14px;
  padding: 0 10px; font-size: 12px; font-family: inherit; outline: none; color: #333;
  background: white; }
#token-input:focus { border-color: #5e6ad2; }
.input-error { display: none; color: #d23; font-size: 11px; padding: 0 4px; }

@keyframes stop-pulse  { 0%{transform:scale(.97)} 15%{transform:scale(1)} 30%{transform:scale(.98)} 45%{transform:scale(1)} 60%{transform:scale(.97)} 85%{transform:scale(1)} 100%{transform:scale(.97)} }
@keyframes stop-pulse2 { 0%{transform:scale(1)} 15%{transform:scale(1.03)} 30%{transform:scale(.98)} 45%{transform:scale(1.04)} 60%{transform:scale(.97)} 85%{transform:scale(1.03)} 100%{transform:scale(1)} }
@keyframes stop-bgRotate { 0%{transform:rotate(0)} 20%{transform:rotate(90deg)} 40%{transform:rotate(180deg) scale(.95,1)} 60%,100%{transform:rotate(360deg)} }
@keyframes stop-bgColor  { 20%{background-color:red} 40%{background-color:#5eff7e} 60%{background-color:#2cb5ff} 80%{background-color:#fc63ff} }
@keyframes stop-blink    { 0%,85%,100%{transform:scaleY(1)} 92%{transform:scaleY(.1)} }
</style></head>
<body>
<div class="orb-wrap">
  <iframe id="orbFrame" class="orb-frame" src="http://127.0.0.1:5000/pc_button.html"
          scrolling="no" frameborder="0"></iframe>
</div>
<span class="msg" id="msg">Starting…</span>
<button class="next-btn" id="next"
        onclick="webkit.messageHandlers.next_clicked.postMessage(1)">Next</button>
<div class="choice-row" id="choice-row">
  <button class="next-btn" id="choice-left"
          onclick="webkit.messageHandlers.choice_clicked.postMessage('left')">Left</button>
  <button class="next-btn" id="choice-right"
          onclick="webkit.messageHandlers.choice_clicked.postMessage('right')">Right</button>
</div>
<div class="input-row" id="input-row">
  <div class="input-line">
    <input type="text" id="token-input" placeholder="Paste your BotFather token here" />
    <button class="next-btn" id="save-btn"
            onclick="(function(){var v=document.getElementById('token-input').value;
                     webkit.messageHandlers.save_clicked.postMessage(v);})()">Save</button>
  </div>
  <div class="input-error" id="input-error"></div>
</div>
<script>
  // The orb is pc_button.html in an iframe; it starts hidden and shows when
  // told, so nudge it visible once the iframe has loaded.
  (function () {
    var f = document.getElementById('orbFrame');
    if (f) f.addEventListener('load', function () {
      try { f.contentWindow.postMessage('pcbtn:show', '*'); } catch (e) {}
    });
  })();
  // Word-by-word reveal: Python calls setMsg("…") with the full text; we
  // animate it in word-at-a-time so the banner reads smoothly. A new call
  // cancels any in-flight animation and starts over with the latest text.
  let _revealTimer = null;
  function setMsg(fullText) {
    if (_revealTimer) { clearTimeout(_revealTimer); _revealTimer = null; }
    const el = document.getElementById('msg');
    if (!el) return;
    const words = (fullText || '').split(/(\s+)/);  // keep whitespace tokens
    el.textContent = '';
    let i = 0;
    const step = () => {
      if (i >= words.length) {
        _revealTimer = null;
        // Tell Python the streaming reveal has finished so it can now show
        // whichever control set (Next / choice / input) is appropriate for
        // this step. Without this signal the button would pop in while the
        // text is still being typed out.
        try { webkit.messageHandlers.reveal_done.postMessage(1); } catch (e) {}
        return;
      }
      // Wrap each token in its own span and fade it in. Multiple spans are
      // in their transition at once because the inter-word delay (55 ms) is
      // shorter than the fade duration (220 ms) — that overlap is what
      // makes the stream read as smooth rather than as discrete pops.
      const span = document.createElement('span');
      span.textContent = words[i];
      span.style.opacity = '0';
      span.style.transition = 'opacity 220ms ease-out';
      el.appendChild(span);
      requestAnimationFrame(() => { span.style.opacity = '1'; });
      i++;
      _revealTimer = setTimeout(step, 55);
    };
    step();
  }
  window.setMsg = setMsg;

  // ── choice / input UI controls (paired with wait_for_choice / wait_for_input
  //    on the Python side). All three rows — #next, #choice-row, #input-row —
  //    are mutually exclusive: showing one hides the others.
  function setChoice(leftLabel, rightLabel) {
    document.getElementById('choice-left').textContent = leftLabel;
    document.getElementById('choice-right').textContent = rightLabel;
    document.getElementById('choice-row').style.display = 'flex';
    document.getElementById('next').style.display = 'none';
    document.getElementById('input-row').style.display = 'none';
  }
  function setInput(saveLabel) {
    document.getElementById('save-btn').textContent = saveLabel || 'Save';
    document.getElementById('input-row').style.display = 'flex';
    document.getElementById('choice-row').style.display = 'none';
    document.getElementById('next').style.display = 'none';
    document.getElementById('input-error').style.display = 'none';
    var inp = document.getElementById('token-input');
    inp.value = '';
    setTimeout(function(){ inp.focus(); }, 30);
  }
  function setInputError(msg) {
    var el = document.getElementById('input-error');
    if (msg) { el.textContent = msg; el.style.display = 'block'; }
    else     { el.style.display = 'none'; }
  }
  function clearAll() {
    document.getElementById('choice-row').style.display = 'none';
    document.getElementById('input-row').style.display = 'none';
    document.getElementById('input-error').style.display = 'none';
  }
  window.setChoice = setChoice;
  window.setInput = setInput;
  window.setInputError = setInputError;
  window.clearAll = clearAll;

  // Enter in the token input acts as Save.
  document.getElementById('token-input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
      webkit.messageHandlers.save_clicked.postMessage(this.value);
    }
  });

  // Tell Python whenever the body's natural height changes so the NSWindow
  // can grow/shrink to fit. Debounced to the last reported value to avoid
  // a resize loop (window resize → WebView resize → body re-measure → fire).
  (function () {
    let last = -1;
    const report = () => {
      const h = Math.ceil(document.body.scrollHeight);
      if (h === last) return;
      last = h;
      try { webkit.messageHandlers.height_changed.postMessage(h); } catch (e) {}
    };
    window.addEventListener('load', () => setTimeout(report, 30));
    const ro = new ResizeObserver(report);
    ro.observe(document.body);
    ro.observe(document.getElementById('msg'));
  })();
</script>
</body></html>
"""


# Compact HTML — used when StatusBanner(compact=True).
#
# Visual model:
#   Empty state (no task running):
#     ┌──┐
#     │○ │   44×44 white circle, just the orb.
#     └──┘
#
#   Text streaming (during a task):
#     ┌────────────────────────────────────────┐
#     │○  single-line text streams to the right│
#     └────────────────────────────────────────┘
#                                            440 px
#
# Pre-expand width, then stream (no jitter):
#  - body has fixed empty width (44) and fixed has-text width (440); the
#    height stays at 44 in both states. On first setMsg(), JS toggles
#    body.has-text, body width snaps 44→440, ResizeObserver fires once,
#    Python animates the NSPanel over ~0.25 s. JS then waits 350 ms
#    before appending the first word, so streaming starts AFTER the
#    banner finishes expanding.
#
# Paging through long messages:
#  - .msg is `white-space: nowrap; overflow: hidden` and capped at
#    max-width: 388 px. After each word, JS does a sync layout read to
#    check if scrollWidth has exceeded the visible width. If yes, the
#    word that overflowed is removed, we hold the current line briefly,
#    then clear and continue streaming the remaining words on a fresh
#    line. Loops until every word has been displayed.
COMPACT_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
html { margin: 0; padding: 0; background: transparent;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }

/* Empty state: 44×44 perfect circle (orb only). has-text: 440×44
   stadium — height stays the same, only width animates. Python's
   setFrame_display_animate_ handles the smooth NSPanel animation while
   the WKWebView frame follows it via its autoresizing mask. */
body { margin: 0; padding: 4px; box-sizing: border-box;
  background: transparent;
  display: flex; flex-direction: column; align-items: stretch; gap: 0;
  width: 44px; height: 44px; overflow: hidden; }
body.has-text { width: 440px; }
/* Coder terminal open: the pill expands into a wide, tall card. Declared
   AFTER .has-text so its width/height win regardless of the orb ticker's
   has-text toggle. Height is content-driven; Python clamps the window to
   COMPACT_CODER_MAX_H. */
body.coder { width: 540px; height: auto; padding-bottom: 12px; }

/* Top row = orb + the single-line step ticker (the original compact pill). */
.toprow { display: flex; align-items: center; gap: 8px; height: 36px;
  width: 100%; flex-shrink: 0; }

.orb-wrap { position: relative; width: 36px; height: 36px;
  flex-shrink: 0;
  display: flex; align-items: center; justify-content: center; }
/* The orb is telergam_animation.html embedded in an iframe (single source of
   truth); centred + click-through, a touch bigger so the glow isn't clipped. */
.orb-frame { position: absolute; top: 50%; left: 50%;
  width: 50px; height: 50px; transform: translate(-50%, -50%);
  border: 0; background: transparent; pointer-events: none; }

.stop-circle-1 {
  width: 36px; height: 36px; border-radius: 50%; position: absolute; background: transparent;
  display: flex; align-items: center; justify-content: center;
  animation: stop-pulse 4.2s ease-in-out infinite 0.3s; z-index: 1;
}
.stop-circle-1::before, .stop-circle-1::after {
  content: ""; position: absolute; border-radius: 50%; filter: blur(7px); width: 30%; height: 30%;
}
.stop-circle-1::before { background: #ff0073; top: 30%; right: 30%; }
.stop-circle-1::after  { background: #00baff; bottom: 10%; left: 30%; }
.stop-circle-2 {
  width: 28px; height: 28px; border-radius: 50%; position: absolute; inset: 0; margin: auto;
  background-color: white; z-index: 9;
  animation: stop-pulse2 4.2s ease-in-out infinite;
}
.stop-circle-2::before, .stop-circle-2::after {
  content: ""; position: absolute; border-radius: 50%; filter: blur(5px); z-index: 1;
}
.stop-circle-2::before { background: #ff0073; width: 30%; height: 30%; top: 20%; right: 20%; }
.stop-circle-2::after  { background: #00bbff; width: 20%; height: 20%; bottom: 10%; left: 40%; }
.stop-bg {
  position: absolute; inset: 0; border-radius: 50%;
  box-shadow: inset 0 0 5px 2px rgba(255,255,255,0.8), 0 0 2px 2px rgba(255,255,255,0.9);
  background-color: #9292d8; animation: stop-bgRotate 2.5s linear infinite;
}
.stop-bg::before {
  content: ""; position: absolute; inset: 0; border-radius: inherit;
  animation: stop-bgColor 4s linear infinite;
  box-shadow: inset 0 0 5px 2px rgba(255,255,255,0.8); opacity: 0.2;
}

/* Both icons stacked at the same spot; opposing keyframes cross-fade them. */
.icon-stack {
  position: absolute; inset: 0; margin: auto; width: 28px; height: 28px; z-index: 10;
  display: flex; align-items: center; justify-content: center;
}
.icon-layer {
  position: absolute; inset: 0;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 1px; box-sizing: border-box;
  /* Force each layer onto its own GPU compositor layer up-front so the
     opacity cross-fade doesn't trigger a one-frame promotion artifact (the
     "small square" flash). */
  will-change: opacity;
  transform: translateZ(0);
  -webkit-backface-visibility: hidden;
  backface-visibility: hidden;
}
.icon-pc { animation: icon-cycle-pc 10s ease-in-out infinite; }
.icon-tg { animation: icon-cycle-tg 10s ease-in-out infinite; color: white; }

.stop-monitor { width: 11px; height: 9px; background: transparent; border-radius: 1px; padding: 0;
  border: 1px solid white; box-sizing: border-box; }
.stop-screen { width: 100%; height: 100%; display: flex; justify-content: center; align-items: center; gap: 1px; }
.stop-eye { width: 2px; height: 3px; border-radius: 1px; background: white; position: relative; top: -1px; animation: stop-blink 4s infinite; }
.stop-base { width: 14px; height: 1px; background: white; border-radius: 0.5px; }

/* Single-line streaming text. white-space: nowrap means tokens line up
   left-to-right and never wrap. max-width caps the visible portion at
   exactly the remaining body width (388 = 440 - 4 pad - 36 orb - 8 gap
   - 4 pad), overflow: hidden clips anything past it. The JS pager
   watches scrollWidth and starts a new "page" before content actually
   overflows. */
.msg { font-size: 12.5px; color: #6b6b75; line-height: 1.35;
  white-space: nowrap; overflow: hidden;
  max-width: 388px; padding: 0; }
.msg:empty { display: none; }

/* ── embedded coder terminal panel (shown only while a CLI/coder runs) ── */
#coderPanel { display: none; position: relative; width: 100%; margin-top: 8px; box-sizing: border-box;
  border-radius: 12px; overflow: hidden; background: #f7f7f9;
  box-shadow: inset 0 0 0 1px rgba(139,92,246,0.55), inset 0 0 8px rgba(139,92,246,0.45);
  animation: cp-edgeGlow 3s ease-in-out infinite; }
body.coder #coderPanel { display: block; }
.cp-particles { position: absolute; inset: 0; width: 100%; height: 100%; z-index: 0; pointer-events: none; }
.cp-body { position: relative; z-index: 1; padding: 14px 14px 18px; color: rgba(0,0,0,0.82);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Courier New", monospace;
  font-size: 12px; line-height: 1.5; }
/* Single streaming line (paginated). Top shows the coder's real output (or
   filler while a minion runs); each minion row shows its own real output. */
.cp-output { min-height: 22px; overflow: hidden; }
.cp-line { white-space: nowrap; overflow: hidden; margin: 2px 0; }
.cp-mrow .cp-line { margin: 0; }
.cp-p { color: rgba(0,0,0,0.4); }
.cp-todos { margin: 9px 0 4px 0; display: flex; flex-direction: column; gap: 6px; }
.cp-todos.hidden { display: none; }
.cp-item { display: flex; align-items: center; gap: 8px; }
.cp-item .lbl { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cp-chk { width: 14px; height: 14px; min-width: 14px; border-radius: 4px; border: 1px solid rgba(0,0,0,0.3); }
/* Completed task: gradient box with a subtle breathing pulse. */
.cp-chk.done { background: linear-gradient(135deg,#8b5cf6,#3b82f6,#8b5cf6); background-size: 200% 200%;
  animation: cp-grad 2.5s ease infinite, cp-breathe 3s ease-in-out infinite; border-color: transparent; }
/* Current (first not-yet-done) task: a spinning loading circle. */
.cp-loading { width: 14px; height: 14px; min-width: 14px; box-sizing: border-box; border-radius: 50%;
  border: 2px solid rgba(139,92,246,0.3); border-top-color: #8b5cf6;
  animation: cp-spin 1s linear infinite; display: inline-block; }
.cp-minions { display: flex; flex-direction: column; gap: 6px; margin: 6px 0 2px 0; }
.cp-mrow { display: flex; align-items: center; gap: 8px; color: rgba(0,0,0,0.7);
  animation: cp-rise 0.28s ease-out; }
.cp-mrow .mline { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  opacity: 0.85; transition: opacity 0.25s ease; }
.tb { --s: 16px; --sp: 0.8s; --c: #5D3FD3; position: relative; display: inline-block;
  height: var(--s); width: var(--s); min-width: var(--s);
  animation: tb-spin calc(var(--sp)*2.5) infinite linear; }
.tb i { position: absolute; height: 100%; width: 30%; }
.tb i:after { content: ''; position: absolute; height: 0; width: 100%; padding-bottom: 100%; background: var(--c); border-radius: 50%; }
.tb i:nth-child(1) { bottom: 5%; left: 0; transform: rotate(60deg); transform-origin: 50% 85%; }
.tb i:nth-child(1):after { bottom: 0; left: 0; animation: tb-w1 var(--sp) infinite ease-in-out; animation-delay: calc(var(--sp)*-0.3); }
.tb i:nth-child(2) { bottom: 5%; right: 0; transform: rotate(-60deg); transform-origin: 50% 85%; }
.tb i:nth-child(2):after { bottom: 0; left: 0; animation: tb-w1 var(--sp) infinite calc(var(--sp)*-0.15) ease-in-out; }
.tb i:nth-child(3) { bottom: -5%; left: 0; transform: translateX(116.666%); }
.tb i:nth-child(3):after { top: 0; left: 0; animation: tb-w2 var(--sp) infinite ease-in-out; }
.cp-progress { margin-top: 14px; height: 7px; border-radius: 999px; position: relative; overflow: hidden;
  background: rgba(0,0,0,0.06); border: 1px solid rgba(0,0,0,0.04); }
.cp-fill { position: absolute; inset: 0;
  background: linear-gradient(90deg, rgba(10,160,190,0), rgba(10,160,190,0.6), rgba(130,80,220,0.6), rgba(10,160,190,0));
  transform: translateX(-70%); animation: cp-flow 1.05s cubic-bezier(0.2,0.8,0.2,1) infinite; }
@keyframes cp-grad { 0%{background-position:0% 50%} 50%{background-position:100% 50%} 100%{background-position:0% 50%} }
@keyframes cp-breathe { 0%,100%{transform:scale(1);box-shadow:0 0 7px rgba(139,92,246,0.5)} 50%{transform:scale(1.12);box-shadow:0 0 14px rgba(139,92,246,0.9)} }
@keyframes cp-spin { to { transform: rotate(360deg); } }
@keyframes cp-rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@keyframes cp-edgeGlow {
  0%, 100% { box-shadow: inset 0 0 0 1px rgba(139,92,246,0.45), inset 0 0 6px rgba(139,92,246,0.35); }
  50%      { box-shadow: inset 0 0 0 1px rgba(139,92,246,0.85), inset 0 0 12px rgba(139,92,246,0.6); }
}
@keyframes tb-spin { 0%{transform:rotate(0)} 100%{transform:rotate(360deg)} }
@keyframes tb-w1 { 0%,100%{transform:translateY(0) scale(1);opacity:1} 50%{transform:translateY(-66%) scale(0.65);opacity:0.8} }
@keyframes tb-w2 { 0%,100%{transform:translateY(0) scale(1);opacity:1} 50%{transform:translateY(66%) scale(0.65);opacity:0.8} }
@keyframes cp-flow { 0%{transform:translateX(-75%);opacity:0.8} 50%{opacity:1} 100%{transform:translateX(75%);opacity:0.8} }

@keyframes stop-pulse  { 0%{transform:scale(.97)} 15%{transform:scale(1)} 30%{transform:scale(.98)} 45%{transform:scale(1)} 60%{transform:scale(.97)} 85%{transform:scale(1)} 100%{transform:scale(.97)} }
@keyframes stop-pulse2 { 0%{transform:scale(1)} 15%{transform:scale(1.03)} 30%{transform:scale(.98)} 45%{transform:scale(1.04)} 60%{transform:scale(.97)} 85%{transform:scale(1.03)} 100%{transform:scale(1)} }
@keyframes stop-bgRotate { 0%{transform:rotate(0)} 20%{transform:rotate(90deg)} 40%{transform:rotate(180deg) scale(.95,1)} 60%,100%{transform:rotate(360deg)} }
@keyframes stop-bgColor  { 20%{background-color:red} 40%{background-color:#5eff7e} 60%{background-color:#2cb5ff} 80%{background-color:#fc63ff} }
@keyframes stop-blink    { 0%,85%,100%{transform:scaleY(1)} 92%{transform:scaleY(.1)} }
@keyframes icon-cycle-pc { 0%, 40% { opacity: 1 } 50%, 90% { opacity: 0 } 100% { opacity: 1 } }
@keyframes icon-cycle-tg { 0%, 40% { opacity: 0 } 50%, 90% { opacity: 1 } 100% { opacity: 0 } }
</style></head>
<body>
<div class="toprow">
  <div class="orb-wrap">
    <iframe class="orb-frame" src="http://127.0.0.1:5000/telegram/telergam_animation.html"
            scrolling="no" frameborder="0"></iframe>
  </div>
  <span class="msg" id="msg"></span>
</div>
<div id="coderPanel">
  <canvas class="cp-particles" id="cp-particles"></canvas>
  <div class="cp-body">
    <div class="cp-output" id="cp-output"></div>
    <div class="cp-todos hidden" id="cp-todos"></div>
    <div class="cp-minions" id="cp-minions"></div>
    <div class="cp-progress"><span class="cp-fill"></span></div>
  </div>
</div>
<script>
  // Stream `text` LETTER-BY-LETTER into the msg element. Long messages
  // are PAGED — after each letter we check if scrollWidth has exceeded
  // the visible line width; if so we yank the offending letter, hold
  // the line briefly so the user can read it, clear, and continue on a
  // fresh line. Loops until every letter has been displayed.
  //
  // First setMsg() of a task waits ~350 ms before streaming so the
  // NSPanel finishes its 44→440 width expansion before any text appears.
  // Subsequent calls (next agent step, banner already wide) start
  // immediately.
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

    // Array.from splits by code point so 🧠 / 🎯 / etc. stay intact
    // (text.split('') would split them into surrogate halves).
    const chars = Array.from(text);
    let i = 0;

    const streamChar = () => {
      if (i >= chars.length) {
        // End of message — hold the final page briefly so the user can
        // read it, then clear and drop the has-text class so body
        // shrinks back to the 44×44 circle. A new setMsg() during the
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

      // Sync layout read forces reflow → we see whether this letter
      // overflowed the visible line. clientWidth is the max-width cap
      // (388), scrollWidth is the natural width of all spans appended
      // so far. If scrollWidth > clientWidth this letter doesn't fit —
      // yank it out, hold the line briefly, then resume on a fresh page
      // from the same letter.
      if (el.scrollWidth > el.clientWidth + 0.5) {
        // Defensive: single letter wider than the line (huge font?)
        // can't be paged out — keep it (overflow:hidden clips) and
        // advance so we don't loop forever.
        if (el.children.length === 1) {
          requestAnimationFrame(() => { span.style.opacity = '1'; });
          i++;
          _revealTimer = setTimeout(streamChar, _CHAR_DELAY_MS);
          return;
        }
        el.removeChild(span);
        _revealTimer = setTimeout(() => {
          el.textContent = '';
          // Skip leading whitespace so the new page doesn't open with
          // a space gap.
          while (i < chars.length && /\\s/.test(chars[i])) i++;
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

  // Report body size to Python whenever it changes (basically just on
  // the empty ↔ has-text toggle, since the dimensions are fixed in
  // both states). Last-value debounce avoids a resize feedback loop.
  (function () {
    let lastW = -1, lastH = -1;
    const report = () => {
      const w = Math.ceil(document.body.getBoundingClientRect().width);
      const h = Math.ceil(document.body.getBoundingClientRect().height);
      if (w === lastW && h === lastH) return;
      lastW = w; lastH = h;
      try {
        webkit.messageHandlers.size_changed.postMessage({w: w, h: h});
      } catch (e) {}
    };
    window.addEventListener('load', () => setTimeout(report, 30));
    const ro = new ResizeObserver(report);
    ro.observe(document.body);
  })();

  // ── embedded coder terminal ──────────────────────────────────────────────
  // Faithful port of the web frontend's CLI pill (frontend/script.js). Every
  // incoming line is QUEUED and streamed letter-by-letter with pagination
  // (overflow -> hold -> clear -> continue), so the full real content flows by
  // rather than just the latest fragment. The top line shows the coder's REAL
  // output; only while a minion is running AND the coder is idle does it stream
  // playful filler phrases (CP_MSGS). Each minion row shows that minion's REAL
  // per-iteration output. Python toggles the panel via coderShow()/coderHide().
  (function () {
    const CHAR_STAGGER    = 8;     // ms between letters (fallback)
    const REAL_STAGGER    = 4;     // the agent's real output streams FAST
    const FILLER_STAGGER  = 18;    // playful filler streams a little slower
    const CHAR_FADE       = 60;    // ms opacity fade-in per letter
    const PAGE_HOLD       = 550;   // ms a full page lingers before clearing
    const LINE_HOLD       = 260;   // ms between distinct lines
    const FILLER_IDLE     = 1800;  // ms of coder silence before filler starts
    const FILLER_HOLD     = 3000;  // ms a filler phrase lingers AFTER it finishes streaming

    // Playful "thinking" lines each minion row cycles through (one at a time).
    // A backtick template literal avoids escaping the many apostrophes/quotes;
    // trim()+filter() strips the code indentation and blank edges.
    const CP_MSGS = (`
      summoned minions…
      digging through the codebase…
      don't bother me, i'm busy still
      reticulating splines…
      consulting the rubber duck…
      untangling spaghetti…
      watching minions go brrr…
      this is fine, everything is fine…
      spinning up neurons…
      arguing with the linter…
      minions are reading… patience, human
      pondering the orb…
      grepping the unknown…
      feeding the hamsters…
      still thinking, hold tight…
      writing tiny love letters to stdout…
      asking stack overflow nicely…
      looking for the missing semicolon…
      blaming the intern…
      performing arcane git rituals…
      have you tried turning it off and on…
      just one more refactor, i promise…
      regex go brrr…
      negotiating with the type checker…
      Schrödinger's bug: works on my machine…
      pretending to understand recursion…
      this codebase has feelings too…
      arguing with prettier…
      i swear i tested this earlier…
      checking if it's a feature, not a bug…
      the code works, nobody knows why…
      reading documentation as last resort…
      blaming the cache…
      praying to the build gods…
      git blame says it was past me…
      compiling existential dread…
      tabs vs spaces war ongoing…
      training a goldfish to write tests…
      minions arguing over indentation…
      still cheaper than a senior dev…
      pushing to prod on a friday…
      one does not simply async in python…
      404: motivation not found…
      rewriting it in rust… mentally…
      petting the dog, brb…
      yelling politely at the json…
      checking if it's plugged in…
      the cake is a bug…
      explaining mondays to the AI…
      deploying vibes…
      this stack trace feels personal…
      i was promised flying cars, got jira…
      writing tests… eventually…
      minions on coffee break…
      this wasn't in the spec…
      ctrl-z is my therapist…
      running from technical debt…
      console.log debugger gang…
      the docs lied to us…
      trying to remember what i was doing…
      rebooting reality…
      yes, that's a feature now…
      speedrun: any% blame git…
      thinking too hard, please wait…
      yet another deeply nested if…
      promise resolved with disappointment…
      hot reload, cold coffee…
      aligning ducks in rows…
      one liner that took two hours…
      naming things, the hardest problem…
      off-by-one somewhere, definitely…
      loading more excuses…
      the bug is in another castle…
      your code is fine, the universe is broken…
      the linter has strong opinions…
      minion overheard saying lgtm…
      convincing the tests to pass…
      renaming the variable to fix it…
      drowning in callback hell…
      73 unread warnings, vibes only…
      yes it works, no i don't know why…
      minions found 47 todos, ignored all…
      scrolling error logs like reels…
      two minions, one task…
      sacrificing a keyboard to the demo gods…
      undoing the undo…
      reading the error message, finally…
      minions whispering to each other…
      the algorithm has thoughts…
      trying not to break prod…
      minion union meeting in progress…
      shaking the magic 8-ball…
      asking the cat for code review…
      running tests with fingers crossed…
      exorcising the legacy code…
      i promise this is the last bug…
      binary search through 200 tabs…
      feature creep is a feature now…
      minions found a TODO from 2014…
      deprecated, but still working…
      putting console.logs in production…
      redefining what "done" means…
    `).split('\\n').map((s) => s.trim()).filter(Boolean);

    const $ = (id) => document.getElementById(id);
    const escHtml = (s) => (s == null ? '' : String(s))
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const mid = (id) => 'cpm_' + String(id).replace(/[^a-zA-Z0-9_]/g, '_');

    // ── line runner: queue + paginated letter-by-letter streaming ──
    // Streams one queued line at a time into `outEl`, replacing its children
    // with one page; on overflow it holds, clears, and continues on a new page.
    // `prompt` (e.g. "> ") is re-rendered at the start of each page.
    function makeRunner() { return { queue: [], running: false, timer: null, onIdle: null }; }

    function pump(outEl, runner, prompt) {
      if (!outEl || runner.running || !runner.queue.length) return;
      runner.running = true;
      const item = runner.queue.shift();
      const text = (item && item.text != null) ? item.text : item;
      const stagger = (item && item.stagger) || CHAR_STAGGER;
      const chars = Array.from(String(text));
      if (!chars.length) { runner.running = false; pump(outEl, runner, prompt); return; }

      let page = null;
      const startPage = () => {
        page = document.createElement('div');
        page.className = 'cp-line';
        if (prompt) {
          const p = document.createElement('span');
          p.className = 'cp-p'; p.textContent = prompt;
          page.appendChild(p);
        }
        outEl.replaceChildren(page);
      };
      startPage();

      let i = 0;
      const tick = () => {
        if (i >= chars.length) {
          runner.timer = setTimeout(() => {
            runner.running = false;
            if (runner.queue.length) {
              pump(outEl, runner, prompt);
            } else if (runner.onIdle) {
              const cb = runner.onIdle; runner.onIdle = null; cb();  // line finished, queue empty
            }
          }, LINE_HOLD);
          return;
        }
        const firstChar = page.querySelector('.cp-char') == null;
        const span = document.createElement('span');
        span.className = 'cp-char';
        span.textContent = chars[i];
        page.appendChild(span);
        if (page.scrollWidth > page.clientWidth + 1 && !firstChar) {
          page.removeChild(span);
          runner.timer = setTimeout(() => {
            startPage();
            while (i < chars.length && /\\s/.test(chars[i])) i++;  // drop leading space on the new page
            tick();
          }, PAGE_HOLD);
          return;
        }
        // First char of a page shows instantly (no empty flash); the rest fade.
        if (firstChar) {
          span.style.opacity = '1';
        } else {
          span.style.opacity = '0';
          span.style.transition = 'opacity ' + CHAR_FADE + 'ms ease-out';
          requestAnimationFrame(() => { span.style.opacity = '1'; });
        }
        i++;
        runner.timer = setTimeout(tick, stagger);
      };
      tick();
    }

    function stopRunner(runner) {
      if (!runner) return;
      if (runner.timer) { clearTimeout(runner.timer); runner.timer = null; }
      runner.queue = []; runner.running = false; runner.onIdle = null;
    }

    // ── top (coder) line + filler ──
    const topRunner = makeRunner();
    const filler = { active: false, hasMinion: false, minions: 0, bag: [], lastIdx: -1, idleTimer: null, tickTimer: null };

    function fillerPhrase() {
      if (!filler.bag.length) {
        const bag = CP_MSGS.map((_, i) => i);
        for (let i = bag.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); const x = bag[i]; bag[i] = bag[j]; bag[j] = x; }
        if (filler.lastIdx >= 0 && bag.length > 1 && bag[bag.length - 1] === filler.lastIdx) { const x = bag[bag.length - 1]; bag[bag.length - 1] = bag[0]; bag[0] = x; }
        filler.bag = bag;
      }
      const idx = filler.bag.pop(); filler.lastIdx = idx; return CP_MSGS[idx];
    }

    function stopFiller() {
      filler.active = false;
      if (filler.idleTimer) { clearTimeout(filler.idleTimer); filler.idleTimer = null; }
      if (filler.tickTimer) { clearTimeout(filler.tickTimer); filler.tickTimer = null; }
      topRunner.onIdle = null;  // drop any pending "next phrase" chain
    }

    function scheduleFiller() {
      if (!filler.hasMinion) return;
      if (filler.idleTimer) clearTimeout(filler.idleTimer);
      filler.idleTimer = setTimeout(() => {
        filler.idleTimer = null;
        if (!filler.hasMinion) return;
        filler.active = true;
        const tickFiller = () => {
          if (!filler.active) return;
          const out = $('cp-output'); if (!out) { filler.active = false; return; }
          // When this phrase finishes streaming, linger FILLER_HOLD, then next —
          // so the witty line sits still for ~3s instead of swapping on a timer.
          topRunner.onIdle = () => {
            if (!filler.active) return;
            filler.tickTimer = setTimeout(tickFiller, FILLER_HOLD);
          };
          topRunner.queue.push({ text: fillerPhrase(), stagger: FILLER_STAGGER });
          pump(out, topRunner, '> ');
        };
        tickFiller();
      }, FILLER_IDLE);
    }

    window.coderShow = function () { document.body.classList.add('coder'); };

    window.coderHide = function () {
      document.body.classList.remove('coder');
      stopFiller();
      filler.hasMinion = false; filler.minions = 0; filler.bag = []; filler.lastIdx = -1;
      stopRunner(topRunner);
      const o = $('cp-output'); if (o) o.innerHTML = '';
      const t = $('cp-todos'); if (t) { t.innerHTML = ''; t.classList.add('hidden'); }
      const m = $('cp-minions');
      if (m) {
        m.querySelectorAll('.cp-mrow').forEach((row) => { if (row._runner) stopRunner(row._runner); });
        m.innerHTML = '';
      }
    };

    // Real coder line -> top. A real line beats filler: stop it, then re-arm for
    // the next idle window if minions are still running.
    window.pushLine = function (text) {
      const out = $('cp-output'); if (!out) return;
      const t = (text == null ? '' : String(text));
      if (!t.trim()) return;
      stopFiller();
      if (filler.hasMinion) scheduleFiller();
      topRunner.queue.push({ text: t, stagger: REAL_STAGGER });
      pump(out, topRunner, '> ');
    };

    window.setTodo = function (todoText) {
      const el = $('cp-todos'); if (!el) return;
      const raw = (todoText == null ? '' : String(todoText)).split('\\n');
      const items = [];
      for (let i = 0; i < raw.length; i++) {
        const ln = raw[i].trim();
        if (!ln) continue;
        if (/^objective\\s*:/i.test(ln)) continue;
        let m = ln.match(/^#\\d+\\.\\s*-\\s*\\[([ xX])\\]\\s*(.*)$/);
        if (!m) m = ln.match(/^-\\s*\\[([ xX])\\]\\s*(.*)$/);
        if (!m) continue;
        items.push({ done: m[1].toLowerCase() === 'x', text: m[2] });
      }
      if (!items.length) { el.innerHTML = ''; el.classList.add('hidden'); return; }
      // The current task is the first not-yet-done one: it gets the spinning
      // loading circle. Once it's marked done, the next pending task becomes
      // current. Done tasks show the breathing gradient box; the rest are empty.
      let activeIdx = -1;
      for (let k = 0; k < items.length; k++) { if (!items[k].done) { activeIdx = k; break; } }
      let html = '';
      for (let j = 0; j < items.length; j++) {
        let marker;
        if (items[j].done) marker = '<span class="cp-chk done"></span>';
        else if (j === activeIdx) marker = '<span class="cp-loading"></span>';
        else marker = '<span class="cp-chk"></span>';
        html += '<div class="cp-item">' + marker + '<span class="lbl">' + escHtml(items[j].text) + '</span></div>';
      }
      el.innerHTML = html;
      el.classList.remove('hidden');
    };

    window.addMinion = function (id, label) {
      const wrap = $('cp-minions'); if (!wrap || id == null) return;
      const sid = mid(id);
      if (document.getElementById(sid)) return;
      const row = document.createElement('div');
      row.className = 'cp-mrow'; row.id = sid;
      // The row streams the minion's REAL output, queued + paginated (same as
      // the frontend's minion pill) so the full thinking/goal/action flows by.
      row.innerHTML = '<span class="tb"><i></i><i></i><i></i></span>'
        + '<span class="mline"></span>';
      wrap.appendChild(row);
      row._runner = makeRunner();
      // A running minion means the coder line may go idle -> arm top filler.
      filler.minions++;
      filler.hasMinion = true;
      scheduleFiller();
    };

    window.setMinionLine = function (id, text) {
      const row = document.getElementById(mid(id)); if (!row) return;
      const ml = row.querySelector('.mline'); if (!ml) return;
      const t = (text == null ? '' : String(text));
      if (!t.trim()) return;
      if (!row._runner) row._runner = makeRunner();
      row._runner.queue.push({ text: t, stagger: REAL_STAGGER });
      pump(ml, row._runner);
    };

    window.removeMinion = function (id) {
      const row = document.getElementById(mid(id));
      if (!row) return;
      if (row._runner) stopRunner(row._runner);
      if (row.parentNode) row.parentNode.removeChild(row);
      filler.minions = Math.max(0, filler.minions - 1);
      if (filler.minions === 0) { filler.hasMinion = false; stopFiller(); }
    };
  })();

  // Floating twinkling purple particles behind the terminal panel. Resizes with
  // the panel (height grows as todos / minion rows appear); cheap (16 dots).
  (function () {
    const canvas = document.getElementById('cp-particles');
    if (!canvas || !canvas.getContext) return;
    const ctx = canvas.getContext('2d');
    const host = canvas.parentElement;  // #coderPanel
    const dpr = window.devicePixelRatio || 1;
    let W = 0, H = 0;
    function resize() {
      const r = host.getBoundingClientRect();
      W = r.width; H = r.height;
      canvas.width = Math.max(1, Math.round(W * dpr));
      canvas.height = Math.max(1, Math.round(H * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    resize();
    try { new ResizeObserver(resize).observe(host); } catch (e) {}
    const COUNT = 16;
    const ps = [];
    for (let i = 0; i < COUNT; i++) {
      ps.push({
        x: Math.random() * (W || 540), y: Math.random() * (H || 300),
        r: 0.6 + Math.random() * 1.4,
        vx: (Math.random() - 0.5) * 0.35, vy: (Math.random() - 0.5) * 0.35,
        a: 0.2 + Math.random() * 0.5, tw: Math.random() * Math.PI * 2,
      });
    }
    function tick(t) {
      if (W > 0 && H > 0) {
        ctx.clearRect(0, 0, W, H);
        for (const p of ps) {
          p.vx += (Math.random() - 0.5) * 0.04; p.vy += (Math.random() - 0.5) * 0.04;
          p.vx = Math.max(-0.6, Math.min(0.6, p.vx));
          p.vy = Math.max(-0.6, Math.min(0.6, p.vy));
          p.x += p.vx; p.y += p.vy;
          if (p.x < -2) p.x = W + 2; if (p.x > W + 2) p.x = -2;
          if (p.y < -2) p.y = H + 2; if (p.y > H + 2) p.y = -2;
          const twinkle = 0.6 + 0.4 * Math.sin(t * 0.002 + p.tw);
          ctx.beginPath();
          ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
          ctx.fillStyle = 'rgba(139, 92, 246,' + (p.a * twinkle) + ')';
          ctx.shadowBlur = 6;
          ctx.shadowColor = 'rgba(139, 92, 246, 0.9)';
          ctx.fill();
        }
      }
      requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  })();
</script>
</body></html>
"""


if _COCOA_OK:
    class _NonActivatingPanel(NSPanel):
        """Borderless NSPanel that can still become key.

        AppKit returns NO from -canBecomeKeyWindow for borderless panels by
        default, which blocks WKWebView text inputs from ever receiving
        keyboard focus (the user clicks the field and nothing happens).
        Overriding to YES makes the field usable. NSWindowStyleMaskNonactivatingPanel
        is still set on the instance, so becoming key still doesn't activate
        this Python process — Safari stays in the foreground."""
        def canBecomeKeyWindow(self):
            return True


    class _ClickableWebView(WKWebView):
        """WKWebView that returns YES from acceptsFirstMouse:.

        Without this, the first click after the panel loses key status
        (e.g. user just clicked Safari) is swallowed by AppKit while it
        promotes the panel back to key — the button click never fires, and
        the user has to tap a second time. Returning YES tells AppKit to
        forward the very first click straight to the view, so single-tap
        works regardless of key-window state."""
        def acceptsFirstMouse_(self, event):
            return True


    class _NextHandler(NSObject):
        """WKScriptMessageHandler — fires self._event when JS posts to 'next_clicked'.

        No custom init: PyObjC's bridged NSObject.init takes no args, so calling
        NSObject.init(self) inside a subclass crashes with "Need 0 arguments,
        got 1". Instead, allocate with the default init and set the event as a
        plain Python attribute right after — PyObjC subclasses accept arbitrary
        Python attributes just fine.
        """
        def userContentController_didReceiveScriptMessage_(self, controller, message):
            try:
                self._event.set()
            except Exception:
                pass

    class _HeightHandler(NSObject):
        """WKScriptMessageHandler — receives body.scrollHeight from JS and calls
        the banner's _on_height_changed on the main thread (already the current
        thread, since WK message delivery is on main)."""
        def userContentController_didReceiveScriptMessage_(self, controller, message):
            try:
                banner = self._banner
                if banner is not None:
                    banner._on_height_changed(int(message.body()))
            except Exception:
                pass

    class _ChoiceHandler(NSObject):
        """WKScriptMessageHandler for the two-button choice row. Stores the
        clicked label ('left' or 'right') on self._value, then fires self._event."""
        def userContentController_didReceiveScriptMessage_(self, controller, message):
            try:
                self._value = str(message.body())
                self._event.set()
            except Exception:
                pass

    class _SaveHandler(NSObject):
        """WKScriptMessageHandler for the token input. Stores the typed string
        on self._value, then fires self._event."""
        def userContentController_didReceiveScriptMessage_(self, controller, message):
            try:
                self._value = str(message.body())
                self._event.set()
            except Exception:
                pass

    class _RevealHandler(NSObject):
        """WKScriptMessageHandler fired by JS when the word-by-word setMsg
        reveal finishes. Used to gate control-set visibility on stream
        completion so buttons don't pop in mid-sentence."""
        def userContentController_didReceiveScriptMessage_(self, controller, message):
            try:
                self._event.set()
            except Exception:
                pass

    class _SizeHandler(NSObject):
        """WKScriptMessageHandler — receives `{w, h}` from JS (compact
        pill's ResizeObserver) and routes to banner._on_size_changed on
        the main thread. Used for the compact pill which animates BOTH
        width and height as thinking text streams in; the standard
        banner keeps using _HeightHandler since its width is fixed."""
        def userContentController_didReceiveScriptMessage_(self, controller, message):
            try:
                banner = self._banner
                if banner is None:
                    return
                body = message.body()
                # JS posts a plain object → arrives as NSDictionary in
                # PyObjC. Either dict-style access works in modern PyObjC.
                w = int(body["w"]) if "w" in body else 0
                h = int(body["h"]) if "h" in body else 0
                banner._on_size_changed(w, h)
            except Exception:
                pass
else:
    _NextHandler = None
    _HeightHandler = None
    _ChoiceHandler = None
    _SaveHandler = None
    _RevealHandler = None
    _SizeHandler = None


class StatusBanner:
    W, MIN_H, MAX_H, TOP_MARGIN, RIGHT_MARGIN = 440, 44, 200, 56, 20
    # Compact variant: starts as a 44×44 white circle (orb only). When
    # streaming text arrives the pill grows rightward into a 440×44
    # stadium — a single-line ticker. Height never changes. Long messages
    # page through one line at a time (fill, pause, clear, continue).
    # Top-right corner stays anchored so width grows leftward only.
    COMPACT_MIN_W = 44      # square → circle when only the orb is visible
    COMPACT_MIN_H = 44
    COMPACT_MAX_W = 440     # = self.W (the setup-wizard banner's max width)
    COMPACT_MAX_H = 44      # single-line height — pill never grows taller

    # Coder terminal: while a CLI/coder sub-agent runs, the compact orb pill
    # expands (wider + taller) to host an embedded terminal panel BELOW the orb
    # (streamed lines + todo checklist + minion rows). Same single top-right
    # anchored window — it just grows leftward and downward. These bigger
    # clamps are used by _on_size_changed only while the panel is active.
    COMPACT_CODER_MAX_W = 560
    COMPACT_CODER_MAX_H = 470

    def __init__(self, compact: bool = False):
        self._compact = compact
        self._coder_active = False  # True while the embedded terminal panel shows
        self._window = None
        self._webview = None
        self._next_handler = None    # strong refs so the JS-bridge handlers
        self._height_handler = None  # don't get GC'd
        self._choice_handler = None
        self._save_handler = None
        self._reveal_handler = None
        self._size_handler = None
        self._next_event = threading.Event()
        self._choice_event = threading.Event()
        self._save_event = threading.Event()
        # Set initially: no streaming reveal is pending until update() is called.
        # update() clears this; the JS reveal_done handler re-sets it.
        self._reveal_event = threading.Event()
        self._reveal_event.set()
        # Track both axes so _on_size_changed can detect no-ops and skip
        # the AppKit setFrame call (which would re-trigger the animation).
        self._current_w = self.COMPACT_MIN_W if compact else self.W
        self._current_h = self.COMPACT_MIN_H if compact else self.MIN_H

    # ---- public API (callable from any thread) ----

    def show(self):
        if not _COCOA_OK:
            return
        callAfter(self._create)

    def update(self, text):
        if not _COCOA_OK:
            return
        # A streaming reveal is about to start in JS; clear the event so any
        # following wait_for_* call blocks until JS posts reveal_done. Only
        # relevant in standard mode (compact pill never waits on reveals).
        if not self._compact:
            self._reveal_event.clear()
        callAfter(self._set_text, text)

    # Cap the wait-for-reveal so a JS hiccup that drops the reveal_done
    # message can never deadlock us. Realistic banner messages stream out
    # in well under this — and shorter is better, because the wait is what
    # the user experiences between the message finishing and the button
    # showing.
    _REVEAL_WAIT_SEC = 3.0

    def _await_reveal(self):
        """Block until the most recent update()'s reveal animation has
        finished (or the safety timeout fires). No-op if no update() is
        pending — the event stays set in that case."""
        self._reveal_event.wait(self._REVEAL_WAIT_SEC)

    def wait_for_next(self, timeout=None):
        """Block calling thread until user clicks Next (or timeout). Returns True if clicked.

        Shows the Next button on entry and hides it on exit, so during normal
        update() calls the button stays hidden — only the entry/exit boundaries
        of a wait_for_next show a clickable Next.
        """
        if not _COCOA_OK:
            return True  # no banner → don't block forever
        if self._compact:
            # No Next button in compact mode — return immediately so callers
            # that accidentally chain it don't hang forever.
            return True
        # Clear the click event BEFORE the reveal wait. If we cleared after,
        # any click that lands during streaming (rare, since the button is
        # hidden until reveal finishes — but defensive) would be wiped here
        # and the user would have to click a second time.
        self._next_event.clear()
        self._await_reveal()
        callAfter(self._clear_extra_ui)
        callAfter(self._set_next_visible, True)
        clicked = self._next_event.wait(timeout)
        callAfter(self._set_next_visible, False)
        return clicked

    def wait_for_choice(self, left_label, right_label, timeout=None):
        """Show two side-by-side buttons; block until one is clicked.
        Returns 'left' or 'right', or None on timeout / no Cocoa."""
        if not _COCOA_OK or self._compact:
            return None
        self._choice_event.clear()
        self._await_reveal()
        callAfter(self._set_next_visible, False)
        callAfter(self._show_choice, left_label, right_label)
        clicked = self._choice_event.wait(timeout)
        value = getattr(self._choice_handler, "_value", None) if clicked else None
        callAfter(self._clear_extra_ui)
        return value

    def wait_for_input(self, save_label="Save", validate=None,
                       error_msg="Token can't be empty"):
        """Show a text input + Save button; block until user submits a value
        that passes `validate` (default: non-empty after strip). Failed
        validation surfaces `error_msg` in red below the input and keeps
        waiting. Returns the accepted value, or None on no Cocoa."""
        if not _COCOA_OK or self._compact:
            return None
        if validate is None:
            validate = lambda v: bool((v or "").strip())
        self._save_event.clear()
        self._await_reveal()
        callAfter(self._set_next_visible, False)
        callAfter(self._show_input, save_label)
        try:
            while True:
                self._save_event.wait()
                # _destroy() also sets the event — bail out if the banner
                # has been torn down out from under us.
                if self._webview is None:
                    return None
                value = getattr(self._save_handler, "_value", "") or ""
                if validate(value):
                    return value
                callAfter(self._set_input_error, error_msg)
                self._save_event.clear()
        finally:
            callAfter(self._clear_extra_ui)

    def close(self):
        if not _COCOA_OK:
            return
        callAfter(self._destroy)

    # ---- main-thread implementations ----

    def _create(self):
        try:
            # Anchor to the PRIMARY display (the menu-bar screen), NOT
            # mainScreen(). mainScreen() follows keyboard focus, and this runs
            # via callAfter on the main thread — so by the time it fires the
            # agent may have brought another app (e.g. Chrome) to the front on
            # a *secondary* display, making mainScreen() that other screen.
            # screens()[0] is the stable primary screen and never jumps.
            screens = NSScreen.screens()
            scr_obj = screens[0] if screens else NSScreen.mainScreen()
            scr = scr_obj.frame()
            if self._compact:
                w_px, h_px = self.COMPACT_MIN_W, self.COMPACT_MIN_H
                # Stadium pill — same corner radius as the standard banner
                # so the shape stays consistent as the pill grows.
                corner = self.COMPACT_MIN_H / 2.0
                html = COMPACT_HTML
                ignores_mouse = True  # click-through; purely visual
            else:
                w_px, h_px = self.W, self.MIN_H
                corner = self.MIN_H / 2.0
                html = BANNER_HTML
                ignores_mouse = False
            # Include the screen origin so the right edge is correct in the
            # global multi-display coordinate space (origin is (0,0) for the
            # primary display, non-zero for others). Without it the pill lands
            # on the wrong (left) side whenever the anchor screen isn't at 0,0.
            x = scr.origin.x + scr.size.width - w_px - self.RIGHT_MARGIN
            y = scr.origin.y + scr.size.height - h_px - self.TOP_MARGIN
            rect = NSMakeRect(x, y, w_px, h_px)

            w = _NonActivatingPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, NSWindowStyleMaskNonactivatingPanel,
                NSBackingStoreBuffered, False,
            )
            w.setLevel_(NSStatusWindowLevel)
            w.setOpaque_(False)
            w.setBackgroundColor_(NSColor.clearColor())
            w.setIgnoresMouseEvents_(ignores_mouse)
            w.setHasShadow_(True)
            w.setReleasedWhenClosed_(False)
            # Panels normally hide when their app deactivates — we want the
            # banner to stay visible the entire time Safari is in front.
            # Leave becomesKeyOnlyIfNeeded at the NSPanel default (NO) so a
            # click on the token input properly makes the panel key and the
            # field accepts paste / typing. NonactivatingPanelMask means
            # becoming key still doesn't activate the Python process.
            try:
                w.setHidesOnDeactivate_(False)
            except Exception:
                pass

            content = w.contentView()
            content.setWantsLayer_(True)
            content.layer().setBackgroundColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.96).CGColor()
            )
            # Fixed at MIN_H/2 so the pill stays a stadium at default height
            # and becomes a rounded-rectangle when the height grows to fit
            # multi-line messages — cleaner than a fat oval. In compact mode
            # we use W/2 → perfect circle.
            content.layer().setCornerRadius_(corner)
            content.layer().setMasksToBounds_(True)

            cfg = WKWebViewConfiguration.alloc().init()

            # JS→Python bridges. The compact pill only needs setMsg + the
            # size_changed bridge (it has no buttons, no input, no reveal
            # gating). The standard banner registers the full set.
            size_h = _SizeHandler.alloc().init()
            size_h._banner = self
            cfg.userContentController().addScriptMessageHandler_name_(size_h, "size_changed")

            if not self._compact:
                nh = _NextHandler.alloc().init()
                nh._event = self._next_event
                cfg.userContentController().addScriptMessageHandler_name_(nh, "next_clicked")

                hh = _HeightHandler.alloc().init()
                hh._banner = self
                cfg.userContentController().addScriptMessageHandler_name_(hh, "height_changed")

                ch = _ChoiceHandler.alloc().init()
                ch._event = self._choice_event
                ch._value = None
                cfg.userContentController().addScriptMessageHandler_name_(ch, "choice_clicked")

                sh = _SaveHandler.alloc().init()
                sh._event = self._save_event
                sh._value = ""
                cfg.userContentController().addScriptMessageHandler_name_(sh, "save_clicked")

                rh = _RevealHandler.alloc().init()
                rh._event = self._reveal_event
                cfg.userContentController().addScriptMessageHandler_name_(rh, "reveal_done")
            else:
                nh = hh = ch = sh = rh = None

            wv_rect = NSMakeRect(0, 0, w_px, h_px)
            wv = _ClickableWebView.alloc().initWithFrame_configuration_(wv_rect, cfg)
            try:
                wv.setValue_forKey_(False, "drawsBackground")
            except Exception:
                pass
            try:
                wv.setWantsLayer_(True)
                wv.layer().setBackgroundColor_(NSColor.clearColor().CGColor())
            except Exception:
                pass
            # NSViewWidthSizable (2) | NSViewHeightSizable (16). When the
            # window animates between sizes (multi-line message growing,
            # collapsing back to single line), the WebView's frame follows
            # the animation instead of snapping — that's what makes the
            # pill grow/shrink as a smooth shape.
            try:
                wv.setAutoresizingMask_(2 | 16)
            except Exception:
                pass
            wv.loadHTMLString_baseURL_(html, None)
            content.addSubview_(wv)

            w.orderFrontRegardless()
            # Make the panel key on show so the first user click on Next
            # registers as the button click — not as "promote panel to key".
            # NonActivatingPanelMask means becoming key still doesn't
            # activate this Python process, so Safari stays in front.
            if not self._compact:
                try:
                    w.makeKeyWindow()
                except Exception:
                    pass
            self._window, self._webview = w, wv
            self._next_handler, self._height_handler = nh, hh
            self._choice_handler, self._save_handler = ch, sh
            self._reveal_handler = rh
            self._size_handler = size_h
            self._current_w = w_px
            self._current_h = h_px
        except Exception as e:
            logger.warning(f"banner: _create failed ({e})")

    def _set_text(self, text):
        try:
            if self._webview is None:
                return
            safe = (str(text)
                    .replace("\\", "\\\\")
                    .replace("'", "\\'")
                    .replace("\n", " ")
                    .replace("\r", " "))
            # Primary path: hand the full text to JS which animates it
            # word-by-word and fires reveal_done when finished. Fallback:
            # if the page-side script hasn't run yet (window.setMsg is
            # undefined — happens for the very first update right after
            # the WebView starts loading), set textContent directly and
            # post reveal_done ourselves so wait_for_next doesn't sit on
            # its safety timeout.
            js = (f"if (window.setMsg) {{ setMsg('{safe}'); }}"
                  f" else {{"
                  f"   var m = document.getElementById('msg');"
                  f"   if (m) m.textContent = '{safe}';"
                  f"   try {{ webkit.messageHandlers.reveal_done.postMessage(1); }}"
                  f"   catch (e) {{}}"
                  f" }}")
            self._webview.evaluateJavaScript_completionHandler_(js, None)
        except Exception:
            pass

    def _set_next_visible(self, visible):
        try:
            if self._webview is None:
                return
            disp = "inline-block" if visible else "none"
            js = (f"var b=document.getElementById('next'); "
                  f"if (b) b.style.display='{disp}';")
            self._webview.evaluateJavaScript_completionHandler_(js, None)
        except Exception:
            pass

    @staticmethod
    def _js_escape(text):
        return (str(text)
                .replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace("\n", " ")
                .replace("\r", " "))

    def _show_choice(self, left_label, right_label):
        try:
            if self._webview is None:
                return
            l = self._js_escape(left_label)
            r = self._js_escape(right_label)
            js = f"if (window.setChoice) setChoice('{l}', '{r}');"
            self._webview.evaluateJavaScript_completionHandler_(js, None)
        except Exception:
            pass

    def _show_input(self, save_label):
        try:
            if self._webview is None:
                return
            s = self._js_escape(save_label)
            js = f"if (window.setInput) setInput('{s}');"
            self._webview.evaluateJavaScript_completionHandler_(js, None)
        except Exception:
            pass

    def _set_input_error(self, msg):
        try:
            if self._webview is None:
                return
            m = self._js_escape(msg or "")
            js = f"if (window.setInputError) setInputError('{m}');"
            self._webview.evaluateJavaScript_completionHandler_(js, None)
        except Exception:
            pass

    def _clear_extra_ui(self):
        try:
            if self._webview is None:
                return
            js = "if (window.clearAll) clearAll();"
            self._webview.evaluateJavaScript_completionHandler_(js, None)
        except Exception:
            pass

    def _on_size_changed(self, requested_w, requested_h):
        """Resize the compact pill to fit its natural content.

        Compact mode only. Top-right corner stays anchored — the pill
        grows leftward and downward. Sizes are clamped to
        [COMPACT_MIN_W..COMPACT_MAX_W] × [COMPACT_MIN_H..COMPACT_MAX_H].
        The contentView's corner radius is updated to half the smaller
        dimension so the pill is a perfect circle when 44×44 (empty
        state) and a proper stadium when 440×78 (has-text state).
        Standard mode never gets here because its width is fixed; height
        changes for the standard banner come through _on_height_changed."""
        try:
            if self._window is None or not self._compact:
                return
            # While the coder terminal panel is showing, the pill expands well
            # past the single-line stadium — use the bigger clamps and a fixed
            # rounded-rect corner instead of the half-height stadium radius.
            if self._coder_active:
                max_w, max_h = self.COMPACT_CODER_MAX_W, self.COMPACT_CODER_MAX_H
            else:
                max_w, max_h = self.COMPACT_MAX_W, self.COMPACT_MAX_H
            new_w = max(self.COMPACT_MIN_W, min(int(requested_w), max_w))
            new_h = max(self.COMPACT_MIN_H, min(int(requested_h), max_h))
            if abs(new_w - self._current_w) < 1 and abs(new_h - self._current_h) < 1:
                return
            self._current_w = new_w
            self._current_h = new_h
            frame = self._window.frame()
            # NSWindow origin is bottom-left. Anchor top-right by shifting
            # origin x leftward as width grows AND shifting origin y down
            # so the top edge stays fixed.
            new_x = frame.origin.x + frame.size.width - new_w
            new_y = frame.origin.y + frame.size.height - new_h
            new_frame = NSMakeRect(new_x, new_y, new_w, new_h)
            self._window.setFrame_display_animate_(new_frame, True, True)
            # Update the rounded clip — half the smaller dimension keeps
            # the shape stadium-pill (or perfect circle at 44×44). With the
            # terminal panel open the window is tall, so clamp to a sane
            # card radius instead of a huge oval.
            try:
                content = self._window.contentView()
                if content is not None:
                    corner = 16.0 if self._coder_active else (min(new_w, new_h) / 2.0)
                    content.layer().setCornerRadius_(corner)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"banner: _on_size_changed failed ({e})")

    def _on_height_changed(self, requested_h):
        """Resize the NSWindow to match the WebView's content height.

        Top edge stays put — height grows downward by adjusting NSWindow's
        bottom-left origin Y. Clamped to [MIN_H, MAX_H].
        """
        try:
            if self._window is None:
                return
            new_h = max(self.MIN_H, min(int(requested_h), self.MAX_H))
            if abs(new_h - self._current_h) < 1:
                return
            self._current_h = new_h
            frame = self._window.frame()
            # NSWindow origin is bottom-left; to keep top edge fixed while
            # height changes, shift origin Y by (old_h - new_h).
            new_y = frame.origin.y + frame.size.height - new_h
            new_frame = NSMakeRect(frame.origin.x, new_y, frame.size.width, new_h)
            self._window.setFrame_display_animate_(new_frame, True, True)
            # The WebView resizes with the window via its autoresizingMask
            # (set in _create), so no manual setFrame snap is needed here —
            # snapping would override the in-flight animation and the pill
            # would visually jump to its final size rather than morph.
        except Exception as e:
            logger.warning(f"banner: _on_height_changed failed ({e})")

    # ---- embedded coder terminal API (compact mode; callable from any thread) ----
    #
    # While a CLI/coder sub-agent runs, the compact orb pill expands to host a
    # terminal panel below the orb. These methods drive the JS in COMPACT_HTML.
    # All are no-ops unless this is a compact banner.

    @staticmethod
    def _js_multiline(text):
        """Escape a string for a single-quoted JS literal while PRESERVING
        newlines as \\n (so JS-side .split('\\n') still works). Used for the
        todo blob; _js_escape collapses newlines and must not be used for
        multi-line payloads."""
        return (str(text)
                .replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace("\r", "")
                .replace("\n", "\\n"))

    def _coder_eval(self, js):
        try:
            if self._webview is not None:
                self._webview.evaluateJavaScript_completionHandler_(js, None)
        except Exception:
            pass

    def coder_start(self):
        """Reveal the embedded terminal panel (the pill expands wider+taller)."""
        if not _COCOA_OK or not self._compact:
            return
        self._coder_active = True
        callAfter(self._coder_eval, "if (window.coderShow) coderShow();")

    def coder_stop(self):
        """Hide the terminal panel and collapse the pill back to the orb pill."""
        if not _COCOA_OK or not self._compact:
            return
        self._coder_active = False
        callAfter(self._coder_eval, "if (window.coderHide) coderHide();")

    def push_cli_line(self, line):
        """Stream one of the coder's real output lines into the top line."""
        if not _COCOA_OK or not self._compact:
            return
        callAfter(self._coder_eval, f"if (window.pushLine) pushLine('{self._js_escape(line)}');")

    def set_todo(self, todo_text):
        """(Re)render the todo checklist from the raw todo.md text."""
        if not _COCOA_OK or not self._compact:
            return
        callAfter(self._coder_eval, f"if (window.setTodo) setTodo('{self._js_multiline(todo_text)}');")

    def add_minion(self, minion_id, label="minion"):
        """Add a spinner row for a minion."""
        if not _COCOA_OK or not self._compact:
            return
        callAfter(self._coder_eval,
                  f"if (window.addMinion) addMinion('{self._js_escape(minion_id)}', '{self._js_escape(label)}');")

    def set_minion_line(self, minion_id, line):
        """Stream the minion's latest real output line into its row."""
        if not _COCOA_OK or not self._compact:
            return
        callAfter(self._coder_eval,
                  f"if (window.setMinionLine) setMinionLine('{self._js_escape(minion_id)}', '{self._js_escape(line)}');")

    def remove_minion(self, minion_id):
        """Remove a minion row (it exited)."""
        if not _COCOA_OK or not self._compact:
            return
        callAfter(self._coder_eval, f"if (window.removeMinion) removeMinion('{self._js_escape(minion_id)}');")

    def _destroy(self):
        try:
            if self._webview is not None:
                try:
                    self._webview.stopLoading()
                except Exception:
                    pass
                try:
                    cfg = self._webview.configuration()
                    if cfg is not None:
                        uc = cfg.userContentController()
                        uc.removeScriptMessageHandlerForName_("next_clicked")
                        uc.removeScriptMessageHandlerForName_("height_changed")
                        uc.removeScriptMessageHandlerForName_("choice_clicked")
                        uc.removeScriptMessageHandlerForName_("save_clicked")
                        uc.removeScriptMessageHandlerForName_("reveal_done")
                        uc.removeScriptMessageHandlerForName_("size_changed")
                except Exception:
                    pass
            if self._window is not None:
                self._window.orderOut_(None)
        except Exception:
            pass
        finally:
            for ev in (self._next_event, self._choice_event,
                       self._save_event, self._reveal_event):
                try:
                    ev.set()
                except Exception:
                    pass
            self._window = None
            self._webview = None
            self._next_handler = None
            self._height_handler = None
            self._choice_handler = None
            self._save_handler = None
            self._reveal_handler = None
            self._size_handler = None


class CoderBannerManager:
    """Turns the agent's `cli_callback` event stream into the embedded terminal
    panel inside the compact orb banner.

    It does NOT own a window — it drives an existing compact StatusBanner (the
    "task in progress" orb): while a CLI/coder sub-agent runs, the orb pill
    expands to show a terminal panel (streamed lines + todo checklist + minion
    spinner rows), then collapses back when the CLI agent is done.

    Surface-agnostic (knows nothing about Telegram). A caller constructs it with
    the compact banner and passes `cli_callback=mgr.handle_event` to
    AgentService, then calls `mgr.close_all()` when the run finishes.

    Lifecycle — the panel stays up for the whole cli_await halt and only closes
    when the CLI agent is actually done:

        await_start(reason)                        main agent halts → SHOW panel
        task_start(task_id, desc)                  coder begins      → track only
                                                   (panel waits for cli_await so it
                                                    can't occlude live OS actions)
        task_line(task_id, line, stream)           coder stdout      → stream the top line
                                                   (filler phrases fill in only
                                                    while a minion runs + coder idle)
        todo_update(task_id, todo_text)            todo changed      → render checklist
        task_end(task_id, status, summary)         coder done        → hide iff no await
        minion_start(parent_id, m_id, query)       minion begins     → add spinner row
        minion_line(m_id, line, stream)            minion stdout     → stream into the row
        minion_end(m_id, status, summary)          minion done       → remove the row
        await_end()                                halt over         → hide panel

    `task_start` whose description starts with "[minion] " is a bare top-level
    minion (no coder stream) and is ignored; coder-spawned minions arrive as
    `minion_*`. All StatusBanner methods marshal onto the Cocoa main thread
    internally, so handle_event is safe to call from the agent/pipe-reader
    threads.
    """

    def __init__(self, banner):
        self._banner = banner            # the compact StatusBanner (orb pill)
        self._lock = threading.Lock()
        self._coder_tasks = set()        # active coder task_ids (own a terminal stream)
        self._minion_ids = set()         # active minion ids (own a spinner row)
        self._await_active = False       # True between await_start and await_end

    def handle_event(self, event_type, *args):
        try:
            self._dispatch(event_type, args)
        except Exception:
            logger.warning("CoderBannerManager.handle_event(%s) failed", event_type, exc_info=True)

    def close_all(self):
        """Hide the panel and reset (call from the caller's finally block)."""
        with self._lock:
            self._coder_tasks.clear()
            self._minion_ids.clear()
            self._await_active = False
        try:
            self._banner.coder_stop()
        except Exception:
            pass

    def _dispatch(self, event_type, args):
        if event_type == "await_start":
            with self._lock:
                self._await_active = True
            self._banner.coder_start()

        elif event_type == "await_end":
            with self._lock:
                self._await_active = False
            self._banner.coder_stop()

        elif event_type == "task_start":
            task_id = args[0] if len(args) > 0 else None
            desc = args[1] if len(args) > 1 else ""
            if task_id is None:
                return
            if isinstance(desc, str) and desc.startswith("[minion] "):
                return  # bare top-level minion — no coder stream of its own
            with self._lock:
                self._coder_tasks.add(task_id)
            # Deliberately do NOT show the panel here. A cli_agent dispatch is
            # non-blocking — the main agent keeps performing on-screen OS actions
            # until it runs cli_await. Expanding the panel now would occlude the
            # screen the agent is automating. The panel expands only on
            # await_start (main agent halted); lines stream into it meanwhile.

        elif event_type == "task_line":
            task_id = args[0] if len(args) > 0 else None
            line = args[1] if len(args) > 1 else ""
            with self._lock:
                is_coder = task_id in self._coder_tasks
            if is_coder and line is not None and str(line).strip():
                self._banner.push_cli_line(str(line))

        elif event_type == "todo_update":
            todo_text = args[1] if len(args) > 1 else ""
            self._banner.set_todo(str(todo_text))

        elif event_type == "task_end":
            task_id = args[0] if len(args) > 0 else None
            with self._lock:
                self._coder_tasks.discard(task_id)
                # Hide only if no cli_await is holding the panel open and no
                # other coder is still streaming — otherwise keep it up until
                # await_end (the CLI agent isn't "done" until the halt lifts).
                hide = (not self._await_active) and (not self._coder_tasks)
            if hide:
                self._banner.coder_stop()

        elif event_type == "minion_start":
            minion_id = args[1] if len(args) > 1 else None
            if minion_id is None:
                return
            with self._lock:
                self._minion_ids.add(minion_id)
            # Add the row but do NOT expand the panel (same reason as task_start —
            # a minion can spawn before the main agent halts at cli_await). The
            # row becomes visible when await_start expands the panel.
            self._banner.add_minion(minion_id, "minion")

        elif event_type == "minion_line":
            minion_id = args[0] if len(args) > 0 else None
            line = args[1] if len(args) > 1 else ""
            with self._lock:
                known = minion_id in self._minion_ids
            if known and line is not None and str(line).strip():
                self._banner.set_minion_line(minion_id, str(line))

        elif event_type == "minion_end":
            minion_id = args[0] if len(args) > 0 else None
            with self._lock:
                self._minion_ids.discard(minion_id)
            self._banner.remove_minion(minion_id)

        # pill_web_loading_* and other events: nothing to do.
