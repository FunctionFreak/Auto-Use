# Copyright 2026 Ashish Yadav — Auto-Use

# Model mappings for OpenRouter provider
# Maps user-friendly names to actual API model names

# `effort` is how hard the model thinks, carried to OpenRouter as its unified
# reasoning field: {"reasoning": {"effort": ...}}. OpenRouter translates that
# into whatever each backend actually speaks, so one key covers OpenAI,
# Anthropic, Gemini, Grok, Mistral and Qwen alike.
#
# THE LEVELS ARE NOT A SHARED LADDER. Each model publishes its own set, and a
# level outside that set is not portable: `xhigh`/`max` do not exist on
# Gemini, Grok or Mistral, `medium` does not exist on Kimi, and `minimal`
# exists ONLY on Gemini Flash and Qwen. Every value below was checked against
# that model's own published list — - so treat this table as per-model
# configuration, not as a dial you can sweep uniformly.
#
# Stating the level matters even where it matches the default, because the
# defaults disagree wildly: Kimi K3 defaults to `max`, Qwen to `xhigh`, the
# Anthropic trio to `high`, Grok 4.3 to `low`. Left unset, two models in the
# same drop-up behave nothing alike, and the expensive ones bill against the
# same max_tokens ceiling that has to hold the tool call.
#
# Reasoning cannot be switched off on Gemini, Grok 4.5 or Qwen — those are
# mandatory-reasoning models, so the lowest level they publish is the floor.
MODEL_MAPPINGS = {
    "gemini-3.1-pro": {
        "api_name": "google/gemini-3.1-pro-preview",
        "vision": True,
        "display_name": "Gemini 3.1 Pro Preview",
        # `low` is the floor: Pro publishes only high/medium/low, with no
        # `minimal` (that exists on Flash below, not here). Pinned to the
        # floor because Pro's thinking length is the most volatile in this
        # table — at `medium` a single trivial one-tool prompt was measured
        # spending 9,596 reasoning tokens against the 10,000 max_tokens
        # ceiling, on a step whose whole job was to emit one tool call.
        "effort": "low"
    },
    "gemini-3.6-flash": {
        "api_name": "google/gemini-3.6-flash",
        "vision": True,
        "display_name": "Gemini 3.6 Flash",
        # Flash is the one Gemini that publishes `minimal`.
        "effort": "minimal"
    },
    # OpenAI is the GPT-5.6 family, Terra and Luna only — the same two the
    # direct openai provider carries, so switching a model between the two
    # providers is a routing change and not a capability change.
    "gpt-5.6-terra": {
        "api_name": "openai/gpt-5.6-terra",
        "vision": True,
        "display_name": "GPT-5.6 Terra",
        "effort": "low"
    },
    "gpt-5.6-luna": {
        "api_name": "openai/gpt-5.6-luna",
        "vision": True,
        "display_name": "GPT-5.6 Luna",
        "effort": "low"
    },
    "claude-opus-5": {
        "api_name": "anthropic/claude-opus-5",
        "vision": True,
        "display_name": "Claude Opus 5",
        "effort": "medium"
    },
    "claude-sonnet-5": {
        "api_name": "anthropic/claude-sonnet-5",
        "vision": True,
        "display_name": "Claude Sonnet 5",
        "effort": "low"
    },
    "grok-4.3": {
        "api_name": "x-ai/grok-4.3",
        "vision": True,
        "display_name": "Grok 4.3",
        "effort": "low"
    },
    "grok-4.5": {
        "api_name": "x-ai/grok-4.5",
        "vision": True,
        "display_name": "Grok 4.5",
        # `low` is the floor here — 4.5 has mandatory reasoning and publishes
        # only high/medium/low, so this is as cheap as it gets.
        "effort": "low"
    },
    "kimi-k3": {
        "api_name": "moonshotai/kimi-k3",
        "vision": True,
        "display_name": "Kimi K3",
        # Its ladder is max/high/low — there is no `medium` — and it defaults
        # to `max`, the highest default in the catalog. Setting `low` is the
        # single biggest saving in this table.
        "effort": "low"
    },
    "claude-opus-5-fast": {
        "api_name": "anthropic/claude-opus-5-fast",
        "vision": True,
        "display_name": "Claude Opus 5 Fast",
        "effort": "low"
    },
    # Still the newest Mistral on OpenRouter — the line has not moved since
    # 2026-04-30, so this entry is current, not stale.
    "mistral-medium-3.5": {
        "api_name": "mistralai/mistral-medium-3-5",
        "vision": True,
        "display_name": "Mistral Medium 3.5",
        # Mistral publishes only two levels, high and none, so `none` here
        # means reasoning genuinely off — not "lowest setting" as elsewhere.
        "effort": "none"
    },
    # NO QWEN ENTRY, deliberately. Qwen's OpenRouter backend rejects
    # tool_choice="required" with a 400 ("does not support being set to
    # required or object in thinking mode"), and this loop sends that on
    # every call because a text-only turn is never a valid step. Verified
    # against qwen3.8-max, qwen3.7-max and qwen3.7-plus, with and without a
    # reasoning field — the whole line fails, so there is no Qwen model and
    # no effort level that can be listed here. tool_choice="auto" is what it
    # accepts, which is exactly the contract this loop cannot give up.
}

def get_model_info(short_name: str) -> dict:
    """Get full model information from short name"""
    if short_name in MODEL_MAPPINGS:
        return MODEL_MAPPINGS[short_name]
    # If not found, assume it's already a full model name
    return {
        "api_name": short_name,
        "vision": True,
        "display_name": short_name
    }


def get_reasoning_params(api_name: str) -> dict:
    """OpenRouter's unified reasoning field for the model being called.

    Keyed by api_name — what send_request actually receives, and what the
    emergency fallback swaps in mid-call — matching the same helper in
    anthropic/view.py and openai/view.py.

    Returns {} for an entry with no level and for any hand-typed model name.
    That is the safe direction on this provider: the levels are per-model, so
    a guess is as likely to be rejected as honoured, and sending nothing
    leaves the model on the default OpenRouter already publishes for it.
    """
    for info in MODEL_MAPPINGS.values():
        if info["api_name"] == api_name and info.get("effort"):
            return {"reasoning": {"effort": info["effort"]}}
    return {}
