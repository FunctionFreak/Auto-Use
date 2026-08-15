# Copyright 2026 Ashish Yadav — Auto-Use

"""The web agent — implemented in Rust (agent_native), same import surface as
the old Python package.

The whole web side is ONE crate rooted at Auto_Use/web/: Cargo.toml, target/
and the built agent_native.so all live there, and the .rs sources sit in
their own folders (agent/, controller/, llm_provider/, tree/) mirroring the
old Python layout. tree/element.rs builds as the crate's `element` binary in
the same cargo run.
The extension is compiled with plain `cargo build --release` on first import
(or when a .rs source is newer than the built module) — no maturin, no
pyproject.toml, the same lazy-build contract the element scanner binary uses.
This __init__ is the loader plus the re-export facade.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent
_WEB_DIR = _AGENT_DIR.parent

# Two different naming schemes have to line up here, and neither is portable:
# Python only imports an extension named .pyd on Windows and .so elsewhere,
# while cargo names its cdylib agent_native.dll / libagent_native.dylib /
# libagent_native.so depending on the host. Resolve both per-platform instead
# of hardcoding the macOS pair.
if sys.platform == "win32":
    _EXT_SUFFIX, _CARGO_ARTIFACT = ".pyd", "agent_native.dll"
elif sys.platform == "darwin":
    _EXT_SUFFIX, _CARGO_ARTIFACT = ".so", "libagent_native.dylib"
else:
    _EXT_SUFFIX, _CARGO_ARTIFACT = ".so", "libagent_native.so"

_SO = _WEB_DIR / f"agent_native{_EXT_SUFFIX}"
_DYLIB = _WEB_DIR / "target" / "release" / _CARGO_ARTIFACT
# Everything the crate compiles — including tree/element.rs, which builds as
# the `element` binary target of this same crate (one cargo build, one
# target/, both artifacts).
_SOURCES = [
    _WEB_DIR / "Cargo.toml",
    *_WEB_DIR.glob("*.rs"),
    *_AGENT_DIR.glob("*.rs"),
    *(_AGENT_DIR / "main_driver").glob("*.rs"),
    *(_WEB_DIR / "controller").rglob("*.rs"),
    *(_WEB_DIR / "llm_provider").rglob("*.rs"),
    *(_WEB_DIR / "tree").glob("*.rs"),
]


def _ensure_built():
    """Build the extension once — a minute on first use, then never again."""
    stamps = [p.stat().st_mtime for p in _SOURCES if p.exists()]
    if _SO.exists() and stamps and _SO.stat().st_mtime >= max(stamps):
        return
    # shutil.which applies PATHEXT itself, so it already finds cargo.exe; only
    # the rustup-default fallback path needs the suffix spelled out.
    _cargo_exe = "cargo.exe" if sys.platform == "win32" else "cargo"
    cargo = shutil.which("cargo") or str(Path.home() / ".cargo" / "bin" / _cargo_exe)
    if not Path(cargo).exists():
        raise RuntimeError(
            "cargo not found — the web agent is a Rust extension and needs "
            "Rust installed (https://rustup.rs) to build once."
        )
    print("Building the web agent (first run, ~1 min)...")
    # Pin the build to the interpreter that is importing us. pyo3-build-config
    # otherwise picks whatever "python" PATH resolves to, which in a venv that
    # was never activated is a different install than the one that will load
    # the result.
    env = dict(os.environ)
    env.setdefault("PYO3_PYTHON", sys.executable)
    subprocess.check_call(
        [cargo, "build", "--release", "--manifest-path", str(_WEB_DIR / "Cargo.toml")],
        cwd=str(_WEB_DIR), env=env)
    if not _DYLIB.exists():
        raise RuntimeError(f"build succeeded but {_DYLIB} is missing")
    shutil.copy2(_DYLIB, _SO)


_ensure_built()

from Auto_Use.web.agent_native import (   # noqa: E402
    AgentService,
    AgentResponseFormatter,
    BrowserScanner,
    launch_chrome,
    ScannerError,
    CHROME_PORT,
)

__all__ = [
    "AgentService",
    "AgentResponseFormatter",
    "BrowserScanner",
    "launch_chrome",
    "ScannerError",
    "CHROME_PORT",
]
