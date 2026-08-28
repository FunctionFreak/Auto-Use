# Copyright 2026 Cursortouch — Auto-Use

#this main.py give terminal interface to the user to interact with the agent for ui refer app.py
from Auto_Use.agent_launcher import run_agent

# Configuration (check the agent_operation.md for more details)
MODE = "mobile use, ios"  # "computer use" (this PC) / "shell use" (CLI agent straight to the terminal) / "web use" (CDP-controlled Chrome) / "mobile use, ios" / "mobile use, android"
DEVICE = "hardware"       # "simulation" (iOS Simulator) / "hardware" (paired iPhone)
IOS_VERSION = None        # simulation only — e.g. "26.5"; ignored on hardware
SIM_DEVICE = "iphone"     # simulation only — "iphone" / "ipad" / exact name e.g. "iPad Pro 11-inch (M5)"; hardware uses the paired device as it is
PROVIDER = "anthropic"
MODEL = "claude-haiku-4.5" #refer to the model name correctly from model_list.txt.
# Your task here
task = """
login to now
"""
conversation=True

# Run the agent
run_agent(
    mode=MODE,
    provider=PROVIDER,
    model=MODEL,
    task=task,
    device=DEVICE,
    ios_version=IOS_VERSION,
    sim_device=SIM_DEVICE,
    external_terminal=True,
    save_conversation=conversation,
)

# Response is displayed inside process_request
