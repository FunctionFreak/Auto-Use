// Copyright 2026 Cursortouch — Auto-Use

//! Model mappings for the Anthropic provider.
//!
//! Sampling knobs only for the models that still accept them — Haiku only.
//! `temperature` is never sent (the frontier models removed it); top_p does
//! the real work and top_k is a tail guard. The 5-series instead takes
//! ADAPTIVE thinking plus an `effort` dial — the two feature sets are
//! mutually exclusive across this table, and sending the wrong one is a 400
//! that fails every attempt of every step. Opus 5 and Sonnet 5 DEFAULT to
//! `high` effort, so omitting the field is not neutral.

use std::sync::OnceLock;

use serde_json::{json, Value};

pub fn sampling_params() -> Value {
    json!({"top_p": 0.5, "top_k": 40})
}

pub fn adaptive_thinking() -> Value {
    json!({"type": "adaptive"})
}

pub fn model_mappings() -> &'static Value {
    static M: OnceLock<Value> = OnceLock::new();
    M.get_or_init(|| {
        json!({
            // Haiku takes neither adaptive thinking nor `effort` — it
            // predates both, and each is a 400 here. It is the one model left
            // that still accepts the sampling knobs.
            "claude-haiku-4.5": {
                "api_name": "claude-haiku-4-5-20251001",
                "vision": true,
                "display_name": "Claude Haiku 4.5",
                "sampling": true
            },
            "claude-opus-5": {
                "api_name": "claude-opus-5",
                "vision": true,
                "display_name": "Claude Opus 5",
                "sampling": false,
                "effort": "low"
            },
            "claude-sonnet-5": {
                "api_name": "claude-sonnet-5",
                "vision": true,
                "display_name": "Claude Sonnet 5",
                "sampling": false,
                "effort": "low"
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

/// Sampling knobs for the model about to be called, keyed by api_name (the
/// two differ for the Haiku entry). {} for anything not sampling-capable.
pub fn get_sampling_params(api_name: &str) -> Value {
    if let Some(map) = model_mappings().as_object() {
        for info in map.values() {
            if info.get("api_name").and_then(Value::as_str) == Some(api_name)
                && info.get("sampling").and_then(Value::as_bool) == Some(true)
            {
                return sampling_params();
            }
        }
    }
    json!({})
}

/// Adaptive-thinking + effort request fields for the model being called. {}
/// for anything without an `effort` entry — Haiku, and any hand-typed name.
pub fn get_thinking_params(api_name: &str) -> Value {
    if let Some(map) = model_mappings().as_object() {
        for info in map.values() {
            if info.get("api_name").and_then(Value::as_str) == Some(api_name) {
                if let Some(effort) = info.get("effort").and_then(Value::as_str) {
                    return json!({
                        "thinking": adaptive_thinking(),
                        "output_config": {"effort": effort},
                    });
                }
            }
        }
    }
    json!({})
}
