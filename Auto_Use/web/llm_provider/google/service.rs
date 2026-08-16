// Copyright 2026 Ashish Yadav — Auto-Use

//! Google Gemini API provider. The Python original went through the
//! google-genai SDK; this speaks the same generateContent REST API directly —
//! Content/Part JSON, thought signatures riding as base64 strings on the
//! parts, function results mapped back by Gemini 3's own call ids.
//!
//! Vertex models authenticate with an OAuth access token. The SDK used
//! Application Default Credentials; here the token comes from
//! `gcloud auth print-access-token` (cached ~45 min) — the same credentials
//! ADC resolves for a developer machine.

use std::collections::{HashMap, HashSet};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

use super::view::get_thinking_level;
use crate::llm_provider::{api_url, args_dict, extract_text_first, post_json, SCREENSHOT_MEDIA_TYPE};

/// Recursively remove 'additionalProperties', which the Gemini API doesn't
/// support, and spell schema `type` values the way the genai SDK does — as
/// the Schema proto's enum names ("object" -> "OBJECT", "string" ->
/// "STRING"). Non-mutating — the shared registries are safe.
fn clean_schema_for_google(schema: &Value) -> Value {
    match schema {
        Value::Object(map) => Value::Object(
            map.iter()
                .filter(|(k, _)| k.as_str() != "additionalProperties")
                .map(|(k, v)| {
                    if k == "type" {
                        if let Value::String(t) = v {
                            return (k.clone(), Value::String(t.to_uppercase()));
                        }
                    }
                    (k.clone(), clean_schema_for_google(v))
                })
                .collect(),
        ),
        Value::Array(items) => {
            Value::Array(items.iter().map(clean_schema_for_google).collect())
        }
        other => other.clone(),
    }
}

/// One functionCall part, carrying back the thought signature Gemini 3
/// minted for it — how the model re-attaches its own call (and the
/// functionResponse that follows) to the reasoning that produced it.
fn function_call_part(name: &str, args: Value, signature: &str) -> Value {
    let mut part = Map::new();
    part.insert("functionCall".into(), json!({"name": name, "args": args}));
    if !signature.is_empty() {
        // REST carries the signature as a base64 string — exactly how the
        // transcript stores it.
        part.insert("thoughtSignature".into(), json!(signature));
    }
    Value::Object(part)
}

/// One functionResponse part. Gemini 3 mints an `id` on every functionCall
/// and maps the result back BY that id — echo it whenever the model gave us
/// one; name-only matching is ambiguous the moment one step calls the same
/// tool twice, which this agent does routinely.
fn function_response_part(name: &str, output: &str, call_id: &str) -> Value {
    let mut fr = Map::new();
    if !call_id.is_empty() {
        fr.insert("id".into(), json!(call_id));
    }
    fr.insert("name".into(), json!(name));
    fr.insert("response".into(), json!({"output": output}));
    json!({"functionResponse": Value::Object(fr)})
}

/// Access token for Vertex calls, from gcloud's ADC, cached ~45 minutes.
fn vertex_access_token() -> Result<String, String> {
    static CACHE: Mutex<Option<(String, Instant)>> = Mutex::new(None);
    let mut cache = CACHE.lock().unwrap();
    if let Some((token, at)) = cache.as_ref() {
        if at.elapsed() < Duration::from_secs(45 * 60) {
            return Ok(token.clone());
        }
    }
    let out = std::process::Command::new("gcloud")
        .args(["auth", "print-access-token"])
        .output()
        .map_err(|e| format!("could not run gcloud for Vertex credentials: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "gcloud auth print-access-token failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    let token = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if token.is_empty() {
        return Err("gcloud returned an empty Vertex access token".to_string());
    }
    *cache = Some((token.clone(), Instant::now()));
    Ok(token)
}

pub struct GoogleProvider {
    pub api_key: Option<String>,
    pub cli_agent: bool,
    pub is_vertex: bool,
    pub project: Option<String>,
    pub location: String,
    /// Function declarations (schemas cleaned); None only for mode="text".
    pub tools: Option<Vec<Value>>,
}

impl GoogleProvider {
    pub fn new(
        api_key: Option<String>,
        cli_agent: bool,
        is_vertex: bool,
        project: Option<String>,
        location: Option<String>,
        tools: Option<Vec<Value>>,
    ) -> Self {
        GoogleProvider {
            api_key,
            cli_agent,
            is_vertex,
            project,
            location: location.unwrap_or_else(|| "global".to_string()),
            tools: tools.map(|list| list.iter().map(clean_schema_for_google).collect()),
        }
    }

    fn endpoint(&self, model: &str) -> String {
        if let Ok(override_url) = std::env::var("AUTOUSE_LLM_API_URL") {
            return format!("{override_url}/{model}:generateContent");
        }
        if self.is_vertex {
            let project = self.project.as_deref().unwrap_or("");
            let loc = &self.location;
            if loc == "global" {
                format!(
                    "https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/publishers/google/models/{model}:generateContent"
                )
            } else {
                format!(
                    "https://{loc}-aiplatform.googleapis.com/v1/projects/{project}/locations/{loc}/publishers/google/models/{model}:generateContent"
                )
            }
        } else {
            let _ = api_url(""); // the non-vertex override path is handled above
            format!(
                "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            )
        }
    }

    pub fn send_request(
        &self,
        messages: &[Value],
        model: &str,
        annotated_screenshot_base64: Option<&str>,
    ) -> Result<Value, String> {
        // NATIVE TRANSCRIPT translation: function_call / function_response
        // parts, results matched by name (id -> name tracked while walking)
        // and by Gemini 3's own ids where the model minted them.
        let mut system_instruction: Option<String> = None;
        let mut contents: Vec<Value> = Vec::new();
        let mut call_names: HashMap<String, String> = HashMap::new();
        let mut native_ids: HashSet<String> = HashSet::new();

        for msg in messages {
            let role = msg.get("role").and_then(Value::as_str).unwrap_or("");
            let raw_content = msg.get("content").cloned().unwrap_or(json!(""));
            match role {
                "system" => {
                    system_instruction = Some(extract_text_first(&raw_content));
                }
                "tool" => {
                    let call_id = msg
                        .get("tool_call_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string();
                    let name = call_names
                        .get(&call_id)
                        .cloned()
                        .or_else(|| {
                            msg.get("name").and_then(Value::as_str).map(str::to_string)
                        })
                        .unwrap_or_else(|| "tool".to_string());
                    let echo_id = if native_ids.contains(&call_id) { call_id.as_str() } else { "" };
                    contents.push(json!({
                        "role": "user",
                        "parts": [function_response_part(
                            &name, &extract_text_first(&raw_content), echo_id)],
                    }));
                }
                "assistant" => {
                    let text = extract_text_first(&raw_content);
                    let meta = msg.get("provider_meta").cloned().unwrap_or(json!({}));
                    let meta = if meta.get("provider").and_then(Value::as_str) == Some("google") {
                        meta
                    } else {
                        json!({})
                    };
                    let sigs = meta.get("signatures").cloned().unwrap_or(json!({}));
                    for id in meta
                        .get("native_ids")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default()
                    {
                        if let Some(id) = id.as_str() {
                            native_ids.insert(id.to_string());
                        }
                    }
                    let mut parts: Vec<Value> = Vec::new();
                    if !text.trim().is_empty() {
                        parts.push(json!({"text": text}));
                    }
                    for tc in msg
                        .get("tool_calls")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default()
                    {
                        let fn_ = tc.get("function").cloned().unwrap_or(json!({}));
                        let fname = fn_.get("name").and_then(Value::as_str).unwrap_or("");
                        let tc_id = tc.get("id").and_then(Value::as_str).unwrap_or("");
                        call_names.insert(tc_id.to_string(), fname.to_string());
                        let sig = sigs.get(tc_id).and_then(Value::as_str).unwrap_or("");
                        parts.push(function_call_part(
                            fname,
                            args_dict(fn_.get("arguments")),
                            sig,
                        ));
                    }
                    if parts.is_empty() {
                        parts.push(json!({"text": text}));
                    }
                    contents.push(json!({"role": "model", "parts": parts}));
                }
                "user" => {
                    contents.push(json!({
                        "role": "user",
                        "parts": [{"text": extract_text_first(&raw_content)}],
                    }));
                }
                _ => {}
            }
        }

        // Screenshot on the last user message.
        if let Some(shot) = annotated_screenshot_base64 {
            if !self.cli_agent && !contents.is_empty() {
                let last = contents.len() - 1;
                if contents[last].get("role").and_then(Value::as_str) == Some("user") {
                    if let Some(Value::Array(parts)) = contents[last].get_mut("parts") {
                        parts.push(json!({
                            "inlineData": {"mimeType": SCREENSHOT_MEDIA_TYPE, "data": shot}
                        }));
                    }
                }
            }
        }

        let mut generation_config = Map::new();
        generation_config.insert("maxOutputTokens".into(), json!(10000));
        // Gemini 3 takes a named level, not a token budget. Omitted for an
        // unregistered model, which then keeps Google's (expensive) default.
        if let Some(level) = get_thinking_level(model) {
            // snake_case field, matching what the genai SDK actually sends
            // (the API accepts both spellings; the SDK's is ground truth).
            generation_config.insert(
                "thinkingConfig".into(),
                json!({"thinking_level": level.to_uppercase()}),
            );
        }

        let mut body = Map::new();
        body.insert("contents".into(), Value::Array(contents));
        if let Some(system) = &system_instruction {
            // The SDK stamps role "user" on the systemInstruction Content —
            // mirrored for byte-identical payloads.
            body.insert(
                "systemInstruction".into(),
                json!({"parts": [{"text": system}], "role": "user"}),
            );
        }
        if let Some(tools) = &self.tools {
            body.insert("tools".into(), json!([{"functionDeclarations": tools}]));
            // mode="ANY" — every turn must call at least one function;
            // canonical rationale in openrouter/service.rs.
            body.insert(
                "toolConfig".into(),
                json!({"functionCallingConfig": {"mode": "ANY"}}),
            );
        }
        body.insert("generationConfig".into(), Value::Object(generation_config));

        let url = self.endpoint(model);
        let attempt = (|| -> Result<Value, String> {
            let resp = if self.is_vertex {
                let token = vertex_access_token()?;
                let auth = format!("Bearer {token}");
                post_json(
                    &url,
                    &[("Authorization", auth.as_str()), ("Content-Type", "application/json")],
                    &Value::Object(body.clone()),
                )?
            } else {
                let key = self.api_key.clone().unwrap_or_default();
                post_json(
                    &url,
                    &[("x-goog-api-key", key.as_str()), ("Content-Type", "application/json")],
                    &Value::Object(body.clone()),
                )?
            };
            if resp.status >= 400 {
                return Err(format!("HTTP {}: {}", resp.status, resp.body));
            }
            serde_json::from_str(&resp.body).map_err(|e| e.to_string())
        })();
        let result = attempt.map_err(|e| format!("Google Gemini API request failed: {e}"))?;

        // Normalize to the OpenAI-style shape the manager reads.
        let um = result.get("usageMetadata");
        let usage = match um {
            Some(um) => json!({
                "input_tokens": um.get("promptTokenCount").cloned().unwrap_or(json!(0)),
                "output_tokens": um.get("candidatesTokenCount").cloned().unwrap_or(json!(0)),
            }),
            None => json!({}),
        };
        let parts = result
            .get("candidates")
            .and_then(Value::as_array)
            .and_then(|c| c.first())
            .and_then(|c| c.get("content"))
            .and_then(|c| c.get("parts"))
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();

        if self.tools.is_some() {
            let mut texts: Vec<String> = Vec::new();
            let mut calls: Vec<Value> = Vec::new();
            let mut signatures = Map::new();
            let mut native_out: Vec<Value> = Vec::new();
            for (i, part) in parts.iter().enumerate() {
                if part.get("thought").and_then(Value::as_bool) == Some(true) {
                    continue;
                }
                if let Some(fc) = part.get("functionCall") {
                    // Gemini 3 mints its own call id; older models don't, so
                    // fall back to a stable synthetic one and remember which
                    // is which.
                    let native_id = fc.get("id").and_then(Value::as_str).unwrap_or("");
                    let call_id = if native_id.is_empty() {
                        format!("call_{i}")
                    } else {
                        native_id.to_string()
                    };
                    let args = fc.get("args").cloned().unwrap_or(json!({}));
                    calls.push(json!({
                        "id": call_id,
                        "name": fc.get("name").and_then(Value::as_str).unwrap_or(""),
                        "arguments": if args.is_object() { args } else { json!({}) },
                    }));
                    if !native_id.is_empty() {
                        native_out.push(json!(call_id));
                    }
                    if let Some(sig) = part.get("thoughtSignature").and_then(Value::as_str) {
                        if !sig.is_empty() {
                            signatures.insert(call_id, json!(sig));
                        }
                    }
                } else if let Some(text) = part.get("text").and_then(Value::as_str) {
                    if !text.is_empty() {
                        texts.push(text.to_string());
                    }
                }
            }
            let mut message = Map::new();
            message.insert("content".into(), json!(texts.join("\n")));
            message.insert("tool_calls".into(), Value::Array(calls));
            if !signatures.is_empty() || !native_out.is_empty() {
                message.insert(
                    "provider_meta".into(),
                    json!({"provider": "google", "signatures": Value::Object(signatures),
                           "native_ids": native_out}),
                );
            }
            return Ok(json!({
                "choices": [{"message": Value::Object(message)}],
                "usage": usage,
            }));
        }

        // Text mode: concatenate the non-thought text parts (SDK .text).
        let text: String = parts
            .iter()
            .filter(|p| p.get("thought").and_then(Value::as_bool) != Some(true))
            .filter_map(|p| p.get("text").and_then(Value::as_str))
            .collect();
        Ok(json!({
            "choices": [{"message": {"content": text}}],
            "usage": usage,
        }))
    }
}
