from agent_core.agent.service import AgentService
import threading
import iphone_viewer

# Configuration
PROVIDER = "google"
MODEL = "gemini-2.5-flash"

# Your task here
task = "open netflix app and open ashish profile and then search for conjuring"

# Control conversation saving
conversation = True

# Run the agent in a separate thread (not main thread)
def run_agent():
    agent = AgentService(provider=PROVIDER, model=MODEL, save_conversation=conversation)
    response = agent.process_request(task)
    print("\n" + "="*50)
    print("AGENT RESPONSE:")
    print("="*50)
    print(response)
    print("="*50)

# Start agent in background thread
agent_thread = threading.Thread(target=run_agent)
agent_thread.daemon = True
agent_thread.start()

# Run the iPhone viewer on main thread (required for OpenCV on macOS)
# Press 'q' to quit the viewer
iphone_viewer.start()