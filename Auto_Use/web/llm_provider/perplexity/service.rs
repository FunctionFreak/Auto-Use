// Copyright 2026 Ashish Yadav — Auto-Use

//! Perplexity Agent API provider (Responses-style dialect).

use serde_json::{json, Map, Value};

use super::view::get_reasoning_params;
use crate::agent::main_driver::view::dumps;
use crate::llm_provider::{api_url, args_dict, extract_text_first, post_json, SCREENSHOT_MEDIA_TYPE};

pub struct PerplexityProvider {
    pub api_key: String,
    pub cli_agent: bool,
    /// Responses-style flat function tools; None only for mode="text".
    pub tools: Option<Vec<Value>>,
}

impl PerplexityProvider {
    pub fn send_request(
        &self,
        messages: &[Value],
        model: &str,
        annotated_screenshot_base64: Option<&str>,
    ) -> Result<Value, String> {
        // Separate system prompt (top-level `instructions`) from the
        // conversation, translating the NATIVE TRANSCRIPT into the Responses
        // dialect: flat function_call / function_call_output items.
        let mut instructions: Option<String> = None;
        let mut input_messages: Vec<Value> = Vec::new();

        for msg in messages {
            let role = msg.get("role").and_then(Value::as_str).unwrap_or("");
            match role {
                "system" => {
                    let content = msg.get("content").cloned().unwrap_or(json!(""));
                    instructions = Some(match &content {
                        Value::Array(items) => items
                            .iter()
                            .find(|it| it.get("type").and_then(Value::as_str) == Some("text"))
                            .and_then(|it| it.get("text").and_then(Value::as_str))
                            .unwrap_or("")
                            .to_string(),
                        Value::String(s) => s.clone(),
                        other => crate::agent::main_driver::view::py_str_of(other),
                    });
                }
                "tool" => {
                    input_messages.push(json!({
                        "type": "function_call_output",
                        "call_id": msg.get("tool_call_id").and_then(Value::as_str).unwrap_or(""),
                        "output": extract_text_first(msg.get("content").unwrap_or(&Value::Null)),
                    }));
                }
                "assistant" => {
                    let text = extract_text_first(msg.get("content").unwrap_or(&Value::Null));
                    if !text.trim().is_empty() {
                        input_messages.push(json!({
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        }));
                    }
                    for tc in msg
                        .get("tool_calls")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default()
                    {
                        let fn_ = tc.get("function").cloned().unwrap_or(json!({}));
                        let raw = fn_.get("arguments").cloned().unwrap_or(Value::Null);
                        let arguments = match raw {
                            Value::String(s) => s,
                            Value::Null => dumps(&json!({})),
                            other => dumps(&other),
                        };
                        input_messages.push(json!({
                            "type": "function_call",
                            "call_id": tc.get("id").and_then(Value::as_str).unwrap_or(""),
                            "name": fn_.get("name").and_then(Value::as_str).unwrap_or(""),
                            "arguments": arguments,
                        }));
                    }
                }
                "user" => {
                    input_messages.push(json!({
                        "role": "user",
                        "content": [{"type": "input_text",
                                     "text": extract_text_first(msg.get("content").unwrap_or(&Value::Null))}],
                    }));
                }
                _ => {}
            }
        }

        // Screenshot on the last user message — `last` may be a flat
        // function_call* item with no "role" key.
        if let Some(shot) = annotated_screenshot_base64 {
            if !self.cli_agent && !input_messages.is_empty() {
                let last = input_messages.len() - 1;
                if input_messages[last].get("role").and_then(Value::as_str) == Some("user") {
                    if let Some(Value::Array(content)) = input_messages[last].get_mut("content") {
                        content.push(json!({
                            "type": "input_image",
                            "image_url": format!("data:{SCREENSHOT_MEDIA_TYPE};base64,{shot}"),
                        }));
                    }
                }
            }
        }

        let mut data = Map::new();
        data.insert("model".into(), json!(model));
        data.insert("input".into(), Value::Array(input_messages));
        data.insert("max_output_tokens".into(), json!(10000));
        if let Value::Object(extra) = get_reasoning_params(model) {
            for (k, v) in extra {
                data.insert(k, v);
            }
        }
        if let Some(instructions) = &instructions {
            if !instructions.is_empty() {
                data.insert("instructions".into(), json!(instructions));
            }
        }
        if let Some(tools) = &self.tools {
            data.insert("tools".into(), Value::Array(tools.clone()));
            // Canonical rationale in openrouter/service.rs.
            data.insert("tool_choice".into(), json!("required"));
        }

        let url = api_url("https://api.perplexity.ai/v1/agent");
        let auth = format!("Bearer {}", self.api_key);
        let headers = [
            ("Authorization", auth.as_str()),
            ("Content-Type", "application/json"),
        ];
        let resp = post_json(&url, &headers, &Value::Object(data))
            .map_err(|e| format!("Perplexity API request failed: {e}"))?;
        if resp.status >= 400 {
            return Err(format!(
                "Perplexity API request failed: HTTP {}\nResponse Body: {}",
                resp.status, resp.body
            ));
        }
        let result: Value = serde_json::from_str(&resp.body)
            .map_err(|e| format!("Perplexity API request failed: {e}"))?;

        // Normalize to choices[0].message.content format.
        let mut text_content = String::new();
        let mut calls: Vec<Value> = Vec::new();
        for output_item in result.get("output").and_then(Value::as_array).cloned().unwrap_or_default() {
            match output_item.get("type").and_then(Value::as_str) {
                Some("message") => {
                    for content_block in output_item
                        .get("content")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default()
                    {
                        if content_block.get("type").and_then(Value::as_str)
                            == Some("output_text")
                            && text_content.is_empty()
                        {
                            text_content = content_block
                                .get("text")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_string();
                        }
                    }
                }
                Some("function_call") if self.tools.is_some() => {
                    let args = args_dict(output_item.get("arguments"));
                    let id = ["call_id", "id"]
                        .iter()
                        .find_map(|k| {
                            let v = output_item.get(*k)?;
                            let s = crate::agent::main_driver::view::py_str_of(v);
                            if s.is_empty() || s == "None" { None } else { Some(s) }
                        })
                        .unwrap_or_else(|| format!("call_{}", calls.len()));
                    calls.push(json!({
                        "id": id,
                        "name": output_item.get("name").and_then(Value::as_str).unwrap_or(""),
                        "arguments": args,
                    }));
                }
                _ => {}
            }
        }

        let mut message = Map::new();
        message.insert("content".into(), json!(text_content));
        if self.tools.is_some() {
            message.insert("tool_calls".into(), Value::Array(calls));
        }
        Ok(json!({
            "choices": [{"message": Value::Object(message)}],
            "usage": result.get("usage").cloned().unwrap_or(json!({})),
        }))
    }
}
