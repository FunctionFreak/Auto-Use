# Copyright 2026 Ashish Yadav — Auto-Use

import json
from typing import Dict, Any, Optional

from openai import OpenAI

from .view import get_reasoning_effort

class OpenAIProvider:
    """OpenAI API provider for LLM interactions"""
    
    def __init__(self, api_key: str, cli_agent: bool = False, tools: list = None):
        self.client = OpenAI(api_key=api_key)
        self.cli_agent = cli_agent
        # Native tool calling: OpenAI-format function tools — the model's
        # output contract. None only for mode="text" (plain prose).
        self.tools = tools or None
        
    def send_request(self, messages: list, model: str, annotated_screenshot_base64: Optional[str] = None) -> Dict[str, Any]:
        """Send request to OpenAI API"""
        
        # If screenshot is provided and NOT cli_agent, modify the user message to include the annotated image
        if annotated_screenshot_base64 and not self.cli_agent and len(messages) > 1:
            user_message = messages[-1]["content"]
            
            # Handle case where content might already be a list
            if isinstance(user_message, list):
                # Extract text from existing list structure
                text_content = ""
                for item in user_message:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_content = item.get("text", "")
                        break
                user_message = text_content
            
            messages[-1]["content"] = [
                {
                    "type": "text",
                    "text": user_message
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{annotated_screenshot_base64}"
                    }
                }
            ]
        
        # Prepare API call parameters
        params = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": 4000,
            "verbosity": "medium"  # Set verbosity to medium
        }
        # How hard the model thinks, per view.py's table. Kept out of the dict
        # above because it is per-model, not a constant: an unregistered model
        # contributes nothing and keeps OpenAI's default. No sampling params
        # are ever added here — GPT-5.6 rejects temperature/top_p/seed.
        params.update(get_reasoning_effort(model))


        # No tools (mode="text") -> plain text: omit the tool params entirely.
        if self.tools:
            params["tools"] = self.tools
            # "required" - every turn must call at least one tool; a text-only
            # turn is never a valid step (even termination is the `exit`
            # tool). Canonical rationale: see openrouter/service.py.
            params["tool_choice"] = "required"

        try:
            response = self.client.chat.completions.create(**params)

            # Return in the same format as other providers
            msg = response.choices[0].message
            message: Dict[str, Any] = {"content": msg.content or ""}
            if self.tools:
                # SDK tool-call objects -> [{"id", "name", "arguments": dict}]
                # (arguments arrive as a JSON string; malformed -> {}). The id
                # is echoed back by the loop to match each result to its call.
                calls = []
                for i, tc in enumerate(msg.tool_calls or []):
                    fn = tc.function
                    try:
                        args = json.loads(fn.arguments or "{}")
                    except Exception:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    calls.append({
                        "id": str(getattr(tc, "id", "") or "") or f"call_{i}",
                        "name": fn.name or "",
                        "arguments": args,
                    })
                message["tool_calls"] = calls
            usage = getattr(response, "usage", None)
            return {
                "choices": [{
                    "message": message
                }],
                "usage": {
                    "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                    "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
                    "total_tokens": getattr(usage, "total_tokens", 0) or 0,
                } if usage else {},
            }
        except Exception as e:
            error_msg = f"OpenAI API request failed: {str(e)}"
            raise Exception(error_msg)