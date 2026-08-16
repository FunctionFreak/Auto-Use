// Copyright 2026 Ashish Yadav — Auto-Use

//! Groq API provider.

use serde_json::{json, Map, Value};

use crate::llm_provider::{api_url, normalize_openai_tool_calls, post_json, SCREENSHOT_MEDIA_TYPE};

pub struct GroqProvider {
    pub api_key: String,
    pub cli_agent: bool,
    /// OpenAI-format function tools; None only for mode="text".
    pub tools: Option<Vec<Value>>,
}

impl GroqProvider {
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
                let user_message = messages[last].get("content").cloned().unwrap_or(Value::Null);
                messages[last]["content"] = json!([
                    {"type": "text", "text": user_message},
                    {"type": "image_url",
                     "image_url": {"url": format!("data:{SCREENSHOT_MEDIA_TYPE};base64,{shot}")}},
                ]);
            }
        }

        let mut data = Map::new();
        data.insert("model".into(), json!(model));
        data.insert("messages".into(), Value::Array(messages));
        data.insert("temperature".into(), json!(0.2));
        data.insert("max_tokens".into(), json!(4000));
        if let Some(tools) = &self.tools {
            data.insert("tools".into(), Value::Array(tools.clone()));
            // "required" — every turn must call at least one tool; canonical
            // rationale in openrouter/service.rs.
            data.insert("tool_choice".into(), json!("required"));
        }

        let url = api_url("https://api.groq.com/openai/v1/chat/completions");
        let auth = format!("Bearer {}", self.api_key);
        let headers = [
            ("Authorization", auth.as_str()),
            ("Content-Type", "application/json"),
        ];
        let resp = post_json(&url, &headers, &Value::Object(data))
            .map_err(|e| format!("Groq API request failed: {e}"))?;
        if resp.status >= 400 {
            return Err(format!(
                "Groq API request failed: HTTP {}\nResponse: {}",
                resp.status, resp.body
            ));
        }
        let result: Value = serde_json::from_str(&resp.body)
            .map_err(|e| format!("Groq API request failed: {e}"))?;
        if self.tools.is_some() {
            Ok(normalize_tool_response(&result))
        } else {
            Ok(result)
        }
    }
}

fn normalize_tool_response(result: &Value) -> Value {
    let message = result
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|c| c.first())
        .and_then(|c| c.get("message"))
        .cloned()
        .unwrap_or(json!({}));
    let calls = normalize_openai_tool_calls(&message);
    let content = message.get("content").cloned().unwrap_or(Value::Null);
    json!({
        "choices": [{
            "message": {
                "content": if crate::agent::browser::truthy(&content) { content } else { json!("") },
                "tool_calls": calls,
            }
        }],
        "usage": result.get("usage").cloned().unwrap_or(json!({})),
    })
}
