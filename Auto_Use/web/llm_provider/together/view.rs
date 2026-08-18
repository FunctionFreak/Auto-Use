// Copyright 2026 Ashish Yadav — Auto-Use

//! Model mappings for the Together AI provider.
//!
//! Three models, all image+text in / text out with native (OpenAI-format)
//! tool calling — the two things this loop needs on every step. Reasoning
//! is deliberately left unset: Together exposes `reasoning_effort`
//! (low|medium|high) — Inkling's controllable-effort knob — but with no field
//! on the request each model keeps Together's own default, the same choice
//! groq/ makes. Temperature is handled once in service.rs (0.2).

use std::sync::OnceLock;

use serde_json::{json, Value};

pub fn model_mappings() -> &'static Value {
    static M: OnceLock<Value> = OnceLock::new();
    M.get_or_init(|| {
        json!({
            "inkling": {
                "api_name": "thinkingmachines/Inkling",
                "vision": true,
                "display_name": "Inkling"
            },
            "muse-glimmer-30b": {
                "api_name": "meta-models/Muse-Glimmer-30B",
                "vision": true,
                "display_name": "Muse Glimmer 30B"
            },
            "minimax-m3": {
                "api_name": "MiniMaxAI/MiniMax-M3",
                "vision": true,
                "display_name": "MiniMax M3"
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
