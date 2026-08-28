# Copyright 2026 Cursortouch — Auto-Use

# Model mappings for OpenAI provider
# Maps user-friendly names to actual API model names

# GPT-5.6 is a REASONING family, so `reasoning_effort` is the only real handle
# on how hard the model works. The sampling knobs are gone: temperature, top_p
# and n are locked at 1 and the penalties at 0 — sending any of them returns
# 400 "Unsupported parameter: '<name>' is not supported with this model" — and
# `seed` is not accepted either, so there is no reproducibility lever to reach
# for on this family. send_request therefore sends effort and nothing else.
#
# Levels, cheapest to deepest: none | low | medium | high | xhigh | max.
# Omitting the parameter already yields `medium`; every entry states its level
# anyway so the choice is readable here instead of inherited from the API.
#
# One ceiling to respect when raising a level: max_completion_tokens counts
# REASONING tokens as well as the visible answer and the tool-call JSON. Since
# the loop runs tool_choice="required", a turn that spends its whole budget
# thinking loses the tool call it was supposed to emit.
MODEL_MAPPINGS = {
    "gpt-5.6-luna": {
        "api_name": "gpt-5.6-luna",
        "vision": True,
        "display_name": "GPT-5.6 Luna",
        "json_mode": True,
        # Luna is the latency/cost tier; `none` is what makes it that. It is a
        # real level here, not a synonym for omitting the field, and is meant
        # for work that gains nothing from multi-chained reasoning.
        "reasoning_effort": "none"
    },
    "gpt-5.6-terra": {
        "api_name": "gpt-5.6-terra",
        "vision": True,
        "display_name": "GPT-5.6 Terra",
        "json_mode": True,
        # `none`, not `medium`: service.py calls /v1/chat/completions, and
        # that endpoint rejects function tools combined with any
        # reasoning_effort other than "none" —
        #   "Function tools with reasoning_effort are not supported for
        #    gpt-5.6-terra in /v1/chat/completions. To use function tools,
        #    use /v1/responses or set reasoning_effort to 'none'."
        # This loop always sends tools (tool_choice="required"), so `none` is
        # the only legal value here. Terra WITH reasoning is available via the
        # openrouter and perplexity maps, which do not carry this restriction.
        "reasoning_effort": "none"
    }
}

def get_model_info(short_name: str) -> dict:
    """Get full model information from short name"""
    if short_name in MODEL_MAPPINGS:
        return MODEL_MAPPINGS[short_name]
    # If not found, assume it's already a full model name
    return {
        "api_name": short_name,
        "vision": True,
        "display_name": short_name,
        "json_mode": True  # Default to supporting JSON mode for OpenAI
    }


def get_reasoning_effort(api_name: str) -> dict:
    """`reasoning_effort` kwarg for the model send_request is about to call.

    Keyed by api_name — what the service actually receives — NOT the short
    name, matching anthropic/view.py's get_sampling_params. The two differ
    whenever a mapping renames a model, and api_name is also what the
    emergency fallback swaps in mid-call, so keying on it keeps the level
    following the model that is actually being called.

    An unregistered model returns {} rather than a guess: a hand-typed name
    may be a non-reasoning model, which rejects `reasoning_effort` outright.
    Sending nothing leaves OpenAI's own default in place.
    """
    for info in MODEL_MAPPINGS.values():
        if info["api_name"] == api_name and info.get("reasoning_effort"):
            return {"reasoning_effort": info["reasoning_effort"]}
    return {}