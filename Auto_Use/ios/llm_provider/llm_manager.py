# Copyright 2026 Cursortouch — Auto-Use

import copy
import os
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

# Every agent here speaks NATIVE TOOL CALLING - the tool registry below IS
# the output contract. There is no JSON-envelope response schema anymore:
# CLI_AGENT_SCHEMA, MINION_SCHEMA, AGENT_OUTPUT_SCHEMA and FAST_AGENT_SCHEMA
# are all gone, and no provider is handed a response_format. The only
# schema-less, tool-less path left is mode="text" (the memory-compression
# handoff), which wants plain prose.


# The native transcript carries an optional per-turn `provider_meta` on
# assistant messages - the provider's OWN metadata for that turn (Gemini 3
# thought signatures, OpenRouter reasoning blocks), needed to echo the turn
# back exactly as the model produced it. Only these providers translate it;
# for everyone else the key is stripped before the request is built, because
# openai/groq/perplexity/together forward the message dicts to their API
# verbatim and an unknown key there is a 400.
_META_KEY = "provider_meta"
_META_PROVIDERS = ("openrouter", "google")


# ---------------------------------------------------------------------------
# NATIVE TOOLS - the main driver calls these tools natively; no JSON envelope.
# The provider returns structured calls, tool_calls_to_steps() converts them
# into the same `[{type, ...}]` action dicts route_action already consumes -
# the controller is untouched.
#
# Each tool carries its action fields plus the tracking params ahead of them
# (see _MAIN_TRACK_PARAMS). tool_choice is forced (required / {"type": "any"} /
# mode="ANY"), so a text-only step is unrepresentable; the text channel remains
# for free prose only.
# ---------------------------------------------------------------------------

# The MAIN DRIVER's tracking params - THREE, matching system_prompt.md's block
# contract ({thinking, memory, next_goal}; the calls themselves are the action
# block). The first call of a step carries them, later calls pass "". The skip
# token stays the agent's own "not required" - it is what <thinking> and the
# bridge notes agent_conversation writes already use.
_MAIN_TRACK_PARAMS = {
    "thinking": 'Follow the <thinking> rules - the five labeled stages "OBSERVE: ... VERIFY: ... PROGRESS: ... PLAN: ... PREDICT: ..." (FULL) at think triggers, a short freeform paragraph (RECOVERY) on a local failure, or exactly "not required" when the SKIP TEST passes. Never empty. Fill on the FIRST tool call of the step; pass "" on every additional call in the same step.',
    "memory": 'Follow the <memory> rules - line 1 is the verdict on the previous step\'s guard, judged on the CURRENT screenshot: "S<n> ok" or "S<n> fail: <short why>" ("S1 start" on the first step). Then key context (app/screen state, tool name + result) and, for any step touching UI, the "Targets: id N (element_name/type/value)" line resolved from the CURRENT element_tree. 2-4 concise lines. Fill on the FIRST tool call of the step; pass "" on every additional call in the same step.',
    "next_goal": 'Follow the <next_goal> rules - "Doing: <this step> (ToDo: <task>). If <visible change>, then Next: <action on named target | think: <decision>>." Name successor targets by NAME/ROLE only, NEVER by [id] (ids are re-assigned every scan). Fill on the FIRST tool call of the step; pass "" on every additional call in the same step.',
}

# Fast mode carries TWO tracking params: `memory` and `next_goal` - matching
# fast_system_prompt.md's block contract ({memory, next_goal, action}).
# Fast mode carries ONE tracking param: `memory`. `thinking` and `next_goal`
# are dropped ENTIRELY rather than left required-but-empty - a required field
# is an invitation to fill it, and fast mode exists to stop the model spending
# tokens on planning prose. This MIRRORS fast_system_prompt.md, which says
# "Every tool carries `memory` as its single tracking parameter" and folds the
# forward plan (Now / Plan / Then) and the "Expect:" guard INTO memory - it has
# no <next_goal> section at all, so a `next_goal` param here would be a
# required field the prompt never documents.
_MAIN_TRACK_PARAMS_FAST = {
    "memory": 'Follow the <memory> rules - if the last action FAILED verification, open with one short clause naming the failure (skip it entirely when it passed). Then the context that matters next: current app/screen state, key ids used with their (element_name/type/value), and any tool name + purpose + important result. Then the forward plan: "Now: <immediate step> (ToDo: <task_name>). Plan: <next 2-3 steps>. Then: <very next step>." END with the predicted visible change of THIS step\'s action prefixed "Expect:", so the next step can verify against the new screenshot. 3-5 concise lines. Fill on the FIRST tool call of the step; pass "" on every additional call in the same step.',
}


def _tool(name: str, params: dict, description: str = "", track: dict = None) -> dict:
    """Build one canonical tool def - name + parameters + description. The
    REGISTRY is the single source of tool documentation: each tool's rules
    live in its `description`, which flows to every provider dialect. The
    system prompt keeps only the step protocol and operating procedure. Tool
    defs sit in the cached prefix, so this bills once. All params are required,
    so the controller always sees every field of an action - the tracking
    params (`track`) ride ahead of the action fields."""
    props = {b: {"type": "string", "description": d} for b, d in (track or _MAIN_TRACK_PARAMS).items()}
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


def _main_tools(track: dict) -> list:
    """The MAIN DRIVER's registry - one tool per action type. route_action and
    the frontend's tool-flow map key on these names and fields, so they must
    never drift.

    Descriptions are the system prompt's OWN tool text, copied verbatim from
    <tool_capability> / <os_interaction> / <task_completion> in
    agent/main_driver/system_prompt.md - the prompt stays the source of truth
    (it is user-managed and never edited from here); this just puts the same
    words where a native tool call can read them. Built twice: quality mode
    with three tracking params, fast mode with memory + next_goal. Both modes
    share this one description text (the quality prompt's, which is pure
    ASCII).

    iOS-specific vs the macOS registry: `scroll` and `vault` carry `value`
    (not macOS's `direction`), `click` has no `clicks` field, and `vault` /
    `video_player` exist here only."""
    return [
        # -- <tool_capability> ------------------------------------------------
        _tool("open_app", {"value": {"type": "string"}},
              'Launch an installed application directly by name - faster than searching on the device. Use the special name "home" to return to the home screen.\n'
              '    1. Requirement: Typically call wait 2-3 seconds after this tool to allow loading.\n'
              '    2. Success means the app is confirmed in the foreground (if the result says verified: false, confirm on the next screenshot). An unknown or ambiguous name returns an error naming the closest installed apps - re-issue with that exact name.\n'
              '    3. Format: open_app {"value": "app name"}\n'
              '    4. Examples:\n'
              '        1. open_app {"value": "disney+"}\n'
              '        2. open_app {"value": "home"}', track=track),
        _tool("wait", {"value": {"type": "string"}},
              'Pause before the next screen scan to allow UI loading. Never exceed 3 seconds at a time.\n'
              '    1. Format: wait {"value": "time in seconds"}\n'
              '    2. Examples:\n'
              '        1. wait {"value": "3"}\n'
              '        2. wait {"value": "2"}', track=track),
        _tool("web", {"value": {"type": "string"}},
              'Delegate to a specialized AI that fetches real-time information and provides data at runtime. Use it for speed instead of browsing manually on the phone.\n'
              '    1. Format: web {"value": "search query"}\n'
              '    2. Examples:\n'
              '        1. web {"value": "financial results of Nvidia Q4 2025"}\n'
              '        2. web {"value": "latest Netflix app version on the App Store"}', track=track),
        _tool("shell", {"value": {"type": "string"}},
              'Run a shell/zsh command on the host Mac where this agent is running - not on the iPhone. Use it to check information or perform actions on the host OS, then continue the task on the phone (e.g. photo sharing: read the photo names and details on the Mac first, then share those same photos from the phone). Accepts every shell command, including AppleScript via osascript.\n'
              '    1. Format: shell {"value": "command"}\n'
              '    2. Examples:\n'
              '        1. shell {"value": "ls ~/Pictures/holiday | head -5"}\n'
              '        2. shell {"value": "osascript -e \'tell application \\"Finder\\" to get name of every file of desktop\'"}', track=track),
        _tool("todo_list", {"value": {"type": "string"}},
              'Create the ToDo task list (iteration 1 by default; you may also create/expand it later if complexity emerges). See <todo_capability>.', track=track),
        _tool("update_todo", {"value": {"type": "string"}},
              'Tasks are auto-numbered #1, #2, #3, etc. when saved.\n'
              '    1. Update a task only after it is confirmed complete via <agent_history> and the effect is visible in the latest input (image or any relevant tag); one item per call.\n'
              '    2. Example: update_todo {"value": "1"}', track=track),
        _tool("vault", {"id": {"type": "integer"}, "value": {"type": "string"}},
              'Fill a secure credential into an element straight from the vault - three-part action like scroll: the element [id] and the credential kind (value: username/password/phone_number). The credential is typed automatically; secrets never appear in your context.\n'
              '    1. Critical: vault must be the ONLY action in the list, and it fills one element per step. This holds on EVERY step, including steps where thinking is `not required`.\n'
              '    2. Fill every required credential field (repeat vault across steps) before planning the next move.\n'
              '    3. Format: vault {"id": <element_id>, "value": "<credential_kind>"}\n'
              '    4. Examples:\n'
              '        1. vault {"id": 3, "value": "username"}\n'
              '        2. vault {"id": 4, "value": "password"}', track=track),
        _tool("video_player", {"value": {"type": "string"}},
              'Track and control full-screen video playback through the control center (works despite DRM screenshot restrictions). Commands: close, streaming (check whether content is playing), pause, play.\n'
              '    1. Format: video_player {"value": "one of: close/streaming/pause/play"}\n'
              '    2. Examples:\n'
              '        1. video_player {"value": "streaming"}\n'
              '        2. video_player {"value": "pause"}', track=track),
        _tool("scratchpad", {"value": {"type": "string"}},
              'Record a verified checkpoint or any critical fact (file path, metric, finding). Follow <scratchpad> rules.\n'
              '    1. Write `value` in Markdown - inline only (`**bold**`, backticks), never a line break.\n'
              '    2. Example: scratchpad {"value": "**Key metric:** Disney+ revenue (Q3 2025) = **$2.1B**"}', track=track),

        # -- <tool_capability> #10 + <task_completion> -------------------------
        _tool("done", {"value": {"type": "string"}},
              'End the task with an end-to-end summary of what was achieved. Dedicated final step - never combine with any other action; do cleanup and ToDo/scratchpad updates in the step before.\n'
              '    1. Write `value` in Markdown - headings, `-` bullets, `**bold**`, backticks and fenced code blocks as the summary needs them.\n'
              '    2. Examples:\n'
              '        1. done {"value": "**Netflix updated** to the latest version - login verified and version noted."}\n'
              '        2. done {"value": "**Message sent to John** - delivery confirmed on screen."}\n'
              '    3. Only start completion after reviewing <agent_history> to confirm every requested task is finished.\n'
              '    4. Then do a final verification from the latest input (double-check the last steps match the request; if playback is DRM-blocked, verify via a video_player check).\n'
              '    5. Use `done` as a dedicated final step only:\n'
              '        1. Step 1 (no `done`): finish/cleanup + update ToDos/scratchpad.\n'
              '        2. Step 2: output ONLY Format: done {"value": "<end-to-end summary in markdown format>"}\n'
              '    6. Never combine `done` with any other action/tool in the same step.', track=track),

        # -- <os_interaction> -------------------------------------------------
        _tool("click", {"id": {"type": "integer"}},
              'Tap the centre of an element by its [id].\n'
              '    1. Examples:\n'
              '        1. click {"id": 4}\n'
              '        2. click {"id": 23}', track=track),
        _tool("input", {"id": {"type": "integer"}, "value": {"type": "string"}},
              'Type into an element by its [id]. Existing text in the field is auto-deleted before typing.\n'
              '    1. Examples:\n'
              '        1. input {"id": 3, "value": "Hi, how are you"}\n'
              '        2. input {"id": 4, "value": "conjuring"}', track=track),
        _tool("scroll", {"id": {"type": "integer"}, "value": {"type": "string"}},
              'Swipe within an element\'s bounds - three-part action: the element [id] and the direction (value: up/down/left/right).\n'
              '    1. To reveal content below the visible area, scroll "up"; to reveal content above, scroll "down".\n'
              '    2. To reveal content on the right, scroll "left"; to reveal content on the left, scroll "right".\n'
              '    3. Examples:\n'
              '        1. scroll {"id": 3, "value": "up"}\n'
              '        2. scroll {"id": 7, "value": "left"}', track=track),
    ]


MAIN_TOOLS = _main_tools(_MAIN_TRACK_PARAMS)
MAIN_TOOLS_FAST = _main_tools(_MAIN_TRACK_PARAMS_FAST)

# Per-action defaults for the main driver - guarantees route_action receives
# every field of an action, even if the model omits an optional-feeling one.
# Doubles as the field whitelist: the tracking params are absent here, so they
# never reach the controller.
MAIN_ACTION_DEFAULTS = {
    "click": {"id": 0},
    "input": {"id": 0, "value": ""},
    "scroll": {"id": 0, "value": "up"},
    "vault": {"id": 0, "value": ""},
    "open_app": {"value": ""},
    "wait": {"value": ""},
    "web": {"value": ""},
    "shell": {"value": ""},
    "todo_list": {"value": ""},
    "update_todo": {"value": ""},
    "video_player": {"value": ""},
    "scratchpad": {"value": ""},
    "done": {"value": ""},
}

# Names the driver may call - anything else is answered with an error tool
# result instead of being silently dropped, so the model can correct itself
# on the very next turn.
MAIN_TOOL_NAMES = frozenset(t["name"] for t in MAIN_TOOLS)


def _with_description(tool: dict, source: dict) -> dict:
    """Carry the registry description into a dialect dict when present
    (_tool() only sets the key when non-empty)."""
    if source.get("description"):
        tool["description"] = source["description"]
    return tool


def native_tools_openai(registry: list = None) -> list:
    """OpenAI/OpenRouter/Groq chat-completions function format."""
    return [{"type": "function", "function": t} for t in (registry or MAIN_TOOLS)]


def native_tools_anthropic(registry: list = None) -> list:
    """Anthropic Messages API tools format."""
    return [_with_description({"name": t["name"], "input_schema": t["parameters"]}, t)
            for t in (registry or MAIN_TOOLS)]


def native_tools_gemini(registry: list = None) -> list:
    """Gemini function declarations (dicts accepted by google-genai)."""
    return [_with_description({"name": t["name"], "parameters": t["parameters"]}, t)
            for t in (registry or MAIN_TOOLS)]


def native_tools_perplexity(registry: list = None) -> list:
    """Perplexity agent API (Responses-style flat function tools)."""
    return [_with_description({"type": "function", "name": t["name"],
                               "parameters": t["parameters"]}, t)
            for t in (registry or MAIN_TOOLS)]


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


def tool_calls_to_steps(tool_calls: list, allowed=None, defaults_map=None, track_params=None) -> tuple:
    """Convert normalized provider tool calls into
    (actions, calls, rejects, track) where:

      actions - the SAME `[{type, ...}]` dicts route_action always consumed,
                with the tracking params STRIPPED (the controller never sees
                them)
      calls   - parallel to actions: {"id", "name", "arguments"} for each,
                so the loop can echo the model's OWN tool calls back in the
                next request and match each result to its call id (the native
                transcript that makes models behave natively). Arguments are
                kept EXACTLY as the model sent them - tracking params
                included - so the echoed transcript never contradicts the
                schema that required them.
      rejects - {"id", "name", "error"} for calls naming a tool that does not
                exist; the loop feeds these back as error tool results
      track   - the tracking params stitched from the step's calls: the first
                call carries them in full, later calls pass "".
    """
    actions, calls, rejects = [], [], []
    track = {b: "" for b in (track_params or _MAIN_TRACK_PARAMS)}
    names = allowed if allowed is not None else MAIN_TOOL_NAMES
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
        defaults = (defaults_map or MAIN_ACTION_DEFAULTS).get(name)
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


class LLMManager:
    """Manager to route requests to the correct LLM provider"""

    def __init__(self, provider: str, model: str, api_key: str = None, cli_agent: bool = False, mode: str = "main", speed: str = "quality"):
        self.provider = provider.lower()
        self.model_short_name = model
        self.runtime_api_key = api_key  # Runtime key from frontend (priority)
        # CLI-agent flag. NOT a "native tools" switch — every mode but "text"
        # is native now. It stays False for the MAIN DRIVER because every
        # provider gates its screenshot splice on `not self.cli_agent`, so
        # flipping it would silently kill the driver's vision.
        self.cli_agent = cli_agent
        # "main" | "text". iOS has ONE registry (MAIN_TOOLS, picked between
        # quality/fast by `speed`), so mode does not choose a registry the way
        # it does on macOS/Windows - it only decides whether tools are sent at
        # all, via native_tools below.
        self.mode = mode
        self.speed = speed  # "quality" | "fast" — fast trims the main-agent tracking params

        # CLI agent gets its own hardcoded model per provider (independent from main agent)
        if cli_agent:
            is_vertex = model.endswith("-vertex")
            # Every name in both maps must exist in the matching provider
            # view.py MODEL_MAPPINGS. A name that does not falls through
            # get_model_info's passthrough and is sent VERBATIM as the
            # api_name — which silently drops the provider prefix the real ID
            # needs ("gpt-oss-120b" instead of "openai/gpt-oss-120b") or the
            # dash spelling Anthropic requires ("claude-sonnet-4.6" instead of
            # "claude-sonnet-4-6"). Both 404 on the one call they exist to
            # rescue, so a stale entry here fails exactly when it matters.
            _CLI_MODEL_MAP = {
                "groq": "qwen3.6-27b",                # only registered groq model
                "openai": "gpt-5.6-terra",            # balanced tier
                "openrouter": "gemini-3.1-pro",
                "anthropic": "claude-sonnet-5",
                "google": "gemini-3.1-pro-vertex" if is_vertex else "gemini-3.1-pro",
                "perplexity": "gemini-3.1-pro",
                "together": "minimax-m3",
            }
            _CLI_FALLBACK_MAP = {
                # groq registers ONE model, which is already the primary above
                # — there is nothing to fall back to, and None is what the
                # guard at the retry site expects.
                "groq": None,
                "openai": "gpt-5.6-luna",             # cheap tier
                "openrouter": "gemini-3.6-flash",
                "anthropic": "claude-haiku-4.5",      # fast, and takes the
                                                      # sampling knobs Sonnet 5
                                                      # no longer accepts
                "google": "gemini-3.6-flash-vertex" if is_vertex else "gemini-3.6-flash",
                "perplexity": "gemini-3.6-flash",
                "together": "inkling",
            }
            self._cli_fallback_model = _CLI_FALLBACK_MAP.get(self.provider)
            model = _CLI_MODEL_MAP.get(self.provider, model)
        
        # Get model info based on provider
        if self.provider == "openrouter":
            model_info = get_openrouter_model_info(model)
        elif self.provider == "groq":
            model_info = get_groq_model_info(model)
        elif self.provider == "openai":
            model_info = get_openai_model_info(model)
        elif self.provider == "anthropic":
            model_info = get_anthropic_model_info(model)
        elif self.provider == "google":
            model_info = get_google_model_info(model)
        elif self.provider == "perplexity":
            model_info = get_perplexity_model_info(model)
        elif self.provider == "together":
            model_info = get_together_model_info(model)
        else:
            model_info = {"api_name": model, "vision": True, "display_name": model}
        
        self.model = model_info["api_name"]
        self.has_vision = model_info["vision"]
        self.display_name = model_info["display_name"]
        self.model_info = model_info  # Full model info, forwarded to the provider

        # NATIVE TOOL CALLING is the only path: the main driver's tool
        # definitions ARE its output contract — no JSON envelope, no
        # response_format, nothing to parse. The single exception is
        # mode="text" (the memory-compression handoff), which wants plain
        # prose and so gets neither tools nor a schema.
        self.native_tools = mode != "text"
        
        # Most recent send_request's normalized token usage (input/output/total).
        # Captured as a side effect so callers (e.g. the memory bar) can read it
        # without changing send_request's return shape.
        self.last_usage = {}
        self.provider_instance = self._initialize_provider()
        
    def _initialize_provider(self):
        """Initialize the appropriate provider based on selection"""
        # Hand each provider its dialect's tool definitions - the main driver's
        # action tools, thinking-less in fast mode. mode="text" is the only
        # caller that gets no tools at all.
        native = self.native_tools
        registry = MAIN_TOOLS_FAST if self.speed == "fast" else MAIN_TOOLS
        if self.provider == "openrouter":
            # Priority: Runtime key > .env fallback
            api_key = self.runtime_api_key or os.getenv('OPENROUTER_API_KEY')
            if not api_key:
                raise ValueError("OpenRouter API key not provided and not found in .env file")
            return OpenRouterProvider(api_key, self.cli_agent, self.model_info,
                                      tools=native_tools_openai(registry) if native else None)
        elif self.provider == "groq":
            # Priority: Runtime key > .env fallback
            api_key = self.runtime_api_key or os.getenv('GROQ_API_KEY')
            if not api_key:
                raise ValueError("Groq API key not provided and not found in .env file")
            return GroqProvider(api_key, self.cli_agent, self.model_info,
                                tools=native_tools_openai(registry) if native else None)
        elif self.provider == "openai":
            # Priority: Runtime key > .env fallback
            api_key = self.runtime_api_key or os.getenv('OPENAI_API_KEY')
            if not api_key:
                raise ValueError("OpenAI API key not provided and not found in .env file")
            return OpenAIProvider(api_key, self.cli_agent,
                                  tools=native_tools_openai(registry) if native else None)
        elif self.provider == "anthropic":
            # Priority: Runtime key > .env fallback
            api_key = self.runtime_api_key or os.getenv('ANTHROPIC_API_KEY')
            if not api_key:
                raise ValueError("Anthropic API key not provided and not found in .env file")
            return AnthropicProvider(api_key, self.cli_agent,
                                     tools=native_tools_anthropic(registry) if native else None)
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
                    # folder, and the SAME file the Settings panel writes. The
                    # old walk stopped one level short at ios/api_key/, a
                    # folder that never existed, so Vertex config always read
                    # back empty here.
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
                    tools=native_tools_gemini(registry) if native else None
                )
            else:
                # AI Studio — needs API key
                api_key = self.runtime_api_key or os.getenv('GOOGLE_API_KEY')
                if not api_key:
                    raise ValueError("Google API key not provided and not found in .env file")
                return GoogleProvider(api_key, self.cli_agent, model=self.model_short_name,
                                      tools=native_tools_gemini(registry) if native else None)
        elif self.provider == "perplexity":
            api_key = self.runtime_api_key or os.getenv('PERPLEXITY_API_KEY')
            if not api_key:
                raise ValueError("Perplexity API key not provided and not found in .env file")
            return PerplexityProvider(api_key, self.cli_agent, self.model_info,
                                      tools=native_tools_perplexity(registry) if native else None)
        elif self.provider == "together":
            # Priority: Runtime key > .env fallback
            api_key = self.runtime_api_key or os.getenv('TOGETHER_API_KEY')
            if not api_key:
                raise ValueError("Together API key not provided and not found in .env file")
            return TogetherProvider(api_key, self.cli_agent, self.model_info,
                                    tools=native_tools_openai(registry) if native else None)
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

    def _strip_meta(self, messages: list) -> list:
        """Drop `provider_meta` from every message unless this provider knows
        how to translate it. openai/groq/perplexity/anthropic/together forward the
        message dicts to their API verbatim, and an unknown key there is a
        400."""
        if self.provider not in _META_PROVIDERS:
            for m in messages:
                if isinstance(m, dict):
                    m.pop(_META_KEY, None)
        return messages

    def _unpack(self, response: dict):
        """Provider response -> what the caller gets. Plain content string
        normally; in native-tools mode a dict
        {"text", "tool_calls": [{"id", "name", "arguments"}], "provider_meta"}
        - the provider already normalized the calls."""
        message = response['choices'][0]['message']
        if self.native_tools:
            return {
                "text": message.get('content') or "",
                # Provider metadata for THIS turn, persisted with the step and
                # handed back to the same provider next request (see
                # _META_KEY). {} when the provider emits none.
                "tool_calls": message.get('tool_calls') or [],
                _META_KEY: message.get(_META_KEY) or {},
            }
        return message['content']

    def send_request(self, messages: list, annotated_screenshot_base64: Optional[str] = None):
        """Send request to the selected provider with idempotent retries."""
        last_error = None
        for attempt in range(3):
            # Providers may mutate messages in-place (e.g. wrapping the last user
            # message into multimodal content blocks); deep-copy per attempt so
            # those mutations cannot compound across retries.
            attempt_messages = self._strip_meta(copy.deepcopy(messages))
            try:
                response = self.provider_instance.send_request(
                    attempt_messages, self.model, annotated_screenshot_base64
                )
                self.last_usage = self._normalize_usage(response.get("usage"))
                return self._unpack(response)
            except Exception as e:
                last_error = e
                if attempt < 2:
                    print(f"⚠️ API request failed (attempt {attempt + 1}/3): {e}")
                    print("   Retrying in 1 second with a fresh message copy...")
                    time.sleep(1)
                    continue
                print(f"❌ API request failed after 3 attempts: {e}")
                break

        # All 3 attempts failed. CLI agent: seamless fallback to secondary model (never die)
        if self.cli_agent and hasattr(self, '_cli_fallback_model') and self._cli_fallback_model:
            print(f"⚠️ CLI Agent: {self.display_name} failed after 3 attempts. Switching to fallback...")
            # Resolve fallback model info (same provider, different model)
            if self.provider == "openrouter":
                model_info = get_openrouter_model_info(self._cli_fallback_model)
            elif self.provider == "groq":
                model_info = get_groq_model_info(self._cli_fallback_model)
            elif self.provider == "openai":
                model_info = get_openai_model_info(self._cli_fallback_model)
            elif self.provider == "anthropic":
                model_info = get_anthropic_model_info(self._cli_fallback_model)
            elif self.provider == "google":
                model_info = get_google_model_info(self._cli_fallback_model)
            elif self.provider == "perplexity":
                model_info = get_perplexity_model_info(self._cli_fallback_model)
            elif self.provider == "together":
                model_info = get_together_model_info(self._cli_fallback_model)
            else:
                raise last_error
            # Hot-swap model (provider stays the same, no re-init needed)
            self.model = model_info["api_name"]
            self.has_vision = model_info["vision"]
            self.display_name = model_info["display_name"]
            self.model_info = model_info
            # Clear fallback so we don't loop forever
            self._cli_fallback_model = None
            print(f"✅ CLI Agent: Now using {self.display_name}")
            # Retry with fallback (fresh copy, full history intact)
            try:
                response = self.provider_instance.send_request(
                    self._strip_meta(copy.deepcopy(messages)), self.model, annotated_screenshot_base64
                )
                self.last_usage = self._normalize_usage(response.get("usage"))
                return self._unpack(response)
            except Exception as fallback_e:
                print(f"❌ CLI Agent: Fallback {self.display_name} also failed: {fallback_e}")
                raise fallback_e
        else:
            raise last_error
    
    def get_model_name(self) -> str:
        """Get the current model short name (preserves vertex suffix for downstream routing)"""
        return self.model_short_name
    
    def get_provider_name(self) -> str:
        """Get the current provider name"""
        return self.provider