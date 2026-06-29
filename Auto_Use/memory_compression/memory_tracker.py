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

"""MemoryTracker — current memory fullness for the MAIN agent's bar.

The bar answers "how full is the MAIN agent's working memory right now?", NOT
"how many tokens have we burned in total". Every turn the agent rebuilds and
re-sends its whole (trimmed) context, and the provider reports the true size of
that exact prompt. We show that latest size against a FIXED budget — MEMORY_CAP
(300k) — so the bar rises as history grows and DROPS when the runtime memory
optimization (stripping old images / element trees) kicks in.

The cap is fixed on purpose, NOT the model's context window: a future
memory-compression system will keep the agent's real context under this budget,
so 300k is the headroom gauge for it.

`context_tokens` is the full prompt size INCLUDING cached tokens — a cache-read
token still occupies memory, so it counts whether it was cached or not (see
LLMManager._normalize_usage). Only the main agent is tracked; CLI / coder /
minion sub-agents run on their own LLMManager and never feed this bar. On reopen
the caller seeds `initial_context` from the chat's last saved value.
"""

# Fixed memory budget the bar fills toward (headroom for future compression).
MEMORY_CAP = 300_000


class MemoryTracker:
    """Holds the latest context size for a single chat (overwrite, never sum).

    PURELY COSMETIC. This is a display gauge ONLY — it never gates the agent. The
    agent does not stop at the cap (or at 1M); `self.current` keeps the REAL,
    unclamped token count, and only the bar's `pct` saturates at 100% so a full
    bar reads "memory full" while the agent keeps running. (A future, user-driven
    compaction system is what will actually manage memory.) Do NOT wire any agent
    stop/limit to this class."""

    def __init__(self, cap: int = MEMORY_CAP, initial_context: int = 0):
        self.cap = int(cap or MEMORY_CAP)
        self.current = int(initial_context or 0)
        self.last_input = 0
        self.last_output = 0

    def record(self, usage: dict) -> dict:
        """Set the bar to this LLM call's full prompt size. `usage` is the
        normalized {input_tokens, output_tokens, context_tokens, ...} from
        llm_manager. A turn that reports 0 context (e.g. a failed call left
        last_usage stale) is ignored so the bar holds its last good value
        instead of collapsing to empty. Returns the current payload."""
        usage = usage or {}
        ctx = int(usage.get("context_tokens", 0) or 0)
        self.last_input = int(usage.get("input_tokens", 0) or 0)
        self.last_output = int(usage.get("output_tokens", 0) or 0)
        if ctx > 0:
            self.current = ctx
        return self.payload()

    def payload(self) -> dict:
        """The frontend-facing snapshot: current size, cap, and % (clamped).
        `used` is the real count (may exceed `cap`); `pct` clamps to 100 for the
        bar height only — it is not a limit the agent obeys."""
        pct = min(100.0, (self.current / self.cap) * 100) if self.cap else 0.0
        return {
            "used": self.current,
            "cap": self.cap,
            "pct": pct,
            "last_input": self.last_input,
            "last_output": self.last_output,
        }
