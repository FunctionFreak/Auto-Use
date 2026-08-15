// Copyright 2026 Ashish Yadav — Auto-Use

//! LLM Provider module for managing different language model providers —
//! the Rust analog of `llm_provider/__init__.py`. One folder per provider,
//! service.rs (the wire client) + view.rs (the model table), same names the
//! Python package used.

use std::sync::{Arc, OnceLock};

use serde_json::Value;

pub mod llm_manager;

pub mod openrouter {
    pub mod service;
    pub mod view;
}
pub mod groq {
    pub mod service;
    pub mod view;
}
pub mod openai {
    pub mod service;
    pub mod view;
}
pub mod anthropic {
    pub mod service;
    pub mod view;
}
pub mod google {
    pub mod service;
    pub mod view;
}
pub mod perplexity {
    pub mod service;
    pub mod view;
}

/// A provider endpoint, overridable with AUTOUSE_LLM_API_URL — a test hook so
/// the differential/e2e suites can point every provider at a local mock
/// server. Never set in normal runs.
pub fn api_url(default: &str) -> String {
    std::env::var("AUTOUSE_LLM_API_URL").unwrap_or_else(|_| default.to_string())
}

pub struct HttpResponse {
    pub status: u16,
    pub body: String,
}

/// One JSON POST. HTTP error statuses come back as a normal response (the
/// caller formats the provider-specific error message, body included, exactly
/// as the Python `raise_for_status` handlers did); only transport failures
/// are an Err.
/// One shared HTTP agent that trusts the OPERATING SYSTEM's certificates on
/// top of the bundled Mozilla roots.
///
/// ureq's default agent validates against `webpki-roots` alone — a root list
/// compiled into the binary. That breaks on any machine where something
/// re-signs TLS: antivirus with HTTPS scanning (Norton's "Web/Mail Shield",
/// ESET, Kaspersky, Bitdefender) or a corporate proxy (Zscaler, Netskope).
/// Those products install their root into the OS trust store, so the browser
/// is happy while every request from here dies with
/// `invalid peer certificate: UnknownIssuer`.
///
/// Loading the OS store fixes it and is what the platform expects. The bundled
/// roots stay as a floor, so a machine with an unreadable or empty system store
/// still validates normally.
fn agent() -> &'static ureq::Agent {
    static AGENT: OnceLock<ureq::Agent> = OnceLock::new();
    AGENT.get_or_init(|| {
        let mut roots = rustls::RootCertStore::empty();
        roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
        // load_native_certs() reports partial success: it returns whatever it
        // could read plus any errors, so a store that is unreadable in part
        // still contributes the certs it did yield. Individual unparseable
        // certs are skipped — the bundled roots above already cover the public
        // web, so the only thing at stake here is the interceptor's own root.
        for cert in rustls_native_certs::load_native_certs().certs {
            let _ = roots.add(cert);
        }
        let config = rustls::ClientConfig::builder()
            .with_root_certificates(roots)
            .with_no_client_auth();
        ureq::AgentBuilder::new().tls_config(Arc::new(config)).build()
    })
}

pub fn post_json(url: &str, headers: &[(&str, &str)], body: &Value) -> Result<HttpResponse, String> {
    let mut req = agent().post(url);
    for (k, v) in headers {
        req = req.set(k, v);
    }
    let payload = serde_json::to_string(body).map_err(|e| e.to_string())?;
    match req.send_string(&payload) {
        Ok(resp) => {
            let status = resp.status();
            let body = resp.into_string().map_err(|e| e.to_string())?;
            Ok(HttpResponse { status, body })
        }
        Err(ureq::Error::Status(status, resp)) => {
            let body = resp.into_string().unwrap_or_default();
            Ok(HttpResponse { status, body })
        }
        Err(e) => Err(e.to_string()),
    }
}

/// Tool-call arguments as a dict (they may ride as a JSON string in the
/// canonical transcript; malformed -> {}).
pub fn args_dict(raw: Option<&Value>) -> Value {
    match raw {
        Some(Value::Object(o)) => Value::Object(o.clone()),
        Some(Value::String(s)) => match serde_json::from_str::<Value>(if s.is_empty() { "{}" } else { s }) {
            Ok(Value::Object(o)) => Value::Object(o),
            _ => serde_json::json!({}),
        },
        _ => serde_json::json!({}),
    }
}

/// Extract text from a message content field (string, or list of content
/// blocks with cache_control) — the shared `_extract_text` shape: first text
/// block wins for list content.
pub fn extract_text_first(content: &Value) -> String {
    match content {
        Value::String(s) => s.clone(),
        Value::Array(items) => {
            for item in items {
                if item.get("type").and_then(Value::as_str) == Some("text") {
                    return item
                        .get("text")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string();
                }
            }
            crate::agent::main_driver::view::dumps(content)
        }
        Value::Null => String::new(),
        other => crate::agent::main_driver::view::py_str_of(other),
    }
}

/// Normalize one OpenAI-format tool call list into
/// [{"id", "name", "arguments": dict}] — shared by the chat-completions
/// providers (openrouter/groq), matching their identical Python helpers.
pub fn normalize_openai_tool_calls(message: &Value) -> Vec<Value> {
    let mut calls = Vec::new();
    let list = message
        .get("tool_calls")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for (i, tc) in list.iter().enumerate() {
        let fn_ = tc.get("function").cloned().unwrap_or(serde_json::json!({}));
        let args = args_dict(fn_.get("arguments"));
        let id = tc
            .get("id")
            .map(crate::agent::main_driver::view::py_str_of)
            .filter(|s| !s.is_empty() && s != "None")
            .unwrap_or_else(|| format!("call_{i}"));
        calls.push(serde_json::json!({
            "id": id,
            "name": fn_.get("name").and_then(Value::as_str).unwrap_or(""),
            "arguments": args,
        }));
    }
    calls
}
