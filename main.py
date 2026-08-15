# Copyright 2026 Ashish Yadav — Auto-Use

#this main.py give terminal interface to the user to interact with the agent for ui refer app.py
from Auto_Use.agent_launcher import run_agent

# Configuration
MODE = "shell use"  # "computer use" (this PC) / "shell use" (CLI agent straight to the terminal) / "web use" (CDP-controlled Chrome) / "mobile use, ios" / "mobile use, android"
PROVIDER = "anthropic"
MODEL = "claude-sonnet-5" #refer to the model name correctly from model_list.txt.
# Your task here
task = """

convert this xml file  on download Sales and Return Register Report (22) - Tally.xml this file since i cant view the excel file . i want to how this xml file will look in tally software so convert in pdf to view it

"""

# Control conversation saving
conversation = True  # Set to False to disable conversation.txt
# Control speed mode (all platforms)
speed = "fast"  # "quality" or "fast" — fast = lean output + fast prompt (ignored by shell use)
# "web use" only — run Chrome without a visible window. Ignored by every other
# mode (run_agent drops kwargs the target AgentService doesn't declare).
headless = False

# ── Parallel tasks ("web use" only) ─────────────────────────────────────────
# Set any of these to run them IN PARALLEL with `task` above — each one gets
# its own web agent in the SAME Chrome, pinned to its own single tab.
# Leave as None to skip. Add task_5 = ... and append it to extra_tasks below
# to go wider. Results and logs land in ./parallel/task_N/.
#task_2 = "add iphone 17 pro max to the cart"
#task_3 = "check nice leather beige color bacpack  by ralph lauren"
#task_4 = None

# Run the agent
run_agent(
    mode=MODE,
    provider=PROVIDER,
    model=MODEL,
    task=task,
    save_conversation=conversation,
    external_terminal=True,
    speed=speed,
    headless=headless,
    #extra_tasks=[task_2, task_3, task_4],
)

# Response is displayed inside process_request
