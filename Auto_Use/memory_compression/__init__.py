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

"""Memory compression / accounting for the agent.

MemoryTracker measures the MAIN agent's current context-window fullness (the
live "memory bar" gauge) — the exact size of the latest prompt sent, including
cached tokens. CompressionController is the runtime side of the handoff
compression agent (agent/service.py): shared by every platform agent, it owns
the 110k trigger, the background worker, the indicator, and the splice policy.
"""

from .memory_tracker import MemoryTracker
from .controller import CompressionController

__all__ = ["MemoryTracker", "CompressionController"]
