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

"""Permanent (resumable) chat memory for the UI path.

All conversation-memory management lives in this package — saving, loading and
optimizing a session's conversation, and the on-disk session folders + index.
The agent stays a thin one-shot loop; app.py talks ONLY to ConversationService
(load a session -> trigger the main agent -> save the run). main.py / cli.py
never import this package, so they remain direct one-shot entry points.
"""

from .service import ConversationService, conversation

__all__ = ["ConversationService", "conversation"]
