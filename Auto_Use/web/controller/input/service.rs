// Copyright 2026 Cursortouch — Auto-Use

//! Typing into an element.
//!
//! Focus the field, clear it, type, optionally submit — dispatched here over
//! the CDP session the browser side owns and lends out. Trusted keyboard
//! input through Input.dispatchKeyEvent / Input.insertText: no JavaScript in
//! the page, nothing injected.
//!
//! `enter` matters more here than it would on desktop: the browser agent has
//! no `hotkey` tool, so without it a search box could be filled but never
//! submitted.

use std::sync::{Arc, Mutex};

use pyo3::prelude::*;
use serde_json::{json, Map, Value};

use crate::browser::{truthy, Cdp, CdpFail, ScannerInner};
use crate::controller::click::service::press;
use crate::controller::service::{
    label_for, rect_for, reject_viewport, resolve_element_id, with_box, ActResult,
};
use crate::agent::main_driver::view::py_str_of;

/// One key down + up. `text` is what the key inserts, empty for keys that
/// insert nothing.
fn key(cdp: &mut Cdp, sess: &str, key: &str, vk: i64, text: &str) -> Result<(), CdpFail> {
    for kind in ["keyDown", "keyUp"] {
        let mut p = json!({
            "type": kind,
            "key": key,
            "code": key,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
        });
        if kind == "keyDown" && !text.is_empty() {
            p["text"] = json!(text);
        }
        cdp.rpc("Input.dispatchKeyEvent", p, Some(sess), 5.0)?;
    }
    Ok(())
}

/// Focus the element at `rect`, clear whatever is in it, type `text`, and
/// submit when asked.
fn type_into_rect(
    cdp: &mut Cdp,
    sess: &str,
    rect: &[f64; 4],
    text: &str,
    enter: bool,
) -> Result<(), CdpFail> {
    press(cdp, sess, rect, 0.0, 1)?; // focus the field

    // `commands` is Chrome's editing-command channel. selectAll through it
    // works whatever the platform modifier is, so there is no cmd-vs-ctrl
    // branch here and no dependence on the page honouring a synthetic ctrl+a
    // that a JS keydown handler may well swallow.
    cdp.rpc(
        "Input.dispatchKeyEvent",
        json!({"type": "keyDown", "key": "a", "code": "KeyA",
               "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65,
               "commands": ["selectAll"]}),
        Some(sess),
        5.0,
    )?;
    cdp.rpc(
        "Input.dispatchKeyEvent",
        json!({"type": "keyUp", "key": "a", "code": "KeyA",
               "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65}),
        Some(sess),
        5.0,
    )?;

    if text.is_empty() {
        // insertText("") is a no-op, so an explicit clear needs a real
        // Backspace against the selection.
        key(cdp, sess, "Backspace", 8, "")?;
    } else {
        // insertText replaces the selection in ONE event. Per-character key
        // events are both far slower and far less reliable on React-style
        // inputs, which re-render between keystrokes.
        cdp.rpc("Input.insertText", json!({"text": text}), Some(sess), 5.0)?;
    }

    if enter {
        key(cdp, sess, "Enter", 13, "\r")?;
    }
    Ok(())
}

/// Clear [id], type `value`, and press Enter when `enter` is set.
pub fn type_into(
    py: Python<'_>,
    scanner: &Arc<Mutex<ScannerInner>>,
    raw_id: &Value,
    value: &Value,
    enter: &Value,
    elements: &Map<String, Value>,
) -> ActResult<Value> {
    let idx = resolve_element_id(elements, raw_id)?;
    reject_viewport(idx, "input")?;
    let text = if value.is_null() {
        String::new()
    } else {
        py_str_of(value)
    };
    let submit = truthy(enter);
    let rect = rect_for(elements, idx)?;
    {
        let text = text.clone();
        with_box(py, scanner, &rect, move |s| {
            s.with_tab(|cdp, sess| type_into_rect(cdp, sess, &rect, &text, submit))
        })?;
    }
    let mut message = format!("typed into [{idx}] {}", label_for(elements, idx))
        .trim()
        .to_string();
    if submit {
        message.push_str(" and pressed Enter");
    }
    Ok(json!({"status": "success", "tool": "input", "id": idx,
              "value": text, "enter": submit, "message": message}))
}
