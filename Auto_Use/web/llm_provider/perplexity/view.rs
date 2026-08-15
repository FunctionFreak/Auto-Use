// Copyright 2026 Ashish Yadav — Auto-Use

//! Model mappings for the Perplexity provider.
//!
//! Perplexity fronts other vendors' models, so api_name here is a PERPLEXITY
//! route, not the vendor's own ID. Two traps: xAI is `xai/` (OpenRouter says
//! `x-ai/`), and Moonshot rides under `perplexity/kimi-k3` — Perplexity's
//! OWN namespace. Short names are kept identical to the other maps on
//! purpose; api_name never is. `effort` is carried as the Agent API's
//! reasoning config; every level below exists on its own model's published
//! ladder. No `sonar` entry: Perplexity's own search model is text-only,
//! which cannot answer a driver step that hands the model a screenshot.

use std::sync::OnceLock;

use serde_json::{json, Value};

pub fn model_mappings() -> &'static Value {
    static M: OnceLock<Value> = OnceLock::new();
    M.get_or_init(|| {
        json!({
            "gpt-5.6-terra": {
                "api_name": "openai/gpt-5.6-terra",
                "vision": true,
                "display_name": "GPT-5.6 Terra",
                // The one model here above the floor — Terra is the balanced
                // tier and carries the heavier work.
                "effort": "medium"
            },
            "gpt-5.6-luna": {
                "api_name": "openai/gpt-5.6-luna",
                "vision": true,
                "display_name": "GPT-5.6 Luna",
                "effort": "low"
            },
            // Anthropic — dashes, not dots. max_output_tokens is REQUIRED by
            // the Agent API for anthropic/* routes; service.rs sends it on
            // every request.
            "claude-opus-5": {
                "api_name": "anthropic/claude-opus-5",
                "vision": true,
                "display_name": "Claude Opus 5",
                "effort": "low"
            },
            "claude-sonnet-5": {
                "api_name": "anthropic/claude-sonnet-5",
                "vision": true,
                "display_name": "Claude Sonnet 5",
                "effort": "low"
            },
            "gemini-3.1-pro": {
                "api_name": "google/gemini-3.1-pro-preview",
                "vision": true,
                "display_name": "Gemini 3.1 Pro Preview",
                "effort": "low"
            },
            "gemini-3.6-flash": {
                "api_name": "google/gemini-3.6-flash",
                "vision": true,
                "display_name": "Gemini 3.6 Flash",
                "effort": "low"
            },
            "grok-4.5": {
                "api_name": "xai/grok-4.5",
                "vision": true,
                "display_name": "Grok 4.5",
                "effort": "low"
            },
            "grok-4.3": {
                "api_name": "xai/grok-4.3",
                "vision": true,
                "display_name": "Grok 4.3",
                "effort": "low"
            },
            "kimi-k3": {
                "api_name": "perplexity/kimi-k3",
                "vision": true,
                "display_name": "Kimi K3",
                "effort": "low"
            }
        })
    })
}

/// Get full model information from short name.
pub fn get_model_info(short_name: &str) -> Value {
    if let Some(info) = model_mappings().get(short_name) {
        return info.clone();
    }
    json!({"api_name": short_name, "vision": true, "display_name": short_name})
}

/// Agent API reasoning config for the model about to be called, keyed by
/// api_name (unique here, unlike google's). {} for a hand-typed model name.
pub fn get_reasoning_params(api_name: &str) -> Value {
    if let Some(map) = model_mappings().as_object() {
        for info in map.values() {
            if info.get("api_name").and_then(Value::as_str) == Some(api_name) {
                if let Some(effort) = info.get("effort").and_then(Value::as_str) {
                    return json!({"reasoning": {"effort": effort}});
                }
            }
        }
    }
    json!({})
}
