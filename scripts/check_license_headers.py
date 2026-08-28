#!/usr/bin/env python3
# Copyright 2026 Cursortouch — https://gitlab.com/auto-use/auto-use
"""License header checker for AutoUse."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Build the expected header from small pieces so this script's own source
# does not contain a second literal copy of the header that would confuse
# a naive duplicate detector.
_COPYRIGHT_OWNER = "Cursortouch"
_COPYRIGHT_URL = "https://gitlab.com/auto-use/auto-use"
_COPYRIGHT_YEAR = "2026"

EXPECTED_HEADER_LINES = [
    f"# Copyright {_COPYRIGHT_YEAR} {_COPYRIGHT_OWNER} — {_COPYRIGHT_URL}",
]

# Used for duplicate detection. The copyright line is unique enough that
# two real copies in one file means there's actually a duplicate.
DUPLICATE_MARKER = f"Copyright {_COPYRIGHT_YEAR} {_COPYRIGHT_OWNER}"

EXCLUDE_PREFIXES = (
    ".git/",
    ".venv/",
    "venv/",
    "env/",
    "build/",
    "dist/",
)

# The checker itself is excluded so it does not flag its own header
# definitions as a duplicate.
EXCLUDE_FILES = {
    "scripts/check_license_headers.py",
}


def tracked_python_files() -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "*.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.splitlines()
    except (FileNotFoundError, subprocess.CalledProcessError):
        # Fall back when git is unavailable (local workspaces without git).
        lines = [
            p.relative_to(REPO_ROOT).as_posix()
            for p in REPO_ROOT.rglob("*.py")
        ]

    files = []
    for line in lines:
        line = line.strip().replace("\\", "/")
        if not line:
            continue
        if any(line.startswith(p) for p in EXCLUDE_PREFIXES):
            continue
        if any(f"/{name}/" in f"/{line}/" for name in (
            ".venv", "venv", "env", "build", "dist", "__pycache__",
            "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        )):
            continue
        if line in EXCLUDE_FILES:
            continue
        files.append(REPO_ROOT / line)
    return files


def check_file(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [f"{path}: file is not valid UTF-8"]

    lines = text.splitlines()
    start = 1 if lines and lines[0].startswith("#!") else 0
    header_slice = lines[start : start + len(EXPECTED_HEADER_LINES)]
    rel = path.relative_to(REPO_ROOT)

    if header_slice != EXPECTED_HEADER_LINES:
        if DUPLICATE_MARKER in text or "Copyright 2026 Autouse AI" in text:
            errors.append(
                f"{rel}: copyright header present but does not match the "
                f"expected AutoUse header (check wording or position)."
            )
        else:
            errors.append(f"{rel}: missing copyright header.")

    if text.count(DUPLICATE_MARKER) > 1:
        errors.append(
            f"{rel}: copyright header appears more than once "
            f"(only one header allowed)."
        )

    return errors


def main() -> int:
    files = tracked_python_files()
    if not files:
        print("No .py files tracked by git. Nothing to check.")
        return 0

    all_errors: list[str] = []
    for f in files:
        all_errors.extend(check_file(f))

    if all_errors:
        print("License header check FAILED:\n")
        for err in all_errors:
            print(f"  - {err}")
        return 1

    print(f"License header check PASSED ({len(files)} file(s) checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
