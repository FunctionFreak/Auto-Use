// Copyright 2026 Cursortouch — Auto-Use

//! Model mappings for the Google provider.
//!
//! `thinking_level` is Gemini 3's thinking control — a named level, not a
//! token budget. Both models are pinned to `low`, but it means a different
//! thing on each: Pro's ladder is low/medium/high (default high — the top of
//! its range), Flash's is minimal/low/medium/high (default medium).
//!
//! The `-vertex` entries are the SAME models reached through a different
//! client — api_name is intentionally duplicated across each pair; only
//! `vertex` differs, which picks the client in service.rs.

use std::sync::OnceLock;

use serde_json::{json, Value};

pub fn model_mappings() -> &'static Value {
    static M: OnceLock<Value> = OnceLock::new();
    M.get_or_init(|| {
        json!({
            "gemini-3.1-pro": {
                "api_name": "gemini-3.1-pro-preview",
                "vision": true,
                "display_name": "Gemini 3.1 Pro",
                "vertex": false,
                "thinking_level": "low"
            },
            "gemini-3.6-flash": {
                "api_name": "gemini-3.6-flash",
                "vision": true,
                "display_name": "Gemini 3.6 Flash",
                "vertex": false,
                "thinking_level": "low"
            },
            "gemini-3.1-pro-vertex": {
                "api_name": "gemini-3.1-pro-preview",
                "vision": true,
                "display_name": "Gemini 3.1 Pro (Vertex)",
                "vertex": true,
                "thinking_level": "low"
            },
            "gemini-3.6-flash-vertex": {
                "api_name": "gemini-3.6-flash",
                "vision": true,
                "display_name": "Gemini 3.6 Flash (Vertex)",
                "vertex": true,
                "thinking_level": "low"
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

/// `thinking_level` for the model about to be called, or None. api_name is
/// NOT unique here (each model appears as itself and as its -vertex twin) —
/// safe only while both halves of a pair carry the same level, which they
/// do; first match wins. None for an unregistered model, so a hand-typed
/// name keeps Google's own default.
pub fn get_thinking_level(api_name: &str) -> Option<String> {
    if let Some(map) = model_mappings().as_object() {
        for info in map.values() {
            if info.get("api_name").and_then(Value::as_str) == Some(api_name) {
                if let Some(level) = info.get("thinking_level").and_then(Value::as_str) {
                    return Some(level.to_string());
                }
            }
        }
    }
    None
}
