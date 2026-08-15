// Copyright 2026 Ashish Yadav — Auto-Use

//! The research sub-agent.
//!
//! NOT BUILT YET. The sub-agent that would browse several sites on its own
//! and return one digested report does not exist, so this hands back a tool
//! ERROR rather than pretending.
//!
//! That is deliberate: the loop treats a research result as a dispatch, and
//! the NEXT step becomes a digest iteration with no screenshot and no element
//! tree. Returning a fake success would spend a whole blind step digesting
//! nothing. An error keeps the agent on the page, and the message tells it
//! the one thing it needs — that it can do this itself by opening the pages
//! and reading them.
//!
//! When the sub-agent lands, `dispatch()` returns
//!     {"status": "success", "tool": "research", "query": ..., "result": <report>}
//! and the digest machinery in the loop starts working with no change there.

use pyo3::prelude::*;
use serde_json::{json, Value};

use crate::agent::browser::truthy;
use crate::controller::service::ActResult;
use crate::agent::main_driver::view::py_str_of;

pub struct ResearchService {
    /// Drives the frontend's globe animation while a lookup is in flight.
    pub web_callback: Option<Py<PyAny>>,
}

impl ResearchService {
    pub fn available(&self) -> bool {
        false
    }

    pub fn dispatch(&self, py: Python<'_>, query: &Value) -> ActResult<Value> {
        let q = if truthy(query) {
            py_str_of(query).trim().to_string()
        } else {
            String::new()
        };
        if let Some(cb) = &self.web_callback {
            let _ = cb.call1(py, (false,)); // nothing in flight — stop the animation
        }
        Ok(json!({"status": "error", "tool": "research", "query": q,
                  "message": "the research sub-agent is not available yet — \
                              gather this yourself by opening the pages and \
                              reading them from the element_tree and screenshot."}))
    }
}
