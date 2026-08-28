# Copyright 2026 Cursortouch — Auto-Use

import json
import requests
import base64
from typing import Dict, Any, Optional

from .view import get_reasoning_params
from .. import LLM_HTTP_TIMEOUT

def _is_gemini_route(model: str) -> bool:
    """True when OpenRouter will hand this request to Google's backend, where a
    tool result stops being a string and becomes a protobuf Struct."""
    name = str(model or "").lower()
    return name.startswith("google/") or "gemini" in name


def _wrap_for_gemini(content) -> str:
    """Carry one tool result to a Gemini route as a SINGLE JSON object.

    Gemini's functionResponse body is a Struct, so the text we send is coerced
    into JSON on the way in. A plain envelope survives that trip only while it
    contains nothing JSON-shaped: an envelope whose output body merely
    CONTAINED braces reached the model as scraped fragments instead of the
    text. Wrapping the whole envelope as one JSON object makes the coercion
    lossless: the Struct holds a single string field, and the model receives
    the envelope byte-for-byte. Only Google routes are wrapped - every other
    model on OpenRouter keeps the raw envelope."""
    text = content if isinstance(content, str) else str(content or "")
    return json.dumps({"tool_response": text}, ensure_ascii=False)


class OpenRouterProvider:
    """OpenRouter API provider for LLM interactions"""

    def __init__(self, api_key: str, cli_agent: bool = False, model_info: dict = None, tools: list = None):
        self.api_key = api_key
        self.api_url = "https://openrouter.ai/api/v1/chat/completions"
        self.cli_agent = cli_agent
        self.model_info = model_info or {}
        # Native tool calling: OpenAI-format function tools - the model's
        # output contract. None only for mode="text" (plain prose).
        self.tools = tools or None

    def send_request(self, messages: list, model: str, annotated_screenshot_base64: Optional[str] = None) -> Dict[str, Any]:
        """Send request to OpenRouter API"""

        # A `role: "tool"` message must carry a PLAIN STRING here. The agent
        # loop marks its newest persistent turn with a parts-array
        # cache_control breakpoint, and in a native transcript that turn is a
        # tool result - but OpenRouter forwards messages verbatim to whatever
        # backend it routes to, and a tool message whose content is an array
        # carrying the Anthropic-only `cache_control` key is not something a
        # non-Anthropic backend (Gemini, GPT, ...) is obliged to read. Flatten
        # it back to text; the breakpoint is dropped for tool turns on this
        # provider, which costs some cache reuse and buys the model actually
        # seeing its tool output.
        # Rebuilt, not mutated in place: the caller's message dicts are shared
        # with the agent loop's own history.
        #
        # The same pass translates the transcript's private `provider_meta`
        # into this dialect: a reasoning model's own blocks go back on the
        # assistant turn as `reasoning_details`. Gemini 3 (and every other
        # reasoning model routed here) binds a function RESULT to the call
        # through the encrypted signature inside those blocks - strip them and
        # the model either rejects the turn or reads its tool output as empty.
        # The key itself is ALWAYS removed, tagged for this provider or not -
        # it is ours, not OpenRouter's, and an unknown message key is a 400
        # risk. Tool results bound for a Gemini route are wrapped as one JSON
        # object (see _wrap_for_gemini).
        gemini_route = _is_gemini_route(model)
        prepared = []
        for msg in messages:
            if msg.get("role") == "tool":
                if isinstance(msg.get("content"), list):
                    msg = {**msg, "content": "\n".join(
                        part.get("text", "") for part in msg["content"]
                        if isinstance(part, dict) and part.get("type") == "text")}
                if gemini_route:
                    msg = {**msg, "content": _wrap_for_gemini(msg.get("content"))}
            meta = msg.get("provider_meta")
            if meta is not None:
                msg = {k: v for k, v in msg.items() if k != "provider_meta"}
                if (msg.get("role") == "assistant" and isinstance(meta, dict)
                        and meta.get("provider") == "openrouter"
                        and meta.get("reasoning_details")):
                    msg["reasoning_details"] = meta["reasoning_details"]
            prepared.append(msg)
        messages = prepared

        # If screenshot is provided and NOT cli_agent, modify the user message to include the annotated image
        if annotated_screenshot_base64 and not self.cli_agent and len(messages) > 1:
            user_message = messages[-1]["content"]
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
        

        data = {
            "model": model,
            "messages": messages,
            # 0.2, not 0.5: this loop wants the confident, repeatable choice
            # every step, and its output is tool-call JSON rather than prose.
            # Models that no longer accept the knob at all (the Anthropic
            # entries) are unaffected — OpenRouter drops parameters a route
            # does not support rather than erroring, verified against
            # claude-sonnet-5.
            "temperature": 0.2,
            "max_tokens": 10000,
            "route": "fallback",
            "seed": 42,
        }
        # How hard the model thinks, per view.py's per-model table. Carried in
        # OpenRouter's unified `reasoning` field, which it translates to each
        # backend's own control. Left off for a model with no level set and
        # for any hand-typed name, which then keeps its published default.
        data.update(get_reasoning_params(model))

        # No tools (mode="text") -> plain text: omit the tool params entirely.
        if self.tools:
            data["tools"] = self.tools
            # "required" - every turn MUST call at least one tool. A text-only
            # turn is never a valid step in this loop: every step routes an
            # action and even termination is a tool (`done` carries the final
            # summary in its value), so there is no "final answer on the text
            # channel" ending. Forcing makes the text-only failure mode
            # unrepresentable instead of salvageable; prose still rides the
            # text channel alongside the calls. OpenRouter passes tool_choice
            # through to the backend and upstream support is uneven, so the
            # no-tool-called repair turn stays as a backstop.
            data["tool_choice"] = "required"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        try:
            response = requests.post(self.api_url, json=data, headers=headers, timeout=LLM_HTTP_TIMEOUT)
            response.raise_for_status()
            result = response.json()
            if self.tools:
                return _normalize_tool_response(result)
            return result
        except requests.exceptions.RequestException as e:
            error_msg = f"OpenRouter API request failed: {str(e)}"
            if hasattr(e, 'response') and e.response is not None:
                error_msg += f"\nResponse Body: {e.response.text}"
            raise Exception(error_msg)


def _normalize_tool_response(result: dict) -> dict:
    """Normalize an OpenAI-format tool-call response: keep the usual
    choices/message shape but replace tool_calls with
    [{"id", "name", "arguments": dict}] (arguments JSON-decoded). The `id` is
    load-bearing - the loop echoes it back so each tool result is matched to
    the call that produced it.

    `reasoning_details` is preserved the same way, as `provider_meta`: it
    carries the reasoning blocks (including Gemini 3's encrypted thought
    signature, keyed to the first tool call's id) that this turn must be
    replayed with. Dropping it here is what leaves the model unable to bind its
    tool results to the calls that asked for them."""
    message = ((result.get("choices") or [{}])[0].get("message")) or {}
    calls = []
    for i, tc in enumerate(message.get("tool_calls") or []):
        fn = (tc or {}).get("function") or {}
        raw_args = fn.get("arguments")
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
            "id": str((tc or {}).get("id") or "") or f"call_{i}",
            "name": fn.get("name") or "",
            "arguments": args,
        })
    out = {"content": message.get("content") or "", "tool_calls": calls}
    details = message.get("reasoning_details")
    if isinstance(details, list) and details:
        out["provider_meta"] = {"provider": "openrouter", "reasoning_details": details}
    return {
        "choices": [{"message": out}],
        "usage": result.get("usage", {}),
    }