import sys
import os

# Ensure project root (Auto-Use/) is on path when run directly: python main.py
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))

from Auto_Use.IOS_USE.agent.main_driver.service import AgentService

# Configuration
PROVIDER = "google"
MODEL = "gemini-2.5-flash"

# Your task here
task = """
open sky go and check if the application is logged in.
if logged in already then  check the version of that application.
then open appstore downlaod the stable version of sky go and open it.
 make sure this tim it should be logged in  cross check it and make a note of the version.
 then open outlook  then search sky uk sky go then download the testflight version again

"""

# Control conversation saving
conversation = True  # Set to False to disable conversation.txt

# Run the agent
agent = AgentService(provider=PROVIDER, model=MODEL, save_conversation=conversation)
response = agent.process_request(task)

# Display response
print(response)
