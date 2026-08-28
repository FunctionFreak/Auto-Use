// Copyright 2026 Cursortouch — Auto-Use

//! Model mappings for the OpenAI provider.
//!
//! GPT-5.6 is a REASONING family, so `reasoning_effort` is the only real
//! handle on how hard the model works — the sampling knobs (temperature,
//! top_p, n, penalties, seed) are all locked and 400 if sent. One ceiling to
//! respect when raising a level: max_completion_tokens counts REASONING
//! tokens as well as the visible answer and the tool-call JSON, and the loop
//! runs tool_choice="required" — a turn that spends its whole budget
//! thinking loses the tool call it was supposed to emit.

use std::sync::OnceLock;

use serde_json::{json, Value};

pub fn model_mappings() -> &'static Value {
    static M: OnceLock<Value> = OnceLock::new();
    M.get_or_init(|| {
        json!({
            "gpt-5.6-luna": {
                "api_name": "gpt-5.6-luna",
                "vision": true,
                "display_name": "GPT-5.6 Luna",
                "json_mode": true,
                // Luna is the latency/cost tier; `none` is what makes it that.
                "reasoning_effort": "none"
            },
            "gpt-5.6-terra": {
                "api_name": "gpt-5.6-terra",
                "vision": true,
                "display_name": "GPT-5.6 Terra",
                "json_mode": true,
                // `none`, not `medium`: /v1/chat/completions rejects function
                // tools combined with any other reasoning_effort on Terra.
                // Terra WITH reasoning is available via openrouter/perplexity.
                "reasoning_effort": "none"
            }
        })
    })
}

/// Get full model information from short name; unregistered names pass
/// through (defaulting to JSON-mode support, like the Python table did).
pub fn get_model_info(short_name: &str) -> Value {
    if let Some(info) = model_mappings().get(short_name) {
        return info.clone();
    }
    json!({"api_name": short_name, "vision": true, "display_name": short_name,
           "json_mode": true})
}

/// `reasoning_effort` request field for the model about to be called, keyed
/// by api_name. {} for an unregistered name — a hand-typed model may be a
/// non-reasoning model, which rejects the parameter outright.
pub fn get_reasoning_effort(api_name: &str) -> Value {
    if let Some(map) = model_mappings().as_object() {
        for info in map.values() {
            if info.get("api_name").and_then(Value::as_str) == Some(api_name) {
                if let Some(effort) = info.get("reasoning_effort").and_then(Value::as_str) {
                    return json!({"reasoning_effort": effort});
                }
            }
        }
    }
    json!({})
}
