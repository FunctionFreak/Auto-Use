# Agent Operation — Optional Flags

Reference for anyone (human or AI agent) editing [main.py](main.py) who wants to
use the agent's optional flags. `main.py` ships with only the essentials
(`MODE`, `PROVIDER`, `MODEL`, `task`); everything below is opt-in — add it to
`run_agent(...)` only when you need it.

| Flag | Applies to | Default | Section |
|---|---|---|---|
| `headless` | `"web use"` only | `False` (visible Chrome) | Part 1 |
| `extra_tasks` | `"web use"`, `"mobile use, ios"` + `device="simulation"` | none (single task) | Part 2 |
| `speed` | `"computer use"` (mac + windows), `"mobile use, ios"` | `"quality"` | Part 3 |
| `save_conversation` | every mode | `False` (nothing written) | Part 4 |
| `device` | `"mobile use, ios"` only | `"simulation"` (iOS Simulator) | Part 6 |
| `ios_version` | `"mobile use, ios"` + `device="simulation"` only | newest installed runtime | Part 6 |

> `headless` applies to `"web use"` mode ONLY. `extra_tasks` works in `"web use"`
> (N agents, one shared Chrome) and in `"mobile use, ios"` with
> `device="simulation"` (N agents, one simulator each). Every other mode —
> including `device="hardware"`, where there is only one phone — runs exactly
> one task and raises a `ValueError` if you pass extras.

---

# Part 1 — Headless mode

## What it is

`"web use"` drives a real Chrome via CDP. **Headless** means Chrome runs
**without a visible window** — the agent still loads pages, clicks, types and
takes screenshots exactly the same way, you just don't see a browser on screen.

| | Headful (default) | Headless |
|---|---|---|
| Chrome window | Visible on screen | Hidden — no window at all |
| You can watch the agent | ✅ Yes | ❌ No (check `conversation/` + logs) |
| Screen clutter / tab flicker | Yes, esp. with parallel tasks | None |
| Agent behaviour | Same | Same |

## Default

**`headless = False` — Chrome opens a visible window.** This is the default at
every layer (`run_agent`, the web `AgentService`, `launch_chrome`), so if you
don't pass `headless` at all you get a headful browser.

## How to turn it on in main.py

Add a `headless` variable and pass it to `run_agent`:

```python
# "web use" only — run Chrome without a visible window. Ignored by every other
# mode (run_agent drops kwargs the target AgentService doesn't declare).
headless = True

run_agent(
    mode=MODE,
    provider=PROVIDER,
    model=MODEL,
    task=task,
    save_conversation=conversation,
    external_terminal=True,
    speed=speed,
    headless=headless,      # ← add this
)
```

Set it back to `False` (or remove the argument) to see the browser again.

⚠️ **The flag only applies when Chrome is launched.** Chrome deliberately stays
up after a run, and the next run **attaches** to whatever is already on port
9222 — headless or not — ignoring the flag (you'll see "Attached to Chrome
already on port 9222"). So if you switch between headless and headful, quit
the old Chrome first:

```
pkill -f "remote-debugging-port=9222"
```

## When to use which

- **Headful (default)** — first runs, debugging, demos where you want to watch
  the agent work.
- **Headless** — long unattended runs, servers/CI, and **parallel tasks** (see
  Part 2), where N agents share one window and tabs switch on screen constantly.

---

# Part 2 — Running multiple tasks in parallel

Works in two modes, with the same `extra_tasks` flag:

| | `"web use"` | `"mobile use, ios"` + `device="simulation"` |
|---|---|---|
| What each task gets | Its own web agent, pinned to **its own tab** | Its own **iOS Simulator** + its own WebDriverAgent |
| Shared resource | **One Chrome** for everyone | Nothing shared — a phone screen can't be split |
| Ports | One debug port | One WDA port per task (8100, 8101, 8102, …) |
| Limit | Practical (tabs + RAM) | Number of simulator devices you have installed |

Not supported with `device="hardware"` — you only have one iPhone, so extras
raise a `ValueError` telling you to use simulation.

## How it works

When you pass extra tasks, **all** tasks — including the main `task` — run in
parallel, each as its own child process.

- Live terminal output is prefixed per task: `[task 1] ...`, `[task 2] ...`
- One `Ctrl+C` stops every task at once.
- A summary prints when every task has finished.
- On iOS the parent boots every simulator and starts every WebDriverAgent
  **before** any agent runs, and shuts them all down at the end — success,
  error, or `Ctrl+C`.

## How to write it in main.py

`main.py` runs one task by default. To go parallel, do three things:

**1. Set the mode** — web use:

```python
MODE = "web use"
```

…or iOS simulators (one per task):

```python
MODE = "mobile use, ios"
DEVICE = "simulation"      # required for parallel — hardware can't split
IOS_VERSION = None         # optional: pin a runtime, e.g. "26.5"
SIM_DEVICE = "iphone"      # "iphone" / "ipad" / exact simulator name (simulation only)
```

**2. Define the extra tasks** (below the main `task`). Use `None` for slots you
don't need — `None` and empty strings are skipped silently:

```python
# ── Parallel tasks ("web use" only) ──────────────────────────────────────────
task_2 = """
search for the current weather in London and note the temperature
"""
task_3 = """
open github.com and find the trending Python repositories today
"""
task_4 = None   # unused slot — skipped
```

**3. Pass them to `run_agent` via `extra_tasks`:**

```python
run_agent(
    mode=MODE,
    provider=PROVIDER,
    model=MODEL,
    task=task,                              # task 1 — also runs in parallel
    save_conversation=conversation,
    external_terminal=True,
    speed=speed,
    headless=True,                          # web use only — recommended for parallel (Part 1)
    extra_tasks=[task_2, task_3, task_4],   # tasks 2..N
)
```

On iOS, pass the device flags instead of `headless`:

```python
run_agent(
    mode=MODE,                              # "mobile use, ios"
    provider=PROVIDER,
    model=MODEL,
    task=task,
    device=DEVICE,                          # "simulation" — required for parallel
    ios_version=IOS_VERSION,
    sim_device=SIM_DEVICE,
    save_conversation=conversation,
    extra_tasks=[task_2, task_3, task_4],
)
```

**Want more than 4 tasks?** Define `task_5 = """..."""` (and so on) and append
it to the list: `extra_tasks=[task_2, task_3, task_4, task_5]`.

## Where results and logs go

Each task runs inside its own working directory under `./parallel/`:

```
parallel/
├── task_1/
│   ├── result.json        # {"status": "success"|"error", "message": ...}
│   ├── conversation/      # if save_conversation is enabled
│   ├── raw_reasoning/
│   └── debug/
├── task_2/
└── task_3/
```

⚠️ `./parallel/` is **deleted and recreated on every parallel run** — copy out
anything you want to keep before running again.

`run_agent` returns a combined dict:

```python
{"status": "success"|"error", "message": "...",
 "results": [{"task": "...", "status": "...", "message": "..."}, ...]}
```

## Tips

- **Headless (web):** with a visible window, N agents share one Chrome and tabs
  switch on screen constantly. Pass `headless=True` (Part 1) for a calmer run.
- **One task = simpler path:** if only `task` is set (all extras are `None`),
  the normal single-agent path runs — no `parallel/` folder, no child processes.
- **Independent tasks only:** the agents don't talk to each other. Write each
  task so it stands alone; don't make task 3 depend on task 2's result.
- **iOS — how many tasks can I run?** One per installed simulator device
  (`xcrun simctl list devices`). Ask for more than you have and the run stops
  with "Not enough simulators"; add devices in Xcode → Window → Devices and
  Simulators.
- **iOS — first parallel run is slower.** Each task builds WebDriverAgent into
  its own folder (`build-sim`, `build-sim-2`, …) because concurrent builds
  sharing one folder re-sign the same app underneath each other and kill one
  another's test runner. Task 1 reuses the build you already have, so only the
  extra tasks pay, and only once (~270 MB each, all gitignored).
- **iOS — watch them work:** every simulator is visible on screen at once. They
  are all shut down when the run ends.

## Under the hood

Implemented in [Auto_Use/agent_launcher.py](Auto_Use/agent_launcher.py):
`run_agent(..., extra_tasks=[...])` filters out empty entries and, if any
remain, hands `[task] + extras` to either
`run_parallel_web_agents()` — one `python -m Auto_Use.web.agent` child per task
against the shared Chrome port — or `run_parallel_sim_agents()` — one
simulator + one WebDriverAgent (`USE_PORT` 8100, 8101, …) + one
`python -m Auto_Use.ios.agent` child per task. Each iOS child gets its port via
`AUTOUSE_WDA_PORT` and its own `Auto_Use/ios/scratchpad/task_N/` via
`AUTOUSE_IOS_SESSION`, so parallel agents never share a screen, a port, or a
notes folder. You can also call either function directly from your own script.

---

# Part 3 — Speed mode (`"computer use"` and `"mobile use, ios"`)

## What it is

`speed` picks between two ways of running the main agent loop:

| | `"quality"` (default) | `"fast"` |
|---|---|---|
| System prompt | `system_prompt.md` (full) | `fast_system_prompt.md` (leaner) |
| Per-step output | `thinking` + `memory` + `next_goal` | `memory` only |
| Reasoning per step | More deliberate | Lighter — fewer tokens, quicker steps |
| Best for | Complex / multi-step / unfamiliar tasks | Simple, well-defined tasks where latency matters |

Both modes use the same tools and the same agent — `fast` just trims what the
model has to write out each step (it drops the `thinking` and `next_goal`
fields) and swaps in a shorter prompt, so each step is cheaper and quicker.

## Which modes support it

| Mode | `speed` supported? |
|---|---|
| `"computer use"` — macOS | ✅ `"quality"` / `"fast"` |
| `"computer use"` — Windows | ✅ `"quality"` / `"fast"` |
| `"mobile use, ios"` | ✅ `"quality"` / `"fast"` |
| `"web use"` | ⚠️ Accepted, but **falls back to `"quality"`** — the browser agent has no `fast_system_prompt.md` yet (you'll see a one-line notice at startup) |
| `"shell use"` | ❌ Ignored |

## Default

**`speed = "quality"`.** Every `AgentService` (mac, windows, iOS) declares
`speed: str = "quality"`, and any value other than `"fast"` is treated as
`"quality"`. If you don't pass `speed` at all, you get quality mode.

## How to turn on fast mode in main.py

Add a `speed` variable and pass it to `run_agent`:

```python
# Control speed mode — "quality" (default) or "fast" — fast = lean output +
# fast prompt. Honoured by computer use (mac/windows) and iOS; ignored by shell
# use; web use falls back to quality for now.
speed = "fast"

run_agent(
    mode=MODE,
    provider=PROVIDER,
    model=MODEL,
    task=task,
    save_conversation=conversation,
    external_terminal=True,
    speed=speed,            # ← add this
)
```

Set it to `"quality"` (or remove the argument) to go back to the default.

## When to use which

- **`"quality"` (default)** — first runs, anything multi-step, tasks where the
  agent needs to plan or recover from surprises. Start here.
- **`"fast"`** — short, well-defined tasks you've seen the agent do reliably
  ("open X and click Y", "read the number on this screen") where you want
  lower latency and fewer tokens per step.

## Under the hood

Each platform's `AgentService.__init__` (e.g.
[Auto_Use/mac/agent/main_driver/service.py](Auto_Use/mac/agent/main_driver/service.py))
normalises `speed`, passes it to `LLMManager` (which selects the fast or full
tool registry), picks `_MAIN_TRACK_PARAMS_FAST` vs `_MAIN_TRACK_PARAMS`, and
loads `fast_system_prompt.md` vs `system_prompt.md` from the same folder.

---

# Part 4 — Conversation saving (all modes)

## What it is

`save_conversation` makes the agent write a readable log of **exactly what it
sent to the LLM at every step** — the system prompt, the interleaved
assistant/user turns, and the response it got back. It's the tool for
debugging "why did the agent do that?" and for reviewing a run after the fact.

Works in **every mode**: `"computer use"` (mac + windows), `"shell use"`,
`"web use"`, and `"mobile use, ios"`.

## Default

**`save_conversation = False` — nothing is written to disk.** Every
`AgentService` (mac, windows, iOS, web, and the shell/coder agent) declares
`save_conversation: bool = False`. If you don't pass it, you get no log files.

## What it writes (when `True`)

Files land in the **current working directory** (wherever you ran
`python main.py` from):

```
conversation/
├── conversation.txt        # session header, started fresh each run
├── conversation_1.txt      # step 1 — full payload sent + response received
├── conversation_2.txt      # step 2 ...
└── ...
raw_reasoning/              # raw LLM outputs per step (cleared each run)
```

Each `conversation_N.txt` is a "memory snapshot" — a faithful peek at what the
agent could see at step N. You'll also see
`Memory snapshot saved: conversation_N.txt` in the terminal after each step.

⚠️ Both folders are **reset at the start of every run** — copy anything you
want to keep before running again.

For **parallel web tasks** (Part 2) each task writes its own copy under
`./parallel/task_N/conversation/`.

## How to turn it on in main.py

Add a `conversation` variable and pass it as `save_conversation`:

```python
# Control conversation saving — writes conversation/ + raw_reasoning/ to the
# current directory. Set to False (the default) to write nothing.
conversation = True

run_agent(
    mode=MODE,
    provider=PROVIDER,
    model=MODEL,
    task=task,
    save_conversation=conversation,   # ← add this
    external_terminal=True,
)
```

Set it to `False` (or remove the argument) to stop writing logs.

## When to use which

- **`False` (default)** — normal runs and demos; keeps your working directory
  clean and skips the small per-step file writes.
- **`True`** — debugging a task that goes wrong, tuning prompts, or when you
  need an audit trail of what the agent saw and decided at each step.

## Under the hood

Each `AgentService.__init__` (e.g.
[Auto_Use/mac/agent/main_driver/service.py](Auto_Use/mac/agent/main_driver/service.py))
creates `conversation/` and `raw_reasoning/` when the flag is on, and calls
`_save_conversation_snapshot()` after every LLM step to write
`conversation_N.txt`. The web agent does the same in Rust
(`save_conversation_snapshot` in
[Auto_Use/web/agent/main_driver/service.rs](Auto_Use/web/agent/main_driver/service.rs)).

---

# Part 5 — Together AI provider (`PROVIDER = "together"`)

## What it is

Together AI is an OpenAI-compatible provider with native tool calling and
image input. Available on every platform - `"computer use"` (macOS and
Windows), `"web use"` and `"ios use"`. Set `TOGETHER_API_KEY` in `.env` (or save it in the app's
Settings → API Keys).

| `MODEL` (main.py) | Together model id |
|---|---|
| `inkling` | `thinkingmachines/Inkling` |
| `muse-glimmer-30b` | `meta-models/Muse-Glimmer-30B` |
| `minimax-m3` | `MiniMaxAI/MiniMax-M3` |

## How the `web` tool works on Together

Together has no native web search. When the desktop or iOS agent calls its `web` tool
under `PROVIDER = "together"`, the query is handed to the **browser agent**
(`Auto_Use/web`) running **headless on the same model**, and its final
`done` report comes back as the web result. Expect that step to take a few
minutes rather than seconds.

- Runs in its own headless Chrome on port **9333** (`AUTOUSE_WEB_FALLBACK_PORT`)
  with its own profile **web_fallback** (`AUTOUSE_WEB_FALLBACK_PROFILE`) — never
  the visible one `"web use"` uses — so it can't disturb the desktop agent's
  screen.
- Wall-clock cap **15 min** (`AUTOUSE_WEB_FALLBACK_TIMEOUT`, seconds); the
  Stop button / Ctrl+C interrupts it.
- Each run's `result.json` + `agent.log` are kept under
  `autouse_data/web_fallback/<run-id>/` for inspection.

Implementation: `Auto_Use/{mac,windows,ios}/controller/tool/web/web_agent.py` (identical on all three).

---

# Part 6 — iOS device target (`"mobile use, ios"`)

## What it is

`device` picks WHAT the iPhone agent drives:

| | `"simulation"` (default) | `"hardware"` |
|---|---|---|
| Runs on | An iOS Simulator on this Mac | Your paired physical iPhone |
| iOS version | `ios_version` picks the runtime (e.g. `"26.5"`); omitted = newest installed, preferring an already-booted simulator | Whatever the phone runs — `ios_version` is ignored |
| Needs | Full Xcode (simulator runtimes ship with it) — no signing, no Apple account, no pairing | One-time pairing in Settings → Connect Device (Team ID signing, WDA install) |
| First start | Builds WebDriverAgent for the simulator once — a few minutes; later runs boot + attach in under a minute | Seconds (WDA is pre-installed at pairing) |
| Agent behaviour | Same — identical WDA endpoints, tree, taps, screenshots | Same |

## Default

**`device = "simulation"`.** Omitting both flags boots the best installed
simulator. Hardware is the explicit opt-in.

## How to use it in main.py

```python
MODE = "mobile use, ios"
DEVICE = "simulation"   # or "hardware" for your paired iPhone
IOS_VERSION = None      # simulation only — e.g. "26.5"; None = newest installed
SIM_DEVICE = "iphone"   # simulation only — "iphone" / "ipad" / exact simulator name

run_agent(
    mode=MODE,
    provider=PROVIDER,
    model=MODEL,
    task=task,
    device=DEVICE,          # ← add this
    ios_version=IOS_VERSION,
    sim_device=SIM_DEVICE,
    save_conversation=conversation,
)
```

Both flags are ignored by every other mode, so they're safe to leave in place
when you switch `MODE`.

`device="simulation"` is also what unlocks **parallel tasks** on iOS — each task
gets its own simulator. See Part 2.

## When to use which

- **`"simulation"` (default)** — day-to-day runs, testing tasks, no phone on
  the desk, or when you need a specific iOS version installed via Xcode.
- **`"hardware"`** — anything that needs the real device: real apps with your
  logins, camera, cellular, notifications, app-store apps not in the sim.

## Under the hood

The launcher branches in [Auto_Use/agent_launcher.py](Auto_Use/agent_launcher.py):
hardware keeps the existing `wda_session` (pymobiledevice3 USB forward +
xcuitest launch of the pre-installed WDA). Simulation uses
[Auto_Use/ios_connector/sim_session.py](Auto_Use/ios_connector/sim_session.py):
`xcrun simctl` resolves/boots the device, then one unsigned
`xcodebuild test -destination id=<sim>` serves WDA on the same
`localhost:8100` the agent already talks to — so the whole agent stack is
unchanged. App scanning switches from `pymobiledevice3 apps list` to
`xcrun simctl listapps` automatically. On teardown WDA is stopped, the
simulator is **shut down**, and the Simulator app is closed — it's not a
real device, so nothing is left running after the agent terminates.
