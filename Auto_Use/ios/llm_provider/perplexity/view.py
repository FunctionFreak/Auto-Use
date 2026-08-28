# Copyright 2026 Cursortouch — Auto-Use

# Model mappings for Perplexity provider
# Maps user-friendly names to actual Perplexity Agent API model names

# Perplexity fronts other vendors' models, so api_name here is a PERPLEXITY
# route, not the vendor's own ID, and the spelling differs from the same model
# on other providers. Two traps live in this table:
#
#   xAI       Perplexity says `xai/`, OpenRouter says `x-ai/`
#   Moonshot  Perplexity says `perplexity/kimi-k3` — its OWN namespace, not
#             `moonshotai/` as on OpenRouter
#
# So a slug copied across provider maps will 404 even though it names the same
# model. Short names are kept identical to the other maps on purpose — that is
# what makes a model switchable between providers — but api_name never is.
#
# `effort` is carried as the Agent API's reasoning config,
# {"reasoning": {"effort": ...}} — the same shape OpenRouter uses. The endpoint
# accepts minimal/low/medium/high/xhigh/max, but that is the API's range, NOT
# any single model's: `minimal` does not exist on the Anthropic, Grok or Kimi
# routes, and `xhigh`/`max` do not exist on the Gemini, Grok or Kimi ones.
# Every value below was checked against the underlying model's own published
# ladder.
#
# Untested against the live endpoint — there is no Perplexity key configured,
# so it is unverified whether Perplexity clamps an out-of-range level to the
# nearest supported one (as OpenRouter does) or rejects it outright. That is
# the reason to keep these conservative: every level here exists on its own
# model either way.
MODEL_MAPPINGS = {
    # OpenAI — Terra and Luna only, matching the openai and openrouter maps.
    "gpt-5.6-terra": {
        "api_name": "openai/gpt-5.6-terra",
        "vision": True,
        "display_name": "GPT-5.6 Terra",
        # The one model here above the floor — Terra is the balanced tier and
        # carries the heavier work, so it gets the extra reasoning.
        "effort": "medium"
    },
    "gpt-5.6-luna": {
        "api_name": "openai/gpt-5.6-luna",
        "vision": True,
        "display_name": "GPT-5.6 Luna",
        "effort": "low"
    },
    # Anthropic — one Opus, one Sonnet. Dashes, not dots.
    # Note: max_output_tokens is REQUIRED by the Agent API for anthropic/*
    # routes specifically; service.py already sends it on every request.
    "claude-opus-5": {
        "api_name": "anthropic/claude-opus-5",
        "vision": True,
        "display_name": "Claude Opus 5",
        "effort": "low"
    },
    "claude-sonnet-5": {
        "api_name": "anthropic/claude-sonnet-5",
        "vision": True,
        "display_name": "Claude Sonnet 5",
        "effort": "low"
    },
    # Google — Pro is still preview-only; there is no GA Gemini 3.x Pro.
    "gemini-3.1-pro": {
        "api_name": "google/gemini-3.1-pro-preview",
        "vision": True,
        "display_name": "Gemini 3.1 Pro Preview",
        # `low` is Pro's floor: its ladder is only high/medium/low.
        "effort": "low"
    },
    "gemini-3.6-flash": {
        "api_name": "google/gemini-3.6-flash",
        "vision": True,
        "display_name": "Gemini 3.6 Flash",
        "effort": "low"
    },
    # xAI — note the `xai/` prefix, NOT `x-ai/` as on OpenRouter.
    "grok-4.5": {
        "api_name": "xai/grok-4.5",
        "vision": True,
        "display_name": "Grok 4.5",
        # Grok 4.5 cannot switch reasoning off at all, so `low` is its floor.
        "effort": "low"
    },
    "grok-4.3": {
        "api_name": "xai/grok-4.3",
        "vision": True,
        "display_name": "Grok 4.3",
        "effort": "low"
    },
    # Moonshot — routed under Perplexity's own namespace, not `moonshotai/`.
    "kimi-k3": {
        "api_name": "perplexity/kimi-k3",
        "vision": True,
        "display_name": "Kimi K3",
        # K3's ladder is max/high/low with no `medium`, and it defaults to
        # `max` — the most expensive default of anything in this table.
        "effort": "low"
    },
    # No `sonar` entry. Perplexity's own search-grounded model is text-only,
    # which makes it unusable for the driver's screenshot step — and while it
    # was registered it was also the secondary fallback, so a failed vision
    # call could land on a model that cannot see. Every entry above takes
    # images, so that whole failure mode is gone.
}

def get_model_info(short_name: str) -> dict:
    """Get full model information from short name"""
    if short_name in MODEL_MAPPINGS:
        return MODEL_MAPPINGS[short_name]
    return {
        "api_name": short_name,
        "vision": True,
        "display_name": short_name
    }


def get_reasoning_params(api_name: str) -> dict:
    """Agent API reasoning config for the model send_request is about to call.

    Keyed by api_name — what the service actually receives, and what the
    emergency fallback swaps in mid-call — matching the same helper in the
    other provider views. Unlike google/view.py, api_name is unique here, so
    first-match is unambiguous.

    Returns {} for a hand-typed model name, which then keeps whatever default
    Perplexity applies. That is the safe direction: the levels are per-model,
    so a guess is as likely to be rejected as honoured.
    """
    for info in MODEL_MAPPINGS.values():
        if info["api_name"] == api_name and info.get("effort"):
            return {"reasoning": {"effort": info["effort"]}}
    return {}
