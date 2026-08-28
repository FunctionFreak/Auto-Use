// Copyright 2026 Cursortouch — Auto-Use

//! route_action — the browser agent's hands.
//!
//! Takes the `[{type, ...}]` action dicts the model's native tool calls were
//! converted into, and carries each one out. Return shapes are NOT free-form:
//! the agent loop keys on specific fields, so they are fixed contracts —
//!
//!     done        -> {"action": "done", "summary": ...}      ends the loop
//!     wait        -> {"tool": "wait", "duration": N}          sets the inter-step delay
//!     todo_list   -> {"action": "todo_created"}
//!     update_todo -> {"action": "todo_updated"}
//!     a batch     -> {"action": "multiple", "results": [...]} one entry PER action,
//!                                                             in order, so the loop can
//!                                                             pair each result to the
//!                                                             tool call that produced it
//!
//! That last one is load-bearing: `_pair_results` in the agent only pairs
//! per-call when len(results) == len(calls), so every action must contribute
//! exactly one result, failures included.

use std::sync::{Arc, Mutex};

use pyo3::prelude::*;
use serde_json::{json, Map, Value};

use super::service::{ActErr, ControllerService};
use super::{click, done, input, scratchpad, scroll, tab, todo_tracker, wait};
use crate::browser::{truthy, BrowserScanner, ScannerInner};
use crate::agent::main_driver::view::py_str_of;

/// Routes one step's actions against the live browser.
pub struct ControllerView {
    /// The scanner owns the CDP connection, so every page action has to go
    /// through it — there is no second channel to the browser.
    scanner: Arc<Mutex<ScannerInner>>,
    stop_event: Option<Py<PyAny>>,
    /// Parallel-run mode: this agent drives exactly ONE dedicated tab, so the
    /// tab-lifecycle tools (new_tab/switch_tab/close_tab) are refused here as
    /// a backstop for providers that don't enforce the tool registry.
    single_tab: bool,
    pub controller_service: ControllerService,
    pub todo_tracker: todo_tracker::service::TodoTrackerService,
    pub scratchpad_service: scratchpad::service::ScratchpadService,
    /// The ids from the scan the model was just shown — replaced per step by
    /// set_elements.
    state: Mutex<ElementsState>,
}

struct ElementsState {
    elements: Map<String, Value>,
}

impl ControllerView {
    /// `web_dir` is Auto_Use/web — where the scratchpad/todo files live.
    /// `session_id` scopes them to scratchpad/{sid}/ so parallel agents never
    /// share notes; `single_tab` marks a parallel agent pinned to one tab.
    pub fn new(
        web_dir: &std::path::Path,
        session_id: Option<&str>,
        single_tab: bool,
        scanner: Arc<Mutex<ScannerInner>>,
        stop_event: Option<Py<PyAny>>,
    ) -> std::io::Result<Self> {
        let scratchpad_base = match session_id {
            Some(sid) => web_dir.join("scratchpad").join(sid),
            None => web_dir.join("scratchpad"),
        };
        Ok(ControllerView {
            scanner,
            stop_event,
            single_tab,
            controller_service: ControllerService,
            todo_tracker: todo_tracker::service::TodoTrackerService::new(&scratchpad_base)?,
            scratchpad_service: scratchpad::service::ScratchpadService::new(&scratchpad_base)?,
            state: Mutex::new(ElementsState { elements: Map::new() }),
        })
    }

    // ------------------------------------------------------------------ setup

    /// Hand over the ids from the scan the model was just shown. The app-name
    /// parameter is accepted for signature parity with the mac/ios controllers
    /// and unused here — nothing web-side ever read it.
    pub fn set_elements(&self, elements_mapping: Map<String, Value>, _application_name: &str) {
        self.state.lock().unwrap().elements = elements_mapping;
    }

    // ---------------------------------------------------------------- helpers

    fn stopped(&self, py: Python<'_>) -> PyResult<bool> {
        match &self.stop_event {
            Some(ev) => ev.bind(py).call_method0("is_set")?.is_truthy(),
            None => Ok(false),
        }
    }

    // ----------------------------------------------------------------- routing

    /// Execute this step's actions in order. Never returns a tool failure as
    /// an error — a failed action becomes a failed RESULT so the model sees
    /// it and can recover. (A raised Python error — KeyboardInterrupt — still
    /// flies, exactly as it did through Python's `except Exception`.)
    pub fn route_action(&self, py: Python<'_>, actions: &Value) -> PyResult<Value> {
        let actions: Vec<Value> = match actions {
            Value::Object(_) => vec![actions.clone()],
            Value::Array(items) => items.clone(),
            _ => Vec::new(),
        };

        if actions.len() == 1 {
            return self.one(py, &actions[0]);
        }

        let mut results: Vec<Value> = Vec::new();
        let mut failed_at: Option<usize> = None;
        for (n, action) in actions.iter().enumerate() {
            let n = n + 1;
            if self.stopped(py)? {
                results.push(json!({
                    "status": "stopped",
                    "tool": action.get("type").cloned().unwrap_or(Value::Null),
                    "message": "stopped by user",
                }));
                continue;
            }
            if let Some(failed) = failed_at {
                // Everything after a failure is reported, never silently
                // dropped: the loop pairs results to tool calls one-for-one
                // and a short list breaks the transcript. Saying WHICH action
                // broke the chain also tells the model why its later steps
                // did not happen.
                let failed_tool = results[failed - 1].get("tool").cloned().unwrap_or(Value::Null);
                results.push(json!({
                    "status": "not_run",
                    "tool": action.get("type").cloned().unwrap_or(Value::Null),
                    "message": format!(
                        "not run - stopped by the failure of action {failed} \
                         ({}). The page may have \
                         changed, so re-read the latest element_tree before retrying.",
                        py_str_of(&failed_tool)
                    ),
                }));
                continue;
            }
            let result = self.one(py, action)?;
            // A batch is a chain: each action assumes the page the previous
            // one left behind. Running on past a failure acts on a page that
            // never arrived, with ids resolved against a scan that is now
            // wrong.
            let ok = result.get("status").and_then(Value::as_str) == Some("success");
            results.push(result);
            if !ok {
                failed_at = Some(n);
            }
        }

        // "stopped" has to surface on the envelope too — the loop checks the
        // top-level status to decide whether the run was interrupted.
        let status = if results
            .iter()
            .any(|r| r.get("status").and_then(Value::as_str) == Some("stopped"))
        {
            "stopped"
        } else if failed_at.is_some() {
            "error"
        } else {
            "success"
        };
        Ok(json!({"status": status, "action": "multiple", "results": results}))
    }

    /// Run the tool, then tell the truth about what else happened.
    ///
    /// A dialog that blocked the page, a renderer that died — these arrive on
    /// the socket while a tool is running and used to go nowhere. The tool
    /// then answered "clicked [3]" and the model had no way to know why the
    /// page it saw next made no sense. Appending them here covers every tool
    /// at once, including the early-return branches inside `one_inner`.
    fn one(&self, py: Python<'_>, action: &Value) -> PyResult<Value> {
        let mut result = self.one_inner(py, action)?;
        let notes: Vec<String> =
            super::service::scan_op(py, &self.scanner, |s| Ok(s.take_notices()))
                .unwrap_or_default();
        if !notes.is_empty() {
            let base = result
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_string();
            let joined = notes.join(" ");
            let message = if base.is_empty() {
                joined
            } else {
                format!("{base} Note: {joined}")
            };
            if let Some(obj) = result.as_object_mut() {
                obj.insert("message".into(), Value::String(message));
            }
        }
        Ok(result)
    }

    fn one_inner(&self, py: Python<'_>, action: &Value) -> PyResult<Value> {
        let kind = action
            .get("type")
            .filter(|v| truthy(v))
            .map(|v| py_str_of(v).trim().to_string())
            .unwrap_or_default();
        let elements = self.state.lock().unwrap().elements.clone();
        let get = |key: &str| action.get(key).cloned().unwrap_or(Value::Null);
        let sc = &self.scanner;

        // Single-tab agents have no tab lifecycle. The tools are already
        // absent from their registry; this arm catches non-strict providers
        // that emit the name anyway.
        if self.single_tab && matches!(kind.as_str(), "new_tab" | "switch_tab" | "close_tab") {
            return Ok(json!({
                "status": "error", "tool": kind,
                "message": "this session drives a single dedicated tab - there are no \
                            tab tools here. Navigate the current tab with `update_tab` \
                            (url) or `navigate_tab` (back/forward/reload) instead.",
            }));
        }

        let outcome = match kind.as_str() {
            // ------------------------------------------------- page actions
            "new_tab" => tab::service::open_new(py, sc, &get("value")),
            "switch_tab" => tab::service::switch(py, sc, &get("id")),
            "close_tab" => tab::service::close(py, sc, &get("id")),
            "update_tab" => tab::service::update(py, sc, &get("value")),
            "navigate_tab" => tab::service::navigate(py, sc, &get("value")),
            "click" => click::service::click(py, sc, &get("id"), &get("times"), &elements),
            "hold_click" => {
                click::service::hold_click(py, sc, &get("id"), &get("time"), &elements)
            }
            "input" => input::service::type_into(
                py, sc, &get("id"), &get("value"), &get("enter"), &elements,
            ),
            "scroll" => scroll::service::scroll(py, sc, &get("id"), &get("direction"), &elements),
            "wait" => wait::service::wait(&get("value")),
            // -------------------------------------------------- bookkeeping
            "scratchpad" => {
                let value = py_str_of_or_empty(&get("value")).trim().to_string();
                if value.is_empty() {
                    Ok(json!({"status": "error", "tool": "scratchpad",
                              "message": "empty scratchpad entry"}))
                } else {
                    let ok = self.scratchpad_service.append_scratchpad(&value);
                    Ok(json!({
                        "status": if ok { "success" } else { "error" },
                        "tool": "scratchpad",
                        "message": if ok { "recorded" } else { "could not write the scratchpad" },
                    }))
                }
            }
            "todo_list" => {
                let value = py_str_of_or_empty(&get("value")).trim().to_string();
                let ok = self.todo_tracker.save_todo(&value);
                Ok(json!({
                    "status": if ok { "success" } else { "error" },
                    "action": if ok { json!("todo_created") } else { Value::Null },
                    "tool": "todo_list",
                    "message": if ok { "todo list saved" } else { "could not write the todo list" },
                }))
            }
            "update_todo" => {
                let raw = py_str_of_or_empty(&get("value")).trim().to_string();
                let digits: String = raw.chars().filter(|c| c.is_ascii_digit()).collect();
                if digits.is_empty() {
                    Ok(json!({"status": "error", "tool": "update_todo",
                              "message": format!("'{raw}' has no task number in it")}))
                } else {
                    let num: i64 = digits.parse().unwrap_or(i64::MAX);
                    // A ToDo has to exist before a task can be marked. Saying
                    // WHICH thing is missing turns a dead end into a step the
                    // model can recover from.
                    if !self.todo_tracker.todo_file.exists() {
                        Ok(json!({"status": "error", "tool": "update_todo",
                                  "message": format!(
                                      "no todo list exists yet, so there is no task \
                                       #{num} to mark - call `todo_list` first, \
                                       then update it.")}))
                    } else {
                        let ok = self.todo_tracker.update_task(num);
                        Ok(json!({
                            "status": if ok { "success" } else { "error" },
                            "action": if ok { json!("todo_updated") } else { Value::Null },
                            "tool": "update_todo",
                            "message": if ok {
                                format!("task #{num} marked complete")
                            } else {
                                format!("could not mark task #{num} complete")
                            },
                        }))
                    }
                }
            }
            // --------------------------------------------------- completion
            "done" => done::service::finish(&get("value")),
            _ => Ok(json!({"status": "error", "tool": kind,
                           "message": format!("no handler for action '{kind}'")})),
        };

        match outcome {
            Ok(v) => Ok(v),
            Err(ActErr::Msg(message)) => Ok(json!({
                "status": "error", "tool": kind, "message": message,
            })),
            Err(ActErr::Py(e)) => Err(e),
        }
    }
}

/// Python `str(x or "")`.
fn py_str_of_or_empty(v: &Value) -> String {
    if truthy(v) {
        py_str_of(v)
    } else {
        String::new()
    }
}

// ---------------------------------------------------------------------------
// Test hook — routes actions through a fresh ControllerView wired to an
// existing scanner, using that scanner's current element mapping. Exists so
// the controller can be exercised (and differentially tested) from Python
// without the whole agent loop.
// ---------------------------------------------------------------------------

#[pyfunction]
#[pyo3(signature = (scanner, actions, application_name=None))]
pub fn _route_action_debug<'py>(
    py: Python<'py>,
    scanner: Py<BrowserScanner>,
    actions: Bound<'py, PyAny>,
    application_name: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let web_dir = crate::web_dir(py)?.clone();
    let inner = scanner.get().inner.clone();
    let view = ControllerView::new(&web_dir, None, false, inner.clone(), None)
        .map_err(|e| pyo3::exceptions::PyIOError::new_err(e.to_string()))?;
    let mapping = {
        let guard = inner
            .lock()
            .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("scanner state poisoned"))?;
        guard.mapping.clone()
    };
    view.set_elements(mapping, application_name.as_deref().unwrap_or(""));
    let actions_val: Value = pythonize::depythonize(&actions)?;
    let result = view.route_action(py, &actions_val)?;
    pythonize::pythonize(py, &result).map_err(Into::into)
}
