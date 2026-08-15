#!/usr/bin/env python3
"""
Manual browser scan runner for element.rs

Opens Chrome (CDP) and drops you into the scanner REPL.
Type any URL in the browser yourself, then scan from the prompt.

This file lives in:  <repo>/Auto_Use/web/tree/
Repo root here is:   /Users/ashishyadav/Desktop/Auto-Use

Use `python3` (there is no bare `python` on this machine).

Usage (from this directory):
    cd /Users/ashishyadav/Desktop/Auto-Use/Auto_Use/web/tree
    python3 test.py
    python3 test.py --port 9222
    python3 test.py --url https://example.com   # optional start page

Usage (from project root / outside this folder):
    cd /Users/ashishyadav/Desktop/Auto-Use
    python3 Auto_Use/web/tree/test.py
    python3 Auto_Use/web/tree/test.py --port 9222
    python3 Auto_Use/web/tree/test.py --url https://example.com

    # absolute path also works from anywhere:
    python3 /Users/ashishyadav/Desktop/Auto-Use/Auto_Use/web/tree/test.py
    python3 /Users/ashishyadav/Desktop/Auto-Use/Auto_Use/web/tree/test.py --url https://example.com

Filtering:
    element.config.json (this folder) is picked up automatically and merged
    over the built-in defaults — edit it to tune what gets marked. The "noise"
    block controls the marks filter, "hosts" holds per-site overrides.
    Press c in the REPL to reload it without restarting.

Notes:
    - First run compiles the web crate with cargo (a minute or two); later
      runs can skip it with --no-build once web/target/release/element exists.
    - cargo is not on PATH here; the script falls back to ~/.cargo/bin/cargo.

REPL tips:
    s / enter   scan page  -> debug/scans/tree.txt + shot.jpg  (overwritten each scan)
    g <url>     navigate
    t           list tabs
    n <url>     new tab
    m           toggle marks
    q           quit (browser stays open)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent                       # the one crate for the whole web side
ROOT = HERE.parents[2]                  # repo root — debug/ + scans live here
BIN = WEB / "target" / "release" / "element"


def ensure_built() -> Path:
    cargo = shutil.which("cargo")
    if not cargo:
        # common install location when PATH is thin
        home_cargo = Path.home() / ".cargo" / "bin" / "cargo"
        if home_cargo.exists():
            cargo = str(home_cargo)
        else:
            sys.exit("cargo not found — install Rust from https://rustup.rs")

    print("building element (release)…")
    subprocess.check_call(
        [cargo, "build", "--release", "--manifest-path", str(WEB / "Cargo.toml")],
        cwd=str(WEB),
    )
    if not BIN.exists():
        sys.exit(f"build ok but binary missing: {BIN}")
    return BIN


def main() -> None:
    ap = argparse.ArgumentParser(description="Open Chrome and run the element scanner REPL")
    ap.add_argument("--port", type=int, default=9222, help="Chrome remote-debugging port")
    ap.add_argument("--url", default=None, help="Optional start URL (otherwise blank / last tab)")
    ap.add_argument("--out", default="debug/scans", help="Output directory for tree/shot files (relative to the repo root)")
    ap.add_argument("--no-build", action="store_true", help="Skip cargo build")
    args = ap.parse_args()

    # Run from the repo root: the binary writes debug/iteration_<n>/ and the
    # scan output relative to its CWD, and the ROOT debug/ folder is the one
    # place run data belongs (web/tree stays source-only).
    os.chdir(ROOT)
    bin_path = BIN if args.no_build and BIN.exists() else ensure_built()

    cmd = [str(bin_path), "--port", str(args.port), "--out", args.out,
           "--config", str(HERE / "element.config.json")]
    if args.url:
        cmd.extend(["--goto", args.url])

    print()
    print("Chrome will open (or attach). Browse anywhere manually.")
    print("Back here, use the autouse> prompt — press Enter / type s to scan.")
    print()
    os.execv(str(bin_path), cmd)


if __name__ == "__main__":
    main()
