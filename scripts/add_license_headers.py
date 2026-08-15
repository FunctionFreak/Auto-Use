#!/usr/bin/env python3
# Copyright 2026 Ashish Yadav — Auto-Use
"""Add or replace the copyright header on every .py file."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_COPYRIGHT_OWNER = "Ashish Yadav"
_COPYRIGHT_PROJECT = "Auto-Use"
_COPYRIGHT_YEAR = "2026"

HEADER_LINES = [
    f"# Copyright {_COPYRIGHT_YEAR} {_COPYRIGHT_OWNER} — {_COPYRIGHT_PROJECT}",
]

HEADER_TEXT = "\n".join(HEADER_LINES)
DUPLICATE_MARKER = f"Copyright {_COPYRIGHT_YEAR} {_COPYRIGHT_OWNER}"

# Recognise both the new header and legacy Autouse AI blocks for replacement.
_OLD_COPYRIGHT_PREFIXES = (
    f"# Copyright {_COPYRIGHT_YEAR} {_COPYRIGHT_OWNER}",
    f"# Copyright {_COPYRIGHT_YEAR} Autouse AI",
)

# Directories to skip entirely.
EXCLUDE_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "env",
    "build",
    "dist",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

# Files to skip (checker and this fixer already have the header).
EXCLUDE_RELATIVE = {
    "scripts/check_license_headers.py",
    "scripts/add_license_headers.py",
}


def iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in EXCLUDE_DIR_NAMES for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        if rel in EXCLUDE_RELATIVE:
            continue
        yield path


def already_has_correct_header(text: str) -> bool:
    lines = text.splitlines()
    start = 1 if lines and lines[0].startswith("#!") else 0
    return lines[start : start + len(HEADER_LINES)] == HEADER_LINES


def _is_copyright_start(line: str) -> bool:
    return any(line.startswith(prefix) for prefix in _OLD_COPYRIGHT_PREFIXES)


def strip_existing_copyright_block(lines: list[str], start: int) -> int:
    """Return index after an existing copyright comment block, if any."""
    if start >= len(lines) or not _is_copyright_start(lines[start]):
        return start

    i = start + 1
    while i < len(lines):
        line = lines[i]
        # Keep consuming license/attribution comment lines (including bare "#").
        if line == "#" or (line.startswith("#") and not line.startswith("#!")):
            i += 1
            continue
        break

    # Drop one blank line that commonly sits between header and body.
    if i < len(lines) and lines[i] == "":
        i += 1
    return i


def add_header(path: Path) -> bool:
    """Return True if the file was modified, False if it already had the header."""
    text = path.read_text(encoding="utf-8")

    if already_has_correct_header(text):
        return False

    lines = text.splitlines(keepends=False)
    ends_with_newline = text.endswith("\n")

    if lines and lines[0].startswith("#!"):
        shebang = lines[0]
        body_start = strip_existing_copyright_block(lines, 1)
        rest = lines[body_start:]
        new_lines = [shebang, *HEADER_LINES]
        if rest:
            new_lines.append("")
            new_lines.extend(rest)
    else:
        body_start = strip_existing_copyright_block(lines, 0)
        rest = lines[body_start:]
        new_lines = list(HEADER_LINES)
        if rest:
            new_lines.append("")
            new_lines.extend(rest)

    new_text = "\n".join(new_lines)
    if ends_with_newline or not text:
        new_text += "\n"

    path.write_text(new_text, encoding="utf-8")
    return True


def main() -> int:
    modified = 0
    already_ok = 0
    skipped = 0

    for path in iter_python_files(REPO_ROOT):
        try:
            result = add_header(path)
        except UnicodeDecodeError:
            print(f"  SKIP (not UTF-8): {path.relative_to(REPO_ROOT)}")
            skipped += 1
            continue

        if result:
            modified += 1
            print(f"  UPDATED: {path.relative_to(REPO_ROOT)}")
        else:
            already_ok += 1

    print()
    print(f"Modified:     {modified}")
    print(f"Already OK:   {already_ok}")
    print(f"Skipped:      {skipped}")
    print()
    print("Done. Review the diff with `git diff` before committing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
