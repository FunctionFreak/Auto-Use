import os
import base64
import json
import re
import xml.etree.ElementTree as ET
import time
from pathlib import Path
from datetime import datetime
from typing import Optional
from ..llm_provider.llm_manager import LLMManager
from .view import AgentResponseFormatter
from ..tree.element import UIElementScanner, ELEMENT_CONFIG
from ..controller.view import ControllerView
from ..controller.app import app_launcher_service
from ..vault.service import vault_service

class AgentService:
    """Service for Windows automation agent"""
    
    # Expected output format for agent responses
    OUTPUT_FORMAT = """
```json
{
  "thinking": "",
  "verdict_last_action": "",
  "image_observation": "",
  "memory": "",
  "current_goal": "",
  "action": {

  }
}
```"""
    
    def __init__(self, provider: str, model: str, save_conversation: bool = True):
        """Initialize the Agent Service"""
        # Clear todo.md silently before anything starts
        self._clear_todo()
        
        # Initialize LLM Manager
        self.llm_manager = LLMManager(provider, model)
        
        # Initialize UI Element Scanner
        self.scanner = UIElementScanner(ELEMENT_CONFIG)
        
        # Initialize Controller
        self.controller = ControllerView()
        
        # Save conversation flag
        self.save_conversation = save_conversation
        
        # Load system prompt
        self.system_prompt = self._load_system_prompt()
        
        # Create conversation directory
        if self.save_conversation:
            self.conversation_dir = Path("conversation")
            self.conversation_dir.mkdir(exist_ok=True)
            
        # Start fresh each session
        self.interaction_count = 0
        
        # Store conversation history for complete snapshots
        self.conversation_history = []
        
        # Scan installed apps at startup
        app_launcher_service.scan_apps()
        
    def _clear_todo(self):
        """Silently clear todo.md file at agent startup"""
        try:
            todo_file = Path("todo/todo.md")
            if todo_file.exists():
                todo_file.unlink()
        except:
            pass  # Silently ignore any errors
    
    def _load_system_prompt(self) -> str:
        """Load the system prompt from system_prompt.md file"""
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            prompt_path = os.path.join(current_dir, "system_prompt.md")
            
            with open(prompt_path, 'r', encoding='utf-8') as file:
                return file.read()
        except FileNotFoundError:
            raise FileNotFoundError("system_prompt.md file not found in the agent directory")
        except Exception as e:
            raise Exception(f"Error loading system prompt: {str(e)}")
    
    def _save_conversation_async(self, image_sent: bool = False):
        """Create a NEW timestamped file with COMPLETE agent memory snapshot at each iteration"""
        try:
            self.interaction_count += 1
            
            # Create timestamped filename
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            conversation_file = self.conversation_dir / f"conversation_step_{self.interaction_count}_{timestamp}.txt"
            
            with open(conversation_file, 'w', encoding='utf-8') as f:
                # Header
                f.write("=" * 80 + "\n")
                f.write(f"AGENT MEMORY SNAPSHOT - ITERATION #{self.interaction_count}\n")
                f.write(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Provider: {self.llm_manager.get_provider_name()}\n")
                f.write(f"Model: {self.llm_manager.get_model_name()}\n")
                f.write("=" * 80 + "\n\n")
                
                # System Prompt
                f.write("=" * 80 + "\n")
                f.write("SYSTEM PROMPT (Always in Agent Memory)\n")
                f.write("=" * 80 + "\n")
                f.write(self.system_prompt)
                f.write("\n\n")
                
                # All conversation history
                f.write("=" * 80 + "\n")
                f.write("COMPLETE CONVERSATION HISTORY\n")
                f.write("=" * 80 + "\n\n")
                
                interaction_num = 0
                for i, message in enumerate(self.conversation_history):
                    role = message["role"]
                    content = message["content"]
                    
                    if role == "system":
                        # Skip system as we already printed it above
                        continue
                    elif role == "user":
                        interaction_num += 1
                        f.write(f"\n{'=' * 80}\n")
                        f.write(f"INTERACTION #{interaction_num}\n")
                        f.write(f"{'=' * 80}\n\n")
                        f.write(f"USER MESSAGE:\n")
                        f.write(f"{'-' * 80}\n")
                        f.write(f"{content}\n")
                        f.write(f"{'-' * 80}\n")
                        if image_sent and i == len(self.conversation_history) - 2:
                            f.write("\n[Screenshot was sent with this message]\n")
                        f.write("\n")
                    elif role == "assistant":
                        f.write(f"ASSISTANT RESPONSE:\n")
                        f.write(f"{'-' * 80}\n")
                        f.write(f"{content}\n")
                        f.write(f"{'-' * 80}\n\n")
                
                # Summary footer
                f.write("\n" + "=" * 80 + "\n")
                f.write(f"END OF SNAPSHOT - Total Interactions: {self.interaction_count}\n")
                f.write("=" * 80 + "\n")
            
            print(f"✓ Memory snapshot saved: {conversation_file.name}")
        except Exception as e:
            print(f"Error saving conversation snapshot: {str(e)}")
    
    
    def _save_conversation(self, image_sent: bool):
        """Save complete conversation snapshot to timestamped file"""
        if self.save_conversation:
            self._save_conversation_async(image_sent)
    
    def _execute_action(self, normalized_json: str) -> dict:
        """Extract action from normalized JSON and execute it via controller"""
        try:
            # Extract JSON from markdown code block
            json_match = re.search(r'```json\s*(.*?)\s*```', normalized_json, re.DOTALL)
            if not json_match:
                return {"status": "error", "message": "No JSON found in response"}
            
            json_str = json_match.group(1)
            data = json.loads(json_str)
            
            # Extract action block
            if "action" not in data:
                return {"status": "error", "message": "No action block in response"}
            
            action_data = data["action"]
            
            # Route action to controller
            result = self.controller.route_action(action_data)
            
            return result
            
        except Exception as e:
            return {"status": "error", "message": f"Error executing action: {str(e)}"}
    
    def process_request(self, task: str) -> str:
        """Process a user request and return agent response"""
        # Initialize conversation history with system prompt
        self.conversation_history = [
            {"role": "system", "content": self.system_prompt}
        ]
        
        # Store the initial task
        initial_task = task
        loop_count = 0
        
        # Show model info only once at the beginning
        print(f"\n🤖 Using: {self.llm_manager.get_model_name()}")
        
        # Main agent loop
        wait_duration = 0  # Track wait time from previous action
        action_result = {}  # Track action results across iterations
        while True:
            loop_count += 1
            print(f"\n{'='*60}")
            print(f"📍 Step {loop_count}")
            
            # Wait before scanning - either from wait action or default 2 seconds
            if loop_count > 1:  # Skip wait on first iteration
                sleep_time = wait_duration if wait_duration > 0 else 2
                if sleep_time > 0:
                    print(f"⏳ Waiting {sleep_time} seconds before scanning...")
                    time.sleep(sleep_time)
                wait_duration = 0  # Reset for next iteration
            
            # Scan UI elements and get annotated screenshot
            print("🔍 Scanning snapshot.")
            self.scanner.scan_elements()
            element_tree_text, image_base64 = self.scanner.get_scan_data()
            if image_base64:
                print(f"✅ Image captured - size: {len(image_base64)} chars")
            else:
                print("❌ NO IMAGE - image_base64 is None!")
            
            image_sent = image_base64 is not None
            
            # Update vault with current element tree so it can identify fields
            vault_service.update_element_tree(element_tree_text)
            
            # Wrap element tree in proper tags
            formatted_element_tree = f"<element_tree>\n{element_tree_text}\n</element_tree>"
            
            # Read todo list if it exists
            todo_content = ""
            todo_file = Path("todo/todo.md")
            if todo_file.exists():
                with open(todo_file, 'r', encoding='utf-8') as f:
                    todo_content = f.read()
            
            # Check if previous action had a tool_response (from video_player)
            tool_response_text = ""
            tool_response_found = None
            
            # Check for tool_response at top level (single action)
            if action_result and action_result.get('tool_response'):
                tool_response_found = action_result['tool_response']
            # Check for tool_response in combined actions (inside results array)
            elif action_result and action_result.get('action') == 'combined' and action_result.get('results'):
                for result in action_result['results']:
                    if result.get('tool_response'):
                        tool_response_found = result['tool_response']
                        break
            
            if tool_response_found:
                tool_response_text = f"""<tool_response>
{tool_response_found}
</tool_response>

"""
                print(f"📨 Tool response will be included in next prompt: {tool_response_found}")
            # Construct user message - only include task in first iteration
            if loop_count == 1:
                user_message = f"""<user_request>
{task}
</user_request>

<todo>
{todo_content}
</todo>

{formatted_element_tree}

{tool_response_text}<output_format>
{self.OUTPUT_FORMAT}
</output_format>"""
            else:
                # For subsequent iterations, just provide element tree
                user_message = f"""<todo>
{todo_content}
</todo>

{formatted_element_tree}

{tool_response_text}<output_format>
{self.OUTPUT_FORMAT}
</output_format>"""
            
            # Remove all previous user messages, keep only system and assistant
            self.conversation_history = [msg for msg in self.conversation_history if msg["role"] != "user"]
            
            # Add user message to conversation history
            self.conversation_history.append({"role": "user", "content": user_message})
            
            try:
                # Get raw response from LLM
                raw_response = self.llm_manager.send_request(self.conversation_history, image_base64)
                print("✅ Response received")
                
                # Normalize the response to ensure consistent JSON format
                normalized_json = AgentResponseFormatter.normalize_response(raw_response)
                
                # Add assistant response to conversation history
                self.conversation_history.append({"role": "assistant", "content": raw_response})
                
                # Extract and execute action
                action_result = self._execute_action(normalized_json)
                
                # Check if wait action was executed and store duration for next iteration
                if action_result.get("action") == "wait":
                    wait_duration = action_result.get("seconds", 2)
                
                # Add wait for video_player actions to complete
                elif action_result.get("action") == "video_player":
                    command = action_result.get("command", "")
                    if command == "streaming":
                        # Streaming check needs 4 seconds to compare timestamps
                        print("⏳ Waiting for video streaming check to complete...")
                        time.sleep(4.5)
                    else:
                        # Other video commands need time for UI response
                        print(f"⏳ Waiting for video {command} to complete...")
                        time.sleep(1)
                
                # Check action result and log any errors
                if action_result.get("status") == "error":
                    print(f"⚠️  Action failed: {action_result.get('message', 'Unknown error')}")
                
                # Save COMPLETE agent memory snapshot (system prompt + all conversations)
                self._save_conversation(image_sent)
                
                # Format the response with emojis for console output
                formatted_response = AgentResponseFormatter.format_response(normalized_json)
                print(f"\n{formatted_response}")
                
                # Check if done action was executed
                if action_result.get("action") == "done":
                    print(f"\n{'='*60}")
                    print("✅ Agent completed task!")
                    print(f"Summary: {action_result.get('summary', 'No summary provided')}")
                    print(f"Total iterations: {loop_count}")
                    print(f"{'='*60}")
                    return action_result.get("summary", "Task completed")
                
            except Exception as e:
                return f"❌ Error in loop #{loop_count}: {str(e)}"