# Copyright 2026 Ashish Yadav — Auto-Use

#this main.py give terminal interface to the user to interact with the agent for ui refer app.py
from Auto_Use.agent_launcher import run_agent

# Configuration
MODE = "mobile use, ios"  # "computer use" (this PC) / "shell use" (CLI agent straight to the terminal) / "web use" (CDP-controlled Chrome) / "mobile use, ios" / "mobile use, android"
DEVICE = "simulation"  # "mobile use, ios" only: "simulation" (default, iOS Simulator) / "hardware" (your paired iPhone). Ignored by every other mode.
IOS_VERSION = None     # simulation only: None = newest runtime ALREADY here that Xcode can build to (never downloads); name one like "26.5" and it is installed if missing (hardware ignores this — the phone runs whatever iOS it has)
PROVIDER = "anthropic"
MODEL = "claude-sonnet-5" #refer to the model name correctly from model_list.txt.
# Your task here
task = """

open setting and check the ios version 
"""
conversation=True
# Optional flags — conversation saving, speed ("quality"/"fast"), headless
# Chrome, and running MULTIPLE tasks in parallel — are documented in
# agent_operation.md.

# ── Parallel tasks ───────────────────────────────────────────────────────────
# "web use" (one shared Chrome, a tab each) and "mobile use, ios" with
# DEVICE = "simulation" (ONE SIMULATOR EACH). Unused slots stay None.
task_2 = None
task_3 = None

# Run the agent
run_agent(
    mode=MODE,
    provider=PROVIDER,
    model=MODEL,
    task=task,
    device=DEVICE,
    ios_version=IOS_VERSION,
    external_terminal=True,
    save_conversation=conversation,
    extra_tasks=[task_2, task_3],

)

# Response is displayed inside process_request
