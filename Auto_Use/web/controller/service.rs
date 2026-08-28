// Copyright 2026 Cursortouch — Auto-Use

//! Low-level browser control — shared helpers for every tool.
//!
//! The desktop platforms drive a real mouse and keyboard here; a browser has
//! no such state: every event is dispatched into the page through CDP by the
//! scanner and is over the moment it returns, so this stays thin on purpose.
//!
//! Error strings here are model-visible tool results — they are contracts,
//! ported byte-for-byte from the Python originals.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use pyo3::prelude::*;
use serde_json::{Map, Value};

use crate::browser::{truthy, SResult, ScanErr, ScannerInner};
use crate::agent::main_driver::view::py_str_of;

/// [1] is the page itself on every scan — the scanner reserves it. It is a
/// scroll target, never a control.
pub const VIEWPORT_ID: i64 = 1;

/// How long the box stays on screen after the action. Long enough to register
/// on a headful screen, short enough not to pace the agent.
pub const BOX_SECONDS: f64 = 0.18;

/// A tool failure: either a message the model reads back (Python's caught
/// Exception -> error result), or a raised Python error (KeyboardInterrupt)
/// that must fly past the per-action catch exactly as it did in Python.
pub enum ActErr {
    Msg(String),
    Py(PyErr),
}

impl From<ScanErr> for ActErr {
    fn from(e: ScanErr) -> ActErr {
        match e {
            ScanErr::Scanner(m) => ActErr::Msg(m),
            ScanErr::Py(err) => ActErr::Py(err),
        }
    }
}

pub type ActResult<T> = Result<T, ActErr>;

pub fn err<T>(msg: impl Into<String>) -> ActResult<T> {
    Err(ActErr::Msg(msg.into()))
}

/// Python `int(x)` over a JSON value: numbers truncate, strings parse as a
/// whole number, bools count as 0/1, anything else fails.
pub fn py_int_value(v: &Value) -> Result<i64, ()> {
    match v {
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(i)
            } else if let Some(f) = n.as_f64() {
                Ok(f as i64)
            } else {
                Err(())
            }
        }
        Value::String(s) => s.trim().parse::<i64>().map_err(|_| ()),
        Value::Bool(b) => Ok(if *b { 1 } else { 0 }),
        _ => Err(()),
    }
}

/// Python `float(x)`: numbers pass, strings parse, bools count as 0/1.
pub fn py_float_value(v: &Value) -> Result<f64, ()> {
    match v {
        Value::Number(n) => n.as_f64().ok_or(()),
        Value::String(s) => s.trim().parse::<f64>().map_err(|_| ()),
        Value::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        _ => Err(()),
    }
}

/// Python `str(float)` — "2.0" for a whole number, the way f-strings render it.
pub fn py_float_str(v: f64) -> String {
    let s = format!("{v}");
    if s.contains('.') || s.contains('e') || s.contains("inf") || s.contains("nan") {
        s
    } else {
        format!("{s}.0")
    }
}

/// Refuse a tool that cannot act on the page as a whole. [1] carries the
/// viewport rect, so a click aimed at it would land on whatever happens to
/// sit in the middle of the page — a silent wrong action rather than a
/// visible error.
pub fn reject_viewport(idx: i64, tool: &str) -> ActResult<()> {
    if idx == VIEWPORT_ID {
        return err(format!(
            "[1] is the page itself, not an element — `{tool}` needs a real \
             [id] from <element_tree>. Use [1] with `scroll` only."
        ));
    }
    Ok(())
}

/// Validate an [id] against the CURRENT scan, shared by every tool that takes
/// one. Ids are re-assigned on every scan, so an id carried over from an
/// earlier tree is a real mistake — catching it here turns it into a tool
/// error the model can recover from.
pub fn resolve_element_id(elements: &Map<String, Value>, raw: &Value) -> ActResult<i64> {
    let idx = match py_int_value(raw) {
        Ok(i) => i,
        Err(_) => {
            return err(format!("'{}' is not a valid element id", py_str_of(raw)));
        }
    };
    if !elements.is_empty() && !elements.contains_key(&idx.to_string()) {
        let mut known: Vec<i64> = elements
            .keys()
            .filter(|k| !k.is_empty() && k.chars().all(|c| c.is_ascii_digit()))
            .filter_map(|k| k.parse::<i64>().ok())
            .collect();
        known.sort_unstable();
        let rng = if known.is_empty() {
            "none".to_string()
        } else {
            format!("{}-{}", known[0], known[known.len() - 1])
        };
        return err(format!(
            "no element [{idx}] in the current element_tree \
             (available ids: {rng}). Ids are re-assigned on every scan — \
             re-read the latest tree and use an id from it."
        ));
    }
    Ok(idx)
}

/// The [x, y, w, h] of [id], in CSS pixels, from the scan the model saw.
pub fn rect_for(elements: &Map<String, Value>, idx: i64) -> ActResult<[f64; 4]> {
    let rect = elements
        .get(&idx.to_string())
        .and_then(|e| e.get("rect"))
        .and_then(Value::as_array);
    if let Some(vals) = rect {
        if vals.len() == 4 {
            let mut out = [0.0f64; 4];
            let mut ok = true;
            for (i, v) in vals.iter().enumerate() {
                match v.as_f64() {
                    Some(f) => out[i] = f,
                    None => {
                        ok = false;
                        break;
                    }
                }
            }
            if ok {
                return Ok(out);
            }
        }
    }
    err(format!(
        "element [{idx}] has no geometry in the current scan — \
         rescan before acting on it"
    ))
}

/// Short human label for an [id], for the action's result message.
pub fn label_for(elements: &Map<String, Value>, idx: i64) -> String {
    let info = elements.get(&idx.to_string());
    let name = ["name", "role", "tag"].iter().find_map(|k| {
        let v = info.and_then(|e| e.get(k))?;
        if truthy(v) {
            Some(py_str_of(v))
        } else {
            None
        }
    });
    match name {
        Some(n) => format!("({n})"),
        None => String::new(),
    }
}

/// Run `act` on the scanner without the GIL held — every page action is I/O.
pub fn scan_op<T: Send>(
    py: Python<'_>,
    scanner: &Arc<Mutex<ScannerInner>>,
    act: impl FnOnce(&mut ScannerInner) -> SResult<T> + Send,
) -> ActResult<T> {
    let scanner = scanner.clone();
    py.detach(move || {
        let mut guard = scanner
            .lock()
            .map_err(|_| ScanErr::s("scanner state poisoned by an earlier panic"))?;
        act(&mut guard)
    })
    .map_err(ActErr::from)
}

/// Run `act` with a box drawn around `rect` (CSS px), then clear it. Shared
/// by every tool that touches an element — "show the human what was hit" is
/// one concern and lives once. Purely cosmetic: the box is best-effort and
/// the action runs either way.
pub fn with_box<T: Send>(
    py: Python<'_>,
    scanner: &Arc<Mutex<ScannerInner>>,
    rect: &[f64; 4],
    act: impl FnOnce(&mut ScannerInner) -> SResult<T> + Send,
) -> ActResult<T> {
    let drawn = {
        let scanner = scanner.clone();
        let rect = *rect;
        py.detach(move || match scanner.lock() {
            Ok(mut g) => g.flash(&rect),
            Err(_) => false,
        })
    };
    let result = {
        let scanner = scanner.clone();
        py.detach(move || {
            let mut guard = scanner
                .lock()
                .map_err(|_| ScanErr::s("scanner state poisoned by an earlier panic"))?;
            act(&mut guard)
        })
    };
    if drawn {
        let scanner = scanner.clone();
        py.detach(move || {
            std::thread::sleep(Duration::from_secs_f64(BOX_SECONDS));
            if let Ok(mut g) = scanner.lock() {
                g.unflash();
            }
        });
    }
    result.map_err(ActErr::from)
}

/// `release_all_inputs()` — kept because the agent loop calls it on every
/// stop path. Still a no-op, but for a better reason than before: the only
/// action that can leave a button pressed is `hold_click`, and its press and
/// release are now two calls in one synchronous function on this side's own
/// session (see controller/click). There is no longer a window in which the
/// button is down and this side has no way to lift it.
pub struct ControllerService;

impl ControllerService {
    pub fn release_all_inputs(&self) {}
}
