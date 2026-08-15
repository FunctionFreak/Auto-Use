// Copyright 2026 Ashish Yadav — Auto-Use

//! OpenAI API provider. The Python original went through the openai SDK;
//! this speaks to the same /v1/chat/completions endpoint directly — the
//! payload carries the identical fields.

use serde_json::{json, Map, Value};

use super::view::get_reasoning_effort;
use crate::llm_provider::{api_url, normalize_openai_tool_calls, post_json};

pub struct OpenAIProvider {
    pub api_key: String,
    pub cli_agent: bool,
    /// OpenAI-format function tools; None only for mode="text".
    pub tools: Option<Vec<Value>>,
}

impl OpenAIProvider {
    pub fn send_request(
        &self,
        messages: &[Value],
        model: &str,
        annotated_screenshot_base64: Option<&str>,
    ) -> Result<Value, String> {
        let mut messages: Vec<Value> = messages.to_vec();

        if let Some(shot) = annotated_screenshot_base64 {
            if !self.cli_agent && messages.len() > 1 {
                let last = messages.len() - 1;
                let mut user_message =
                    messages[last].get("content").cloned().unwrap_or(Value::Null);
                // Content might already be a list — extract its text.
                if let Value::Array(items) = &user_message {
                    let mut text_content = String::new();
                    for item in items {
                        if item.get("type").and_then(Value::as_str) == Some("text") {
                            text_content = item
                                .get("text")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_string();
                            break;
                        }
                    }
                    user_message = Value::String(text_content);
                }
                messages[last]["content"] = json!([
                    {"type": "text", "text": user_message},
                    {"type": "image_url",
                     "image_url": {"url": format!("data:image/png;base64,{shot}")}},
                ]);
            }
        }

        // No sampling params are ever added here — GPT-5.6 rejects
        // temperature/top_p/seed.
        let mut params = Map::new();
        params.insert("model".into(), json!(model));
        params.insert("messages".into(), Value::Array(messages));
        params.insert("max_completion_tokens".into(), json!(4000));
        params.insert("verbosity".into(), json!("medium"));
        if let Value::Object(extra) = get_reasoning_effort(model) {
            for (k, v) in extra {
                params.insert(k, v);
            }
        }
        if let Some(tools) = &self.tools {
            params.insert("tools".into(), Value::Array(tools.clone()));
            // Canonical rationale in openrouter/service.rs.
            params.insert("tool_choice".into(), json!("required"));
        }

        let url = api_url("https://api.openai.com/v1/chat/completions");
        let auth = format!("Bearer {}", self.api_key);
        let headers = [
            ("Authorization", auth.as_str()),
            ("Content-Type", "application/json"),
        ];
        let attempt = (|| -> Result<Value, String> {
            let resp = post_json(&url, &headers, &Value::Object(params)).map_err(|e| e.to_string())?;
            if resp.status >= 400 {
                return Err(format!("HTTP {}: {}", resp.status, resp.body));
            }
            serde_json::from_str(&resp.body).map_err(|e| e.to_string())
        })();
        let result = attempt.map_err(|e| format!("OpenAI API request failed: {e}"))?;

        // Return in the same format as other providers.
        let sdk_message = result
            .get("choices")
            .and_then(Value::as_array)
            .and_then(|c| c.first())
            .and_then(|c| c.get("message"))
            .cloned()
            .unwrap_or(json!({}));
        let content = sdk_message.get("content").cloned().unwrap_or(Value::Null);
        let mut message = Map::new();
        message.insert(
            "content".into(),
            if crate::agent::browser::truthy(&content) { content } else { json!("") },
        );
        if self.tools.is_some() {
            message.insert(
                "tool_calls".into(),
                Value::Array(normalize_openai_tool_calls(&sdk_message)),
            );
        }
        let usage = result.get("usage").cloned();
        let usage_out = match usage {
            Some(u) if !u.is_null() => json!({
                "input_tokens": u.get("prompt_tokens").cloned().unwrap_or(json!(0)),
                "output_tokens": u.get("completion_tokens").cloned().unwrap_or(json!(0)),
                "total_tokens": u.get("total_tokens").cloned().unwrap_or(json!(0)),
            }),
            _ => json!({}),
        };
        Ok(json!({
            "choices": [{"message": Value::Object(message)}],
            "usage": usage_out,
        }))
    }
}
