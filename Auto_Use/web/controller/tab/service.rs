// Copyright 2026 Ashish Yadav — Auto-Use

//! Tab handling — every tab tool lives here.
//!
//! They are separate tools to the model because they are separate intents.
//! Underneath they are the same concern: which tab the agent is pointed at,
//! and what that tab is showing.
//!
//! All of them reach Chrome through the scanner, which owns the CDP session:
//!     new_tab       -> the binary's `n <url>` command
//!     switch_tab    -> the binary's `u <n>` command
//!     close_tab     -> Chrome's /json/close endpoint (the scanner
//!                      re-binds itself when its current tab was the one closed)
//!     update_tab    -> the binary's `g <url>` command
//!     navigate_tab  -> `r` (reload), `bk` (back), `fw` (forward)

use std::sync::{Arc, Mutex};

use pyo3::prelude::*;
use serde_json::{json, Value};

use crate::browser::ScannerInner;
use crate::controller::service::{err, py_int_value, scan_op, ActResult};
use crate::agent::main_driver::view::py_str_of;

/// The three ways to move the CURRENT tab without naming a destination.
pub const MOVES: [&str; 3] = ["back", "forward", "reload"];

/// A bare host into something Chrome will actually navigate to. The model
/// writes "amazon.com" as often as the full url, and Chrome treats a
/// scheme-less string as a search term rather than an address.
pub fn normalize_url(value: &Value) -> String {
    let url = if value.is_null() {
        String::new()
    } else {
        py_str_of(value).trim().to_string()
    };
    if !url.is_empty() && !url.contains("://") {
        format!("https://{url}")
    } else {
        url
    }
}

/// Open `value` in a new tab. A blank value opens the blank page.
pub fn open_new(
    py: Python<'_>,
    scanner: &Arc<Mutex<ScannerInner>>,
    value: &Value,
) -> ActResult<Value> {
    let url = normalize_url(value);
    {
        let url = url.clone();
        scan_op(py, scanner, move |s| s.new_tab(&url))?;
    }
    // Report what the model asked for. Saying "opened a new tab on
    // file:///.../logo.html" would read as a navigation it did not request.
    let where_ = if url.is_empty() { "a blank page" } else { url.as_str() };
    let message = format!("opened a new tab on {where_}");
    Ok(json!({"status": "success", "tool": "new_tab", "url": url, "message": message}))
}

/// Navigate the CURRENT tab to `value`, replacing its page.
pub fn update(
    py: Python<'_>,
    scanner: &Arc<Mutex<ScannerInner>>,
    value: &Value,
) -> ActResult<Value> {
    let url = normalize_url(value);
    if url.is_empty() {
        return err("update_tab needs a url in `value`");
    }
    {
        let url = url.clone();
        scan_op(py, scanner, move |s| s.goto(&url))?;
    }
    let message = format!("navigated the current tab to {url}");
    Ok(json!({"status": "success", "tool": "update_tab", "url": url, "message": message}))
}

/// Move the CURRENT tab: `back`, `forward` or `reload`. Errors are worth more
/// than silence here: "there is no page to go back to" comes back as a failed
/// action the model can re-plan from, rather than a success that changed
/// nothing.
pub fn navigate(
    py: Python<'_>,
    scanner: &Arc<Mutex<ScannerInner>>,
    value: &Value,
) -> ActResult<Value> {
    let mv = if value.is_null() {
        String::new()
    } else {
        py_str_of(value).trim().to_lowercase()
    };
    if !MOVES.contains(&mv.as_str()) {
        return err(format!(
            "'{}' is not a tab move — use one of {}",
            py_str_of(value),
            MOVES.join(", ")
        ));
    }
    let current = scan_op(py, scanner, |s| Ok(s.url.clone()))?;
    let before = if current.is_empty() {
        "the current tab".to_string()
    } else {
        current
    };

    let message = if mv == "reload" {
        scan_op(py, scanner, |s| s.reload())?;
        format!("reloaded {before}")
    } else {
        {
            let mv = mv.clone();
            scan_op(py, scanner, move |s| {
                if mv == "back" {
                    s.back()
                } else {
                    s.forward()
                }
            })?;
        }
        format!(
            "went {mv} from {before}. The tab is on a different \
             page now — read the new element_tree before acting."
        )
    };
    Ok(json!({"status": "success", "tool": "navigate_tab", "value": mv,
              "url": before, "message": message}))
}

/// Make the tab at `index` current. `index` is the [n] from <all_tabs>, NOT
/// an [id] from <element_tree> — bounds-check it against the tab list the
/// model was actually shown so a miss becomes a tool error it can recover
/// from.
pub fn switch(
    py: Python<'_>,
    scanner: &Arc<Mutex<ScannerInner>>,
    index: &Value,
) -> ActResult<Value> {
    let idx = match py_int_value(index) {
        Ok(i) => i,
        Err(_) => {
            return err(format!("'{}' is not a valid tab number", py_str_of(index)));
        }
    };
    let all_tabs = scan_op(py, scanner, |s| Ok(s.all_tabs.clone()))?;
    let tabs: Vec<&str> = all_tabs.lines().filter(|ln| !ln.trim().is_empty()).collect();
    if !tabs.is_empty() && !(1 <= idx && idx <= tabs.len() as i64) {
        return err(format!(
            "no tab [{idx}] — <all_tabs> lists {n} tab(s), \
             1-{n}. Use the [n] from <all_tabs>, not an [id] \
             from <element_tree>.",
            n = tabs.len()
        ));
    }
    scan_op(py, scanner, move |s| s.switch_tab(idx))?;
    Ok(json!({"status": "success", "tool": "switch_tab", "id": idx,
              "message": format!("switched to tab [{idx}]")}))
}

/// Close the tab at `index` — the [n] from <all_tabs>, bounds-checked against
/// the list the model was actually shown, exactly like `switch`. The scanner
/// refuses to close the LAST tab (an error the model can re-plan from: the
/// right move there is `update_tab`), and re-binds itself to a neighbouring
/// tab when the closed one was current. Closing RE-NUMBERS the tab list, so
/// the message says so — an id held over from before the close is a real
/// mistake, the same trap the element-tree ids have.
pub fn close(
    py: Python<'_>,
    scanner: &Arc<Mutex<ScannerInner>>,
    index: &Value,
) -> ActResult<Value> {
    let idx = match py_int_value(index) {
        Ok(i) => i,
        Err(_) => {
            return err(format!("'{}' is not a valid tab number", py_str_of(index)));
        }
    };
    let all_tabs = scan_op(py, scanner, |s| Ok(s.all_tabs.clone()))?;
    let tabs: Vec<&str> = all_tabs.lines().filter(|ln| !ln.trim().is_empty()).collect();
    if !tabs.is_empty() && !(1 <= idx && idx <= tabs.len() as i64) {
        return err(format!(
            "no tab [{idx}] — <all_tabs> lists {n} tab(s), \
             1-{n}. Use the [n] from <all_tabs>, not an [id] \
             from <element_tree>.",
            n = tabs.len()
        ));
    }
    scan_op(py, scanner, move |s| s.close_tab(idx))?;
    Ok(json!({"status": "success", "tool": "close_tab", "id": idx,
              "message": format!(
                  "closed tab [{idx}]. The remaining tabs are re-numbered — \
                   read the fresh <all_tabs> in the next input before any \
                   other tab action.")}))
}
