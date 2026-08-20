// Copyright 2026 Ashish Yadav — Auto-Use

//! Typing into an element.
//!
//! Reaches Chrome through the scanner, which owns the CDP session:
//!     input -> the binary's `ix` command, submitting when `enter` is set
//!
//! `enter` matters more here than it would on desktop: the browser agent has
//! no `hotkey` tool, so without it a search box could be filled but never
//! submitted.

use std::sync::{Arc, Mutex};

use pyo3::prelude::*;
use serde_json::{json, Map, Value};

use crate::browser::{truthy, ScannerInner};
use crate::controller::service::{
    label_for, rect_for, reject_viewport, resolve_element_id, with_box, ActResult,
};
use crate::agent::main_driver::view::py_str_of;

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
            s.input_point(&rect, &text, submit)
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
