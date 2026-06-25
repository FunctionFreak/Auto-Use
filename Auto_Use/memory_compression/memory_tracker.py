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

"""MemoryTracker — exact, cumulative per-chat token accounting for the memory bar.

Each agent iteration sends a prompt (screenshot image included) and gets a
response; the provider reports the true tokens for that call. We add
input + output to a running total for the chat. The total is cumulative and
persists across continuations (the caller seeds initial_tokens from the chat's
saved value), and the visual bar clamps at a CAP (300k for now).
"""


class MemoryTracker:
    """Accumulates exact token usage for a single chat (across iterations/runs)."""

    CAP = 500_000

    def __init__(self, initial_tokens: int = 0, cap: int = CAP):
        self.cap = int(cap or self.CAP)
        self.total = int(initial_tokens or 0)
        self.last_input = 0
        self.last_output = 0

    def record(self, usage: dict) -> dict:
        """Add one LLM call's usage to the running total. `usage` is the
        normalized {input_tokens, output_tokens, total_tokens} from llm_manager
        (missing/empty -> adds nothing). Returns the current payload."""
        usage = usage or {}
        inp = int(usage.get("input_tokens", 0) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
        tot = (inp + out) or int(usage.get("total_tokens", 0) or 0)
        self.last_input, self.last_output = inp, out
        self.total += tot
        return self.payload()

    def payload(self) -> dict:
        """The frontend-facing snapshot: cumulative used, cap, and % (clamped)."""
        pct = min(100.0, (self.total / self.cap) * 100) if self.cap else 0.0
        return {
            "used": self.total,
            "cap": self.cap,
            "pct": pct,
            "last_input": self.last_input,
            "last_output": self.last_output,
        }
