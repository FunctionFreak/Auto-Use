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

import copy
import os
import time
from typing import Optional

from dotenv import load_dotenv

from .openrouter.service import OpenRouterProvider
from .openrouter.view import get_model_info as get_openrouter_model_info
from .groq.service import GroqProvider
from .groq.view import get_model_info as get_groq_model_info
from .openai.service import OpenAIProvider
from .openai.view import get_model_info as get_openai_model_info
from .anthropic.service import AnthropicProvider
from .anthropic.view import get_model_info as get_anthropic_model_info
from .google.service import GoogleProvider
from .google.view import get_model_info as get_google_model_info
from .perplexity.service import PerplexityProvider
from .perplexity.view import get_model_info as get_perplexity_model_info

# Load environment variables
load_dotenv()

# CLI Agent Output Schema (simpler - text only, no vision)
# Uses anyOf discriminated union — each action type carries only its own fields
# Grouped by field signature to minimize anyOf branches (6 instead of 11)
CLI_AGENT_SCHEMA = {
    "name": "cli_agent_response",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "thinking": {"type": "string"},
            "memory": {"type": "string"},
            "next_goal": {"type": "string"},
            "action": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "shell"},
                                "command": {"type": "string"},
                                "input": {"type": "string"}
                            },
                            "required": ["type", "command", "input"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "view"},
                                "path": {"type": "string"},
                                "start": {"type": "integer"},
                                "end": {"type": "integer"}
                            },
                            "required": ["type", "path", "start", "end"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "grep"},
                                "pattern": {"type": "string"},
                                "path": {"type": "string"},
                                "glob": {"type": "string"},
                                "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"]},
                                "case_insensitive": {"type": "boolean"},
                                "head_limit": {"type": "integer"},
                                "context": {"type": "integer"}
                            },
                            "required": ["type", "pattern", "path", "glob", "output_mode", "case_insensitive", "head_limit", "context"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "glob"},
                                "pattern": {"type": "string"},
                                "path": {"type": "string"},
                                "head_limit": {"type": "integer"}
                            },
                            "required": ["type", "pattern", "path", "head_limit"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "write"},
                                "path": {"type": "string"},
                                "line": {"type": "integer"},
                                "content": {"type": "string"}
                            },
                            "required": ["type", "path", "line", "content"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "replace"},
                                "path": {"type": "string"},
                                "line": {"type": "integer"},
                                "old_block": {"type": "string"},
                                "new_block": {"type": "string"}
                            },
                            "required": ["type", "path", "line", "old_block", "new_block"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "plan"},
                                "op": {"type": "string", "enum": ["set", "add", "edit"]},
                                "from": {"type": "integer"},
                                "to": {"type": "integer"},
                                "value": {"type": "string"}
                            },
                            "required": ["type", "op", "from", "to", "value"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["web", "todo_list", "update_todo", "wait", "scratchpad", "minion", "exit"]},
                                "value": {"type": "string"}
                            },
                            "required": ["type", "value"],
                            "additionalProperties": False
                        }
                    ]
                }
            }
        },
        "required": ["thinking", "memory", "next_goal", "action"],
        "additionalProperties": False
    }
}

# Minion Agent Output Schema — read-only scout sub-agent.
# Drops write/replace and the value-field actions web/todo_list/update_todo/wait/minion.
# Allowed: shell, view, grep, glob, scratchpad, exit.
# Output blocks differ from CLI agent: thinking, memory, next_goal, action.
MINION_SCHEMA = {
    "name": "minion_response",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "thinking": {"type": "string"},
            "memory": {"type": "string"},
            "next_goal": {"type": "string"},
            "action": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "shell"},
                                "command": {"type": "string"},
                                "input": {"type": "string"}
                            },
                            "required": ["type", "command", "input"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "view"},
                                "path": {"type": "string"},
                                "start": {"type": "integer"},
                                "end": {"type": "integer"}
                            },
                            "required": ["type", "path", "start", "end"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "grep"},
                                "pattern": {"type": "string"},
                                "path": {"type": "string"},
                                "glob": {"type": "string"},
                                "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"]},
                                "case_insensitive": {"type": "boolean"},
                                "head_limit": {"type": "integer"},
                                "context": {"type": "integer"}
                            },
                            "required": ["type", "pattern", "path", "glob", "output_mode", "case_insensitive", "head_limit", "context"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "glob"},
                                "pattern": {"type": "string"},
                                "path": {"type": "string"},
                                "head_limit": {"type": "integer"}
                            },
                            "required": ["type", "pattern", "path", "head_limit"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["scratchpad", "exit"]},
                                "value": {"type": "string"}
                            },
                            "required": ["type", "value"],
                            "additionalProperties": False
                        }
                    ]
                }
            }
        },
        "required": ["thinking", "memory", "next_goal", "action"],
        "additionalProperties": False
    }
}

# Main Agent Output Schema (with vision support)
# Uses anyOf discriminated union — each action type carries only its own fields (no nulls, no waste)
# Grouped by field signature to minimize anyOf branches (6 instead of 18)
AGENT_OUTPUT_SCHEMA = {
    "name": "agent_response",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "thinking": {"type": "string"},
            "memory": {"type": "string"},
            "next_goal": {"type": "string"},
            "action": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["left_click", "right_click", "screenshot"]},
                                "id": {"type": "integer"},
                                "clicks": {"type": "integer"}
                            },
                            "required": ["type", "id", "clicks"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "input"},
                                "id": {"type": "integer"},
                                "text": {"type": "string"}
                            },
                            "required": ["type", "id", "text"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "typewrite"},
                                "text": {"type": "string"}
                            },
                            "required": ["type", "text"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "scroll"},
                                "id": {"type": "integer"},
                                "direction": {"type": "string"}
                            },
                            "required": ["type", "id", "direction"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["hotkey", "open_app", "wait", "web", "shell", "cli_agent", "cli_await", "todo_list", "update_todo", "scratchpad", "done"]},
                                "value": {"type": "string"}
                            },
                            "required": ["type", "value"],
                            "additionalProperties": False
                        }
                    ]
                }
            }
        },
        "required": ["thinking", "memory", "next_goal", "action"],
        "additionalProperties": False
    }
}

# Fast-mode Main Agent Output Schema (speed="fast") — same tools, no reasoning blocks.
# Only memory/next_goal/action so the model spends output tokens on the action, not
# thinking/eval/decision (pairs with fast_system_prompt.md). The action block is
# deep-copied from AGENT_OUTPUT_SCHEMA so the tool set can never drift between modes.
# properties and required MUST mirror each other — OpenAI strict mode rejects otherwise.
FAST_AGENT_SCHEMA = {
    "name": "agent_response_fast",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "next_goal": {"type": "string"},
            "memory": {"type": "string"},
            "action": copy.deepcopy(AGENT_OUTPUT_SCHEMA["schema"]["properties"]["action"]),
        },
        "required": ["next_goal", "memory", "action"],
        "additionalProperties": False
    }
}

_TLS_PROBED = False


def _ensure_tls_works() -> None:
    """If this machine runs HTTPS interception (antivirus / corporate proxy)
    with a certificate Python can't validate — which breaks EVERY https call,
    including the LLM providers — disable certificate verification process-wide
    (requests + httpx) so the agent can actually reach its model. Without this
    the providers get an SSL error, return nothing useful, and the agent
    "finishes" without doing anything.

    Secure by default: it probes once and only disables verification when the
    probe fails with a genuine CERTIFICATE error (interception), never on a
    plain network error. Idempotent.
    """
    global _TLS_PROBED
    if _TLS_PROBED:
        return
    _TLS_PROBED = True
    import ssl
    import sys
    try:
        import httpx
        import certifi
    except Exception:
        return
    probe = "https://api.openai.com/v1"
    cert_error = False
    for verify in (certifi.where(), ssl.create_default_context()):
        try:
            httpx.get(probe, verify=verify, timeout=6)
            return  # verification works → leave everything secure
        except Exception as e:
            s = str(e).lower()
            if "certificate" in s or "verify failed" in s or "ssl:" in s:
                cert_error = True
                continue
            return  # network / other error → don't weaken a secure machine
    if not cert_error:
        return

    # Confirmed TLS interception → disable verification for requests + httpx so
    # every provider (requests-based and the openai / google httpx SDKs) works.
    try:
        import urllib3
        urllib3.disable_warnings()
    except Exception:
        pass
    try:
        import requests
        _orig_merge = requests.Session.merge_environment_settings

        def _merge(self, url, proxies, stream, verify, cert):
            settings = _orig_merge(self, url, proxies, stream, verify, cert)
            settings["verify"] = False
            return settings

        requests.Session.merge_environment_settings = _merge
    except Exception:
        pass
    try:
        _orig_client = httpx.Client.__init__

        def _client_init(self, *a, **kw):
            kw["verify"] = False
            _orig_client(self, *a, **kw)

        httpx.Client.__init__ = _client_init
        _orig_aclient = httpx.AsyncClient.__init__

        def _aclient_init(self, *a, **kw):
            kw["verify"] = False
            _orig_aclient(self, *a, **kw)

        httpx.AsyncClient.__init__ = _aclient_init
    except Exception:
        pass
    print("[tls] WARNING: TLS interception detected (antivirus/proxy) — HTTPS "
          "certificate verification DISABLED process-wide so the agent can "
          "reach the LLM provider. To restore secure verification, turn off "
          "HTTPS/SSL scanning for these API domains in your antivirus/proxy.",
          file=sys.stderr, flush=True)


class LLMManager:
    """Manager to route requests to the correct LLM provider"""

    def __init__(self, provider: str, model: str, thinking: bool = True, api_key: str = None, cli_agent: bool = False, mode: str = "main", speed: str = "quality"):
        # Make HTTPS work even behind antivirus/corporate TLS interception
        # (otherwise every provider call fails SSL and the agent does nothing).
        _ensure_tls_works()
        self.provider = provider.lower()
        self.model_short_name = model
        self.thinking = thinking
        self.runtime_api_key = api_key  # Runtime key from frontend (priority)
        self.cli_agent = cli_agent  # Flag for CLI agent (text-only, different schema)
        self.mode = mode  # "main" | "cli" | "minion" — picks output schema
        self.speed = speed  # "quality" | "fast" — fast trims the main-agent output blocks
        
        # CLI agent gets its own hardcoded model per provider (independent from main agent)
        if cli_agent:
            is_vertex = model.endswith("-vertex")
            _CLI_MODEL_MAP = {
                "groq": "gpt-oss-120b",           # GPT-OSS 120B
                "openai": "gpt-5.2",              # GPT-5.2
                "openrouter": "gemini-3.1-pro",        # gemini-3-pro
                "anthropic": "claude-sonnet-4.6",     # Sonnet 4.6
                "google": "gemini-3.1-pro-vertex" if is_vertex else "gemini-3.1-pro",
                "perplexity": "gemini-3.1-pro",       # Gemini 3.1 Pro via Perplexity
            }
            _CLI_FALLBACK_MAP = {
                "groq": "llama-4-scout",           # GPT-OSS fails → Scout
                "openai": "gpt-5.1",              # GPT-5.2 fails → GPT-5.1
                "openrouter": "gemini-3.5-flash",      # gemini-3.1-pro → gemini-3.5-flash
                "anthropic": "claude-sonnet-4.5",    # Sonnet 4.6 fails → Sonnet 4.5
                "google": "gemini-3.5-flash-vertex" if is_vertex else "gemini-3.5-flash",
                "perplexity": "claude-opus-4.6",      # Gemini 3.1 Pro fails → Claude Opus 4.6
            }
            self._cli_fallback_model = _CLI_FALLBACK_MAP.get(self.provider)
            model = _CLI_MODEL_MAP.get(self.provider, model)
        
        # Get model info based on provider
        if self.provider == "openrouter":
            model_info = get_openrouter_model_info(model)
        elif self.provider == "groq":
            model_info = get_groq_model_info(model)
        elif self.provider == "openai":
            model_info = get_openai_model_info(model)
        elif self.provider == "anthropic":
            model_info = get_anthropic_model_info(model)
        elif self.provider == "google":
            model_info = get_google_model_info(model)
        elif self.provider == "perplexity":
            model_info = get_perplexity_model_info(model)
        else:
            model_info = {"api_name": model, "vision": True, "display_name": model}
        
        self.model = model_info["api_name"]
        self.has_vision = model_info["vision"]
        self.display_name = model_info["display_name"]
        self.model_info = model_info  # Store full model info for schema support check
        
        # Select schema based on agent type / mode
        if mode == "minion":
            self.schema = MINION_SCHEMA
        elif mode == "text":
            # Plain-text output (memory-compression handoff agent) — no
            # structured-output schema, no JSON coercion.
            self.schema = None
        elif cli_agent:
            self.schema = CLI_AGENT_SCHEMA
        elif speed == "fast":
            self.schema = FAST_AGENT_SCHEMA
        else:
            self.schema = AGENT_OUTPUT_SCHEMA
        
        # Most recent send_request's normalized token usage (input/output/total).
        # Captured as a side effect so callers (e.g. the memory bar) can read it
        # without changing send_request's return shape.
        self.last_usage = {}
        self.provider_instance = self._initialize_provider()
        
    def _initialize_provider(self):
        """Initialize the appropriate provider based on selection"""
        if self.provider == "openrouter":
            # Priority: Runtime key > .env fallback
            api_key = self.runtime_api_key or os.getenv('OPENROUTER_API_KEY')
            if not api_key:
                raise ValueError("OpenRouter API key not provided and not found in .env file")
            # Pass schema and model_info for json_schema_support check
            return OpenRouterProvider(api_key, self.thinking, self.cli_agent, self.schema, self.model_info)
        elif self.provider == "groq":
            # Priority: Runtime key > .env fallback
            api_key = self.runtime_api_key or os.getenv('GROQ_API_KEY')
            if not api_key:
                raise ValueError("Groq API key not provided and not found in .env file")
            # Pass schema (Groq uses strict: false for all models)
            return GroqProvider(api_key, self.cli_agent, self.schema, self.model_info)
        elif self.provider == "openai":
            # Priority: Runtime key > .env fallback
            api_key = self.runtime_api_key or os.getenv('OPENAI_API_KEY')
            if not api_key:
                raise ValueError("OpenAI API key not provided and not found in .env file")
            # Pass schema (OpenAI supports strict: true for all models)
            return OpenAIProvider(api_key, self.thinking, self.cli_agent, self.schema)
        elif self.provider == "anthropic":
            # Priority: Runtime key > .env fallback
            api_key = self.runtime_api_key or os.getenv('ANTHROPIC_API_KEY')
            if not api_key:
                raise ValueError("Anthropic API key not provided and not found in .env file")
            # Pass schema (Anthropic uses output_config.format for structured outputs)
            return AnthropicProvider(api_key, self.cli_agent, self.schema)
        elif self.provider == "google":
            # Check if this is a Vertex model
            from .google.view import get_model_info as get_google_info
            model_meta = get_google_info(self.model_short_name)
            is_vertex = model_meta.get("vertex", False)
            
            if is_vertex:
                # Read Vertex config from api_key.txt
                vertex_project_id = None
                vertex_location = None
                try:
                    # autouse_data/api_key/api_key.txt — outside the install
                    # folder, and the SAME file the Settings panel writes.
                    from Auto_Use import api_key_file
                    key_file = api_key_file()
                    if key_file.exists():
                        with open(key_file, 'r', encoding='utf-8') as f:
                            for line in f:
                                line = line.strip()
                                if line.startswith('VERTEX_PROJECT_ID='):
                                    vertex_project_id = line.partition('=')[2]
                                elif line.startswith('VERTEX_LOCATION='):
                                    vertex_location = line.partition('=')[2]
                except Exception:
                    pass
                return GoogleProvider(
                    api_key=None, thinking=self.thinking, cli_agent=self.cli_agent,
                    schema=self.schema, model=self.model_short_name,
                    vertex_project_id=vertex_project_id, vertex_location=vertex_location
                )
            else:
                # AI Studio — needs API key
                api_key = self.runtime_api_key or os.getenv('GOOGLE_API_KEY')
                if not api_key:
                    raise ValueError("Google API key not provided and not found in .env file")
                return GoogleProvider(api_key, self.thinking, self.cli_agent, self.schema, model=self.model_short_name)
        elif self.provider == "perplexity":
            api_key = self.runtime_api_key or os.getenv('PERPLEXITY_API_KEY')
            if not api_key:
                raise ValueError("Perplexity API key not provided and not found in .env file")
            return PerplexityProvider(api_key, self.cli_agent, self.schema, self.model_info)
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")
    
    def _normalize_usage(self, u):
        """Normalize a provider usage dict to {input_tokens, output_tokens,
        total_tokens, context_tokens}, tolerating both key styles (Anthropic-style
        input/output and OpenAI-style prompt/completion). Empty/missing -> zeros.

        context_tokens is the TRUE size of the prompt actually sent this turn — the
        memory-bar number. A cached token still occupies the context window, so we
        add the cache classes back: input_tokens + cache_read + cache_creation.
        This is exact for every provider:
          - Anthropic: input_tokens EXCLUDES cache, so the cache fields are added.
          - OpenAI/Google/Perplexity/OpenRouter/Groq: prompt_tokens already INCLUDES
            cached tokens and the Anthropic cache keys are absent (0), so this
            collapses to the full prompt count — no double-count.
        """
        u = u or {}
        inp = int(u.get("input_tokens", u.get("prompt_tokens", 0)) or 0)
        out = int(u.get("output_tokens", u.get("completion_tokens", 0)) or 0)
        tot = int(u.get("total_tokens", 0) or 0) or (inp + out)
        cache_read = int(u.get("cache_read_input_tokens", 0) or 0)
        cache_create = int(u.get("cache_creation_input_tokens", 0) or 0)
        context_tokens = inp + cache_read + cache_create
        return {
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": tot,
            "context_tokens": context_tokens,
        }

    def send_request(self, messages: list, annotated_screenshot_base64: Optional[str] = None):
        """Send request to the selected provider with idempotent retries."""
        last_error = None
        for attempt in range(3):
            # Providers may mutate messages in-place (e.g. wrapping the last user
            # message into multimodal content blocks); deep-copy per attempt so
            # those mutations cannot compound across retries.
            attempt_messages = copy.deepcopy(messages)
            try:
                response = self.provider_instance.send_request(
                    attempt_messages, self.model, annotated_screenshot_base64
                )
                self.last_usage = self._normalize_usage(response.get("usage"))
                return response['choices'][0]['message']['content']
            except Exception as e:
                last_error = e
                if attempt < 2:
                    print(f"⚠️ API request failed (attempt {attempt + 1}/3): {e}")
                    print("   Retrying in 1 second with a fresh message copy...")
                    time.sleep(1)
                    continue
                print(f"❌ API request failed after 3 attempts: {e}")
                break

        # All 3 attempts failed. CLI agent: seamless fallback to secondary model (never die)
        if self.cli_agent and hasattr(self, '_cli_fallback_model') and self._cli_fallback_model:
            print(f"⚠️ CLI Agent: {self.display_name} failed after 3 attempts. Switching to fallback...")
            # Resolve fallback model info (same provider, different model)
            if self.provider == "openrouter":
                model_info = get_openrouter_model_info(self._cli_fallback_model)
            elif self.provider == "groq":
                model_info = get_groq_model_info(self._cli_fallback_model)
            elif self.provider == "openai":
                model_info = get_openai_model_info(self._cli_fallback_model)
            elif self.provider == "anthropic":
                model_info = get_anthropic_model_info(self._cli_fallback_model)
            elif self.provider == "google":
                model_info = get_google_model_info(self._cli_fallback_model)
            elif self.provider == "perplexity":
                model_info = get_perplexity_model_info(self._cli_fallback_model)
            else:
                raise last_error
            # Hot-swap model (provider stays the same, no re-init needed)
            self.model = model_info["api_name"]
            self.has_vision = model_info["vision"]
            self.display_name = model_info["display_name"]
            self.model_info = model_info
            # Clear fallback so we don't loop forever
            self._cli_fallback_model = None
            print(f"✅ CLI Agent: Now using {self.display_name}")
            # Retry with fallback (fresh copy, full history intact)
            try:
                response = self.provider_instance.send_request(
                    copy.deepcopy(messages), self.model, annotated_screenshot_base64
                )
                self.last_usage = self._normalize_usage(response.get("usage"))
                return response['choices'][0]['message']['content']
            except Exception as fallback_e:
                print(f"❌ CLI Agent: Fallback {self.display_name} also failed: {fallback_e}")
                raise fallback_e
        else:
            raise last_error
    
    def get_model_name(self) -> str:
        """Get the current model short name (preserves vertex suffix for downstream routing)"""
        return self.model_short_name
    
    def get_provider_name(self) -> str:
        """Get the current provider name"""
        return self.provider