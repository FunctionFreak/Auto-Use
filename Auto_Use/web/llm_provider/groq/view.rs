// Copyright 2026 Ashish Yadav — Auto-Use

//! Model mappings for the Groq provider.
//!
//! One model only. Qwen3.6-27B is the sole Groq model that carries every
//! capability this loop needs at once ("Tool Use, JSON Object Mode,
//! Reasoning, Vision") — and the ONLY vision model Groq serves at all.
//! Reasoning is deliberately left unset (the model keeps Groq's default);
//! temperature is handled once in service.rs (0.2). Groq classes this as a
//! PREVIEW model, and has retired preview models at short notice before.

use std::sync::OnceLock;

use serde_json::{json, Value};

pub fn model_mappings() -> &'static Value {
    static M: OnceLock<Value> = OnceLock::new();
    M.get_or_init(|| {
        json!({
            "qwen3.6-27b": {
                "api_name": "qwen/qwen3.6-27b",
                "vision": true,
                "display_name": "Qwen3.6 27B"
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
