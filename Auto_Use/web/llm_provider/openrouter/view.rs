// Copyright 2026 Ashish Yadav — Auto-Use

//! Model mappings for the OpenRouter provider.
//!
//! `effort` is how hard the model thinks, carried to OpenRouter as its
//! unified reasoning field: {"reasoning": {"effort": ...}}. THE LEVELS ARE
//! NOT A SHARED LADDER — each model publishes its own set, so treat this
//! table as per-model configuration, not a dial to sweep uniformly.
//! Reasoning cannot be switched off on Gemini, Grok 4.5 or Qwen — the lowest
//! level they publish is the floor.
//!
//! NO QWEN ENTRY, deliberately: Qwen's OpenRouter backend rejects
//! tool_choice="required" with a 400, and this loop sends that on every call.

use std::sync::OnceLock;

use serde_json::{json, Value};

pub fn model_mappings() -> &'static Value {
    static M: OnceLock<Value> = OnceLock::new();
    M.get_or_init(|| {
        json!({
            "gemini-3.1-pro": {
                "api_name": "google/gemini-3.1-pro-preview",
                "vision": true,
                "display_name": "Gemini 3.1 Pro Preview",
                // `low` is the floor: Pro publishes only high/medium/low. At
                // `medium` a single trivial prompt was measured spending
                // 9,596 reasoning tokens against the 10,000 max_tokens
                // ceiling.
                "effort": "low"
            },
            "gemini-3.6-flash": {
                "api_name": "google/gemini-3.6-flash",
                "vision": true,
                "display_name": "Gemini 3.6 Flash",
                // Flash is the one Gemini that publishes `minimal`.
                "effort": "minimal"
            },
            "gpt-5.6-terra": {
                "api_name": "openai/gpt-5.6-terra",
                "vision": true,
                "display_name": "GPT-5.6 Terra",
                "effort": "low"
            },
            "gpt-5.6-luna": {
                "api_name": "openai/gpt-5.6-luna",
                "vision": true,
                "display_name": "GPT-5.6 Luna",
                "effort": "low"
            },
            "claude-opus-5": {
                "api_name": "anthropic/claude-opus-5",
                "vision": true,
                "display_name": "Claude Opus 5",
                "effort": "medium"
            },
            "claude-sonnet-5": {
                "api_name": "anthropic/claude-sonnet-5",
                "vision": true,
                "display_name": "Claude Sonnet 5",
                "effort": "low"
            },
            "grok-4.3": {
                "api_name": "x-ai/grok-4.3",
                "vision": true,
                "display_name": "Grok 4.3",
                "effort": "low"
            },
            "grok-4.5": {
                "api_name": "x-ai/grok-4.5",
                "vision": true,
                "display_name": "Grok 4.5",
                // `low` is the floor — 4.5 has mandatory reasoning.
                "effort": "low"
            },
            "kimi-k3": {
                "api_name": "moonshotai/kimi-k3",
                "vision": true,
                "display_name": "Kimi K3",
                // Ladder is max/high/low, defaults to `max` — `low` is the
                // single biggest saving in this table.
                "effort": "low"
            },
            "claude-opus-5-fast": {
                "api_name": "anthropic/claude-opus-5-fast",
                "vision": true,
                "display_name": "Claude Opus 5 Fast",
                "effort": "low"
            },
            "mistral-medium-3.5": {
                "api_name": "mistralai/mistral-medium-3-5",
                "vision": true,
                "display_name": "Mistral Medium 3.5",
                // Mistral publishes only high and none — `none` here means
                // reasoning genuinely off.
                "effort": "none"
            }
        })
    })
}

/// Get full model information from short name; unregistered names pass
/// through as already-full model names.
pub fn get_model_info(short_name: &str) -> Value {
    if let Some(info) = model_mappings().get(short_name) {
        return info.clone();
    }
    json!({"api_name": short_name, "vision": true, "display_name": short_name})
}

/// OpenRouter's unified reasoning field for the model being called, keyed by
/// api_name. {} for an entry with no level and for any hand-typed name — the
/// safe direction, since a guessed level is as likely rejected as honoured.
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
