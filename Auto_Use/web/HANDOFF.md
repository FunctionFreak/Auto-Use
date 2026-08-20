# Auto-Use web agent — issue backlog & handoff

**Written:** 2026-08-20 · **Branch:** `developer_branch` · **HEAD at writing:** `72a7c00`

This is a hand-off document. It records what is wrong with `Auto_Use/web`, why, how to
verify each claim, and how each fix should be done. Everything marked **measured** was
reproduced against real Chrome in this repo — not inferred from reading code.

---

## 0. Read this first

### Current architecture (post-restructure, correct — do not undo)

```
web/
  browser/browser.rs      THE BROWSER: Chrome process, tab set, the ONE CDP socket,
                          tab lifecycle, cosmetics (glow/flash). Owns `Cdp`.
  browser/glow/           glow.css / glow.js / glow.html  (assets)
  tree/element.rs         SCANNING ONLY. Borrows a session (`scan_page(cdp, page, …)`).
                          Contains no navigation, no input, no REPL, no launch.
  controller/tab/         navigate / reload / back / forward / new / switch / close
  controller/click|input|scroll/   input dispatch
  controller/{done,wait,scratchpad,todo_tracker}/   non-page tools
  agent/main_driver/      the agent loop
  llm_provider/           provider adapters
```

Key invariants, all verified:

- **One WebSocket to Chrome.** `tungstenite::client` appears exactly once
  (`browser/browser.rs:502`); one `Cdp` is constructed (`browser/browser.rs:977`).
- **Sessions** = one per tab (flatten mode) + one child per cross-origin iframe.
  The iframe sessions are not a design smell; they are the only way to read an OOPIF.
- **element.rs owns no connection.** It takes `cdp: &mut Cdp` and a session string.
- **No JavaScript is ever injected into the page and `Runtime.enable` is never called.**
  This is a core promise of the scanner. Any fix that breaks it is the wrong fix.
  (The glow overlay is the one exception and it is cosmetics-only, on its own path.)
- There is **no `element` binary and no REPL any more** — it is all one cdylib.

### Uncommitted work in the tree right now

Three files are modified and **not committed**:

| File | Change | State |
|---|---|---|
| `browser/browser.rs` | Fix #1 (event buffer) + Fix #2 (session-scoped waits) + load-state tracking | ✅ verified |
| `tree/element.rs` | `settle()` scopes its Network events to the scanned session | ✅ verified |
| `controller/tab/service.rs` | load waits scope to the tab's session; `switch_tab` waits for a mid-load tab | ⚠️ **see Issue 3 — has an open performance question** |

Build is clean (`cargo build --release`, no warnings).

---

## 1. How to verify anything in this document

There is no test suite. Drive the extension from Python — everything is in-process now:

```python
import socket, time
from Auto_Use.web.agent import BrowserScanner, launch_chrome
from Auto_Use.web import agent_native as N

s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
launch_chrome(port, True)                     # True = headless
sc = BrowserScanner(port=port); sc.start()

# route a tool call exactly as the model would emit it
r = N._route_action_debug(sc, [{"type": "update_tab", "value": "https://example.com"}], "test")

sc.scan_elements()
tree, screenshot_b64, tabs = sc.get_scan_data()
print(sc.current_url, sc._all_tabs)
sc.stop()
```

**Gotchas that cost time — read these:**

- Tool parameter names differ. `update_tab` / `new_tab` take **`value`**;
  `switch_tab` / `close_tab` take **`id`**. Passing the wrong key returns
  `"'None' is not a valid tab number"` — that is the tool working correctly.
  Source of truth: `llm_provider/llm_manager.rs:89-215`.
- The scan header (settle ms, element count) is line 2 of `Path(sc.out_dir)/"tree.txt"`.
  `get_scan_data()` strips it.
- Run test scripts from the **repo root** (`/Users/ashishyadav/Desktop/fork/Auto-Use`)
  or `import Auto_Use` fails.
- Serve test pages from a local `http.server`. Do **not** use `file://` — it is fine in
  principle but a missing file silently yields `chrome-error://chromewebdata/`, which
  looks like a code bug (it isn't — see Issue 4, that is the errorText hole).
- Building a golden-scan regression harness would be worth an hour. There is none.

### Reference: browser-use

A competitor/peer project, useful for comparison. **Local copy:**
`/Users/ashishyadav/Downloads/browser-use-main/` (v0.13.8) ·
Upstream: <https://github.com/browser-use/browser-use>

Findings from reading it that are relevant here:

- It has **no Playwright/patchright dependency** — it drives raw CDP via `cdp-use`
  (`pyproject.toml:42`). Same architectural bet Auto-Use made.
- Its open-source code has **no stealth layer**. Every `stealth` mention routes to
  their paid cloud (`README.md:137,142,260`). Local anti-detection is two launch
  flags (`browser_use/browser/profile.py:71,193`). Auto-Use is not behind here.
- It has **no page-state classifier at all**. Its captcha watchdog is inert on local
  Chrome (it listens for events only their cloud proxy emits,
  `browser_use/browser/watchdogs/captcha_watchdog.py:152`), and its crash watchdog is
  entirely commented out (`browser/session.py:1690,1703`).
- **It throws away the HTTP status.** `NavigationCompleteEvent.status` exists
  (`browser/events.py:452`) but is always dispatched `None` (`browser/session.py:981`)
  with the incorrect comment *"CDP doesn't provide status directly"*. A 403 and a 200
  are indistinguishable to a browser-use agent.
- Worth stealing: the layered timeout ladder (per-CDP-request → per-action → per-step),
  the `loaderId` staleness guard on navigation waits (`browser/session.py:1103-1109`),
  and `DoneAction.success: bool` + tri-state `is_successful()` (`agent/views.py:734`).
- Worth **not** copying: verdicts inferred by an LLM judge (`agent/judge.py`) rather
  than read from the page.

---

## 2. Issues, in the order they should be done

Severity: 🔴 blocker · 🟠 serious · 🟡 moderate

---

### Issue 1 🔴 — DONE, verify before building on it: event buffer dropped the newest event

**Status: fixed in the working tree, measured.**

`stash()` kept 2000 events and, once full, discarded every *new* one. With
`Network.enable` on, Chrome emits ~8 events per request, so a page with ≥250
subresources filled the buffer and `Page.loadEventFired` was among the discards.
`take_events` only removes the method it is asked for, so a buffer full of network
chatter never drained — the wait stayed deaf until the next scan's `clear_events`.

Effect: every navigation to an ordinary page took **exactly `LOAD_TIMEOUT` (30s)** and
then reported **success**.

Fix applied (`browser/browser.rs`): only buffer the five methods anything actually waits
on (`WAITED_METHODS`), and evict **oldest-first** if the cap is ever reached.

Measured: 250 subresources 30.07s → **0.22s**; 600 subresources → 0.42s.

---

### Issue 2 🟠 — DONE, verify before building on it: waits ignored which tab an event came from

**Status: fixed in the working tree, measured.**

All tabs share one socket. `take_events` / `wait_event` matched only on method name, so
any tab's event satisfied any tab's wait.

- A sibling tab's `Page.loadEventFired` ended this tab's navigation wait early —
  measured returning in 0.54s with `status:"success"` while its own page was still loading.
- A sibling tab's network traffic kept `settle()` from ever going quiet — measured
  **3019 ms settle on a completely idle page**.

Fix applied: `take_events(method, session: Option<&str>)` and
`wait_event(method, session, timeout)` now match the envelope's `sessionId`.
`settle()` passes the scanned page's session; the tab load waits pass the tab's session.
`Target.attachedToTarget` deliberately stays **unscoped** (`None`) — OOPIF discovery is
cross-session by nature: the envelope names the *parent* session.

Measured: idle tab with a busy sibling open, 3019 ms settle → **61 ms**, 3.24s → 0.93s.

---

### Issue 3 🟠 — IN PROGRESS, needs finishing: switching to a loading tab scans half a page

**Status: fix written and working, but with an unresolved cost. Decide before committing.**

The required behaviour, plainly: *the agent says "switch to tab 2" → tab 2 is displayed
→ if it is still loading, wait for it → then scan it.*

What the code did: `bind_tab` (`browser/browser.rs`) set `tab_id`, called
`bring_to_front()` and `glow_tabs()` — **no load wait**. `settle()` does not cover this,
because settle only tracks requests whose *start* it personally witnessed; a navigation
begun before the scan is invisible to it.

**Measured before the fix** — switched to a tab streaming its body, scanned immediately:
early content present, **late content missing**, whole thing done in 0.23s. The model
received half a page with nothing indicating more was coming.

Fix written: `Cdp` now tracks a `loading: HashSet<sessionId>`, maintained passively in
`stash()` via `note_load_state()` — `Page.frameNavigated` on a **main** frame marks a
session loading, `Page.loadEventFired` clears it. `switch_tab` calls
`cdp.is_loading(sess)` and, only if true, `await_load(cdp, sess)`.

**Measured after: late content now present.** ✅

**⚠️ Open question — this is why it is not finished.** In the test, the switch itself took
**18.03s** for a page that finishes in ~2s, and a switch to an *already-loaded* tab took
**0.89s** (should be ~0). The 18s measurement was taken with a background thread racing
for the scanner mutex, so it may be an artifact of the test rather than the fix — it was
not isolated before this document was written.

**What the next session should do:**

1. Re-measure `switch_tab` cost with **no concurrent thread**: open two tabs, let both
   fully load, then time switching back and forth. Expected ≈ the cost of
   `bring_to_front` + `glow_tabs` alone.
2. If an already-loaded switch is still ~0.9s, find out which of `is_loading`'s
   `drain(0)`, `bring_to_front`, or `glow_tabs` is paying it. `glow_tabs` is the prime
   suspect — see Issue 6, it attaches and issues three 5s-timeout RPCs *per open tab*.
3. If a mid-load switch still costs far more than the page's real load time, check
   whether the load event is being consumed by a competing `await_load` before
   `switch_tab`'s own wait starts. If so, `is_loading` should be authoritative and the
   wait should be driven off the `loading` set clearing, not off catching the event.
4. Cap the wait. `LOAD_TIMEOUT` is 30s; a switch should probably not block that long —
   consider a shorter ceiling and returning a *warning* in the tool result rather than
   blocking, so the model learns the page was still loading.

---

### Issue 4 🟠 — NOT STARTED, highest value for the monitoring use-case: dead sites report success

**Measured.** `update_tab` to an unresolvable host and to a refused port both return
`{"status": "success", "message": "navigated the current tab to …"}`.

`navigate_to` (`controller/tab/service.rs:39`) discards `Page.navigate`'s reply, so
`errorText` (`net::ERR_NAME_NOT_RESOLVED`, `ERR_CONNECTION_REFUSED`, …) is never read.
Chrome then loads *its own error page* and fires an ordinary `loadEventFired`, so the
wait is satisfied and the tool reports success. `grep errorText` returns nothing in the
whole crate.

**Fix:**

```rust
fn navigate_to(cdp: &mut Cdp, sess: &str, url: &str) -> Result<(), CdpFail> {
    cdp.take_events("Page.loadEventFired", Some(sess));
    let reply = cdp.rpc("Page.navigate", json!({"url": url}), Some(sess), 10.0)?;
    if let Some(e) = reply.get("errorText").and_then(Value::as_str) {
        if !e.is_empty() {
            return Err(CdpFail::Clean(format!("could not load {url}: {e}")));
        }
    }
    await_load(cdp, sess);
    Ok(())
}
```

**Note the deliberate asymmetry, and keep it:** a load-wait *lapse* is intentionally
**not** an error — the comment at `controller/tab/service.rs:24-27` explains that the
scanner settles again before the next scan, so a slow site costs a wait, never a wrong
tree. `errorText` is different: that page is never coming.

Also worth doing here: **capture the main-document HTTP status** from
`Network.responseReceived` (where `params.type == "Document"` on the main frame) and
surface it on `ScanOut`. `Network.enable` is already on. This is the deterministic
signal that makes "did the site load?" answerable without asking the model, and it is
the thing browser-use structurally cannot do. It is the foundation for any page-state
classification later.

---

### Issue 5 🟠 — NOT STARTED: a renderer crash wedges the agent permanently

**Measured.** `update_tab` to `chrome://crash` returned `success` after 30s. Every
subsequent `scan_elements()` then raised `ScannerError: Page.enable: connection lost`
after 45s, then 55s, indefinitely. Nothing recovers.

Cause: `start()` trusts `tab_exists`, and a crashed target **stays in `/json/list`**, so
the tab is never re-created. Its session is dead, so nothing works. Nothing in the crate
consumes `Inspector.targetCrashed` / `Target.targetCrashed` / `Target.detachedFromTarget`
(zero hits).

**Fix:** treat a crash as tab death. `Page` is already enabled, so listen for
`Inspector.targetCrashed`; and on an rpc failure against the driven tab's session,
`forget_tab()` it and reload or re-create the tab once before surfacing an error.

---

### Issue 6 🟠 — NOT STARTED: one busy tab anywhere costs ~30s per scan, and cosmetics can kill the shared socket

**Measured**, agent tab quiet, one sibling tab running a blocking JS loop:

| | quiet sibling | busy sibling |
|---|---|---|
| `start()` | 0.02s | **15.02s** |
| scan ×3 | 0.20s each | **30.23s each** |

Two compounding causes in `browser/browser.rs`:

- `unflash()` (`:1423`) iterates **every** session in `cdp.sessions`, 5s timeout each;
  `glow_tabs()` (`:1479`) iterates **every open tab**, three 5s RPCs each.
- Any rpc timeout calls `drop_conn()` (`:672`), which clears **all** sessions, the event
  buffer and the reply map — so a cosmetic timeout on an unrelated tab destroys the
  session the scan is using.

This is the same root cause as Issues 1 and 2: **rules written for the old
cosmetics-only socket now govern the socket everything depends on.** The comment at
`browser/browser.rs:426` still asserts *"the scanner's socket is never touched"* — no
longer true, and worth fixing as part of this.

**Fix:** scope `unflash` to the driven tab's session; give `glow_tabs` a short per-tab
budget (0.5–1s) and skip a tab that has timed out once for the rest of the run; on a
per-session timeout drop **the session**, not the connection.

This is very likely also what makes `switch_tab` cost 0.89s in Issue 3.

---

### Issue 7 🟠 — NOT STARTED: one scan error aborts the run and skips every cleanup path

`agent/main_driver/service.rs:968` — `scanner.scan_elements(py)?;` sits in
`process_request`'s loop **outside** the guarded `run_step` section (`:1149-1190`), whose
`Err` arm is what turns a step failure into `final_status = "error"` + `close_pairing`.
The `?` returns straight out of the function, skipping `stop()`, `emit_flow("run_end")`,
`compression.finish_run()` and the result dict entirely. `agent_launcher.py:180` has no
`except`, so the caller gets an exception and **no result at all**.

Triggers are ordinary — `Page.captureScreenshot timed out` on a self-reloading page, and
the crash in Issue 5.

**Fix:** wrap the scan the way the step is wrapped — on `Err`, set `final_status="error"`,
set `final_message`, `break`, so the cleanup tail runs and the caller gets a result.

---

### Issue 8 🟡 — NOT STARTED: batch renumbering and stale element mapping

Reported by review as measured; **I did not independently reproduce these**, and one
related claim of mine turned out to be my own test error, so re-verify before fixing.

- **Renumbering.** `refresh_tabs()` (`browser/browser.rs:1613` in `create_tab`,
  `controller/tab/service.rs:306` in `close`) replaces the listing that `[n]` resolves
  against, and `bind_tab` → `bring_to_front` has just reordered Chrome's MRU
  `/json/list`. In a single batch `[{new_tab …}, {switch_tab 3}]` the switch can land on
  a different tab than the model picked, and report success.
  *Note:* sequential `switch_tab` calls are **correct** — verified. Only the
  within-one-batch case is suspect.
- **Stale mapping.** `set_elements` is called once per step and nothing clears it when
  the driven tab changes. `[{switch_tab 2}, {click 7}]` dispatches `[7]`'s rect from tab
  1's scan onto tab 2. Review measured a click landing at the old tab's coordinates and
  reporting `"clicked [2] (textbox)"`.

**Fix:** freeze the tab listing for the step and have `new_tab`/`close_tab` mark it
stale, so a later `[n]` in the same batch returns a tool error telling the model to
re-read `<all_tabs>`. Stamp the scanned tab's target id into `ElementsState` and reject
element actions whose scan came from a different tab.

---

### Issue 9 🟡 — NOT STARTED: smaller items

- **Ctrl-C deferred up to 30s.** `check_py_signals()` (`browser/browser.rs:159`) now has
  exactly one caller (the port-wait loop). Every blocking path runs under `py.detach` and
  never re-acquires the GIL. Measured: SIGINT 2s into an `update_tab` raised
  `KeyboardInterrupt` 28s later. Fix: call it inside `wait_event`'s and `settle()`'s
  loops (both already poll), and slice `rpc`'s read timeout into ~1s waits.
- **A failed `/json/list` reads as "zero tabs."** `browser/browser.rs:244-252` collapses
  every error — refused connection, read timeout, non-JSON body — to `Vec::new()`.
  Combined with the next item this silently abandons the driven tab.
- **Lost tab silently adopts the human's frontmost tab.** `start()`
  (`browser/browser.rs:1002-1007`) falls back to `page_targets(port).first()` whenever
  `tab_exists` is false, then **overwrites it with the logo page** if it is blank
  (`:1016`). Measured: closed the agent's tab, next scan bound to an untouched
  `about:blank` tab and rewrote its content. The docs at `:983` and `:1380` claim this
  MRU guess was eliminated; it was moved, not eliminated. Fix: on losing the bound tab
  mid-run, fail the step with a model-visible message or create our own tab — never
  adopt-and-overwrite a tab the human owns.
- **`Auto_Use/web/target/` is tracked in git.** 998 files, ~296 MB. `.gitignore:146`
  ignores it, but ignore rules do not apply to already-tracked files, so every build
  dirties the tree. `git rm -r --cached Auto_Use/web/target` fixes it (one large commit).
- **`controller/research/`** is unreachable — no `research` tool is declared to the model
  and `ResearchService` is never constructed. ~51 dormant references also remain threaded
  through `agent/main_driver/service.rs` (`pending_research_response`,
  `research_memory_index`, `is_research_digest` in `run_step`'s signature). Deleting it
  is surgery on the live agent loop; do it as its own commit, not bundled with anything.

---

## 3. Suggested order

1. **Finish Issue 3** (decide the cost question, then commit Issues 1–3 together).
2. **Issue 4** — `errorText`, plus capture the HTTP status. Smallest change, largest
   payoff, and everything about page-state reporting later depends on it.
3. **Issue 6** — cosmetics scoping. Likely also fixes Issue 3's residual cost.
4. **Issue 7** — so failures produce a result instead of an exception.
5. **Issue 5** — crash recovery.
6. **Issue 8**, then **Issue 9**.

## 4. The one theme worth remembering

Issues 1, 2 and 6 are all the same mistake in different clothes: **assumptions written
for the old cosmetics-only socket now govern the one socket that everything depends on.**
The restructure itself was right — element.rs really is scanning-only, and one socket
really is better than three. But when the sockets merged, several "this is only
cosmetics, it can fail freely / it can be capped / it need not know which tab" rules came
along and quietly became load-bearing.

When fixing anything in `browser/browser.rs`, ask: *was this rule written back when
nobody was listening to this socket?*
