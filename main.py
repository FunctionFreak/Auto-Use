# Copyright 2026 Ashish Yadav — Auto-Use

#this main.py give terminal interface to the user to interact with the agent for ui refer app.py
from Auto_Use.agent_launcher import run_agent

# Configuration
MODE = "web use"  # "computer use" (this PC) / "shell use" (CLI agent straight to the terminal) / "web use" (CDP-controlled Chrome) / "mobile use, ios" / "mobile use, android"
PROVIDER = "anthropic"
MODEL = "claude-sonnet-5" #refer to the model name correctly from model_list.txt.
# Your task here
task = """
what isthe price of nvdia share 
"""

# Control conversation saving
conversation = True  # Set to False to disable conversation.txt

# Optional flags — speed ("quality"/"fast"), headless Chrome, and running
# MULTIPLE tasks in parallel — are documented in agent_operation.md.

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
