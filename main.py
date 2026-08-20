# Copyright 2026 Ashish Yadav — Auto-Use

#this main.py give terminal interface to the user to interact with the agent for ui refer app.py
from Auto_Use.agent_launcher import run_agent

# Configuration
MODE = "web use"  # "computer use" (this PC) / "shell use" (CLI agent straight to the terminal) / "web use" (CDP-controlled Chrome) / "mobile use, ios" / "mobile use, android"
PROVIDER = "together"
MODEL = "minimax-m3" #refer to the model name correctly from model_list.txt.
# Your task here
task = """

open three tab with different  url then switch between them
"""
conversation=True
# Optional flags — conversation saving, speed ("quality"/"fast"), headless
# Chrome, and running MULTIPLE tasks in parallel — are documented in
# agent_operation.md.

# Run the agent
run_agent(
    mode=MODE,
    provider=PROVIDER,
    model=MODEL,
    task=task,
    external_terminal=True,
    save_conversation=conversation,

)

# Response is displayed inside process_request
