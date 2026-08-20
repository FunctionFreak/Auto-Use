// Copyright 2026 Ashish Yadav — Auto-Use

//! Clicking — both click tools live here.
//!
//! `click` and `hold_click` are one concern: press the pointer on an element
//! and let go. The only difference is how long the button stays down.
//!
//! Reaches Chrome through the scanner, which owns the CDP session:
//!     click       -> the binary's `cx` command (rect-based click)
//!     hold_click  -> the same command with a hold duration

use std::sync::{Arc, Mutex};

use pyo3::prelude::*;
use serde_json::{json, Map, Value};

use crate::browser::ScannerInner;
use crate::controller::service::{
    label_for, py_float_str, py_float_value, py_int_value, rect_for, reject_viewport,
    resolve_element_id, with_box, ActResult,
};

/// A hold shorter than this is just a click with extra steps; the model
/// asking for 0 almost certainly meant a normal click.
pub const MIN_HOLD_SECONDS: f64 = 0.1;
/// Beyond a double click there is no web gesture to express, so anything
/// higher is clamped rather than repeated.
pub const MAX_CLICK_TIMES: i64 = 2;
pub const DEFAULT_HOLD_SECONDS: f64 = 1.0;

/// Click [id] once, or twice as a single double-click gesture.
pub fn click(
    py: Python<'_>,
    scanner: &Arc<Mutex<ScannerInner>>,
    raw_id: &Value,
    times: &Value,
    elements: &Map<String, Value>,
) -> ActResult<Value> {
    let idx = resolve_element_id(elements, raw_id)?;
    reject_viewport(idx, "click")?;
    let n = py_int_value(times).map(|n| n.clamp(1, MAX_CLICK_TIMES)).unwrap_or(1);
    let rect = rect_for(elements, idx)?;
    with_box(py, scanner, &rect, |s| s.click_point(&rect, 0.0, n))?;
    let how = if n == 2 { "double-clicked" } else { "clicked" };
    let message = format!("{how} [{idx}] {}", label_for(elements, idx))
        .trim()
        .to_string();
    Ok(json!({"status": "success", "tool": "click", "id": idx, "times": n,
              "message": message}))
}

/// Press and hold [id] for `seconds`, then release.
pub fn hold_click(
    py: Python<'_>,
    scanner: &Arc<Mutex<ScannerInner>>,
    raw_id: &Value,
    seconds: &Value,
    elements: &Map<String, Value>,
) -> ActResult<Value> {
    let idx = resolve_element_id(elements, raw_id)?;
    reject_viewport(idx, "hold_click")?;
    let secs = py_float_value(seconds)
        .map(|s| s.max(MIN_HOLD_SECONDS))
        .unwrap_or(DEFAULT_HOLD_SECONDS);
    let rect = rect_for(elements, idx)?;
    with_box(py, scanner, &rect, |s| s.click_point(&rect, secs, 1))?;
    let message = format!(
        "held [{idx}] {} for {}s",
        label_for(elements, idx),
        py_float_str(secs)
    )
    .trim()
    .to_string();
    Ok(json!({"status": "success", "tool": "hold_click", "id": idx, "time": secs,
              "message": message}))
}
