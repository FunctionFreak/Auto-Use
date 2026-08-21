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
// Nothing here builds a second binary: the page scanner used to, and is a
// module now.
pub mod agent {
    pub mod main_driver;
}
// The browser session keeps its own directory: web/browser/browser.rs. The
// #[path] lets the file keep its name while the module stays `crate::browser`.
#[path = "browser/browser.rs"]
pub mod browser;
pub mod controller;
// The page scanner. It used to build as a second binary of this package and
// run as a subprocess; it is a plain module now, reading pages over the one
// CDP session browser.rs owns.
pub mod tree {
    pub mod element;
}
pub mod llm_provider;

pyo3::create_exception!(
    agent_native,
    ScannerError,
    pyo3::exceptions::PyRuntimeError,
    "The page scanner failed, or the browser stopped answering."
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

/// The Chrome user-data-dir for a named browser profile, asked of Python.
///
/// Rust cannot answer this itself: `Auto_Use/__init__.py` handles the
/// compiled-vs-dev base directory and the AUTOUSE_DATA_DIR override, and a
/// second definition of "where is autouse_data" drifting apart from that one
/// is the exact bug that module exists to prevent.
pub fn browser_profile_dir(py: Python<'_>, name: Option<&str>) -> PyResult<PathBuf> {
    let module = py.import("Auto_Use")?;
    let dir = module.call_method1("browser_profile_dir", (name,))?;
    Ok(PathBuf::from(dir.str()?.extract::<String>()?))
}

/// Auto_Use/web/agent — kept as a helper because the main_driver prompts the
/// agent reads live under it.
pub fn agent_dir(py: Python<'_>) -> PyResult<PathBuf> {
    Ok(web_dir(py)?.join("agent"))
}

/// Auto_Use/web/browser — browser.rs and the glow assets it injects (glow/).
pub fn browser_dir(py: Python<'_>) -> PyResult<PathBuf> {
    Ok(web_dir(py)?.join("browser"))
}

#[pymodule]
fn agent_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    use agent::main_driver::view;

    m.add("ScannerError", m.py().get_type::<ScannerError>())?;
    m.add("CHROME_PORT", browser::CHROME_PORT)?;
    m.add_class::<browser::BrowserScanner>()?;
    m.add_function(pyo3::wrap_pyfunction!(browser::launch_chrome, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(browser::is_blank_page, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(browser::blank_html, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(browser::ensure_tab, m)?)?;

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
