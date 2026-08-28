# Auto-Use web agent — status & remaining backlog

**Written:** 2026-08-21 · **Branch:** `cleaning_browser` · **HEAD at writing:** `60183a9`
**Supersedes** the 2026-08-20 handoff written at `72a7c00`.

Everything marked **measured** was reproduced against real Chrome in this repo. Numbers are
from this machine (10 cores) — read them as ratios, not absolutes, and check `uptime` before
trusting any timing you take yourself: a load average near 100 makes every measurement here
meaningless, which cost half a session to discover.

---

## 0. Read this first

### Architecture (unchanged, correct — do not undo)

```
web/
  browser/browser.rs      THE BROWSER: Chrome process, tab set, the ONE CDP socket,
                          tab lifecycle, cosmetics (glow/flash). Owns `Cdp`.
  browser/glow/           glow.css / glow.js / glow.html  (assets)
  tree/element.rs         SCANNING ONLY. Borrows a session (`scan_page(cdp, page, …)`).
  controller/tab/         navigate / reload / back / forward / new / switch / close
  controller/click|input|scroll/   input dispatch
  controller/{done,wait,scratchpad,todo_tracker}/   non-page tools
  agent/main_driver/      the agent loop
  llm_provider/           provider adapters
```

Invariants, all still true:

- **One WebSocket to Chrome.** `tungstenite::client` appears exactly once; one `Cdp` exists.
- **Sessions** = one per tab (flatten mode) + one child per cross-origin iframe.
- **element.rs owns no connection.** It takes `cdp: &mut Cdp` and a session string.
- **No JavaScript is injected by the scanner and `Runtime.enable` is never called.**
  This is the crate's strongest property — it is what makes the whole
  console-getter family of bot detection inert against it. Any fix that breaks it is the
  wrong fix. (The glow is the one exception; see "Known exposure" below.)

### One rule that keeps being learned the hard way

> **Was this rule written back when nobody was listening to this socket?**

Issues 1, 2, 5 and 6 were all the same mistake in different clothes: assumptions written for
the old cosmetics-only socket became load-bearing when the sockets merged. The header comment
at `browser/browser.rs` that claimed "the scanner's socket is never touched" has been
corrected — it had been false for a long time.

---

## 1. What was fixed since the last handoff

All measured, all in the working tree.

| Was | Now |
|---|---|
| LLM call could hang **forever** (no read timeout, Stop never reached) | bounded; dead provider errors at 542.1s after 3 retries |
| One CDP timeout tore down **every** session | scoped to the call; only a torn transport hangs up |
| Scan with one wedged sibling tab: **45.24s** | **0.20s** |
| `alert()` blocked the tab until everything timed out | answered on arrival; scan 0.23s; text reported to the model |
| Renderer crash wedged the agent **permanently** | nav 0.24s, next scan 0.71s, tab re-created |
| Dead host → `{"status":"success"}` | `{"status":"error", …ERR_NAME_NOT_RESOLVED}`, and it stops the batch |
| Scan failure → exception, **no result at all** | `final_status="error"`, cleanup runs, caller gets a result |
| Lost tab **adopted the human's frontmost tab** and painted over it | takes a blank surface or creates its own; never adopts a real page |
| `launch_chrome` returned ~3.1s before Chrome could serve `/json/version` | waits for a driveable browser |
| Tab numbers came from Chrome's MRU order and silently renumbered | stable left-to-right; `switch_tab` 0.00–0.01s |
| No persistent browser profile | `autouse_data/browser_profiles/<name>/chrome`, cookies survive restarts |

Also: `requests.post` had **no timeout** in 21 places across `mac`/`windows`/`ios` — same
forever-hang as the Rust side. One `LLM_HTTP_TIMEOUT` constant per platform tree now; 38
`requests.*` calls, 0 missing a timeout.

### Things worth not re-deriving

- **A read timeout is not a broken connection.** `drain` always knew this; `rpc` did not.
  Unix reports it `WouldBlock`, Windows `TimedOut`; tungstenite keeps its partial frame, so
  the stream is still in step and the next read resumes.
- **Cosmetics must not wait.** `Cdp::send` fires and forgets. CDP executes in order per
  session, so the command still lands — only the receipt nobody read is given up.
- **The glow is armed ONCE per session.** `Page.addScriptToEvaluateOnNewDocument` makes Chrome
  re-inject on every document, so it survives navigation with no further calls. Re-walking
  every tab each scan was never what kept it on screen.
- **Crash recovery cannot pre-poll.** The crash event is only noticed while something reads
  the socket. Recovery triggers off the **scan failing**, which is the reliable signal.
- **Chrome writes `Local State` from memory on exit**, so branding a profile has to be seeded
  *before* the first launch. Editing it afterwards is silently thrown away.
- **A custom profile avatar is not possible.** `Google Profile Picture.png` is only honoured
  for a signed-in Google account; with an empty `gaia_id` Chrome keeps the setting and
  ignores the file. A stock `IDR_PROFILE_AVATAR_*` does render. The Auto-Use logo reaches the
  browser as the favicon of the agent's page instead (`logo/logo.html`).

---

## 2. Still open

### Issue A 🟡 — batch renumbering and stale element mapping
**Was Issue 8. Still not reproduced, still not fixed.** Re-verify before touching it; one
related claim in the previous handoff turned out to be a test error.

- **Renumbering.** `refresh_tabs()` replaces the listing `[n]` resolves against. The stable
  `tab_order` added since makes this *much* less likely — numbering no longer moves under
  `bring_to_front` — but a `[{new_tab …}, {switch_tab 3}]` batch has not been re-tested.
- **Stale mapping.** `set_elements` is called once per step and nothing clears it when the
  driven tab changes, so `[{switch_tab 2}, {click 7}]` may dispatch tab 1's rect onto tab 2.
  **This one is untouched and is the real remaining risk.** Fix: stamp the scanned tab's
  target id into `ElementsState` and reject element actions from a different tab.

### Issue B 🟡 — Ctrl-C still deferred up to a timeout
`check_py_signals()` has two callers (the port wait, and `await_browser_ready`). Every other
blocking path runs under `py.detach` and never re-acquires the GIL. The LLM timeout work
bounded the worst case, but SIGINT during a long wait is still not prompt. Fix: call it inside
`wait_event`'s and `settle()`'s loops — both already poll.

### Issue C ✅ fixed — `Auto_Use/web/target/` untracked (commit `1376d01`)
996 files removed from the index; artifacts stay on disk, builds no longer dirty the tree.
The old commits still carry the binaries, so repo HISTORY stays heavy — only worth a
`git filter-repo` if clone size ever matters.

### Issue D ✅ fixed — `controller/research/` deleted
The module, its `mod.rs` declaration, both helper fns, the digest branch in the step loop,
the capture/backfill blocks, and the `pending_research_response` / `research_memory_index` /
`is_research_digest` threading are all gone (~200 lines). `apply_pending` now takes `None`
for its index argument. Verified with a full fake-LLM agent run: two steps, wait then done,
clean result dict. `ElementsState.application_name` (dead, research-justified) went with it;
`set_elements` keeps its two-arg signature for mac/ios parity.

### Issue E 🟡 — 3.6 GB of orphaned profiles
`~/.autouse/chrome-<port>` — 22 directories from the old port-keyed scheme. Nothing reads
them now. Safe to delete once anything worth keeping has been salvaged.

---

## 3. Known exposure (not a bug, a decision to make)

The scanner injects no JavaScript — but **the glow does**, into the main world of every
document: `window.__autouseOverlay`, `__autouseBox`, `__autouseBoxHide`,
`__autouseScrollProbe`, plus a `<div class="autouse-layer">` and a `MutationObserver`.
Detection is one line: `if (window.__autouseOverlay)`.

Arming it in an isolated world (`addScriptToEvaluateOnNewDocument{worldName}`) hides the
globals and costs only `executionContextId` plumbing through the flash and scroll-probe
paths. It does **not** hide the div — isolated worlds share the DOM. Full invisibility would
mean rendering via CDP's `Overlay` domain, which never touches the DOM but cannot draw the
glow's design. That is a product decision, not an engineering one.

---

## 4. How to verify anything here

No test suite. Drive the extension from Python — everything is in-process.

```python
import Auto_Use.web.agent as _a          # IMPORTANT: triggers _ensure_built()
from Auto_Use.web.agent import BrowserScanner, launch_chrome
from Auto_Use.web import agent_native as N
from Auto_Use import browser_profile_dir

launch_chrome(9222, True, str(browser_profile_dir()))   # True = headless
sc = BrowserScanner(port=9222); sc.start()
N._route_action_debug(sc, [{"type": "update_tab", "value": "https://example.com"}], "test")
sc.scan_elements()
tree, screenshot_b64, tabs = sc.get_scan_data()
sc.stop()
```

**Gotchas that cost real time:**

- **Import `Auto_Use.web.agent` first.** Importing `Auto_Use.web.agent_native` directly
  bypasses `_ensure_built()`, so you test a stale `.so` and misread the result. This has
  produced two false conclusions already.
- Tool parameter names differ: `update_tab`/`new_tab` take **`value`**, `switch_tab`/
  `close_tab` take **`id`**. Source of truth: `llm_provider/llm_manager.rs`.
- Run from the repo root or `import Auto_Use` fails.
- Serve test pages from a local `http.server`. Chrome **refuses `data:` URLs via
  `Page.navigate`** ("Cannot navigate to invalid URL"), so a `data:` test page silently
  tests nothing.
- **Kill your test Chrome when you are done.** A leftover headless instance on 9222 gets
  attached to by the next run, and a wedged tab in it will look like a code bug.
- Check `uptime` before trusting a timing.
