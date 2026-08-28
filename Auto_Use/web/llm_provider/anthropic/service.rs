// Copyright 2026 Cursortouch — Auto-Use

//! Anthropic API provider.

use serde_json::{json, Map, Value};

use super::view::{get_sampling_params, get_thinking_params};
use crate::browser::truthy;
use crate::llm_provider::{api_url, args_dict, post_json, SCREENSHOT_MEDIA_TYPE};

/// Flatten a message content field (string, or list of content blocks) to
/// plain text — used when translating the canonical OpenAI-shaped transcript
/// into Anthropic's block format.
fn as_text(content: &Value) -> String {
    match content {
        Value::String(s) => s.clone(),
        Value::Array(items) => items
            .iter()
            .filter(|b| b.get("type").and_then(Value::as_str) == Some("text"))
            .filter_map(|b| b.get("text").and_then(Value::as_str))
            .filter(|t| !t.is_empty())
            .collect::<Vec<_>>()
            .join("\n"),
        Value::Null => String::new(),
        other => crate::agent::main_driver::view::py_str_of(other),
    }
}

/// Recursively strip JSON Schema keywords not supported by Anthropic.
fn strip_unsupported_keywords(obj: &mut Value) {
    match obj {
        Value::Object(map) => {
            for key in ["maxItems", "minItems", "strict"] {
                map.shift_remove(key);
            }
            for (_, v) in map.iter_mut() {
                strip_unsupported_keywords(v);
            }
        }
        Value::Array(items) => {
            for item in items {
                strip_unsupported_keywords(item);
            }
        }
        _ => {}
    }
}

pub struct AnthropicProvider {
    pub api_key: String,
    pub cli_agent: bool,
    /// Messages-format tools ({name, input_schema, description}); None only
    /// for mode="text". Stripped of unsupported schema keywords on a copy —
    /// never on the shared registry.
    pub tools: Option<Vec<Value>>,
}

impl AnthropicProvider {
    pub fn new(api_key: String, cli_agent: bool, tools: Option<Vec<Value>>) -> Self {
        let tools = tools.map(|list| {
            list.into_iter()
                .map(|mut t| {
                    if let Some(schema) = t.get_mut("input_schema") {
                        strip_unsupported_keywords(schema);
                    }
                    t
                })
                .collect()
        });
        AnthropicProvider { api_key, cli_agent, tools }
    }

    pub fn send_request(
        &self,
        messages: &[Value],
        model: &str,
        annotated_screenshot_base64: Option<&str>,
    ) -> Result<Value, String> {
        // Extract system prompt (Anthropic uses a top-level 'system' field)
        // and translate the NATIVE TRANSCRIPT: tool_use blocks on the
        // assistant turn, tool_result blocks on a following user turn.
        let mut system_content: Option<Value> = None;
        let mut api_messages: Vec<Value> = Vec::new();

        for msg in messages {
            let role = msg.get("role").and_then(Value::as_str).unwrap_or("");
            if role == "system" {
                system_content = msg.get("content").cloned();
                continue;
            }

            if role == "tool" {
                let raw = msg.get("content").cloned().unwrap_or(Value::Null);
                let mut block = Map::new();
                block.insert("type".into(), json!("tool_result"));
                block.insert(
                    "tool_use_id".into(),
                    json!(msg.get("tool_call_id").and_then(Value::as_str).unwrap_or("")),
                );
                block.insert("content".into(), json!(as_text(&raw)));
                if msg.get("is_error").map(truthy).unwrap_or(false) {
                    block.insert("is_error".into(), json!(true));
                }
                // Cache breakpoint: the loop marks its newest persistent turn
                // with a parts-array cache_control, which as_text drops —
                // lift it onto the tool_result block instead.
                if let Value::Array(parts) = &raw {
                    if parts.iter().any(|p| p.get("cache_control").is_some()) {
                        block.insert("cache_control".into(), json!({"type": "ephemeral"}));
                    }
                }
                // Results for a batch of calls must ride on ONE user turn.
                let joined = match api_messages.last_mut() {
                    Some(prev)
                        if prev.get("role").and_then(Value::as_str) == Some("user")
                            && prev
                                .get("content")
                                .and_then(Value::as_array)
                                .and_then(|c| c.first())
                                .map(|b| {
                                    b.get("type").and_then(Value::as_str)
                                        == Some("tool_result")
                                })
                                .unwrap_or(false) =>
                    {
                        if let Some(Value::Array(content)) = prev.get_mut("content") {
                            content.push(Value::Object(block.clone()));
                        }
                        true
                    }
                    _ => false,
                };
                if !joined {
                    api_messages.push(json!({"role": "user", "content": [Value::Object(block)]}));
                }
                continue;
            }

            let tool_calls = msg.get("tool_calls").and_then(Value::as_array);
            if role == "assistant" && tool_calls.map(|c| !c.is_empty()).unwrap_or(false) {
                let mut blocks: Vec<Value> = Vec::new();
                let text = as_text(msg.get("content").unwrap_or(&Value::Null));
                if !text.trim().is_empty() {
                    blocks.push(json!({"type": "text", "text": text}));
                }
                for tc in tool_calls.unwrap() {
                    let fn_ = tc.get("function").cloned().unwrap_or(json!({}));
                    blocks.push(json!({
                        "type": "tool_use",
                        "id": tc.get("id").and_then(Value::as_str).unwrap_or(""),
                        "name": fn_.get("name").and_then(Value::as_str).unwrap_or(""),
                        "input": args_dict(fn_.get("arguments")),
                    }));
                }
                api_messages.push(json!({"role": "assistant", "content": blocks}));
                continue;
            }

            api_messages.push(json!({
                "role": role,
                "content": msg.get("content").cloned().unwrap_or(Value::Null),
            }));
        }

        // Screenshot splice — skipped when the transcript ends on tool_result
        // blocks (rebuilding as [text, image] would destroy them).
        if let Some(shot) = annotated_screenshot_base64 {
            if !self.cli_agent && !api_messages.is_empty() {
                let last = api_messages.len() - 1;
                let content = api_messages[last].get("content").cloned().unwrap_or(Value::Null);
                let is_tool_result = matches!(&content, Value::Array(items)
                    if items.first()
                        .map(|b| b.get("type").and_then(Value::as_str) == Some("tool_result"))
                        .unwrap_or(false));
                let is_user =
                    api_messages[last].get("role").and_then(Value::as_str) == Some("user");
                if is_user && !is_tool_result {
                    let mut user_text = content;
                    if let Value::Array(items) = &user_text {
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
                        user_text = Value::String(text_content);
                    }
                    api_messages[last]["content"] = json!([
                        {"type": "text", "text": user_text},
                        {"type": "image",
                         "source": {"type": "base64", "media_type": SCREENSHOT_MEDIA_TYPE,
                                    "data": shot}},
                    ]);
                }
            }
        }

        let mut data = Map::new();
        data.insert("model".into(), json!(model));
        data.insert("messages".into(), Value::Array(api_messages));
        data.insert("max_tokens".into(), json!(4000));
        // top_p / top_k per model — never temperature; then adaptive thinking
        // + effort for the models that take those instead. The two feature
        // sets are mutually exclusive per model.
        if let Value::Object(extra) = get_sampling_params(model) {
            for (k, v) in extra {
                data.insert(k, v);
            }
        }
        if let Value::Object(extra) = get_thinking_params(model) {
            for (k, v) in extra {
                data.insert(k, v);
            }
        }
        // System as a list with cache_control for prompt caching.
        if let Some(system) = &system_content {
            if truthy(system) {
                data.insert(
                    "system".into(),
                    json!([{"type": "text", "text": system,
                            "cache_control": {"type": "ephemeral"}}]),
                );
            }
        }
        if let Some(tools) = &self.tools {
            data.insert("tools".into(), Value::Array(tools.clone()));
            // {"type": "any"} — every turn must call at least one tool;
            // canonical rationale in openrouter/service.rs.
            data.insert("tool_choice".into(), json!({"type": "any"}));
        }

        let url = api_url("https://api.anthropic.com/v1/messages");
        let headers = [
            ("x-api-key", self.api_key.as_str()),
            ("anthropic-version", "2023-06-01"),
            ("content-type", "application/json"),
        ];
        let resp = post_json(&url, &headers, &Value::Object(data))
            .map_err(|e| format!("Anthropic API request failed: {e}"))?;
        if resp.status >= 400 {
            return Err(format!(
                "Anthropic API request failed: HTTP {}\nResponse Body: {}",
                resp.status, resp.body
            ));
        }
        let result: Value = serde_json::from_str(&resp.body)
            .map_err(|e| format!("Anthropic API request failed: {e}"))?;

        // Normalize to the OpenAI-style shape the manager reads.
        let content_blocks = result
            .get("content")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if self.tools.is_some() {
            // Collect ALL text blocks (prose can interleave with tool_use)
            // and every tool_use block.
            let mut texts: Vec<String> = Vec::new();
            let mut calls: Vec<Value> = Vec::new();
            for (i, block) in content_blocks.iter().enumerate() {
                match block.get("type").and_then(Value::as_str) {
                    Some("text") => {
                        texts.push(
                            block.get("text").and_then(Value::as_str).unwrap_or("").to_string(),
                        );
                    }
                    Some("tool_use") => {
                        let args = block.get("input").cloned().unwrap_or(json!({}));
                        let id = block
                            .get("id")
                            .map(crate::agent::main_driver::view::py_str_of)
                            .filter(|s| !s.is_empty() && s != "None")
                            .unwrap_or_else(|| format!("call_{i}"));
                        calls.push(json!({
                            "id": id,
                            "name": block.get("name").and_then(Value::as_str).unwrap_or(""),
                            "arguments": if args.is_object() { args } else { json!({}) },
                        }));
                    }
                    _ => {}
                }
            }
            let joined = texts.iter().filter(|t| !t.is_empty()).cloned()
                .collect::<Vec<_>>().join("\n");
            return Ok(json!({
                "choices": [{"message": {"content": joined, "tool_calls": calls}}],
                "usage": result.get("usage").cloned().unwrap_or(json!({})),
            }));
        }

        let mut text_content = String::new();
        for block in &content_blocks {
            if block.get("type").and_then(Value::as_str) == Some("text") {
                text_content = block.get("text").and_then(Value::as_str).unwrap_or("").to_string();
                break;
            }
        }
        Ok(json!({
            "choices": [{"message": {"content": text_content}}],
            "usage": result.get("usage").cloned().unwrap_or(json!({})),
        }))
    }
}
