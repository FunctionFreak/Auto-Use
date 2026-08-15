// Copyright 2026 Ashish Yadav — Auto-Use

//! OpenRouter API provider.

use serde_json::{json, Map, Value};

use super::view::get_reasoning_params;
use crate::agent::main_driver::view::dumps;
use crate::llm_provider::{api_url, normalize_openai_tool_calls, post_json};

/// True when OpenRouter will hand this request to Google's backend, where a
/// tool result stops being a string and becomes a protobuf Struct.
fn is_gemini_route(model: &str) -> bool {
    let name = model.to_lowercase();
    name.starts_with("google/") || name.contains("gemini")
}

/// Carry one tool result to a Gemini route as a SINGLE JSON object. Gemini's
/// functionResponse body is a Struct, so the text we send is coerced into
/// JSON on the way in; wrapping the whole envelope as one JSON object makes
/// that coercion lossless — the Struct holds a single string field and the
/// model receives the envelope byte-for-byte. Only Google routes are
/// wrapped.
fn wrap_for_gemini(content: &Value) -> String {
    let text = match content {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => crate::agent::main_driver::view::py_str_of(other),
    };
    dumps(&json!({"tool_response": text}))
}

pub struct OpenRouterProvider {
    pub api_key: String,
    pub cli_agent: bool,
    /// OpenAI-format function tools — the model's output contract. None only
    /// for mode="text" (plain prose).
    pub tools: Option<Vec<Value>>,
}

impl OpenRouterProvider {
    pub fn send_request(
        &self,
        messages: &[Value],
        model: &str,
        annotated_screenshot_base64: Option<&str>,
    ) -> Result<Value, String> {
        // A `role: "tool"` message must carry a PLAIN STRING here: OpenRouter
        // forwards messages verbatim to whatever backend it routes to, and a
        // tool message carrying a parts-array with the Anthropic-only
        // cache_control key is not something a non-Anthropic backend is
        // obliged to read. Flatten it back to text. The same pass translates
        // the transcript's private `provider_meta` into this dialect
        // (reasoning_details back on the assistant turn) and ALWAYS removes
        // the key itself — it is ours, not OpenRouter's.
        let gemini_route = is_gemini_route(model);
        let mut prepared: Vec<Value> = Vec::new();
        for msg in messages {
            let mut msg = msg.clone();
            if msg.get("role").and_then(Value::as_str) == Some("tool") {
                if let Some(Value::Array(parts)) = msg.get("content") {
                    let flat = parts
                        .iter()
                        .filter(|p| p.get("type").and_then(Value::as_str) == Some("text"))
                        .map(|p| p.get("text").and_then(Value::as_str).unwrap_or(""))
                        .collect::<Vec<_>>()
                        .join("\n");
                    msg["content"] = Value::String(flat);
                }
                if gemini_route {
                    let wrapped = wrap_for_gemini(msg.get("content").unwrap_or(&Value::Null));
                    msg["content"] = Value::String(wrapped);
                }
            }
            let meta = msg.get("provider_meta").cloned();
            if let Some(meta) = meta {
                if !meta.is_null() {
                    if let Some(obj) = msg.as_object_mut() {
                        obj.shift_remove("provider_meta");
                    }
                    let is_assistant =
                        msg.get("role").and_then(Value::as_str) == Some("assistant");
                    let tagged = meta.get("provider").and_then(Value::as_str)
                        == Some("openrouter");
                    let details = meta.get("reasoning_details");
                    if is_assistant && tagged && details.map(crate::agent::browser::truthy).unwrap_or(false) {
                        msg["reasoning_details"] = details.cloned().unwrap_or(Value::Null);
                    }
                }
            }
            prepared.push(msg);
        }
        let mut messages = prepared;

        // Screenshot splice: the driver's vision, gated on `not cli_agent`.
        if let Some(shot) = annotated_screenshot_base64 {
            if !self.cli_agent && messages.len() > 1 {
                let last = messages.len() - 1;
                let user_message = messages[last].get("content").cloned().unwrap_or(Value::Null);
                messages[last]["content"] = json!([
                    {"type": "text", "text": user_message},
                    {"type": "image_url",
                     "image_url": {"url": format!("data:image/png;base64,{shot}")}},
                ]);
            }
        }

        let mut data = Map::new();
        data.insert("model".into(), json!(model));
        data.insert("messages".into(), Value::Array(messages));
        // 0.2: this loop wants the confident, repeatable choice every step.
        // Models that no longer accept the knob are unaffected — OpenRouter
        // drops parameters a route does not support.
        data.insert("temperature".into(), json!(0.2));
        data.insert("max_tokens".into(), json!(10000));
        data.insert("route".into(), json!("fallback"));
        // Sorting turns price-based load balancing OFF and pins the route by
        // throughput, which also keeps the prefix cache warm.
        data.insert("provider".into(), json!({"sort": "throughput"}));
        if let Value::Object(extra) = get_reasoning_params(model) {
            for (k, v) in extra {
                data.insert(k, v);
            }
        }
        if let Some(tools) = &self.tools {
            data.insert("tools".into(), Value::Array(tools.clone()));
            // "required" — every turn MUST call at least one tool. A
            // text-only turn is never a valid step in this loop: even
            // termination is a tool (`done` carries the final summary).
            data.insert("tool_choice".into(), json!("required"));
        }

        let url = api_url("https://openrouter.ai/api/v1/chat/completions");
        let auth = format!("Bearer {}", self.api_key);
        let headers = [
            ("Authorization", auth.as_str()),
            ("Content-Type", "application/json"),
        ];
        let resp = post_json(&url, &headers, &Value::Object(data))
            .map_err(|e| format!("OpenRouter API request failed: {e}"))?;
        if resp.status >= 400 {
            return Err(format!(
                "OpenRouter API request failed: HTTP {}\nResponse Body: {}",
                resp.status, resp.body
            ));
        }
        let result: Value = serde_json::from_str(&resp.body)
            .map_err(|e| format!("OpenRouter API request failed: {e}"))?;
        if self.tools.is_some() {
            Ok(normalize_tool_response(&result))
        } else {
            Ok(result)
        }
    }
}

/// Normalize an OpenAI-format tool-call response: keep the usual
/// choices/message shape but replace tool_calls with
/// [{"id", "name", "arguments": dict}]. `reasoning_details` is preserved as
/// `provider_meta` — it carries the reasoning blocks (including Gemini 3's
/// encrypted thought signature) this turn must be replayed with.
fn normalize_tool_response(result: &Value) -> Value {
    let message = result
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|c| c.first())
        .and_then(|c| c.get("message"))
        .cloned()
        .unwrap_or(json!({}));
    let calls = normalize_openai_tool_calls(&message);
    let mut out = Map::new();
    let content = message.get("content").cloned().unwrap_or(Value::Null);
    out.insert(
        "content".into(),
        if crate::agent::browser::truthy(&content) { content } else { json!("") },
    );
    out.insert("tool_calls".into(), Value::Array(calls));
    if let Some(Value::Array(details)) = message.get("reasoning_details") {
        if !details.is_empty() {
            out.insert(
                "provider_meta".into(),
                json!({"provider": "openrouter", "reasoning_details": details}),
            );
        }
    }
    json!({
        "choices": [{"message": Value::Object(out)}],
        "usage": result.get("usage").cloned().unwrap_or(json!({})),
    })
}
