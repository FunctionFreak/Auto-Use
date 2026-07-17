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

#this main.py give terminal interface to the user to interact with the agent for ui refer app.py
from Auto_Use.agent_launcher import run_agent

# Configuration
MODE = "mobile use, ios"  # "computer use" (this PC) or "mobile use, ios" / "mobile use, android"
PROVIDER = "openrouter"
MODEL = "gemini-3.5-flash" #refer to the model name correctly from model_list.txt.
# Your task here
task = """

open youtube
"""

# Control conversation saving
conversation = True  # Set to False to disable conversation.txt
# Control thinking/reasoning
thinking = True  # Set to True to enable reasoning for supported models
# Control automation engineer
automation_engineer = True  # Set to True to enable automation engineer mode by default it is False
# Run the agent
run_agent(
    mode=MODE,
    provider=PROVIDER,
    model=MODEL,
    task=task,
    save_conversation=conversation,
    external_terminal=True,
)

# Response is displayed inside process_request