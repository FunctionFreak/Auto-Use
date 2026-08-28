# Copyright 2026 Cursortouch — Auto-Use

import json
import requests
from typing import Dict, Any, Optional

from .view import get_model_info, get_reasoning_params
from .. import LLM_HTTP_TIMEOUT


class PerplexityProvider:
    """Perplexity Agent API provider for LLM interactions"""

    def __init__(self, api_key: str, cli_agent: bool = False, model_info: dict = None, tools: list = None):
        self.api_key = api_key
        self.api_url = "https://api.perplexity.ai/v1/agent"
        self.cli_agent = cli_agent
        self.model_info = model_info or {}
        # Native tool calling: Responses-style flat function tools ({type,
        # name, description, parameters}) - the model's output contract.
        # None only for mode="text" (plain prose).
        self.tools = tools or None

    def send_request(self, messages: list, model: str, annotated_screenshot_base64: Optional[str] = None) -> Dict[str, Any]:
        """Send request to Perplexity Agent API"""
        
        # Separate system prompt from conversation messages
        instructions = None
        input_messages = []
        
        for msg in messages:
            if msg["role"] == "system":
                content = msg["content"]
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            instructions = item.get("text", "")
                            break
                else:
                    instructions = content
            elif msg["role"] == "tool":
                # NATIVE TRANSCRIPT: the Responses dialect carries a tool
                # result as its own flat output item keyed by call_id.
                input_messages.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id") or "",
                    "output": self._extract_text(msg.get("content")),
                })
            elif msg["role"] == "assistant":
                text = self._extract_text(msg["content"])
                if text.strip():
                    input_messages.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
                # ...and one function_call item per tool call the model made,
                # so it sees its OWN calls replayed in the canonical shape.
                for tc in (msg.get("tool_calls") or []):
                    fn = (tc or {}).get("function") or {}
                    raw = fn.get("arguments")
                    input_messages.append({
                        "type": "function_call",
                        "call_id": (tc or {}).get("id") or "",
                        "name": fn.get("name") or "",
                        "arguments": raw if isinstance(raw, str) else json.dumps(raw or {}),
                    })
            elif msg["role"] == "user":
                text = self._extract_text(msg["content"])
                input_messages.append({"role": "user", "content": [{"type": "input_text", "text": text}]})

        # Add screenshot to last user message if provided and NOT cli_agent
        if annotated_screenshot_base64 and not self.cli_agent and len(input_messages) > 0:
            last = input_messages[-1]
            # `last` may be a flat function_call* item with no "role" key.
            if last.get("role") == "user":
                last["content"].append({
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{annotated_screenshot_base64}"
                })
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": model,
            "input": input_messages,
            "max_output_tokens": 10000,
        }

        # How hard the model thinks, per view.py's per-model table, carried as
        # the Agent API's reasoning config. Omitted for a hand-typed model
        # name, which then keeps Perplexity's own default.
        data.update(get_reasoning_params(model))
        
        if instructions:
            data["instructions"] = instructions

        # No tools (mode="text") -> plain text: omit the tool params entirely.
        if self.tools:
            data["tools"] = self.tools
            # "required" - every turn must call at least one tool; a text-only
            # turn is never a valid step (even termination is the `done`
            # tool). Responses-dialect value; if Perplexity's validator ever
            # rejects it, the request 400s loudly - flip back to "auto".
            # Canonical rationale: see openrouter/service.py.
            data["tool_choice"] = "required"

        try:
            response = requests.post(self.api_url, json=data, headers=headers, timeout=LLM_HTTP_TIMEOUT)
            response.raise_for_status()
            result = response.json()

            # Normalize to choices[0].message.content format
            text_content = ""
            calls = []
            for output_item in result.get("output", []):
                if output_item.get("type") == "message":
                    for content_block in output_item.get("content", []):
                        if content_block.get("type") == "output_text" and not text_content:
                            text_content = content_block.get("text", "")
                elif self.tools and output_item.get("type") == "function_call":
                    # Responses dialect: arguments is a JSON string
                    # (malformed -> {}).
                    raw_args = output_item.get("arguments")
                    if isinstance(raw_args, dict):
                        args = raw_args
                    else:
                        try:
                            args = json.loads(raw_args or "{}")
                        except Exception:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    calls.append({
                        "id": str(output_item.get("call_id") or output_item.get("id") or "")
                              or f"call_{len(calls)}",
                        "name": output_item.get("name") or "",
                        "arguments": args,
                    })

            message: Dict[str, Any] = {"content": text_content}
            if self.tools:
                message["tool_calls"] = calls
            return {
                "choices": [{
                    "message": message
                }],
                "usage": result.get("usage", {}),
            }
            
        except requests.exceptions.RequestException as e:
            error_msg = f"Perplexity API request failed: {str(e)}"
            if hasattr(e, 'response') and e.response is not None:
                error_msg += f"\nResponse Body: {e.response.text}"
            raise Exception(error_msg)
    
    @staticmethod
    def _extract_text(content) -> str:
        """Extract text from message content (string or list format with cache_control)"""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    return item.get("text", "")
            return str(content)
        return str(content)