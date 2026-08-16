# Agent Operation — Optional Flags

Reference for anyone (human or AI agent) editing [main.py](main.py) who wants to
use the agent's optional flags. `main.py` ships with only the essentials
(`MODE`, `PROVIDER`, `MODEL`, `task`); everything below is opt-in — add it to
`run_agent(...)` only when you need it.

| Flag | Applies to | Default | Section |
|---|---|---|---|
| `headless` | `"web use"` only | `False` (visible Chrome) | Part 1 |
| `extra_tasks` | `"web use"` only | none (single task) | Part 2 |
| `speed` | `"computer use"` (mac + windows), `"mobile use, ios"` | `"quality"` | Part 3 |
| `save_conversation` | every mode | `False` (nothing written) | Part 4 |

> `headless` and `extra_tasks` apply to `"web use"` mode ONLY. Every other mode
> ignores `headless` and runs exactly one task — passing extra tasks there raises
> a `ValueError`.

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

## When to use which

- **Headful (default)** — first runs, debugging, demos where you want to watch
  the agent work.
- **Headless** — long unattended runs, servers/CI, and **parallel tasks** (see
  Part 2), where N agents share one window and tabs switch on screen constantly.

---

# Part 2 — Running multiple tasks in parallel

## How it works

When you pass extra tasks, **all** tasks — including the main `task` — run in
parallel. Each task gets its own web agent as a separate child process, but they
all share **one Chrome** (launched or attached once, on one debug port), and each
agent is pinned to **its own single browser tab**.

- Live terminal output is prefixed per task: `[task 1] ...`, `[task 2] ...`
- One `Ctrl+C` stops every task at once.
- A summary prints when every task has finished.

## How to write it in main.py

`main.py` runs one task by default. To go parallel, do three things:

**1. Set the mode to web use:**

```python
MODE = "web use"
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
    headless=True,                          # recommended for parallel runs (see Part 1)
    extra_tasks=[task_2, task_3, task_4],   # tasks 2..N ("web use" only)
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

- **Headless:** with a visible window, N agents share one Chrome and tabs switch
  on screen constantly. Pass `headless=True` (Part 1) for a calmer run.
- **One task = simpler path:** if only `task` is set (all extras are `None`),
  the normal single-agent path runs — no `parallel/` folder, no child processes.
- **Independent tasks only:** the agents don't talk to each other. Write each
  task so it stands alone; don't make task 3 depend on task 2's result.

## Under the hood

Implemented in [Auto_Use/agent_launcher.py](Auto_Use/agent_launcher.py):
`run_agent(..., extra_tasks=[...])` filters out empty entries and, if any
remain, hands `[task] + extras` to `run_parallel_web_agents()`, which launches
one `python -m Auto_Use.web.agent` child per task against the shared Chrome
port. You can also call either function directly from your own script.

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
