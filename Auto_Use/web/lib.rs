// Copyright 2026 Ashish Yadav — Auto-Use

//! Crate root — the module behind `Auto_Use.web.agent`.
//!
//! The package's `__init__.py` builds this crate lazily (plain
//! `cargo build --release`, no maturin), copies the dylib beside itself as
//! agent_native.so, and re-exports the classes from it.

use std::path::PathBuf;
use std::sync::OnceLock;

use pyo3::prelude::*;

// One crate for the whole web side — every subdirectory's .rs files compile
// into this single cdylib (one target/, one agent_native.so, both at web/).
// web/tree's element binary can fold in as a second build target when it
// converts.
pub mod agent {
    pub mod browser;
    pub mod main_driver;
}
pub mod controller;
pub mod llm_provider;

pyo3::create_exception!(
    agent_native,
    ScannerError,
    pyo3::exceptions::PyRuntimeError,
    "The scanner binary failed, refused a command, or stopped answering."
);

/// Auto_Use/web — the directory holding this extension module, resolved
/// lazily because importlib only sets `__file__` after module init returns.
pub fn web_dir(py: Python<'_>) -> PyResult<&'static PathBuf> {
    static DIR: OnceLock<PathBuf> = OnceLock::new();
    if let Some(dir) = DIR.get() {
        return Ok(dir);
    }
    let dir = if let Ok(v) = std::env::var("AUTOUSE_WEB_DIR") {
        PathBuf::from(v)
    } else {
        let module = py
            .import("Auto_Use.web.agent_native")
            .or_else(|_| py.import("agent_native"))?;
        let file: String = module.getattr("__file__")?.extract()?;
        PathBuf::from(file)
            .parent()
            .map(|p| p.to_path_buf())
            .unwrap_or_else(|| PathBuf::from("."))
    };
    Ok(DIR.get_or_init(|| dir))
}

/// Auto_Use/web/agent — kept as a helper because every asset path the agent
/// reads (glow.*, main_driver prompts) lives under it.
pub fn agent_dir(py: Python<'_>) -> PyResult<PathBuf> {
    Ok(web_dir(py)?.join("agent"))
}

#[pymodule]
fn agent_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    use agent::main_driver::view;

    m.add("ScannerError", m.py().get_type::<ScannerError>())?;
    m.add("CHROME_PORT", agent::browser::CHROME_PORT)?;
    m.add_class::<agent::browser::BrowserScanner>()?;
    m.add_function(pyo3::wrap_pyfunction!(agent::browser::launch_chrome, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(agent::browser::is_blank_page, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(agent::browser::blank_html, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(agent::browser::ensure_tab, m)?)?;

    m.add_class::<agent::main_driver::service::AgentService>()?;
    m.add_class::<llm_provider::llm_manager::LLMManager>()?;
    m.add_function(pyo3::wrap_pyfunction!(
        llm_provider::llm_manager::_tool_calls_to_steps_debug, m)?)?;
    m.add_class::<view::AgentResponseFormatter>()?;
    m.add_function(pyo3::wrap_pyfunction!(controller::view::_route_action_debug, m)?)?;
    // The transcript codec — exported both for CompressionController (which
    // takes compression_dump/compression_entry as callables) and for
    // differential testing against the Python originals.
    m.add_function(pyo3::wrap_pyfunction!(view::compression_dump, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(view::compression_entry, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(view::decode_step_py, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(view::encode_step_py, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(view::decode_results_py, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(view::encode_results_py, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(view::wire_calls_from_py, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(view::snapshot_turn_py, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(view::looks_native_py, m)?)?;
    Ok(())
}
