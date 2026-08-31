# Copyright 2026 Cursortouch — Auto-Use

import copy
import os
import sys
import time
from typing import Optional

from dotenv import load_dotenv

from .openrouter.service import OpenRouterProvider
from .openrouter.view import get_model_info as get_openrouter_model_info
from .groq.service import GroqProvider
from .groq.view import get_model_info as get_groq_model_info
from .openai.service import OpenAIProvider
from .openai.view import get_model_info as get_openai_model_info
from .anthropic.service import AnthropicProvider
from .anthropic.view import get_model_info as get_anthropic_model_info
from .google.service import GoogleProvider
from .google.view import get_model_info as get_google_model_info
from .perplexity.service import PerplexityProvider
from .perplexity.view import get_model_info as get_perplexity_model_info
from .together.service import TogetherProvider
from .together.view import get_model_info as get_together_model_info

# Load environment variables
load_dotenv()

# Every agent here speaks NATIVE TOOL CALLING — the tool registries below ARE
# the output contract. There is no JSON-envelope response schema anymore: the
# main driver's AGENT_OUTPUT_SCHEMA / FAST_AGENT_SCHEMA are gone the same way
# CLI_AGENT_SCHEMA and MINION_SCHEMA were, and no provider is handed a
# response_format. The only schema-less, tool-less path left is mode="text"
# (the memory-compression handoff), which wants plain prose.


# The coder/minion transcript carries an optional per-turn `provider_meta` on
# assistant messages — the provider's OWN metadata for that turn (Gemini 3
# thought signatures, OpenRouter reasoning blocks), needed to echo the turn
# back exactly as the model produced it. Only these providers translate it;
# for everyone else the key is stripped before the request is built, because
# openai/groq/perplexity/together forward the message dicts to their API
# verbatim and an unknown key there is a 400.
_META_KEY = "provider_meta"
_META_PROVIDERS = ("openrouter", "google")

_TLS_PROBED = False


def _ensure_tls_works() -> None:
    """If this machine runs HTTPS interception (antivirus / corporate proxy)
    with a certificate Python can't validate — which breaks EVERY https call,
    including the LLM providers — disable certificate verification process-wide
    (requests + httpx) so the agent can actually reach its model. Without this
    the providers get an SSL error, return nothing useful, and the agent
    "finishes" without doing anything.

    Secure by default: it probes once and only disables verification when the
    probe fails with a genuine CERTIFICATE error (interception), never on a
    plain network error. Idempotent.
    """
    global _TLS_PROBED
    if _TLS_PROBED:
        return
    _TLS_PROBED = True
    import ssl
    import sys
    try:
        import httpx
        import certifi
    except Exception:
        return
    probe = "https://api.openai.com/v1"
    cert_error = False
    for verify in (certifi.where(), ssl.create_default_context()):
        try:
            httpx.get(probe, verify=verify, timeout=6)
            return  # verification works → leave everything secure
        except Exception as e:
            s = str(e).lower()
            if "certificate" in s or "verify failed" in s or "ssl:" in s:
                cert_error = True
                continue
            return  # network / other error → don't weaken a secure machine
    if not cert_error:
        return

    # Confirmed TLS interception → disable verification for requests + httpx so
    # every provider (requests-based and the openai / google httpx SDKs) works.
    try:
        import urllib3
        urllib3.disable_warnings()
    except Exception:
        pass
    try:
        import requests
        _orig_merge = requests.Session.merge_environment_settings

        def _merge(self, url, proxies, stream, verify, cert):
            settings = _orig_merge(self, url, proxies, stream, verify, cert)
            settings["verify"] = False
            return settings

        requests.Session.merge_environment_settings = _merge
    except Exception:
        pass
    try:
        _orig_client = httpx.Client.__init__

        def _client_init(self, *a, **kw):
            kw["verify"] = False
            _orig_client(self, *a, **kw)

        httpx.Client.__init__ = _client_init
        _orig_aclient = httpx.AsyncClient.__init__

        def _aclient_init(self, *a, **kw):
            kw["verify"] = False
            _orig_aclient(self, *a, **kw)

        httpx.AsyncClient.__init__ = _aclient_init
    except Exception:
        pass
    print("[tls] WARNING: TLS interception detected (antivirus/proxy) — HTTPS "
          "certificate verification DISABLED process-wide so the agent can "
          "reach the LLM provider. To restore secure verification, turn off "
          "HTTPS/SSL scanning for these API domains in your antivirus/proxy.",
          file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# NATIVE TOOLS — coder (cli_agent, mode="main") and minion (read-only subset
# via MINION_TOOL_NAMES) call these tools natively; no JSON envelope. The
# provider returns structured calls, tool_calls_to_steps() converts them into
# the same `[{type, ...}]` action dicts route_action already consumes — the
# controller is untouched. The MAIN DRIVER's own registry lives further down
# (_main_tools) and works exactly the same way.
#
# Each tool carries its action fields plus two tracking params (`thinking`,
# `next_goal`) ahead of them — see _TRACK_PARAMS. tool_choice is forced
# (required / {"type": "any"} / mode="ANY"), so a text-only step is
# unrepresentable; the text channel remains for free prose only.
# ---------------------------------------------------------------------------

# The two tracking params on EVERY tool call, enforced by schema. `next_goal`
# opens with the `S<n>` verdict then the "Doing/If/Next" commitment;
# `thinking` rides as an ARGUMENT (episodic — its rules live in the param
# description below). First call of a step carries both in full; later pass "".
_TRACK_PARAMS = {
    "thinking": 'Follow the <thinking> rules: "THINK: ... PLAN: ... ACT: ..." (FULL) at decision points, a short freeform paragraph (RECOVERY) on failures, 1–2 judgment lines on plan-driven steps — or exactly "skipped" when the SKIP TEST passes. Never empty. Fill on the FIRST tool call of the step; pass "" on every additional call in the same step.',
    "next_goal": 'Follow the <next_goal> rules — labeled format: "memory: <S<n> verdict + key context> next_goal: Doing: ... If ... → Next: ...". Fill on the FIRST tool call of the step; pass "" on every additional call in the same step.',
}


# The MAIN DRIVER's tracking params — THREE, matching its prompt's block
# contract ({thinking, memory, next_goal}; the calls themselves are the action
# block). Same fill convention as the coder's: the first call of a step carries
# them, later calls pass "". The skip token stays the main agent's own
# "not required" (not the coder's "skipped") — it is what <thinking> and the
# bridge notes agent_conversation writes already use.
_MAIN_TRACK_PARAMS = {
    "thinking": 'Follow the <thinking> rules — the full THINK/PLAN/ACT stages at think triggers, or exactly "not required" when the SKIP TEST passes. Never empty. Fill on the FIRST tool call of the step; pass "" on every additional call in the same step.',
    "memory": 'Follow the <memory> rules — open with the `S<n>` verdict on the previous guard, then the Targets line and key context to carry forward. Fill on the FIRST tool call of the step; pass "" on every additional call in the same step.',
    "next_goal": 'Follow the <next_goal> rules — "Doing: ... If <visible change> → Next: ...", pre-committing the next move by NAME/ROLE. Fill on the FIRST tool call of the step; pass "" on every additional call in the same step.',
}

# Fast mode carries ONE tracking param: `memory`. thinking and next_goal are
# dropped ENTIRELY rather than left required-but-empty — a required field is an
# invitation to fill it, and every token spent narrating a plan is a token not
# spent on the action, which is the whole point of fast mode. Memory alone
# carries the verdict and whatever the next step needs to know.
_MAIN_TRACK_PARAMS_FAST = {
    "memory": 'Follow the <memory> rules - if the last action FAILED verification, open with one short clause naming the failure (skip it entirely when it passed). Then the context that matters next: current app/screen state, key ids used with their (name/type/valuePattern.value/active), and any tool name + purpose + important result. Then the forward plan: "Now: <immediate step> (ToDo: <task_name>). Plan: <next 2-3 steps>. Then: <very next step>." END with the predicted visible change of THIS step\'s action prefixed "Expect:", so the next step can verify against the new screenshot. 3-5 concise lines. Fill on the FIRST tool call of the step; pass "" on every additional call in the same step.',
}


def _tool(name: str, params: dict, description: str = "", track: dict = None) -> dict:
    """Build one canonical tool def — name + parameters + description. The
    REGISTRY is the single source of tool documentation: each tool's rules
    live in its `description`, which flows to every provider dialect. The
    system prompt keeps only the step protocol and operating procedure. Tool
    defs sit in the cached prefix, so this bills once. All params are required,
    so the controller always sees every field of an action — the tracking
    params (`track`, the coder's by default) ride ahead of the action fields."""
    props = {b: {"type": "string", "description": d} for b, d in (track or _TRACK_PARAMS).items()}
    props.update(params)
    tool = {
        "name": name,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": list(props.keys()),
        },
    }
    if description:
        tool["description"] = description
    return tool


CODER_TOOLS = [
    _tool("shell", {"command": {"type": "string"},
                    "input": {"type": "string"}},
          r"""Any native PowerShell command.
- Always include `input` parameter. Use `""` when no input needed. Use actual values when program requires user input (input(), Read-Host, prompts, etc.)
- If a result returns `error: permission_dialog`, a Windows UAC / elevation prompt is blocking the command and couldn't be auto-clicked. Do NOT blindly retry — report it to the user and ask them to grant Auto Use the elevation it needs (run as administrator), then retry once they confirm.
- Format: shell {"command": "your_command", "input": ""}
- Example:
  1. shell {"command": "tree /f", "input": ""}
  2. shell {"command": "python calc.py", "input": "5\n10\n"}"""),

    _tool("view", {"path": {"type": "string"},
                   "start": {"type": "integer"},
                   "end": {"type": "integer"}},
          r"""View a file's contents with line numbers. Supports an optional line range — pair this with `grep` to read just the section you need rather than dumping whole files into context.
- All fields required. For whole-file reads pass `start: 0, end: 0`. For a range, pass actual line numbers (1-indexed, inclusive).
- `path` accepts both relative (sandbox cwd) and absolute paths — same as `grep`/`glob`.
- Whole-file mode caps at 2000 lines. If the file is larger, you'll get the first 2000 plus a footer showing the total line count — re-call with `start`/`end` to read other sections.
- Files larger than 5 MB are refused. Use `grep` with `head_limit` instead.
- Output line numbers reflect the file's real line numbers (e.g. `[400] line text` when you view starting at 400), so `write`/`replace` can use them directly without offset arithmetic.
- Format: view {"path": "file_path", "start": 0, "end": 0}
- Examples:
  1. Whole file (small):
     view {"path": "src/auth.py", "start": 0, "end": 0}
  2. Section after a grep hit at line 412:
     view {"path": "src/auth.py", "start": 400, "end": 440}
  3. Project file via absolute path:
     view {"path": "C:\\Users\\you\\projects\\app\\src\\main.py", "start": 0, "end": 0}
  4. Pair pattern — grep first, then view a narrow range:
     Step 1: grep {"pattern": "process_request\\(", "path": "", "glob": "*.py", "output_mode": "content", "case_insensitive": false, "head_limit": 10, "context": 0}
     (grep returns `Auto_Use\\windows\\agent\\cli\\service.py:233: ...`)
     Step 2: view {"path": "Auto_Use\\windows\\agent\\cli\\service.py", "start": 220, "end": 260}"""),

    _tool("grep", {"pattern": {"type": "string"},
                   "path": {"type": "string"},
                   "glob": {"type": "string"},
                   "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"]},
                   "case_insensitive": {"type": "boolean"},
                   "head_limit": {"type": "integer"},
                   "context": {"type": "integer"}},
          r"""Search file contents using regex (Python `re` syntax). Prefer this over `shell findstr / Select-String ...` — it's faster, structured (`path:line: text`), and capped to keep context small.
- All fields are required. Use empty/zero defaults for ones you don't need: `path: ""` (sandbox cwd), `glob: ""` (every text file), `case_insensitive: false`, `context: 0`.
- `path` accepts both **relative** (resolved against sandbox cwd) and **absolute** paths. If the user's task is in a project elsewhere on disk (e.g. `C:\Users\you\projects\app`), pass that absolute path — `grep` will search under it. Always pick a specific directory; never pass a drive root or `~` to crawl your whole disk.
- Returned `path:line` references are **relative to the `path` you specified**, so they're readable and don't leak full host layout. Noise dirs (`venv`, `.git`, `node_modules`, `__pycache__`, `dist`, `build`, `site-packages`, etc.) are auto-skipped.
- Three `output_mode`s — pick the one matching your intent:
  - `content` — `path:line: matching_text`. Use when you want to read the actual matches.
  - `files_with_matches` — one path per line. Use to find which files to `view` next.
  - `count` — `path: N` per file (only files with N ≥ 1). Use for distribution / sanity checks.
- Binary files, files larger than 8 MB, and lines longer than 200 chars are auto-skipped/truncated to keep output bounded.
- Format: grep {"pattern": "regex", "path": "dir_or_file", "glob": "*.py", "output_mode": "content", "case_insensitive": false, "head_limit": 50, "context": 0}
- Examples:
  1. Find callers of `process_request`:
     grep {"pattern": "process_request\\(", "path": "", "glob": "*.py", "output_mode": "content", "case_insensitive": false, "head_limit": 30, "context": 0}
  2. Files importing `requests`:
     grep {"pattern": "^import requests|^from requests", "path": "", "glob": "*.py", "output_mode": "files_with_matches", "case_insensitive": false, "head_limit": 100, "context": 0}
  3. Count TODOs case-insensitively:
     grep {"pattern": "TODO|FIXME", "path": "", "glob": "", "output_mode": "count", "case_insensitive": true, "head_limit": 50, "context": 0}
  4. Match with surrounding lines:
     grep {"pattern": "raise ValueError", "path": "src", "glob": "*.py", "output_mode": "content", "case_insensitive": false, "head_limit": 20, "context": 2}"""),

    _tool("glob", {"pattern": {"type": "string"},
                   "path": {"type": "string"},
                   "head_limit": {"type": "integer"}},
          r"""Find files by name pattern. Results are sorted newest-first (by modification time) so recently-edited files surface first.
- All fields required. Use `path: ""` for sandbox cwd; raise `head_limit` when you need to see everything.
- Like `grep`, `path` accepts both relative (sandbox-cwd-anchored) and absolute paths. To list files in a project elsewhere on disk, pass that project's absolute path. Returned paths are relative to the `path` you specified. Noise dirs (`venv`, `.git`, `node_modules`, etc.) are skipped.
- Format: glob {"pattern": "**/*.ext", "path": "base_dir", "head_limit": 100}
- Examples:
  1. All Python files: glob {"pattern": "**/*.py", "path": "", "head_limit": 200}
  2. Recently-changed YAML in configs/: glob {"pattern": "**/*.yaml", "path": "configs", "head_limit": 20}
  3. Top-level test files: glob {"pattern": "test_*.py", "path": "", "head_limit": 50}"""),

    _tool("write", {"path": {"type": "string"},
                    "line": {"type": "integer"},
                    "content": {"type": "string"}},
          r"""Write code, text, or any content into a file.
- Indentation in `content` must match the target file's style.
- Never write an entire large code in one go; build incrementally — one `write` call per step, one file at a time. Break large code across subsequent iterations.
- Always `view` the file first to get current line numbers before writing.
- `line`: The insertion point. New content starts here; existing lines from this point onward shift down.
  - Empty file: use `line: 1`.
  - Append at end: use the last line number shown by `view`.
  - Insert in the middle: use the exact line number where new content should begin.
- Format: write {"path": "file_path", "line": N, "content": "..."}
- Examples:
  1. write {"path": "scr/script.py", "line": 1, "content": "def add(a, b):\n    return a + b\n"}
  2. write {"path": "src/script.py", "line": 11, "content": "def subtract(a, b):\n    return a - b\n"}
  3. write {"path": "src/script.py", "line": 3, "content": "    print('calculating...')\n"}"""),

    _tool("replace", {"path": {"type": "string"},
                      "line": {"type": "integer"},
                      "old_block": {"type": "string"},
                      "new_block": {"type": "string"}},
          r"""Replace a block of code starting at a specific line.
- Always `view` the file first to get fresh line numbers before replacing.
- `line`: starting line number of the block you want to replace.
- `old_block`: the exact block of code currently in the file (multi-line, must match precisely).
- `new_block`: the replacement block (can be more or fewer lines than old_block).
- Multiple `replace`s in one action are supported and safe — the controller validates `old_block` against the actual file content before writing, so any line drift fails loudly with a `mismatch at line X` error rather than corrupting the file. When batching same-file replaces, order them **bottom-up** (highest line first) so earlier replaces don't shift the line numbers below them. Replaces in different files are always safe to batch.
- Format: replace {"path": "file_path", "line": 5, "old_block": "line5\nline6\nline7", "new_block": "new_line5\nnew_line6"}
- Example:
  1. replace {"path": "src/app.py", "line": 10, "old_block": "def add(a, b):\n    return a + b", "new_block": "def add(a, b):\n    result = a + b\n    print(result)\n    return result"}"""),

    _tool("web", {"value": {"type": "string"}},
          r"""Perform a web search across multiple sites automatically.
- The result arrives ONCE, in the next step's <tool> block, and is NOT kept in your history — on that step, DIGEST it: write each finding you need later as its own `scratchpad` entry. Those entries are folded back into the web step's memory as its durable result; anything you don't record is gone.
- Format: web {"value": "query"}
- Example: web {"value": "fetch the latest available LangChain package version for Groq to install"}"""),

    _tool("plan", {"op": {"type": "string", "enum": ["set", "add", "edit"]},
                   "from": {"type": "integer"},
                   "to": {"type": "integer"},
                   "value": {"type": "string"}},
          r"""Your plan document — the detailed, grounded route written AFTER exploration. The plan explains; the ToDo tracks. Three ops (all fields required; use 0 for `from`/`to` when unused):
- op "set": write/overwrite the COMPLETE plan. Use once post-exploration, or on a genuine full re-scope.
- op "add": append `value` at the end of the plan.
- op "edit": overwrite plan lines `from`..`to` (inclusive) with `value` — `value` may contain more or fewer lines than the range it replaces.
- Edit ranges always use the `[N]` line numbers from the LATEST <plan no="N"> in input — they shift after every op.
- Write `value` as PLAIN content — never write your own line numbers or a revision marker; the `[N]` numbering and the `no="N"` revision are stamped automatically. The tool response is a bare `plan updated`; the refreshed, renumbered render is always present in the current step's <persistent_memory> as <plan no="N">.
- CONTENT FORMAT: write a real structured document, NOT a flat numbered list. Use `#` / `##` markdown headings for sections (e.g. Goal, Findings, Steps, Verification), real newlines (`\n`) between lines, and indentation for sub-points. Put concrete `path:line` anchors inline. A bare "1) do X\n2) do Y" is wrong — that's a ToDo, not a plan.
- Format: plan {"op": "set", "from": 0, "to": 0, "value": "..."}
- Examples:
  1. Full plan (note the headings, newlines, indentation, and inline anchors):
     plan {"op": "set", "from": 0, "to": 0, "value": "# Goal\nSwitch the scratchpad cache to an LRU so it stops growing unbounded.\n\n# Findings\n- Cache write lives at service.py:233 (plain dict).\n- Callers: api.py:41, worker.py:88.\n\n# Steps\n## 1. Replace the cache impl\n- service.py:233 — swap dict for functools.lru_cache-backed store.\n## 2. Update callers\n- api.py:41 — adjust call to new signature.\n- worker.py:88 — same.\n\n# Verification\n- .\\.autouse_verify\\test_cache.py — 6 cases incl. empty input + eviction."}
  2. Append a section: plan {"op": "add", "from": 0, "to": 0, "value": "\n# Follow-up\n- Migrate config flag — settings.py:12."}
  3. Surgical edit (replace the two lines under Update callers): plan {"op": "edit", "from": 12, "to": 13, "value": "- api.py:41 — adjust call to new signature.\n- worker.py:88 — already uses the new signature; no change needed."}"""),

    _tool("todo_list", {"value": {"type": "string"}},
          r"""Create the tracking to-do list, derived from the plan.
- The ToDo is your TRACKER, not your plan. The plan (`plan` tool) holds the detail; each ToDo task is a short one-liner derived from it.
  - Iteration 1: if the task needs exploration, skip both plan and ToDo (or write a one-line skeleton) and dispatch minions first.
  - Right after minions report: think → write the plan (`plan` op set) from `<user_request>` + minion findings (ignore typos) → then write the ToDo from that plan.
- `todo_list` OVERWRITES and re-numbers the whole list. So write it ONCE (right after the plan), before completing any items; after that, advance it with `update_todo` only. Small plan revisions (add/edit) usually need NO ToDo change — re-issue `todo_list` only if the task list itself genuinely re-scopes, and then re-mark items already done.
- Tasks are auto-numbered as #1, #2, #3, etc. when saved.
- Format: todo_list {"value": "Objective: <corrected_user_request>\n- [ ] <task naming file/approach>\n- [ ] <task 2>"}"""),

    _tool("update_todo", {"value": {"type": "string"}},
          r"""Only update once cross verfied thoroughly. Mark a ToDo item complete by providing its #number.
- Update only after the task is confirmed complete; mark one item per call.
- Provide only the task number to mark complete.
- Format: update_todo {"value": "task number #x"}
- Example: update_todo {"value": "2"}"""),

    _tool("wait", {"value": {"type": "string"}},
          r"""Pause the pipeline for x seconds.
- Format: wait {"value": "2"}
- Example: wait {"value": "2"}"""),

    _tool("scratchpad", {"value": {"type": "string"}},
          r"""Your durable note store — verified checkpoints AND any key fact you may need later. Write an entry immediately after something is confirmed. If multiple facts are confirmed in one step, emit one separate `scratchpad` call per fact.
- Purpose: store verified facts for later steps (reduces re-reading `<agent_history>`).
- Only write entries after confirmation (never assume success).
- The live file is rendered in your input as `<scratchpad>` once any entries exist — check it before recording, so entries never duplicate.
- Use for:
  - major task completions (not tiny micro-steps)
  - metrics / numbers / final answers
  - important `web` findings to reuse later
  - exact file save paths + filenames (especially "Save As" / PDF exports)
- `value` is ONE line — a single verified note. Don't batch several facts into one entry.
- Write `value` in Markdown — inline only (`**bold**`, backticks, links), never a line break.
- Format: scratchpad {"value": "<one-line verified note in markdown format>"}
- Examples:
  1. scratchpad {"value": "**Done:** fixed all indentation errors in `app.py`"}
  2. scratchpad {"value": "**Key metric:** Disney+ revenue (Q3 2025) = **$2.1 Billion**"}
  3. Two facts confirmed in one step — two separate calls:
     scratchpad {"value": "**Verified:** parser handles empty input — **6/6** cases pass"}, scratchpad {"value": "**Saved:** report exported to `C:\\Users\\me\\Desktop\\q3_report.pdf`"}"""),

    _tool("minion", {"value": {"type": "string"}},
          r"""Read-only scout. **Don't explore the codebase yourself — send a minion.** It explores the filesystem, traces cross-file connections, and returns ONE structured summary anchored to `path:line`. You never see the intermediate reads — your context stays clean for editing.
- **Rule**: minion handles exploration + connection-tracing. You handle editing (`write`/`replace`).
- **When to send one (any of these → minion, not your own reading):** you need to understand code before editing it; you'd otherwise grep/glob/view more than ~2 times; you're tracing a symbol / caller / dependency across files; or you're mapping an unfamiliar directory. Your own `grep`/`view` are for quick re-checks of something a minion already surfaced — not first-time exploration.
- **Phrase the value as a question or objective — NEVER as instructions about which tools to use.** The minion is self-capable and picks its own tools internally. Do NOT write things like "use grep…" / "use shell…" / "use glob…" / "use view…" — just say what you want to know. The minion will figure out how to find it.
- Format: minion {"value": "<self-contained question a fresh agent can act on>"}
- Multiple minions in one action run in parallel; your loop pauses until all return as `<minion_completed>` blocks.
- **Trust the summary.** Don't re-read files yourself unless the summary is explicitly incomplete. The minion cannot edit — once you have its report, apply the change.
- Good examples (state what you want, not how to get it):
  1. minion {"value": "find every caller of _read_scratchpad_from_file — exact path:line for each."}
  2. minion {"value": "list all imports of ScratchpadService under Auto_Use/windows/ with line numbers + direct usages."}
  3. minion {"value": "give me a list of all files and directories under C:\\Users\\me\\Downloads with a one-line summary of each."}
  4. Parallel: minion {"value": "Q1..."}, minion {"value": "Q2..."}, minion {"value": "Q3..."}
- Anti-pattern (do NOT write): `"Please use the shell or glob tool to list all files in X"` — you ASK what you need; the minion picks what to RUN. Correct version: `"give me a list of all files in X"`."""),

    _tool("exit", {"value": {"type": "string"}},
          r"""End the run and deliver your final summary to the user. Pass the end-to-end summary as the value.
- Only start completion after reviewing `<agent_history>` to confirm every requested task is finished.
- Then do a final verification against actual outputs — the concrete <Tool_response> evidence — double-checking the last steps match the request.
- Use `exit` as a dedicated final step only:
  - Step 1 (no `exit`): confirm <verification> passed with concrete proof and ALL throwaway check files (`.\.autouse_verify\`) are deleted; finish/cleanup + update ToDos/scratchpad.
  - Step 2: call `exit` — the only call in its turn.
- Write `value` in Markdown — headings, `-` bullets, `**bold**`, backticks and fenced code blocks as the summary needs them.
- Format: exit {"value": "<end-to-end summary in markdown format>"}"""),
]

# MINION TOOLS — the minion's read-only subset (shell/view/grep/glob/
# scratchpad/exit; NO write/replace/web/plan/todo/wait/minion). Descriptions
# moved VERBATIM from the minion prompt's <Tool_Capability>. Same two
# tracking params (thinking + next_goal) ride on every call via _tool().
MINION_TOOLS = [
    _tool("shell", {"command": {"type": "string"},
                    "input": {"type": "string"}},
          r"""Native PowerShell - **READ-ONLY commands only** (e.g. `Get-ChildItem`, `tree /f`, `Test-Path`, `Get-Item`, `Select-String -SimpleMatch -List`). Never run anything that writes, deletes, moves, or otherwise mutates state. Always include `input: ""`.
- If a result returns `error: permission_dialog`, a Windows UAC / elevation prompt blocked the command and couldn't be auto-clicked. Don't retry blindly - note it in your final report (the parent agent needs to run Auto Use with the elevation it requires) and continue with what you can.
- Format: shell {"command": "your_command", "input": ""}
- Allowed examples:
  1. shell {"command": "tree /f", "input": ""}
  2. shell {"command": "Get-ChildItem -Recurse -Filter *.py | Select-Object -First 20", "input": ""}
  3. shell {"command": "Get-ChildItem -Recurse -Depth 2 -Directory", "input": ""}
- **Forbidden** (do NOT emit - these mutate state):
  1. shell {"command": "Set-Content ...", "input": ""}
  2. shell {"command": "Remove-Item ...", "input": ""}
  3. shell {"command": "echo hi > a.txt", "input": ""}
  4. shell {"command": "New-Item ...", "input": ""}"""),

    _tool("view", {"path": {"type": "string"},
                   "start": {"type": "integer"},
                   "end": {"type": "integer"}},
          r"""View a file's contents with line numbers. Supports an optional line range - pair this with `grep` to read just the section you need rather than dumping whole files into context.
- All fields required. For whole-file reads pass `start: 0, end: 0`. For a range, pass actual line numbers (1-indexed, inclusive).
- `path` accepts both relative (sandbox cwd) and absolute paths - same as `grep`/`glob`.
- Whole-file mode caps at 2000 lines. If the file is larger, you'll get the first 2000 plus a footer showing the total line count - re-call with `start`/`end` to read other sections.
- Files larger than 5 MB are refused. Use `grep` with `head_limit` instead.
- Output line numbers reflect the file's real line numbers (e.g. `[400] line text` when you view starting at 400) - quote them exactly in your final report.
- Format: view {"path": "file_path", "start": 0, "end": 0}
- Examples:
  1. Whole file (small):
     view {"path": "src/auth.py", "start": 0, "end": 0}
  2. Section after a grep hit at line 412:
     view {"path": "src/auth.py", "start": 400, "end": 440}
  3. Project file via absolute path:
     view {"path": "C:\\Users\\you\\projects\\app\\src\\main.py", "start": 0, "end": 0}
  4. Pair pattern - grep first, then view a narrow range:
     Step 1: grep {"pattern": "process_request\\(", "path": "", "glob": "*.py", "output_mode": "content", "case_insensitive": false, "head_limit": 10, "context": 0}
     (grep returns `Auto_Use\\windows\\agent\\cli\\service.py:233: ...`)
     Step 2: view {"path": "Auto_Use\\windows\\agent\\cli\\service.py", "start": 220, "end": 260}"""),

    _tool("grep", {"pattern": {"type": "string"},
                   "path": {"type": "string"},
                   "glob": {"type": "string"},
                   "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"]},
                   "case_insensitive": {"type": "boolean"},
                   "head_limit": {"type": "integer"},
                   "context": {"type": "integer"}},
          r"""Search file contents using regex (Python `re` syntax). Prefer this over `shell findstr / Select-String ...` - it's faster, structured (`path:line: text`), and capped to keep context small.
- All fields are required. Use empty/zero defaults for ones you don't need: `path: ""` (sandbox cwd), `glob: ""` (every text file), `case_insensitive: false`, `context: 0`.
- `path` accepts both **relative** (resolved against sandbox cwd) and **absolute** paths. Always pick a specific directory; never pass a drive root or `~` to crawl your whole disk.
- Returned `path:line` references are **relative to the `path` you specified**. Noise dirs (`venv`, `.git`, `node_modules`, `__pycache__`, `dist`, `build`, `site-packages`, etc.) are auto-skipped.
- Binary files, files larger than 8 MB, and lines longer than 200 chars are auto-skipped/truncated.
- Three output_modes:
  - `content` - `path:line: matching_text` (default; use when you want to read matches)
  - `files_with_matches` - list of paths only (use to find which files to view next)
  - `count` - `path: N` per file (use for distribution / sanity check)
- Format: grep {"pattern": "regex", "path": "dir_or_file", "glob": "*.py", "output_mode": "content", "case_insensitive": false, "head_limit": 50, "context": 0}
- Examples:
  1. Find callers of `process_request`:
     grep {"pattern": "process_request\\(", "path": "", "glob": "*.py", "output_mode": "content", "case_insensitive": false, "head_limit": 30, "context": 0}
  2. Files importing `requests`:
     grep {"pattern": "^import requests|^from requests", "path": "", "glob": "*.py", "output_mode": "files_with_matches", "case_insensitive": false, "head_limit": 100, "context": 0}
  3. Count TODOs case-insensitively:
     grep {"pattern": "TODO|FIXME", "path": "", "glob": "", "output_mode": "count", "case_insensitive": true, "head_limit": 50, "context": 0}
  4. Match with surrounding lines:
     grep {"pattern": "raise ValueError", "path": "src", "glob": "*.py", "output_mode": "content", "case_insensitive": false, "head_limit": 20, "context": 2}
- Tactics for coverage: anchor on the definition first (`def `/`class `/`function `/`=` shapes) then widen to bare usages; if a symbol might be imported under an alias, also grep `import.*<name>` and `as <alias>`; if a first pattern returns nothing, broaden (drop the `(`, make it case-insensitive, widen the path/glob) rather than concluding it's absent."""),

    _tool("glob", {"pattern": {"type": "string"},
                   "path": {"type": "string"},
                   "head_limit": {"type": "integer"}},
          r"""Find files by name pattern. Results are sorted newest-first (by modification time) so recently-edited files surface first.
- All fields required. Use `path: ""` for sandbox cwd; raise `head_limit` when you need to see everything.
- Like `grep`, `path` accepts both relative and absolute paths. Returned paths are relative to the `path` you specified. Noise dirs (`venv`, `.git`, `node_modules`, etc.) are skipped.
- Format: glob {"pattern": "**/*.ext", "path": "base_dir", "head_limit": 100}
- Examples:
  1. All Python files: glob {"pattern": "**/*.py", "path": "", "head_limit": 200}
  2. Recently-changed YAML in configs/: glob {"pattern": "**/*.yaml", "path": "configs", "head_limit": 20}
  3. Top-level test files: glob {"pattern": "test_*.py", "path": "", "head_limit": 50}"""),

    _tool("scratchpad", {"value": {"type": "string"}},
          r"""Your durable note store while exploring. Every verified finding goes here immediately so the final exit report can be assembled from it without re-reading files.
- Purpose: persist `path:line` findings + key facts across iterations.
- Only write entries after the finding is confirmed by an actual `view`/`grep` result. Never assume.
- Use one entry per finding (don't pack multiple facts into one line).
- Use for:
  - confirmed `path:line` definitions, callers, and connections
  - file/folder layout summaries
  - exact code snippets you want to quote in the report (also note the file's language/extension so the fence tag - e.g. ```python - is ready at exit time)
  - open questions you still need to answer before exit
- Write `value` in Markdown — inline only (`**bold**`, backticks around `path:line` and symbols), never a line break.
- Format: scratchpad {"value": "<one-line verified note in markdown format>"}
- Examples:
  1. scratchpad {"value": "`Auto_Use/windows/agent/service.py:254` — `_read_scratchpad_from_file` definition"}
  2. scratchpad {"value": "`Auto_Use/windows/controller/view.py:620` — `action_type == \"scratchpad\"` routing branch"}
  3. scratchpad {"value": "**Still to verify:** any other callers of `_read_scratchpad_from_file` outside `agent/service.py`"}"""),

    _tool("exit", {"value": {"type": "string"}},
          r"""Deliver your final findings report to the parent CLI agent and end the loop. **This is the only way to terminate.** The `value` must follow the `<exit_format>` template.
- Write `value` in Markdown — headings, `-` bullets, `**bold**`, backticks and fenced code blocks as the report needs them.
- Format: exit {"value": "<structured report in markdown format>"}
- Must be a standalone action - no other tool calls in the same step."""),
]

# The minion's allowed action names — passed to tool_calls_to_steps so a
# hallucinated coder-only call (write/replace/minion/...) is answered with an
# error result, never routed. This is what enforces the read-only guarantee
# now that the registry above, not a strict action union, is the contract.
MINION_TOOL_NAMES = frozenset(t["name"] for t in MINION_TOOLS)


def coder_tools_openai(registry: list = None) -> list:
    """OpenAI/OpenRouter/Groq chat-completions function format."""
    return [{"type": "function", "function": t} for t in (registry or CODER_TOOLS)]


def _with_description(tool: dict, source: dict) -> dict:
    """Carry the registry description into a dialect dict when present (a tool
    with no description — _tool() only sets the key when non-empty)."""
    if source.get("description"):
        tool["description"] = source["description"]
    return tool


def coder_tools_anthropic(registry: list = None) -> list:
    """Anthropic Messages API tools format."""
    return [_with_description({"name": t["name"], "input_schema": t["parameters"]}, t)
            for t in (registry or CODER_TOOLS)]


def coder_tools_gemini(registry: list = None) -> list:
    """Gemini function declarations (dicts accepted by google-genai)."""
    return [_with_description({"name": t["name"], "parameters": t["parameters"]}, t)
            for t in (registry or CODER_TOOLS)]


def coder_tools_perplexity(registry: list = None) -> list:
    """Perplexity agent API (Responses-style flat function tools)."""
    return [_with_description({"type": "function", "name": t["name"],
                               "parameters": t["parameters"]}, t)
            for t in (registry or CODER_TOOLS)]


# Per-action defaults: guarantees route_action always receives every field of
# an action, even if the model omits an optional-feeling one.
_ACTION_DEFAULTS = {
    "shell": {"command": "", "input": ""},
    "view": {"path": "", "start": 0, "end": 0},
    "grep": {"pattern": "", "path": "", "glob": "", "output_mode": "content",
             "case_insensitive": False, "head_limit": 50, "context": 0},
    "glob": {"pattern": "", "path": "", "head_limit": 100},
    "write": {"path": "", "line": 1, "content": ""},
    "replace": {"path": "", "line": 1, "old_block": "", "new_block": ""},
    "plan": {"op": "set", "from": 0, "to": 0, "value": ""},
    "web": {"value": ""},
    "todo_list": {"value": ""},
    "update_todo": {"value": ""},
    "wait": {"value": ""},
    "scratchpad": {"value": ""},
    "minion": {"value": ""},
    "exit": {"value": ""},
}


def _coerce(value, default):
    """Best-effort coercion to the default's type (models sometimes send '5' for 5)."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        return value if isinstance(value, str) else str(value)
    return value


# Names the coder may call — anything else is answered with an error tool
# result instead of being silently dropped, so the model can correct itself
# on the very next turn.
CODER_TOOL_NAMES = frozenset(t["name"] for t in CODER_TOOLS)


def _main_tools(track: dict) -> list:
    """The MAIN DRIVER's registry — one tool per action type. route_action and
    the frontend's tool-flow map key on these names and fields, so they must
    never drift.

    Descriptions are the system prompt's OWN tool text, copied verbatim from
    <Tool_Capability> / <os_interaction> / <task_completion> — the prompt stays
    the source of truth (it is user-managed and never edited from here); this
    just puts the same words where a native tool call can read them. Built
    twice: quality mode with three tracking params, fast mode without
    `thinking`."""
    return [
        _tool("open_app", {"value": {"type": "string"}},
              'Open an installed application. No manual search required within the OS. If the app is already running, its existing window is brought to the foreground (restored if minimized) instead of launching a duplicate instance - the result reports mode "focused" vs "launched".\n'
              '    1. Requirement: after "launched", call wait 3 seconds to allow loading; after "focused" the UI is already loaded, a 1-second wait is enough.\n'
              '    3. Example: open_app {"value": "spotify"}', track=track),
        _tool("wait", {"value": {"type": "string"}},
              'Pause execution to allow UI loading or to trigger a fresh screen scan.\n'
              '    2. Example: wait {"value": "2"}', track=track),
        _tool("web", {"value": {"type": "string"}},
              'Delegate to a specialized AI to fetch real-time information and provide data at runtime. Use this for speed instead of manual browsing.\n'
              '    2. Example: web {"value": "financial result of nvidia Q4 2025"}', track=track),
        _tool("cli_agent", {"value": {"type": "string"}},
              'Delegate a task to the CLI agent.\n'
              '    1. Format: cli_agent {"value": "instruction"}', track=track),
        _tool("cli_await", {"value": {"type": "string"}},
              'Hold pipeline until CLI agent finishes (use only for strict dependencies).\n'
              '    1. Format: cli_await {"value": "Reason"}', track=track),
        _tool("shell", {"value": {"type": "string"}},
              'Run a PowerShell command for fast execution to achieve the goal.\n'
              '    1. Example: shell {"value": "Clear-RecycleBin -Force"}', track=track),
        _tool("todo_list", {"value": {"type": "string"}},
              'Create the ToDo task list (iteration 1 by default; you may also create/expand it later if complexity emerges). See <todo_capability>.\n'
              '    Format: todo_list {"value":"Objective: <goal>\\n- [ ] task_1\\n- [ ] task_2"} (auto-numbered).', track=track),
        _tool("update_todo", {"value": {"type": "string"}},
              'Tasks are auto-numbered #1, #2, #3, etc. when saved.\n'
              '    1. Update (only after confirmed complete via <agent_history> and the effect is visible in the latest input - image or any relevant tag; one item per call)\n'
              '    2. Example: update_todo {"value": "1"}', track=track),
        _tool("scratchpad", {"value": {"type": "string"}},
              'Record a verified checkpoint or any critical fact (file path, metric, finding). Follow <scratchpad> rules.\n'
              '    1. Write `value` in Markdown - inline only (`**bold**`, backticks), never a line break.\n'
              '    2. Example: scratchpad {"value": "**Key metric:** Disney+ revenue (Q3 2025) = **$2.1B**"}', track=track),
        _tool("left_click", {"id": {"type": "integer"}, "clicks": {"type": "integer"}},
              'left mouse click. clicks=1: single click, clicks=2: double click (open files/folders), clicks=3: triple click (OCR_TEXT).\n'
              '    1. Example: left_click {"id": 8, "clicks": 2}\n'
              '    2. Sequence example: left_click {"id": 9, "clicks": 1}, left_click {"id": 10, "clicks": 1}', track=track),
        _tool("right_click", {"id": {"type": "integer"}, "clicks": {"type": "integer"}},
              'right mouse click, open context menu/options.\n'
              '    1. Example: right_click {"id": 9 , "clicks": 1}', track=track),
        _tool("input", {"id": {"type": "integer"}, "text": {"type": "string"}},
              'type into an element.\n'
              '    1. Example: input {"id": 9, "text": "hi, how are you"}', track=track),
        _tool("typewrite", {"text": {"type": "string"}},
              'type into the currently focused area when no element is available.\n'
              '    1. Does not auto-delete; use backspace if needed.\n'
              '    2. Example: typewrite {"text": "hi, how are you"}', track=track),
        _tool("scroll", {"id": {"type": "integer"}, "direction": {"type": "string"}},
              'scroll an element in a direction (`up/down/left/right`).\n'
              '    1. Example: scroll {"id": 9, "direction": "up"}', track=track),
        _tool("hotkey", {"value": {"type": "string"}},
              'OS hotkeys (max 3 keys pairs). Applies to `<Front_screen>`.\n'
              '    1. Use only for OS-level shortcut combinations (e.g., `ctrl+c`, `alt+f4`, `win+down`).\n'
              '    2. Examples:\n'
              '        1. hotkey {"value": "enter"}\n'
              '        2. hotkey {"value": "ctrl+shift+s"}', track=track),
        _tool("screenshot", {"id": {"type": "integer"}, "clicks": {"type": "integer"}},
              'Capture a UI element part as an image and copy it to the clipboard for pasting elsewhere.\n'
              '    1. It takes a screenshot without annotation, so do not trigger it to capture the magenta element number.\n'
              '    2. Image is ready to paste with ctrl+v. The clicks field is a dummy (always 1).\n'
              '    3. Example: screenshot {"id": 15, "clicks": 1}', track=track),
        _tool("done", {"value": {"type": "string"}},
              'Use `done` as a dedicated final step only, after reviewing <agent_history> to confirm every requested task is finished and doing a final visual verification from the latest image.\n'
              '    1. Step 1 (no `done`): finish/cleanup + update ToDos/scratchpad.\n'
              '    2. Step 2: output ONLY Format: done {"value": "<end-to-end summary in markdown format>"}\n'
              '    3. Write `value` in Markdown - headings, `-` bullets, `**bold**`, backticks and fenced code blocks as the summary needs them.\n'
              '    4. Never combine `done` with any other action/tool in the same step.', track=track),
    ]


MAIN_TOOLS = _main_tools(_MAIN_TRACK_PARAMS)
MAIN_TOOLS_FAST = _main_tools(_MAIN_TRACK_PARAMS_FAST)

# Per-action defaults for the main driver — guarantees route_action always
# receives every field of an action, even one the model left out.
MAIN_ACTION_DEFAULTS = {
    "left_click": {"id": 0, "clicks": 1},
    "right_click": {"id": 0, "clicks": 1},
    "screenshot": {"id": 0, "clicks": 1},
    "input": {"id": 0, "text": ""},
    "typewrite": {"text": ""},
    "scroll": {"id": 0, "direction": "down"},
    "hotkey": {"value": ""},
    "open_app": {"value": ""},
    "wait": {"value": ""},
    "web": {"value": ""},
    "shell": {"value": ""},
    "cli_agent": {"value": ""},
    "cli_await": {"value": ""},
    "todo_list": {"value": ""},
    "update_todo": {"value": ""},
    "scratchpad": {"value": ""},
    "done": {"value": ""},
}

MAIN_TOOL_NAMES = frozenset(t["name"] for t in MAIN_TOOLS)


def tool_calls_to_steps(tool_calls: list, allowed=None, defaults_map=None, track_params=None) -> tuple:
    """Coder path. Convert normalized provider tool calls into
    (actions, calls, rejects, track) where:

      actions — the SAME `[{type, ...}]` dicts route_action always consumed,
                with the tracking params STRIPPED (the controller never sees
                them)
      calls   — parallel to actions: {"id", "name", "arguments"} for each,
                so the loop can echo the model's OWN tool calls back in the
                next request and match each result to its call id (the native
                transcript that makes models behave natively). Arguments are
                kept EXACTLY as the model sent them — tracking params
                included — so the echoed transcript never contradicts the
                schema that required them.
      rejects — {"id", "name", "error"} for calls naming a tool that does not
                exist; the loop feeds these back as error tool results
      track   — the tracking params stitched from the step's calls: the first
                call carries them in full, later calls pass "".

    Defaults to the coder's tables; the main driver passes its own via
    defaults_map/track_params (coder/minion call sites stay unchanged)."""
    actions, calls, rejects = [], [], []
    track = {b: "" for b in (track_params or _TRACK_PARAMS)}
    names = allowed if allowed is not None else CODER_TOOL_NAMES
    for i, call in enumerate(tool_calls or []):
        name = str((call or {}).get("name") or "").strip()
        args = (call or {}).get("arguments")
        if not isinstance(args, dict):
            args = {}
        call_id = str((call or {}).get("id") or "") or f"call_{i}"
        for b in track:
            v = str(args.get(b) or "").strip()
            if v and not track[b]:
                track[b] = v
        defaults = (defaults_map or _ACTION_DEFAULTS).get(name)
        if defaults is None or name not in names:
            rejects.append({
                "id": call_id,
                "name": name or "(unnamed)",
                "arguments": args,
                "error": f"No tool named '{name or '(unnamed)'}' exists. "
                         f"Available tools: {', '.join(sorted(names))}. "
                         f"Call one of those instead.",
            })
            continue
        # EMPTY arguments is a schema violation, not a set of omitted optional
        # fields — every param on every tool is `required`. Letting the defaults
        # below fill it would silently promote the model's malformed turn into a
        # REAL action: a `left_click` with no arguments becomes id 0 / clicks 1
        # and clicks whatever element 0 happens to be; an `input` with none
        # becomes id 0 / value "", which focuses element 0 and clears it.
        # Observed in the wild on the web driver (gpt-5.6-luna emitted
        # `"arguments": {}` on one step, then a well-formed call on the next).
        # Reject it exactly the way an unknown tool name is rejected, so the
        # model gets an error keyed to its own call id and re-issues the step
        # instead of acting on an element it never chose.
        #
        # Narrow on purpose: ONLY a fully empty argument object. A partial call
        # still gets default-filled as before, and id 0 remains a perfectly
        # valid element — the problem is never the value 0 itself, only a 0 that
        # arrived from a default the model never sent.
        if defaults and not args:
            rejects.append({
                "id": call_id,
                "name": name,
                "arguments": args,
                "error": f"'{name}' was called with no arguments at all. Every "
                         f"field is required: "
                         f"{', '.join(sorted(set(defaults) | set(track)))}. "
                         f"Re-issue the call with all of them filled in.",
            })
            continue
        action = {"type": name}
        for key, default in defaults.items():
            action[key] = _coerce(args[key], default) if key in args else default
        actions.append(action)
        calls.append({"id": call_id, "name": name, "arguments": args})
    return actions, calls, rejects, track


# Coder / minion emergency fallbacks. The PRIMARY model is always the one the
# user picked (UI drop-up, cli.py's MODEL, --model) — these cover a SINGLE
# failing call and are then dropped. Ordered: the first candidate that isn't
# the user's own model wins, so the fallback is never the model that just
# failed. Every name below exists in the matching provider view.py
# MODEL_MAPPINGS (the old hardcoded map's gpt-5.2 / gpt-5.1 /
# claude-sonnet-4.5 did not, which is why those fallbacks could never
# succeed).
_CLI_FALLBACK_CANDIDATES = {
    # Single entry: groq now registers one model, so a user already on it has
    # nothing to fall back TO and correctly resolves to None. The tuple still
    # earns its place for a hand-typed groq model, which falls back here.
    "groq":       ("qwen3.6-27b",),
    "openai":     ("gpt-5.6-luna", "gpt-5.6-terra"),
    "openrouter": ("gemini-3.6-flash", "gpt-5.6-luna"),
    "anthropic":  ("claude-haiku-4.5", "claude-sonnet-5"),
    "google":     ("gemini-3.6-flash", "gemini-3.1-pro"),
    "perplexity": ("gemini-3.6-flash", "gpt-5.6-luna"),
    "together":   ("minimax-m3", "inkling"),
}


def _pick_cli_fallback(provider: str, model: str):
    """First registered secondary for `provider` that isn't `model` itself."""
    candidates = _CLI_FALLBACK_CANDIDATES.get(provider, ())
    # Vertex and AI-Studio are different clients, decided at provider
    # construction — a vertex model may only fall back to another vertex one.
    if provider == "google" and str(model).endswith("-vertex"):
        candidates = tuple(f"{c}-vertex" for c in candidates)
    return next((c for c in candidates if c != model), None)


class LLMManager:
    """Manager to route requests to the correct LLM provider"""

    def __init__(self, provider: str, model: str, api_key: str = None, cli_agent: bool = False, mode: str = "main", speed: str = "quality"):
        # Make HTTPS work even behind antivirus/corporate TLS interception
        # (otherwise every provider call fails SSL and the agent does nothing).
        _ensure_tls_works()
        self.provider = provider.lower()
        self.model_short_name = model
        self.runtime_api_key = api_key  # Runtime key from frontend (priority)
        # Coder/minion flag. NOT a "native tools" switch — every mode but
        # "text" is native now. It stays False for the MAIN DRIVER because
        # each provider gates its screenshot splice on `not self.cli_agent`,
        # so flipping it would silently kill the driver's vision.
        self.cli_agent = cli_agent
        self.mode = mode  # "main" | "minion" | "text" — picks the tool registry
        self.speed = speed  # "quality" | "fast" — fast trims the main-agent tracking params

        # Coder / minion (cli_agent=True) run the SAME model as everyone else —
        # whatever the UI, cli.py or --model handed in. The only per-provider
        # hardcoding left is the emergency fallback, used for one call (see
        # send_request).
        self._cli_fallback_model = _pick_cli_fallback(self.provider, model) if cli_agent else None

        # Get model info based on provider
        model_info = self._resolve_model_info(model)

        self.model = model_info["api_name"]
        self.has_vision = model_info["vision"]
        self.display_name = model_info["display_name"]
        self.model_info = model_info  # Full model info, forwarded to the provider
        self._primary_model_info = model_info  # restored after every fallback call

        # NATIVE TOOL CALLING is the only path: the main driver, the coder and
        # the minion all get tool definitions as their output contract — no
        # JSON envelope, no response_format, nothing to parse. The single
        # exception is mode="text" (the memory-compression handoff), which
        # wants plain prose and so gets neither tools nor a schema.
        self.native_tools = mode != "text"

        # Most recent send_request's normalized token usage (input/output/total).
        # Captured as a side effect so callers (e.g. the memory bar) can read it
        # without changing send_request's return shape.
        self.last_usage = {}
        # Most recent successful API round-trip in seconds (ping → response),
        # measured fresh per attempt — read this in code for the exact number.
        self.last_call_seconds = 0.0
        self.provider_instance = self._initialize_provider()

    def _resolve_model_info(self, short_name: str) -> dict:
        """Provider-appropriate get_model_info; unknown providers pass through.

        Every provider view's get_model_info already falls through to a
        passthrough dict for unregistered names, so any model string the user
        types is usable — nothing here validates against an allowlist.
        """
        getter = {
            "openrouter": get_openrouter_model_info,
            "groq": get_groq_model_info,
            "openai": get_openai_model_info,
            "anthropic": get_anthropic_model_info,
            "google": get_google_model_info,
            "perplexity": get_perplexity_model_info,
            "together": get_together_model_info,
        }.get(self.provider)
        if getter is None:
            return {"api_name": short_name, "vision": True, "display_name": short_name}
        return getter(short_name)

    def _apply_model_info(self, info: dict) -> None:
        """Point this manager at a model.

        Also re-points provider_instance.model_info, which openrouter/groq/
        perplexity cache at construction — otherwise a swapped model keeps
        serving the other model's metadata.
        """
        self.model = info["api_name"]
        self.has_vision = info["vision"]
        self.display_name = info["display_name"]
        self.model_info = info
        provider_instance = getattr(self, "provider_instance", None)
        if provider_instance is not None and hasattr(provider_instance, "model_info"):
            provider_instance.model_info = info

    def _initialize_provider(self):
        """Initialize the appropriate provider based on selection"""
        # Hand each provider its dialect's tool definitions — the registry is
        # picked by mode (minion gets the read-only subset; the coder gets its
        # own; the main driver gets its action tools, thinking-less in fast
        # mode). mode="text" is the only caller that gets no tools at all.
        native = self.native_tools
        if self.mode == "minion":
            registry = MINION_TOOLS
        elif self.cli_agent:
            registry = CODER_TOOLS
        else:
            registry = MAIN_TOOLS_FAST if self.speed == "fast" else MAIN_TOOLS
        if self.provider == "openrouter":
            # Priority: Runtime key > .env fallback
            api_key = self.runtime_api_key or os.getenv('OPENROUTER_API_KEY')
            if not api_key:
                raise ValueError("OpenRouter API key not provided and not found in .env file")
            return OpenRouterProvider(api_key, self.cli_agent, self.model_info,
                                      tools=coder_tools_openai(registry) if native else None)
        elif self.provider == "groq":
            # Priority: Runtime key > .env fallback
            api_key = self.runtime_api_key or os.getenv('GROQ_API_KEY')
            if not api_key:
                raise ValueError("Groq API key not provided and not found in .env file")
            return GroqProvider(api_key, self.cli_agent, self.model_info,
                                tools=coder_tools_openai(registry) if native else None)
        elif self.provider == "openai":
            # Priority: Runtime key > .env fallback
            api_key = self.runtime_api_key or os.getenv('OPENAI_API_KEY')
            if not api_key:
                raise ValueError("OpenAI API key not provided and not found in .env file")
            return OpenAIProvider(api_key, self.cli_agent,
                                  tools=coder_tools_openai(registry) if native else None)
        elif self.provider == "anthropic":
            # Priority: Runtime key > .env fallback
            api_key = self.runtime_api_key or os.getenv('ANTHROPIC_API_KEY')
            if not api_key:
                raise ValueError("Anthropic API key not provided and not found in .env file")
            return AnthropicProvider(api_key, self.cli_agent,
                                     tools=coder_tools_anthropic(registry) if native else None)
        elif self.provider == "google":
            # Check if this is a Vertex model
            from .google.view import get_model_info as get_google_info
            model_meta = get_google_info(self.model_short_name)
            is_vertex = model_meta.get("vertex", False)
            
            if is_vertex:
                # Read Vertex config from api_key.txt
                vertex_project_id = None
                vertex_location = None
                try:
                    # autouse_data/api_key/api_key.txt — outside the install
                    # folder, and the SAME file the Settings panel writes.
                    from Auto_Use import api_key_file
                    key_file = api_key_file()
                    if key_file.exists():
                        with open(key_file, 'r', encoding='utf-8') as f:
                            for line in f:
                                line = line.strip()
                                if line.startswith('VERTEX_PROJECT_ID='):
                                    vertex_project_id = line.partition('=')[2]
                                elif line.startswith('VERTEX_LOCATION='):
                                    vertex_location = line.partition('=')[2]
                except Exception:
                    pass
                return GoogleProvider(
                    api_key=None, cli_agent=self.cli_agent,
                    model=self.model_short_name,
                    vertex_project_id=vertex_project_id, vertex_location=vertex_location,
                    tools=coder_tools_gemini(registry) if native else None
                )
            else:
                # AI Studio — needs API key
                api_key = self.runtime_api_key or os.getenv('GOOGLE_API_KEY')
                if not api_key:
                    raise ValueError("Google API key not provided and not found in .env file")
                return GoogleProvider(api_key, self.cli_agent, model=self.model_short_name,
                                      tools=coder_tools_gemini(registry) if native else None)
        elif self.provider == "perplexity":
            api_key = self.runtime_api_key or os.getenv('PERPLEXITY_API_KEY')
            if not api_key:
                raise ValueError("Perplexity API key not provided and not found in .env file")
            return PerplexityProvider(api_key, self.cli_agent, self.model_info,
                                      tools=coder_tools_perplexity(registry) if native else None)
        elif self.provider == "together":
            # Priority: Runtime key > .env fallback
            api_key = self.runtime_api_key or os.getenv('TOGETHER_API_KEY')
            if not api_key:
                raise ValueError("Together API key not provided and not found in .env file")
            return TogetherProvider(api_key, self.cli_agent, self.model_info,
                                    tools=coder_tools_openai(registry) if native else None)
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")
    
    def _normalize_usage(self, u):
        """Normalize a provider usage dict to {input_tokens, output_tokens,
        total_tokens, context_tokens}, tolerating both key styles (Anthropic-style
        input/output and OpenAI-style prompt/completion). Empty/missing -> zeros.

        context_tokens is the TRUE size of the prompt actually sent this turn — the
        memory-bar number. A cached token still occupies the context window, so we
        add the cache classes back: input_tokens + cache_read + cache_creation.
        This is exact for every provider:
          - Anthropic: input_tokens EXCLUDES cache, so the cache fields are added.
          - OpenAI/Google/Perplexity/OpenRouter/Groq: prompt_tokens already INCLUDES
            cached tokens and the Anthropic cache keys are absent (0), so this
            collapses to the full prompt count — no double-count.
        """
        u = u or {}
        inp = int(u.get("input_tokens", u.get("prompt_tokens", 0)) or 0)
        out = int(u.get("output_tokens", u.get("completion_tokens", 0)) or 0)
        tot = int(u.get("total_tokens", 0) or 0) or (inp + out)
        cache_read = int(u.get("cache_read_input_tokens", 0) or 0)
        cache_create = int(u.get("cache_creation_input_tokens", 0) or 0)
        context_tokens = inp + cache_read + cache_create
        return {
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": tot,
            "context_tokens": context_tokens,
        }

    def _attempt(self, messages: list, annotated_screenshot_base64: Optional[str] = None):
        """Three idempotent tries against the model currently loaded. Raises the
        last error if all three fail.

        Return shape: plain content string normally; in native-tools mode
        (coder) a dict {"text": str, "tool_calls": [{"name", "arguments"}],
        "provider_meta": dict} — the provider already normalized the calls."""
        last_error = None
        for attempt in range(3):
            # Providers may mutate messages in-place (e.g. wrapping the last user
            # message into multimodal content blocks); deep-copy per attempt so
            # those mutations cannot compound across retries.
            attempt_messages = copy.deepcopy(messages)
            if self.provider not in _META_PROVIDERS:
                for m in attempt_messages:
                    if isinstance(m, dict):
                        m.pop(_META_KEY, None)
            try:
                # Pure API round-trip: clock starts the moment the provider is
                # pinged, stops the moment its response is back. Fresh from 0
                # on EVERY attempt; the deep-copy above is outside the clock.
                _t0 = time.perf_counter()
                response = self.provider_instance.send_request(
                    attempt_messages, self.model, annotated_screenshot_base64
                )
                self.last_call_seconds = time.perf_counter() - _t0
                self.last_usage = self._normalize_usage(response.get("usage"))
                if sys.stdout.isatty():
                    # \r first: overwrite any spinner residue so the line is
                    # clean. TTY-only — never leak into the UI subprocess pipe.
                    # Token counts ride on the same line: a slow call with a
                    # small input/output is NOT a generation cost — it's cold
                    # prefill, provider-side reasoning, or backend routing.
                    _raw_usage = response.get("usage") or {}
                    _cached = int(
                        _raw_usage.get("cache_read_input_tokens", 0)
                        or ((_raw_usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
                        or 0
                    )
                    print(
                        f"\r⏱ LLM call: {self.last_call_seconds:.2f}s ({self.display_name}) "
                        f"| in {self.last_usage['input_tokens']} "
                        f"(cached {_cached}) "
                        f"| out {self.last_usage['output_tokens']}"
                    )
                message = response['choices'][0]['message']
                if self.native_tools:
                    return {
                        "text": message.get('content') or "",
                        "tool_calls": message.get('tool_calls') or [],
                        # Provider metadata for THIS turn, persisted with the
                        # step and handed back to the same provider next
                        # request (see _META_KEY above). {} when the provider
                        # emits none, which is the pre-existing behavior.
                        _META_KEY: message.get(_META_KEY) or {},
                    }
                return message['content']
            except Exception as e:
                last_error = e
                if attempt < 2:
                    print(f"⚠️ {self.display_name} request failed (attempt {attempt + 1}/3): {e}")
                    print("   Retrying in 1 second with a fresh message copy...")
                    time.sleep(1)
                    continue
                print(f"❌ {self.display_name} request failed after 3 attempts: {e}")
        raise last_error

    def send_request(self, messages: list, annotated_screenshot_base64: Optional[str] = None):
        """Send request to the selected provider with idempotent retries.

        Coder/minion only: if the user's model fails all 3 attempts, ONE call is
        served by the per-provider fallback (3 attempts of its own). Win or lose,
        the manager is put back on the user's model afterwards, so the next
        iteration starts on their model again. If the fallback also fails its 3
        attempts the error propagates and the agent stops.

        The main agent has no fallback (cli_agent=False): 3 attempts, then raise.
        """
        try:
            return self._attempt(messages, annotated_screenshot_base64)
        except Exception:
            if not (self.cli_agent and self._cli_fallback_model):
                raise

        fallback_info = self._resolve_model_info(self._cli_fallback_model)
        print(f"⚠️ {self.display_name} failed 3 attempts — this step only, "
              f"falling back to {fallback_info['display_name']}...")
        self._apply_model_info(fallback_info)
        try:
            result = self._attempt(messages, annotated_screenshot_base64)
        except Exception:
            print(f"❌ Fallback {self.display_name} also failed 3 attempts — stopping.")
            raise
        finally:
            # Always revert: the fallback covers this call, not the whole run.
            self._apply_model_info(self._primary_model_info)
        print(f"↩️ Back on {self.display_name} for the next step.")
        return result

    def get_model_name(self) -> str:
        """Get the current model short name (preserves vertex suffix for downstream routing)"""
        return self.model_short_name
    
    def get_provider_name(self) -> str:
        """Get the current provider name"""
        return self.provider