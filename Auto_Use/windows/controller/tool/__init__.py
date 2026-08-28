# Copyright 2026 Cursortouch — Auto-Use

# This file makes tool a Python package
from .open_app import open_on_windows
from .shell import ShellService

__all__ = ['open_on_windows', 'ShellService']