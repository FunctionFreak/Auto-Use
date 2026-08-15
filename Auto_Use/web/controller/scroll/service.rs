// Copyright 2026 Ashish Yadav — Auto-Use

//! Scrolling — moving a surface to bring off-screen content into view.
//!
//! WHICH surface moves is decided by the browser, not by this code. The
//! scanner turns a wheel over a POINT, and the wheel event is delivered to
//! whatever scrolls around that point: the document for a point over the
//! page, the panel for a point inside a panel. That is why no scroll
//! container ever needs an id of its own — the model names any element it
//! can SEE inside the region it wants moved, and the browser resolves the
//! rest.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use pyo3::prelude::*;
use serde_json::{json, Map, Value};

use crate::agent::browser::ScannerInner;
use crate::controller::service::{
    label_for, rect_for, resolve_element_id, scan_op, with_box, ActErr, ActResult, VIEWPORT_ID,
};
use crate::agent::main_driver::view::py_str_of;

pub const DIRECTIONS: [&str; 4] = ["up", "down", "left", "right"];

/// A step is a fraction of the visible surface rather than a pixel count, so
/// it means the same thing on a laptop and on a tall monitor. Less than a
/// full screen on purpose: overlapping steps keep a line of context between
/// them.
pub const STEP_FRACTION: f64 = 0.75;

/// Fallback when no scan has published a viewport yet.
pub const FALLBACK_STEP: f64 = 600.0;

/// Smooth scrolling animates; without a beat the next screenshot catches the
/// page mid-flight.
pub const SETTLE_SECONDS: f64 = 0.35;

/// How far one scroll moves, in CSS pixels.
fn step_size(viewport: Option<[f64; 4]>, horizontal: bool) -> f64 {
    match viewport {
        Some(rect) => {
            let span = if horizontal { rect[2] } else { rect[3] };
            (STEP_FRACTION * span).max(1.0)
        }
        None => FALLBACK_STEP,
    }
}

/// Python list equality over the probe values — numbers compare numerically
/// (1 == 1.0), so an int-vs-float representation change never reads as a
/// scroll that moved.
fn probes_equal(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y.iter()).all(|(m, n)| probes_equal(m, n))
        }
        (Value::Number(x), Value::Number(y)) => x.as_f64() == y.as_f64(),
        _ => a == b,
    }
}

/// Scroll the surface at [id] one step in `direction`.
pub fn scroll(
    py: Python<'_>,
    scanner: &Arc<Mutex<ScannerInner>>,
    raw_id: &Value,
    direction: &Value,
    elements: &Map<String, Value>,
) -> ActResult<Value> {
    let idx = resolve_element_id(elements, raw_id)?;

    let where_ = if direction.is_null() {
        String::new()
    } else {
        py_str_of(direction).trim().to_lowercase()
    };
    if !DIRECTIONS.contains(&where_.as_str()) {
        return Err(ActErr::Msg(format!(
            "'{}' is not a scroll direction — use one of {}",
            py_str_of(direction),
            DIRECTIONS.join(", ")
        )));
    }

    let horizontal = where_ == "left" || where_ == "right";
    let viewport = scan_op(py, scanner, |s| Ok(s.viewport_rect()))?;
    let step = step_size(viewport, horizontal);
    let dx = match where_.as_str() {
        "right" => step,
        "left" => -step,
        _ => 0.0,
    };
    let dy = match where_.as_str() {
        "down" => step,
        "up" => -step,
        _ => 0.0,
    };

    let rect = rect_for(elements, idx)?;
    // Where the surface sits BEFORE, so the result can say whether it
    // actually moved. Nothing else can tell the model that: it sees one
    // screenshot per step and never the previous one.
    let before = scan_op(py, scanner, |s| Ok(s.scroll_probe(&rect)))?;

    if idx == VIEWPORT_ID {
        // No bloom for the page: it would be a full-screen flash, which says
        // nothing about what moved.
        scan_op(py, scanner, |s| s.scroll_point(&rect, dx, dy))?;
    } else {
        with_box(py, scanner, &rect, |s| s.scroll_point(&rect, dx, dy))?;
    }
    py.detach(|| std::thread::sleep(Duration::from_secs_f64(SETTLE_SECONDS)));

    let after = scan_op(py, scanner, |s| Ok(s.scroll_probe(&rect)))?;
    // Unknown (no probe) is NOT reported as "did not move": a guess in either
    // direction is worse than saying nothing.
    let moved: Option<bool> = match (&before, &after) {
        (Some(b), Some(a)) => Some(!probes_equal(b, a)),
        _ => None,
    };

    let target = if idx == VIEWPORT_ID {
        "the page".to_string()
    } else {
        format!("[{idx}] {}", label_for(elements, idx)).trim().to_string()
    };
    let message = if moved == Some(false) {
        format!(
            "scroll {where_} on {target} had NO EFFECT - nothing moved. \
             That surface is already at its end going {where_}{}. \
             Do NOT repeat this scroll: it will do nothing again. {}",
            if idx == VIEWPORT_ID {
                ""
            } else {
                ", or that element is not inside anything scrollable"
            },
            if idx == VIEWPORT_ID {
                "Try the other direction, or scroll an [id] inside a \
                 different region of the page."
            } else {
                "Try the other direction, scroll [1] to move the page \
                 itself, or pick an [id] in a different region."
            }
        )
    } else {
        format!(
            "scrolled {where_} on {target}{}. Re-read the element_tree: ids \
             are re-assigned on every scan and the visible elements have changed.",
            if moved == Some(true) { " - the surface moved" } else { "" }
        )
    };
    let moved_value = match moved {
        Some(b) => Value::Bool(b),
        None => Value::Null,
    };
    Ok(json!({"status": "success", "tool": "scroll", "id": idx,
              "direction": where_, "moved": moved_value, "message": message}))
}
