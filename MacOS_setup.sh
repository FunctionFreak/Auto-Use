#!/bin/bash
#
# Auto Use — macOS one-click setup
# ================================
# Installs uv (if missing), creates a local .venv/, and installs
# mac_requirements.txt into it. No manual Python install required — uv will
# fetch a Python for you if this Mac doesn't already have a suitable one.
#
# How to run (any of these):
#   bash MacOS_setup.sh          # simplest, works right after clone
#   chmod +x MacOS_setup.sh && ./MacOS_setup.sh
#   Finder → right-click MacOS_setup.sh → Open With → Terminal.app
#
# After it finishes:
#   source .venv/bin/activate
#   python app.py                # launch the GUI app (macOS + Windows)
#

set -e

cd "$(dirname "$0")"

MIN_PYTHON="3.10"
# Exclusive upper bound. Some dependencies ship native wheels that lag behind
# the newest CPython (paddlepaddle, for one, stops at cp313), so a Mac whose
# newest interpreter is 3.14 must not be used for the venv.
MAX_PYTHON="3.14"
PYTHON_SPEC=">=$MIN_PYTHON,<$MAX_PYTHON"

# .venv/ is the name uv looks for by default, so `uv pip install`, `uv run` and
# `uv sync` all find it with no --python flag. The `--prompt .` used below then
# labels the activated shell with this repo's folder name rather than ".venv".
VENV_DIR=".venv"

# -----------------------------------------------------------------------------
# Print helpers (match the style used across the project's build scripts)
# -----------------------------------------------------------------------------
print_step()   { printf "\n============================================================\n  %s\n============================================================\n\n" "$1"; }
print_ok()     { printf "  [OK] %s\n" "$1"; }
print_info()   { printf "  [INFO] %s\n" "$1"; }
print_error()  { printf "  [ERROR] %s\n" "$1"; }

# GUI popup so failures are visible even when launched from Finder
gui_alert() {
    osascript -e "display dialog \"$1\" buttons {\"OK\"} default button 1 with icon caution with title \"Auto Use setup\"" >/dev/null 2>&1 || true
}

# -----------------------------------------------------------------------------
# Step 1 — uv
# -----------------------------------------------------------------------------
# NOTE: Previous versions of this script required the user to install Python
# from python.org by hand before setup could proceed. uv removes that step —
# it resolves an existing Python or downloads one itself. It also replaces pip
# for the install in step 3 (same PyPI packages, much faster, and it resolves
# the whole tree at once instead of one package at a time).
#
# The old "sync shared files to macOS flavor" step is long gone too — main.py,
# cli.py, frontend/index.html and frontend/script.js detect the OS at runtime,
# so a single checkout runs on both macOS and Windows with zero file patching.
print_step "STEP 1: Checking for uv"

# Make sure the standard uv install locations are visible to this shell, in
# case uv was installed by a previous run but the user's shell profile hasn't
# been re-sourced yet.
export PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$PATH"

if command -v uv >/dev/null 2>&1; then
    print_ok "Found uv at $(command -v uv) ($(uv --version))"
else
    print_info "uv not found — installing it (https://astral.sh/uv)"

    if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
        print_error "Failed to install uv."
        print_info  "Check your internet connection and re-run this script."
        gui_alert "Could not install uv.\n\nCheck your internet connection, then run this script again."
        exit 1
    fi

    # The installer drops uv in \$XDG_BIN_HOME or ~/.local/bin and appends that
    # to the shell profile — which does not affect the shell we're in now.
    export PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$PATH"

    if ! command -v uv >/dev/null 2>&1; then
        print_error "uv was installed but isn't on PATH in this shell."
        print_info  "Open a new Terminal window and re-run this script."
        gui_alert "uv was installed, but this Terminal session can't see it yet.\n\nOpen a new Terminal window and run the script again."
        exit 1
    fi

    print_ok "Installed uv ($(uv --version))"
fi

# -----------------------------------------------------------------------------
# Step 2 — .venv/
# -----------------------------------------------------------------------------
print_step "STEP 2: Preparing $VENV_DIR/"

# An existing venv is only reusable if its interpreter is still inside the
# supported range — a leftover venv built on an unsupported Python would fail
# in step 3 with an unresolvable dependency tree instead of here.
if [ -x "$VENV_DIR/bin/python" ]; then
    if MIN="$MIN_PYTHON" MAX="$MAX_PYTHON" "$VENV_DIR/bin/python" -c 'import os, sys; bound = lambda k: tuple(int(p) for p in os.environ[k].split(".")); sys.exit(0 if bound("MIN") <= sys.version_info[:2] < bound("MAX") else 1)' 2>/dev/null; then
        print_info "$VENV_DIR/ already exists — reusing"
    else
        print_info "$VENV_DIR/ uses an unsupported Python — rebuilding it"
        rm -rf "$VENV_DIR"
    fi
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
    # Prefer a Python already on this Mac (--no-managed-python), so the venv
    # keeps using the interpreter the user already has. Only if nothing here
    # satisfies $PYTHON_SPEC do we let uv download one.
    if uv venv --no-managed-python --python "$PYTHON_SPEC" --prompt . "$VENV_DIR" 2>/dev/null; then
        print_ok "Created $VENV_DIR/ using a Python already installed on this Mac"
    else
        print_info "No local Python $PYTHON_SPEC found — letting uv fetch one"
        if ! uv venv --python "$PYTHON_SPEC" --prompt . "$VENV_DIR"; then
            print_error "Could not create a virtual environment."
            gui_alert "Could not create the Python environment.\n\nCheck your internet connection, then run this script again."
            exit 1
        fi
        print_ok "Created $VENV_DIR/ with a uv-managed Python"
    fi
fi

VENV_PYTHON="$VENV_DIR/bin/python"
print_info "Python: $("$VENV_PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"

# -----------------------------------------------------------------------------
# Step 3 — install
# -----------------------------------------------------------------------------
print_step "STEP 3: Installing mac_requirements.txt"

if [ ! -f "mac_requirements.txt" ]; then
    print_error "mac_requirements.txt not found in $(pwd)"
    exit 1
fi

if ! uv pip install --python "$VENV_PYTHON" -r mac_requirements.txt; then
    print_error "Dependency installation failed."
    gui_alert "Installing dependencies failed.\n\nScroll up in Terminal for the error, then run this script again."
    exit 1
fi

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
print_step "SETUP COMPLETE"
print_ok "Dependencies installed into $VENV_DIR/"
printf "\n"
print_info "Next steps:"
printf "    source %s/bin/activate\n" "$VENV_DIR"
[ -f "main.py" ]             && printf "    python main.py               # run the CLI agent\n"
[ -f "app.py" ]              && printf "    python app.py                # launch the GUI app\n"
[ -f "mac_binary_build.py" ] && printf "    python mac_binary_build.py   # produce AutoUse.dmg\n"
printf "\n"
# iOS is optional and needs Xcode, so it lives in its own script rather than
# making every install pay for a WebDriverAgent clone it may never use.
if [ -f "ios_setup.sh" ]; then
    print_info "Want to drive an iPhone or iPad? That needs Xcode and one extra step:"
    printf "    bash ios_setup.sh            # fetches WebDriverAgent, checks the toolchain\n"
    printf "\n"
fi
