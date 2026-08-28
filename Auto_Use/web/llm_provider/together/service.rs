// Copyright 2026 Cursortouch — Auto-Use

//! Together AI provider (OpenAI-compatible chat completions).

use std::sync::atomic::{AtomicBool, Ordering};

use serde_json::{json, Map, Value};

use crate::llm_provider::{api_url, normalize_openai_tool_calls, post_json, SCREENSHOT_MEDIA_TYPE};

pub struct TogetherProvider {
    pub api_key: String,
    pub cli_agent: bool,
    /// OpenAI-format function tools; None only for mode="text".
    pub tools: Option<Vec<Value>>,
    /// Together documents tool_choice as auto|none|{function}; "required" is
    /// undocumented. Start with "required" (the loop's contract) and flip
    /// this for the rest of the session only once Together proves it rejects
    /// "required" — see send_request. Atomic because send_request is &self.
    pub tool_choice_auto: AtomicBool,
}

impl TogetherProvider {
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
        // Reasoning models bill their thinking as completion tokens; 4000
        // risks finish_reason "length" before the tool call is emitted.
        data.insert("max_tokens".into(), json!(10000));
        let use_auto = self.tool_choice_auto.load(Ordering::Relaxed);
        if let Some(tools) = &self.tools {
            data.insert("tools".into(), Value::Array(tools.clone()));
            // "required" — every turn must call at least one tool; canonical
            // rationale in openrouter/service.rs.
            data.insert("tool_choice".into(), json!(if use_auto { "auto" } else { "required" }));
        }

        // api.together.ai is an alias of the same endpoint.
        let url = api_url("https://api.together.xyz/v1/chat/completions");
        let auth = format!("Bearer {}", self.api_key);
        let headers = [
            ("Authorization", auth.as_str()),
            ("Content-Type", "application/json"),
        ];
        let mut resp = post_json(&url, &headers, &Value::Object(data.clone()))
            .map_err(|e| format!("Together API request failed: {e}"))?;
        if resp.status == 400 && self.tools.is_some() && !use_auto {
            // Retry once with "auto". Remember it ONLY if that works — an
            // unrelated 400 falls through and surfaces its own body. The
            // agent loop repairs the odd text-only turn ("no tool called"),
            // so "auto" degrades gracefully.
            data.insert("tool_choice".into(), json!("auto"));
            if let Ok(retry) = post_json(&url, &headers, &Value::Object(data)) {
                if retry.status < 400 {
                    self.tool_choice_auto.store(true, Ordering::Relaxed);
                    resp = retry;
                }
            }
        }
        if resp.status >= 400 {
            return Err(format!(
                "Together API request failed: HTTP {}\nResponse: {}",
                resp.status, resp.body
            ));
        }
        let result: Value = serde_json::from_str(&resp.body)
            .map_err(|e| format!("Together API request failed: {e}"))?;
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
                "content": if crate::browser::truthy(&content) { content } else { json!("") },
                "tool_calls": calls,
            }
        }],
        "usage": result.get("usage").cloned().unwrap_or(json!({})),
    })
}
