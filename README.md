<div align="center">
  <img src="Auto_Use/logo/logo_rounded.png" alt="Auto Use Logo" width="120"/>

  # Auto Use

  **🤖 Computer Use Framework for macOS & Windows**

  Let AI drive your computer — **Autouse AI — Computer Use**, now with both the macOS and Windows builds combined in a single repository. Control your entire OS with natural language. Browser automation, coding tasks, file management — all powered by vision-language models.

  [Features](#-features) • [Architecture](#-multi-agent-architecture) • [GUI Engine](#-how-gui-control-works) • [iOS](#-ios--iphone-and-simulator) • [Parallel Tasks](#-parallel-tasks) • [Example Tasks](#-example-tasks) • [Providers](#-supported-providers) • [Setup](#-setup) • [Maintainer](#-maintainer)
</div>

---

<div align="center">

  ## ⭐ Star History

  <a href="https://star-history.com/#FunctionFreak/Auto-Use&Date">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=FunctionFreak/Auto-Use&type=Date&theme=dark&v=2" />
      <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=FunctionFreak/Auto-Use&type=Date&v=2" />
      <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=FunctionFreak/Auto-Use&type=Date&v=2" width="720" />
    </picture>
  </a>

</div>

---

## ✨ Features

- **GUI automation via hybrid Accessibility + Vision** — Quartz screen capture annotated with the macOS Accessibility tree, then handed to a multimodal LLM. Not pure pixel-guessing, not pure DOM-walking — both at once.
- **Multi-agent hierarchy with unlimited spawning** — Parent → CLI Agent → parallel **Minion** scouts. No hard cap on how many sub-agents a task can fan out into.
- **Minion: read-only coding-agent explorer** — sandboxed sub-agents that `grep`, `glob`, `view`, and shell out, but can never write. Spawned in parallel, each in its own isolated session scratchpad.
- **Native AppleScript runner** — full `osascript` execution with automatic TCC permission-dialog handling so first-run consent prompts don't stall the agent.
- **Sandboxed Bash** — subprocess execution confined to a Desktop workspace, with path blocklists, 10-minute total / 15-second idle timeouts, and interactive-stdin detection.
- **Web agent** — browser automation living alongside system control, not bolted on top of it.
- **iOS automation — simulator or real iPhone** — the same agent drives an iOS Simulator (the default: no pairing, no signing, no Apple account) or your paired iPhone, over WebDriverAgent. Pick the target with one flag: `device="simulation"` / `device="hardware"`.
- **Parallel tasks** — run several agents at once from a single `main.py`: N web agents sharing one Chrome (a tab each), or N iOS agents with **one simulator each**. Live per-task output, one `Ctrl+C` stops everything.
- **7 LLM providers** — Anthropic, Google, Groq, OpenAI, OpenRouter, Perplexity, Together AI. Switch providers and models per task.

> ⚙️ Every optional flag — `device`, `ios_version`, `extra_tasks`, `speed`, `headless`, `save_conversation` — is documented in **[agent_operation.md](agent_operation.md)**.

---

## 🧬 Multi-Agent Architecture

Auto Use is not one model in a loop — it's a hierarchy of agents that can spawn more agents.

```
                    ┌─────────────────────┐
                    │   Parent Agent      │
                    │  (GUI · Web · OS)   │
                    └──────────┬──────────┘
                               │ spawns
                    ┌──────────▼──────────┐
                    │     CLI Agent       │
                    │  (coding · shell)   │
                    └──────────┬──────────┘
                               │ spawns ∞
       ┌───────────────────────┼───────────────────────┐
       ▼                       ▼                       ▼
  ┌─────────┐             ┌─────────┐             ┌─────────┐
  │ Minion  │             │ Minion  │             │ Minion  │
  │  scout  │   …         │  scout  │   …         │  scout  │
  └─────────┘             └─────────┘             └─────────┘
   read-only: shell · view · grep · glob · scratchpad · exit
```

- **Parent Agent** — orchestrates GUI, web, and OS-level work. Reaches for the CLI Agent when a task needs serious code reading or writing.
- **CLI Agent** — the coding workhorse. Runs in the terminal, can read/write files, run shell commands, and spawn Minions to parallelize codebase exploration.
- **Minion** — a read-only scout sub-agent. Its action schema is restricted to `shell`, `view`, `grep`, `glob`, `scratchpad`, `exit`. No writes, no Applescript, no GUI — just fast, isolated reconnaissance. Multiple minions run in parallel; the CLI Agent waits on all of them and merges results.

Each Minion runs in its own session-isolated scratchpad at `cli_minion/{session_id}/`, with results polled from a per-run JSON file. Live progress streams to the frontend through `minion_start` / `minion_line` / `minion_end` events.

**Invocation examples:**

```bash
# Direct CLI Agent — main.py with MODE = "shell use"
python main.py

# CLI Agent with a specific task
python -m Auto_Use.mac.agent.coder --task "refactor the auth module"

# Single Minion for a quick read-only question
python -m Auto_Use.mac.agent.minions --task "where is _validate_token defined and who calls it?"
```

<div align="center">
  <img src="demo/coder.gif" alt="CLI Agent coding in the terminal" width="720"/>
</div>

---

## 🔬 How GUI Control Works

Most "AI controls your screen" projects pick one approach. Auto Use uses three, in sequence, on every loop iteration:

1. **Accessibility tree extraction** — `AXUIElementCreateSystemWide` from PyObjC's `ApplicationServices` walks the foreground window into a structured tree of buttons, text fields, links — each with coordinates, labels, and role.
2. **Annotated screenshot** — `Quartz.CGWindowListCreateImage` captures the display; PIL overlays numbered magenta bounding boxes onto every interactive element discovered in step 1. Retina / HiDPI scaling is handled automatically.
3. **Vision-model dispatch** — the annotated screenshot (JPEG-compressed, base64-encoded) plus the element tree are sent to the active multimodal LLM (Claude, GPT-4o, Gemini, …). The agent references targets by their overlay index, not by raw pixel coordinates.

**Why hybrid?** Pure-vision agents hallucinate coordinates and miss off-screen state. Pure-accessibility agents miss everything that lives in a canvas or video. Auto Use shows the model both, refreshed after every action, so it always reasons over current ground truth.

<div align="center">
  <img src="demo/gui_action.gif" alt="Auto Use uninstalling VLC media player via GUI automation" width="720"/>
</div>

---

## 🍎 AppleScript Engine

The AppleScript pathway is a first-class action type, not a bash escape hatch.

- Executes arbitrary `tell application "..."` blocks via `osascript`.
- **Auto-handles TCC permission popups** — a background watcher (`_scan_permission_dialog()`) detects macOS consent popups mid-run and clicks the affirmative button — **"Allow", "OK", or "Always Allow"** — so both automation prompts ("Allow / Don't Allow") and folder prompts ("Don't Allow / OK") don't deadlock the CLI agents. If a popup can't be clicked (Accessibility not granted), the blocked shell command fails fast (~8s) with a clear `permission_dialog` message instead of stalling.
- **Smart app lifecycle** — uses `open` for inactive apps, strips redundant `activate` / `launch` directives for already-running apps so foregrounding doesn't fight the user's focus.
- **30-second execution cap** with structured error reporting back to the agent.

> **One-time setup — grant Full Disk Access.** So the coder/minion agents can read & write Desktop/Documents/Downloads without macOS permission popups, grant Full Disk Access in **System Settings → Privacy & Security → Full Disk Access**: add **AutoUse.app** (packaged) or your **Terminal / VS Code / `python`** binary (dev runs). Auto Use opens this pane for you on first launch if it isn't granted.

---

## 💻 Sandboxed Bash

Shell access is wrapped in a `Sandbox` that the model can't escape by accident.

- Commands run via `subprocess.Popen` inside a designated Desktop workspace.
- **Path protection** — blocks access to `/system`, `/usr/sbin`, `/private/var`.
- **Timeouts** — 600-second total cap; 15-second idle detection so interactive prompts (Python REPL, `Enter name:`, etc.) don't hang the agent.
- **Interactive-stdin detection** — regex-matches common prompt patterns and lets the agent supply `input_text` to drive the dialog.
- **Structured returns** — `{status, output, error, agent_location}` so the agent always knows where it is in the filesystem.

---

## 🌐 Web Search

Live web search built into the agent loop — no manual copy-paste between browser and chat. Backed by Perplexity Sonar (or any Perplexity / OpenAI / Anthropic web-search-capable model when selected), the agent issues queries, reads results, and folds the findings straight into its next action.

<div align="center">
  <img src="demo/web_search.gif" alt="Auto Use answering 'Why is AMD stock price going up?' via live web search" width="720"/>
</div>

---

## 📱 iOS — iPhone and Simulator

The same agent loop that drives your Mac drives an iPhone: it reads the app's
accessibility tree, annotates a screenshot, and taps / swipes / types through
**[WebDriverAgent](https://github.com/appium/WebDriverAgent)**. Two targets,
one flag:

| | `device="simulation"` (default) | `device="hardware"` |
|---|---|---|
| Runs on | An iOS Simulator on your Mac | Your paired iPhone |
| iOS version | `ios_version="26.5"` — or omit for the newest installed runtime | Whatever the phone runs |
| Needs | Full Xcode. **No signing, no Apple ID, no pairing** | One-time pairing + Team ID signing (see [Setup](#-setup)) |
| Best for | Everyday runs, testing, a specific iOS version | Real apps with your logins, camera, cellular |

```python
# main.py
MODE        = "mobile use, ios"
DEVICE      = "simulation"   # or "hardware"
IOS_VERSION = None           # e.g. "26.5"; None = newest installed
```

Auto Use boots the simulator, builds and starts WebDriverAgent on it, runs your
task, then shuts the simulator down when the agent finishes — success, error,
or `Ctrl+C`. The first run compiles WebDriverAgent once (a few minutes); after
that it starts in well under a minute.

---

## ⚡ Parallel Tasks

One `main.py`, many agents. Fill in `task_2`, `task_3`, … and every task —
including the first — runs at the same time in its own child process:

```python
task   = """find the cheapest flight to Tokyo next month"""
task_2 = """summarise today's top Hacker News thread"""
task_3 = """check my GitHub notifications"""
```

| Mode | What each task gets | Shared |
|---|---|---|
| `"web use"` | Its own web agent, pinned to **its own browser tab** | One Chrome for everyone |
| `"mobile use, ios"` + `device="simulation"` | Its own **iOS Simulator** + its own WebDriverAgent port | Nothing — a phone screen can't be split |

- Live output is prefixed per task: `[task 1] …`, `[task 2] …`
- One `Ctrl+C` stops every task; a summary prints when they all finish.
- Results and logs land in `./parallel/task_N/` (`result.json`, `conversation/`, `raw_reasoning/`, `debug/`).
- iOS parallelism is simulator-only — `device="hardware"` runs one task, because you only have one phone.

Full details, limits and examples: **[agent_operation.md](agent_operation.md)**.

---

## 🎯 Example Tasks

Just describe what you want — Auto Use picks the right tool for the job.

### 🖥️ GUI Task
```
"Uninstall VLC media player"
```

### 👨‍💻 Coding Task
```
"Create a Python Flask API with user authentication"
```

### 🌐 Web Search Task
```
"Why is AMD stock price going up?"
```

### 💻 CLI Task
```
"Check disk space and clean up temp files"
```

### 🍎 AppScript Task
```
"Send an iMessage to John saying I'll be 10 minutes late"
```

### 📱 iOS Task
```
"Open Settings, turn on Dark Mode, and tell me which iOS version this is"
```

---

## 🎯 What Can Auto Use Do?

| Category | Examples |
|----------|----------|
| **Browser** | Fill forms, extract data, navigate sites, download files |
| **Productivity** | Create documents, manage spreadsheets, organize files |
| **Development** | Write code, debug errors, run tests, manage git |
| **System** | Install software, configure settings, manage processes |
| **Research** | Search web, compile information, generate reports |
| **Mobile (iOS)** | Drive apps on a simulator or your iPhone — tap, type, scroll, launch apps |
| **At scale** | Run several tasks in parallel — many browser tabs, or many simulators |

---

## 🧠 Supported Providers

Auto Use supports **7 LLM providers**:

- **Anthropic**
- **Google**
- **Groq**
- **OpenAI**
- **OpenRouter**
- **Perplexity**
- **Together AI** — macOS + web agent; its `web` tool routes through the built-in browser agent

---

## 📋 Requirements

- **macOS** (Apple Silicon or Intel) **or** **Windows 10/11**
- **API Key** from any supported provider
- *For iOS only:* macOS with **full Xcode** and at least one iOS simulator runtime (a paired iPhone is needed only for `device="hardware"`)

---

## 🚀 Setup

> 💡 **Recommended for most users:** We strongly encourage installing the **latest binary build** from our [official website](https://autouse.netlify.app/) for a fully seamless installation and the complete UI experience — no manual setup required.
>
> The steps below are for developers who want to run Auto Use directly from source.

### 🍎 macOS

1. **Run the setup script**

   ```bash
   bash MacOS_setup.sh
   ```

2. **Add your API key(s)**

   Copy the example env file and fill in your keys:

   ```bash
   cp .env.example .env
   ```

   Then open `.env` and add the API key for whichever provider(s) you want to use.

3. **Run Auto Use** — pick your experience:

   ```bash
   python app.py    # 🖼️  Full desktop UI (recommended) — webview frontend with live agent streams, minion progress, model switcher
   python main.py   # 💻  Terminal-only experience — same agents, no GUI, fully usable over SSH
   ```

4. **Optional — iOS support** (simulator or iPhone — see [iOS](#-ios--iphone-and-simulator))

   Skip this unless you want Auto Use to drive iOS. Either way it needs **full
   Xcode** (not just the Command Line Tools).

   ```bash
   bash ios_setup.sh
   ```

   This clones **[WebDriverAgent](https://github.com/appium/WebDriverAgent)**
   (Appium/Facebook, BSD 3-Clause) at pinned tag `v15.1.1` into
   `Auto_Use/ios_connector/`. It is not bundled in this repo — you get it from
   the Appium project directly. Re-run it any time; it reuses an existing
   clone, or pass `--force` to re-fetch.

   **Simulator (`device="simulation"`, the default) — that's it.** No Apple ID,
   no signing, no pairing: Auto Use builds WebDriverAgent unsigned and boots a
   simulator on demand. Just make sure you have an iOS runtime installed
   (Xcode → Settings → Components) — check with `xcrun simctl list runtimes`.

   **Physical iPhone (`device="hardware"`)** additionally needs an Apple ID
   under Xcode → Settings → Accounts and a one-time signing + pairing pass in
   the connector UI — either **Settings → Connect Device → iPhone** in the
   desktop app, or standalone via `python Auto_Use/ios_connector/setup.py`.

   > A free Apple ID works, but its provisioning profiles expire after 7 days, so
   > you'll re-sign weekly. A paid developer account lasts a year.

### 🪟 Windows

1. **Run the setup script**

   ```bat
   windows_setup.bat
   ```

2. **Add your API key(s)**

   Copy the example env file and fill in your keys:

   ```bat
   copy .env.example .env
   ```

   Then open `.env` and add the API key for whichever provider(s) you want to use.

3. **Run Auto Use** — pick your experience:

   ```bat
   python app.py     :: 🖼️  Full desktop UI (recommended) — webview frontend with live agent streams, minion progress, model switcher
   python main.py    :: 💻  Terminal-only experience — same agents, no GUI, fully usable over SSH
   ```

> **Note:** On Windows, Python **3.13.3** is the preferred version for best compatibility.

---

## 🛡️ Safety

- **Sandbox Isolation** — Code runs in a protected environment
- **No System Modification** — Won't delete files or run destructive commands without permission
- **Permission Awareness** — Asks for confirmation before accepting elevation prompts
- **Path Protection** — Blocks access to critical system folders

---

## 🌟 Why Auto Use?

| Feature | Auto Use | Others |
|---------|----------|--------|
| Multi-agent system | ✅ | ❌ |
| Domain knowledge injection | ✅ | ❌ |
| Multi-provider LLM support | ✅ | Limited |
| Vision-based automation | ✅ | ✅ |
| Coding agent | ✅ | ❌ |
| Read-only sub-agent scouts (Minion) | ✅ | ❌ |
| Unlimited sub-agent spawning | ✅ | ❌ |
| Hybrid Accessibility + Vision GUI control | ✅ | ❌ |
| Native AppleScript with auto-consent | ✅ | ❌ |
| Web search integration | ✅ | ❌ |
| Secure sandbox | ✅ | ❌ |

---

## 💻 OS Support

This repository supports **both macOS and Windows** — the platform builds live side-by-side in the same repo:

- **macOS** — `Auto_Use/mac`
- **Windows** — `Auto_Use/windows`
- **Web** — `Auto_Use/web` (CDP-controlled Chrome, host-OS independent)
- **iOS** — `Auto_Use/ios` + `Auto_Use/ios_connector` (simulator or iPhone; macOS host only)

---

## 👤 Maintainer

Built and maintained by **[Ashish Yadav](https://github.com/FunctionFreak)**.

Issues and pull requests are welcome at
[FunctionFreak/Auto-Use](https://github.com/FunctionFreak/Auto-Use).

---

## 📄 License & Attribution

Licensed under the **MIT License** — see [LICENSE](LICENSE).

You are free to use, copy, modify, merge, publish, distribute, sublicense, and sell
this software, including commercially. The only condition is:

1. Retain the copyright notice and the MIT permission notice in all copies or
   substantial portions of the software.

Not required, but genuinely appreciated:

2. Credit **Ashish Yadav** as the original author.
3. Link back to the project: https://github.com/FunctionFreak/Auto-Use

### Third-party components

Some directories in this repository contain **vendored third-party code that is not
covered by the MIT license above**. The MIT grant — including the permission to sell
and sublicense — applies only to Auto Use's own code. Full details in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

| Component | How it's included | License |
|---|---|---|
| **WebDriverAgent** — by Facebook, Inc. and the [Appium](https://github.com/appium/WebDriverAgent) project | **Not redistributed.** [`ios_setup.sh`](ios_setup.sh) clones it at pinned tag `v15.1.1` | BSD 3-Clause, with some Apache 2.0 files |
| **Interception** — Windows kernel input driver, by Francisco Lopes da Silva ([oblitum](https://github.com/oblitum/Interception)) | **Not redistributed.** `windows_setup.bat` downloads the author's own signed release (`v1.0.1`, SHA-256 verified) at setup time | **Dual-licensed:** LGPL v3.0 non-commercial; **commercial use requires a separate paid license from the author** |

> ⚠️ **If you plan to ship or sell anything built on Auto Use that bundles or installs
> the Interception driver, you must obtain a commercial Interception license from its
> author yourself.** Auto Use's MIT license does not — and cannot — grant it to you.

### How to cite

> Ashish Yadav. *Auto Use — Computer Use Framework for macOS & Windows.* 2026.
> https://github.com/FunctionFreak/Auto-Use

```bibtex
@software{autouse2026,
  author  = {Ashish Yadav},
  title   = {Auto Use --- Computer Use Framework for macOS \& Windows},
  year    = {2026},
  url     = {https://github.com/FunctionFreak/Auto-Use}
}
```
