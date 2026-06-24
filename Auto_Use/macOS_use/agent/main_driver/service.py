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

import os
import json
import re
import base64
import time
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional
from ...llm_provider.llm_manager import LLMManager
from .view import AgentResponseFormatter
from ...tree.element import UIElementScanner, ELEMENT_CONFIG
from ...controller import ControllerView
from ..skills import DomainKnowledgeService
from PIL import Image
from io import BytesIO

def _compress_screenshot(base64_str: str, max_width: int = 1080, quality: int = 75) -> str:
    """Compress screenshot to reduce token size while keeping UI readable"""
    try:
        img_bytes = base64.b64decode(base64_str)
        img = Image.open(BytesIO(img_bytes))
        
        # Downscale if wider than max_width
        if img.width > max_width:
            ratio = max_width / img.width
            new_size = (max_width, int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        
        # Re-encode as JPEG with lower quality
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=quality)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception:
        return base64_str  # Return original if compression fails

def _cleanup_scratchpad():
    """Clear all contents inside Auto_Use/macOS_use/scratchpad/ for a fresh start."""
    # Clear scratchpad contents
    scratchpad_dir = Path(__file__).parent.parent.parent / "scratchpad"
    if scratchpad_dir.exists():
        for item in scratchpad_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    else:
        scratchpad_dir.mkdir(parents=True, exist_ok=True)
    
    # Also clean CLI subprocess output folders at root
    for folder in ["cli_agent_result", "cli_conversation"]:
        folder_path = Path(folder)
        if folder_path.exists() and folder_path.is_dir():
            shutil.rmtree(folder_path)


class AgentService:
    """Service for Windows automation agent"""
    
    def __init__(self, provider: str, model: str, save_conversation: bool = False, thinking: bool = True, frontend_callback=None, text_callback=None, web_callback=None, shell_callback=None, cli_callback=None, tool_callback=None, api_key: str = None, stop_event=None, external_terminal: bool = False, prior_history: Optional[dict] = None):
        """Initialize the Agent Service"""
        # Clean up scratchpad for a fresh start
        _cleanup_scratchpad()
        
        # Ensure sandbox workspace exists on Desktop
        self.sandbox_root = Path.home() / "Desktop" / "sandbox_workspace"
        if not self.sandbox_root.exists():
            self.sandbox_root.mkdir(parents=True, exist_ok=True)
        
        # Initialize LLM Manager with optional runtime API key
        self.llm_manager = LLMManager(provider, model, thinking, api_key)
        
        # Store stop event
        self.stop_event = stop_event
        
        # Initialize UI Element Scanner with optional frontend callback for image streaming
        self.scanner = UIElementScanner(ELEMENT_CONFIG, frontend_callback=frontend_callback)
        
        # Store text callback for streaming agent responses to frontend
        self.text_callback = text_callback
        
        # Store web callback for globe animation
        self.web_callback = web_callback
        
        # Store shell callback for terminal animation
        self.shell_callback = shell_callback

        # Store CLI callback for streaming CLI agent subprocess output to the frontend
        self.cli_callback = cli_callback

        # Store tool-flow callback for the bottom "Tool response" chain animation.
        # Signature: tool_callback(event: str, payload: dict|None)
        #   events: run_start | turn{hasImage} | received{tools:[{name,clicks?}]} | run_end
        # The per-turn tools come straight from the parsed action block (this driver),
        # so the controller needs no per-action plumbing.
        self.tool_callback = tool_callback

        # Initialize Controller with provider and actual API model name (pass api_key for CLI agent subprocess)
        self.controller = ControllerView(provider=provider, model=self.llm_manager.get_model_name(), web_callback=web_callback, shell_callback=shell_callback, cli_callback=cli_callback, api_key=api_key, stop_event=stop_event, external_terminal=external_terminal)
        
        # Initialize Domain Knowledge Service
        self.skills = DomainKnowledgeService()
        
        # Save conversation flag
        self.save_conversation = save_conversation
        
        # Load system prompt
        self.system_prompt = self._load_system_prompt()
        
        # Clear previous conversation folder to start fresh
        conversation_folder = Path("conversation")
        if conversation_folder.exists():
            shutil.rmtree(conversation_folder)
        
        # Clear previous debug folder to start fresh
        debug_folder = Path("debug")
        if debug_folder.exists():
            shutil.rmtree(debug_folder)
        
        # Clear previous raw_reasoning folder to start fresh
        raw_reasoning_folder = Path("raw_reasoning")
        if raw_reasoning_folder.exists():
            shutil.rmtree(raw_reasoning_folder)
        
        # Create conversation directory and initialize fresh conversation file
        if self.save_conversation:
            self.conversation_dir = Path("conversation")
            self.conversation_dir.mkdir(exist_ok=True)
            self.conversation_file = self.conversation_dir / "conversation.txt"
            
            # Always start fresh - each program run is a new session
            self._initialize_conversation_file()
            
            # Create raw_reasoning directory for storing raw LLM outputs
            self.raw_reasoning_dir = Path("raw_reasoning")
            self.raw_reasoning_dir.mkdir(exist_ok=True)
            
        # Start fresh each session
        self.interaction_count = 0

        # Resumable chat memory (UI path only). prior_history is an optimized
        # snapshot from a PRIOR run of this session, built + loaded by
        # Auto_Use.agent_conversation.service (ALL memory management lives there,
        # not here). When present, process_request seeds the conversation lists
        # from it so the agent "remembers" earlier turns. Scratchpad/todo are
        # still wiped above — continuity comes ONLY from this memory, not files.
        # The final lists are exposed on self so the conversation service can
        # persist them after the run.
        self.prior_history = prior_history if isinstance(prior_history, dict) else None
        self.assistant_messages = []
        self.tool_responses = []
        # The exact final messages payload sent to the model (system + interleaved
        # history + the live user message that re-injects user_request/todo/
        # scratchpad/element_tree each step). Captured for the debug memory log so
        # the download is the TRUE conversation, not a reconstruction.
        self.last_messages = None

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
    
    def _initialize_conversation_file(self):
        """Initialize the conversation file with header information"""
        try:
            with open(self.conversation_file, 'w', encoding='utf-8') as f:
                f.write("=== CONVERSATION LOG ===\n")
                f.write(f"Session Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Provider: {self.llm_manager.get_provider_name()}\n")
                f.write(f"Model: {self.llm_manager.get_model_name()}\n")
                f.write("=" * 60 + "\n\n")
                
                f.write("=== SYSTEM PROMPT ===\n")
                f.write(self.system_prompt)
                f.write("\n\n" + "=" * 60 + "\n\n")
        except Exception as e:
            print(f"Error initializing conversation file: {str(e)}")
    
    def _save_conversation_snapshot(self, messages: list, current_assistant_response: str, image_sent: bool, interaction_count: int):
        """Save a numbered conversation file rendering the EXACT payload sent this
        step (system + interleaved assistant/user turns + current user message) plus
        this step's freshly generated response — a faithful peek into agent memory."""
        try:
            conversation_file = self.conversation_dir / f"conversation_{interaction_count}.txt"

            def _text(content):
                # Cached messages carry content as a list of {type,text,...} blocks.
                if isinstance(content, list):
                    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
                return content if isinstance(content, str) else str(content)

            with open(conversation_file, 'w', encoding='utf-8') as f:
                # Header
                f.write("=== CONVERSATION LOG ===\n")
                f.write(f"Session Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Provider: {self.llm_manager.get_provider_name()}\n")
                f.write(f"Model: {self.llm_manager.get_model_name()}\n")
                f.write(f"Current Interaction: #{interaction_count}\n")
                f.write("=" * 60 + "\n\n")

                # Render every message in the exact order it was sent to the model.
                for m in messages:
                    role = str(m.get("role", "?")).upper()
                    if role == "SYSTEM":
                        f.write("=== SYSTEM PROMPT ===\n")
                        f.write(_text(m.get("content", "")))
                        f.write("\n\n" + "=" * 60 + "\n\n")
                    else:
                        f.write(f"--- {role} ---\n")
                        f.write(_text(m.get("content", "")))
                        f.write("\n\n")

                if image_sent:
                    f.write("[Screenshot sent with the latest user message]\n\n")

                # This step's freshly generated response (the reply, not part of the request).
                f.write(f"--- ASSISTANT (response · step {interaction_count}) ---\n")
                f.write(current_assistant_response)
                f.write("\n")

            print(f"✓ Memory snapshot saved: conversation_{interaction_count}.txt")
        except Exception as e:
            print(f"Error saving conversation snapshot: {str(e)}")
    
    
    def _save_raw_response(self, raw_response: str, step_number: int):
        """Save raw LLM response before any parsing/normalization"""
        if self.save_conversation:
            try:
                raw_file = self.raw_reasoning_dir / f"raw_response_{step_number}.txt"
                with open(raw_file, 'w', encoding='utf-8') as f:
                    f.write(raw_response)
            except Exception as e:
                print(f"⚠ Error saving raw response: {str(e)}")
    
    def _save_conversation(self, messages: list, current_assistant_response: str, image_sent: bool, interaction_count: int):
        """Save conversation snapshot to file - simple and direct"""
        if self.save_conversation:
            self._save_conversation_snapshot(messages, current_assistant_response, image_sent, interaction_count)
    
    def _read_todo_from_file(self) -> str:
        """Read the current todo list from scratchpad/todo/todo.md file"""
        try:
            # Read from the task tracker's own file path (single source of truth —
            # exactly where TaskTrackerService writes), so read can never drift from write.
            todo_file = Path(self.controller.task_tracker.todo_file)
            if todo_file.exists():
                with open(todo_file, 'r', encoding='utf-8') as f:
                    return f.read().strip()
            else:
                return ""
        except Exception as e:
            print(f"⚠ Error reading todo file: {str(e)}")
            return ""
    
    def _read_scratchpad_from_file(self) -> str:
        """Read the current scratchpad entries from scratchpad/milestone/milestone.md file"""
        try:
            # Read from the scratchpad service's own file path (single source of truth).
            scratchpad_file = Path(self.controller.scratchpad_service.scratchpad_file)
            if scratchpad_file.exists():
                with open(scratchpad_file, 'r', encoding='utf-8') as f:
                    return f.read().strip()
            else:
                return ""
        except Exception as e:
            print(f"⚠ Error reading scratchpad file: {str(e)}")
            return ""

    def _remove_action_from_response(self, response_json: str) -> str:
        """Remove 'action' field from assistant response to save tokens in history"""
        try:
            response_data = json.loads(response_json)
            if "action" in response_data:
                del response_data["action"]
            return json.dumps(response_data, indent=2, ensure_ascii=False)
        except Exception:
            # Handle <Step_no=X /> prefix that makes json.loads fail
            match = re.match(r'(<Step_no=\d+ />\n)(.*)', response_json, re.DOTALL)
            if match:
                prefix, json_part = match.group(1), match.group(2)
                try:
                    response_data = json.loads(json_part)
                    if "action" in response_data:
                        del response_data["action"]
                    return prefix + json.dumps(response_data, indent=2, ensure_ascii=False)
                except Exception:
                    return response_json
            return response_json

    def _remove_thinking_from_response(self, response_json: str) -> str:
        """Remove 'thinking' field from assistant response to save tokens in history"""
        try:
            response_data = json.loads(response_json)
            
            # Remove thinking field if it exists
            if "thinking" in response_data:
                del response_data["thinking"]
            
            # Remove eval field if it exists
            if "eval" in response_data:
                del response_data["eval"]
            
            return json.dumps(response_data, indent=2, ensure_ascii=False)
        except Exception:
            return response_json

    def _tool_response_for_memory(self, action_result: dict) -> dict:
        """Build the compact tool_response preserved in agent memory for a step.

        Only web-tool results are compacted: their raw text is already removed and
        digested into the scratchpad, so memory keeps just the query + a pointer.
        Web is compacted ONLY when it succeeded — a failed web call keeps its exact
        response so the agent can see what went wrong. Every other result (shell
        output, click/input results, etc.) is preserved verbatim.
        """
        import copy
        res = copy.deepcopy(action_result)

        def compact(entry):
            if (isinstance(entry, dict)
                    and entry.get("tool") == "web"
                    and entry.get("status") == "success"):
                return {
                    "action": "tool",
                    "tool": "web",
                    "query": entry.get("query", ""),
                    "message": "memory optimized — refer to scratchpad for the web result",
                }
            return entry

        if res.get("action") == "multiple" and "results" in res:
            res["results"] = [compact(r) for r in res["results"]]
            return res
        return compact(res)

    def _trim_history_entry(self, entry: str) -> str:
        """Trim an OLDER history step for context: drop 'thinking', 'eval', and
        'action' (keep decision/memory/next_goal). Only the most recent step is kept
        full so the agent retains its latest reasoning; everything older is trimmed.
        Handles the leading '<Step_no=N />' marker.
        """
        m = re.match(r'(<Step_no=\d+ />\n)(.*)', entry, re.DOTALL)
        prefix, json_part = (m.group(1), m.group(2)) if m else ("", entry)
        try:
            data = json.loads(json_part)
            for field in ("thinking", "eval", "action"):
                data.pop(field, None)
            return prefix + json.dumps(data, indent=2, ensure_ascii=False)
        except Exception:
            return entry

    def _emit_flow(self, event, payload=None):
        """Push a tool-flow event to the frontend bottom chain (best-effort)."""
        if not self.tool_callback:
            return
        try:
            self.tool_callback(event, payload)
        except Exception:
            pass

    def process_request(self, task: str) -> dict:
        """Process a user request in an iterative loop until completion.

        Returns a structured outcome {"status", "message"} so callers (Telegram,
        web UI) can tell a real completion from a failure instead of always
        reporting "done":
          - "success"    a `done` action ran
          - "error"      a critical exception ended the loop (e.g. API failure)
          - "incomplete" the loop ended otherwise (stopped, max steps, parse fails)
        """
        # Initialize tracking variables
        step_number = 0
        last_response = None
        is_first_iteration = True
        assistant_messages = []  # Track all assistant responses for memory
        tool_responses = []      # Per-step tool result, aligned 1:1 with assistant_messages; replayed as user turns
        cli_await_result = None  # Stores cli_await result for next iteration's light message
        pending_web_response = None  # Stores web tool response for light digest iteration
        json_fail_count = 0  # Track consecutive JSON parse failures (max 3 before exit)
        # Final outcome reported to the caller. Defaults to "incomplete" so any
        # loop exit that doesn't explicitly set success/error is reported as
        # not-finished rather than silently looking like a completion.
        final_status = "incomplete"
        final_message = "Agent stopped before completing the task"

        # ---- Resumable memory seed (UI continuation) -------------------------
        # If a prior optimized snapshot was supplied (continuing a saved chat),
        # replay it into the two memory lists so the existing message-build loop
        # re-emits the earlier conversation. Entries are ALREADY trimmed and
        # tool_responses already compacted, so no re-processing. The saved memory
        # ends with a terminal-note step, so on resume that note is the most-recent
        # entry — which makes the loop replay EVERY real step's tool_response.
        is_resumed = False
        if self.prior_history:
            seeded = self.prior_history.get("assistant_messages") or []
            if seeded:
                assistant_messages = list(seeded)
                tool_responses = list(self.prior_history.get("tool_responses") or [])[:len(assistant_messages)]
                tool_responses += [None] * (len(assistant_messages) - len(tool_responses))
                is_resumed = True
                is_first_iteration = False  # continuation, not a fresh dialogue
                last_response = self.prior_history.get("done_message") or "Resuming previous session."
                print(f"🧠 Resumed memory: {len(assistant_messages)} prior step(s) loaded")
        # ----------------------------------------------------------------------

        # Print model info once at the start
        print(f"\n🔄 Processing with {self.llm_manager.get_model_name()}")
        self._emit_flow("run_start")   # tool-flow chain: clear + take over the demo

        # Main agent loop
        while True:
            # Check for stop signal
            if self.stop_event and self.stop_event.is_set():
                print("\n🛑 Agent stopped by user.")
                self.controller.controller_service.release_all_inputs()
                final_status, final_message = "incomplete", "Stopped by user"
                # Don't send callback to frontend to avoid re-opening the strip
                break

            step_number += 1
            
            # Max step limit - prevent infinite loops
            if step_number > 100:
                print("\n🛑 Max step limit reached (100). Exiting agent loop.")
                final_status, final_message = "incomplete", "Reached the 100-step limit without finishing"
                break
            
            # Tool-flow chain: announce the turn. Digest iterations send no image,
            # so the chain skips screenshot+mapping (true to the agent's behaviour).
            self._emit_flow("turn", {"hasImage": not (cli_await_result or pending_web_response)})

            # Skip scan for light digest iterations (CLI await or web tool response)
            if cli_await_result or pending_web_response:
                print(f"\n{'='*60}")
                if cli_await_result:
                    print(f"📋 Step {step_number}: CLI digest iteration (no scan)")
                else:
                    print(f"🌐 Step {step_number}: Web digest iteration (no scan)")
                element_tree_text = ""
                annotated_image_base64 = None
                image_sent = False
                formatted_element_tree = ""
                uac_detected = False
                domain_block = ""
            else:
                # Scan UI elements and get annotated screenshot
                print(f"\n{'='*60}")
                print(f"🔍 Step {step_number}: Scanning snapshot.")
                self.scanner.scan_elements()
                
                # Check Stop AFTER Scan
                if self.stop_event and self.stop_event.is_set():
                    self.controller.controller_service.release_all_inputs()
                    final_status, final_message = "incomplete", "Stopped by user"
                    break

                element_tree_text, annotated_image_base64, uac_detected = self.scanner.get_scan_data()
                
                # Compress screenshot to reduce token size
                if annotated_image_base64:
                    annotated_image_base64 = _compress_screenshot(annotated_image_base64)
            
            # Handle UAC secure desktop detection
            if uac_detected:
                print("🔒 UAC detected - asking agent for decision")
                image_sent = False
                formatted_element_tree = ""
                user_message = """<UAC_Trigger>
A Windows UAC prompt is blocking the screen. Based on your previous actions, do you want to allow this?
Respond with "action": [{"type": "hotkey", "value": "alt+y"}] to accept or "action": [{"type": "hotkey", "value": "alt+n"}] to decline. Skip visual_decision analysis.
</UAC_Trigger>"""
            
            elif not uac_detected:
                image_sent = annotated_image_base64 is not None
            
                if image_sent:
                    print(f"✅ Image captured - annotated: {len(annotated_image_base64)} chars")
                else:
                    print("❌ NO IMAGE - annotated image is None!")
            
                # Wrap element tree in proper tags
                formatted_element_tree = f"<element_tree>\n{element_tree_text}\n</element_tree>"

                # Fetch domain-specific knowledge if available
                domain_block = self.skills.get_knowledge(
                    self.scanner.application_name,
                    element_tree_text
                )
            
            # Construct user message based on iteration
            if uac_detected:
                todo_list = ""  # Not needed for UAC prompt
            elif is_first_iteration:
                # First iteration - user_request + element tree only.
                # ToDo creation and timing are governed by <todo_capability> in the
                # system prompt (flexible: iteration 1 by default, may create later),
                # so no hard-coded todo rules are injected here.
                user_message = f"""<user_request>
{task}
</user_request>
"""
                # Inject domain knowledge if available
                if domain_block:
                    user_message += f"\n{domain_block}\n"

                user_message += f"\n{formatted_element_tree}"
            else:
                # Fetch fresh todo from file system
                todo_list = self._read_todo_from_file()
                
                # Fetch fresh scratchpad entries from file system
                scratchpad_content = self._read_scratchpad_from_file()
                
                # Check if last_response contains web tool result
                web_tool_response = ""
                try:
                    last_response_data = json.loads(last_response)
                    web_results_list = []
                    
                    # Check for single tool action
                    if (last_response_data.get("action") == "tool" and 
                        last_response_data.get("tool") == "web" and 
                        "result" in last_response_data):
                        web_results_list.append(last_response_data["result"])
                        # Remove the web result from last_response to avoid duplication
                        del last_response_data["result"]
                        last_response = json.dumps(last_response_data, indent=2)
                    
                    # Check for multiple actions containing web tool (collect ALL results)
                    elif last_response_data.get("action") == "multiple" and "results" in last_response_data:
                        results = last_response_data["results"]
                        for idx, result in enumerate(results):
                            if (result.get("action") == "tool" and 
                                result.get("tool") == "web" and 
                                "result" in result):
                                web_results_list.append(result["result"])
                                # Remove the web result from this specific result
                                del results[idx]["result"]
                        last_response = json.dumps(last_response_data, indent=2)
                    
                    # Combine all web results with newlines, wrap in <tool> tag
                    if web_results_list:
                        web_tool_response = "<tool>\n" + "\n".join(web_results_list) + "\n</tool>"
                except:
                    pass
                
                # Subsequent iterations - include last_response, todo_list.
                # On a RESUMED session, mark the request as <updated_user_request>
                # so the agent knows this is a continuation of the same session
                # (its prior steps are already in the replayed history above).
                request_tag = "updated_user_request" if is_resumed else "user_request"
                user_message = f"""<{request_tag}>
{task}
</{request_tag}>

<last_response>
{last_response}
</last_response>"""

                if cli_await_result:
                    cli_completed = cli_await_result.get("completed", [])
                    import json as _json
                    cli_json = _json.dumps({"cli": cli_completed}, indent=2, ensure_ascii=False)
                    user_message = f"""<cli_agent>
{cli_json}
</cli_agent>

<critical> No image or element tree is provided. Properly understand all CLI output in this iteration.\n 1. In the scratchpad, clearly mention what has been done so far, the macOS actions you are currently performing, and what is left to complete later. Clearly state where you left off and that the remaining steps will be performed from this point at a later time.\n 2. Plan your next steps accordingly. </critical>

<user_request>
{task}
</user_request>

<todo_list>
{todo_list}
</todo_list>"""

                    if scratchpad_content:
                        user_message += f"""

<scratchpad>
{scratchpad_content}
</scratchpad>"""
                    else:
                        user_message += """

<scratchpad>none</scratchpad>"""

                    self.controller.clear_cli_agent_results()
                    
                    image_sent = False
                    annotated_image_base64 = None
                    
                    # Clear flag after consumption
                    cli_await_result = None
                elif pending_web_response:
                    user_message = f"""<critical>
No image and element tree provided. Focus on digesting the web response below.
1. Analyze thoroughly - extract all relevant data (numbers, dates, names, URLs, prices, etc.)
2. Save ALL important findings to scratchpad in this step's action.
</critical>

{pending_web_response}

<user_request>
{task}
</user_request>

<last_response>
{last_response}
</last_response>

<todo_list>
{todo_list}
</todo_list>"""

                    if scratchpad_content:
                        user_message += f"""

<scratchpad>
{scratchpad_content}
</scratchpad>"""
                    else:
                        user_message += """

<scratchpad>none</scratchpad>"""

                    image_sent = False
                    annotated_image_base64 = None
                    
                    # Clear flag after consumption
                    pending_web_response = None
                else:
                    # Normal iteration - include full context
                    cli_status = self.controller.get_cli_agent_status()
                    if len(cli_status["completed"]) > 0:
                        import json as _json
                        cli_json = _json.dumps({"cli": cli_status["completed"]}, indent=2, ensure_ascii=False)
                        user_message += f"""

<cli_agent>
{cli_json}
</cli_agent>"""
                        self.controller.clear_cli_agent_results()

                    # Add web tool response if present (Note is already embedded in each result)
                    if web_tool_response:
                        user_message += f"\n\n{web_tool_response}"
                    user_message += f"""

<todo_list>
{todo_list}
</todo_list>"""

                    # Always add scratchpad tag (with content or "none")
                    if scratchpad_content:
                        user_message += f"""

<scratchpad>
{scratchpad_content}
</scratchpad>"""
                    else:
                        user_message += f"""

<scratchpad>none</scratchpad>"""

                    # Inject domain knowledge if available
                    if domain_block:
                        user_message += f"\n\n{domain_block}"

                    user_message += f"""

{formatted_element_tree}"""
                    
                   # Add image tag if image is provided
                    if image_sent:
                        user_message += "\n\n<image>Annotated screenshot with bounding boxes</image>"
                        user_message += "\n\n<critical>Pure vision first: decide which element to interact with from the screenshot alone, then refer to <element_tree> for its [id].</critical>"
            
            # Build the API messages as a real interleaved dialogue:
            #   system, then for each past step:  assistant(step) -> user(tool_response),
            #   and finally the current user message (live screen + <last_response>).
            #
            # Sliding-window trim: only the MOST RECENT past step keeps its full
            # thinking/eval/action; every older step is trimmed to
            # decision/memory/next_goal (_trim_history_entry), so the agent always sees
            # its latest reasoning in full and just the outline of older steps.
            # Tool results stay on the USER side (correct role -> clean eval, no schema
            # leakage). The most recent step's result is NOT re-emitted here — it already
            # rides in the current user message as <last_response>.
            messages = [
                {"role": "system", "content": self.system_prompt}
            ]

            n_hist = len(assistant_messages)
            for i, step_msg in enumerate(assistant_messages):
                is_recent = (i == n_hist - 1)
                content = step_msg if is_recent else self._trim_history_entry(step_msg)
                # Reinforce the objective by prepending the task to step 1.
                if i == 0 and not is_first_iteration:
                    content = f"<User_Task>\n{task}\n</User_Task>\n\n{content}"
                messages.append({"role": "assistant", "content": content})

                # Interleave this step's tool result as its own user turn — except the
                # most recent, whose result is carried by the current user message below.
                if not is_recent:
                    tr = tool_responses[i] if i < len(tool_responses) else None
                    if tr:
                        messages.append({"role": "user", "content": f"<tool_response>\n{tr}\n</tool_response>"})

            # Current user message (live screen + most recent <last_response>).
            # The system prompt is added exactly once (above) — the resumable seed
            # never carries a system prompt, so a continued session can't double it.
            messages.append({"role": "user", "content": user_message})

            # Capture the exact payload for the debug memory log (the last
            # iteration's value is retained — i.e. the agent's final true memory).
            self.last_messages = messages

            # Apply prompt caching for OpenRouter/Anthropic (explicit cache_control).
            # messages[-1] = current user message (dynamic, never cache)
            # messages[-2] = most recent assistant step (full now -> trimmed next step)
            # messages[-3] = last frozen turn (older tool_response/assistant) — safe to cache
            # Starts once len >= 4.
            if self.llm_manager.get_provider_name() in ("openrouter", "anthropic") and len(messages) >= 4:
                cache_idx = len(messages) - 3
                content = messages[cache_idx]["content"]
                if isinstance(content, str):
                    messages[cache_idx]["content"] = [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"}
                        }
                    ]
            
            try:
                # Make API request through LLM Manager
                if image_sent:
                    print("📸 Screenshot sent to LLM (annotated)")
                
                # Check Stop BEFORE LLM
                if self.stop_event and self.stop_event.is_set():
                    final_status, final_message = "incomplete", "Stopped by user"
                    break

                # Get raw response from LLM - pass annotated image
                raw_response = self.llm_manager.send_request(messages, annotated_image_base64)
                
                # CRITICAL Check Stop AFTER LLM (discards result if stopped while waiting)
                if self.stop_event and self.stop_event.is_set():
                    print("\n🛑 Agent stopped by user (response discarded).")
                    self.controller.controller_service.release_all_inputs()
                    final_status, final_message = "incomplete", "Stopped by user"
                    break
                    
                print("✓ LLM response received")

                # Save raw response before any parsing (for debugging)
                self._save_raw_response(raw_response, step_number)
                
                # Normalize the response to ensure consistent JSON format
                success, normalized_json, failed_raw = AgentResponseFormatter.normalize_response(raw_response)
                
                # If JSON parse failed, discard response and retry with fresh scan
                if not success:
                    json_fail_count += 1
                    print(f"⚠️ JSON parse failed ({json_fail_count}/3). Discarding and retrying with fresh scan...")
                    
                    if json_fail_count >= 3:
                        print("❌ JSON parsing failed 3 consecutive times. Exiting agent.")
                        final_status, final_message = "incomplete", "Could not parse a valid model response (3 consecutive failures)"
                        break
                    
                    # Rewind step number so next iteration retries the same step
                    step_number -= 1
                    continue
                
                # Reset consecutive JSON fail counter on success
                json_fail_count = 0
                
                # Save a faithful snapshot of the EXACT payload sent this step (system +
                # interleaved assistant/tool-response turns + current user message) plus
                # this step's response — a true peek into agent memory.
                self._save_conversation(messages, normalized_json, image_sent, step_number)

                # Store the FULL response (thinking/eval/action included). The sliding-window
                # trim at send time keeps it full only while it's the most recent step, then
                # trims thinking/eval/action once it's older.
                assistant_messages.append(f"<Step_no={step_number} />\n{normalized_json}")
                # Keep tool_responses aligned 1:1 with assistant_messages; filled in after the
                # action runs below (stays None for a step that takes no action).
                tool_responses.append(None)
                
                # Format the response with emojis for console output (terminal: include action block)
                formatted_response = AgentResponseFormatter.format_response(normalized_json, include_action=True)
                print(formatted_response)
                
                # Send to frontend if callback exists (omit action from stream)
                if self.text_callback:
                    self.text_callback(AgentResponseFormatter.format_response(normalized_json, include_action=False))

                # Tool-flow chain: the packet arrived — tick "packet received" and hand
                # the frontend this turn's tools, read straight from the action block.
                self._emit_flow("received", {"tools": AgentResponseFormatter.extract_tools(normalized_json)})

                # Parse the normalized JSON to extract fields and check for done
                try:
                    agent_response = json.loads(normalized_json)
                    if agent_response:
                        
                        # Execute actions if present
                        if "action" in agent_response and agent_response["action"]:
                            # Check Stop BEFORE Action
                            if self.stop_event and self.stop_event.is_set():
                                final_status, final_message = "incomplete", "Stopped by user"
                                break
                                
                            # Execute the action
                            print("\n⚡ Executing action...")
                            
                            # Pass elements mapping to controller
                            elements_mapping = self.scanner.get_elements_mapping()
                            self.controller.set_elements(elements_mapping, self.scanner.application_name)
                            
                            # Send action to controller
                            action_result = self.controller.route_action(agent_response["action"])
                            
                            # Check if action was stopped mid-execution
                            if action_result.get("status") == "stopped":
                                print("\n🛑 Agent stopped by user (action interrupted).")
                                final_status, final_message = "incomplete", "Stopped by user"
                                break
                            
                            # Check if cli_await was triggered — store for next iteration's light message
                            if action_result.get("action") == "cli_await":
                                cli_await_result = action_result
                                print(f"⏸️ CLI await complete: {len(action_result.get('completed', []))} task(s) collected")
                            else:
                                cli_await_result = None
                            
                            # Check if web tool was used — store for light digest iteration
                            web_results_list = []
                            if action_result.get("tool") == "web" and "result" in action_result:
                                web_results_list.append(action_result["result"])
                                del action_result["result"]
                            elif action_result.get("action") == "multiple" and "results" in action_result:
                                for idx, result in enumerate(action_result["results"]):
                                    if result.get("tool") == "web" and "result" in result:
                                        web_results_list.append(result["result"])
                                        del action_result["results"][idx]["result"]
                            
                            if web_results_list:
                                pending_web_response = "<tool>\n" + "\n".join(web_results_list) + "\n</tool>"
                                print(f"🌐 Web results captured for digest iteration")
                            else:
                                pending_web_response = None
                            
                            # Check if task completed (done action was executed)
                            if action_result.get("action") == "done":
                                print(f"\n🎉 Task Complete: {action_result.get('summary', 'Task completed')}")
                                print("✅ Agent has finished all tasks. Exiting loop.")
                                final_status = "success"
                                final_message = action_result.get("summary", "Task completed")
                                break
                            
                            # Store the action result as last_response
                            last_response = json.dumps(action_result, indent=2)

                            # Preserve this step's tool result on the USER side: stash the
                            # (web-compacted) result in tool_responses, aligned with this
                            # step's assistant entry, so later iterations replay it as a
                            # user turn ("I clicked there -> result"). Web results are
                            # compacted to a scratchpad pointer by _tool_response_for_memory.
                            if tool_responses:
                                tool_responses[-1] = json.dumps(
                                    self._tool_response_for_memory(action_result),
                                    indent=2, ensure_ascii=False
                                )

                            # Wait before next scan (default 3 seconds, unless wait action was used)
                            wait_time = 3.0  # Default wait
                            if action_result.get("tool") == "wait":
                                # If wait was explicitly called, use that duration
                                wait_time = action_result.get("duration", 3.0)
                            elif action_result.get("action") == "multiple":
                                # Check if wait was in multiple actions
                                for result in action_result.get("results", []):
                                    if result.get("tool") == "wait":
                                        wait_time = result.get("duration", 3.0)
                                        break
                            
                            print(f"⏳ Waiting {wait_time}s before next scan...")
                            elapsed = 0.0
                            while elapsed < wait_time:
                                if self.stop_event and self.stop_event.is_set():
                                    break
                                time.sleep(min(0.5, wait_time - elapsed))
                                elapsed += 0.5
                            
                            # Print action result
                            if action_result.get("status") == "success":
                                print(f"✓ Action executed successfully")
                                
                                # Check if this was a todo creation
                                if action_result.get("action") == "todo_created":
                                    print("📋 Todo list created")
                                        
                                # Check if this was a todo update
                                elif action_result.get("action") == "todo_updated":
                                    print("✓ Todo task marked complete")
                            else:
                                print(f"⚠ Action result: {action_result.get('message', 'Unknown error')}")
                        
                        # Mark first iteration as done
                        is_first_iteration = False
                        
                except Exception as e:
                    print(f"⚠ Error processing action: {str(e)}")
                    # Even on error, continue the loop
                    last_response = json.dumps({"status": "error", "message": str(e)})
                    is_first_iteration = False
                
            except Exception as e:
                error_msg = f"❌ Error processing request: {str(e)}"
                print(error_msg)
                # On critical error, record it and break the loop so the caller
                # can report the failure instead of a false "done".
                final_status, final_message = "error", str(e)
                break

        # Cleanup: Stop CLI agent subprocess if running
        self.controller.stop_cli_agent()

        # tool-flow chain: if the run didn't finish cleanly, cap it with a "!" drop.
        if final_status == "error":
            self._emit_flow("error", {"text": "llm service not responding"})
        elif final_status == "incomplete" and final_message and "stop" in final_message.lower():
            self._emit_flow("error", {"text": "agent interrupted"})
        self._emit_flow("run_end")   # tool-flow chain: run finished

        # Expose the final conversation lists so the conversation service (NOT the
        # agent) can optimize + persist them after the run. Memory management is
        # kept entirely out of the agent. Return shape is unchanged.
        self.assistant_messages = assistant_messages
        self.tool_responses = tool_responses

        return {"status": final_status, "message": final_message}