from __future__ import annotations

import sys
from pathlib import Path


def get_runtime_dir() -> Path:
    """Return the base directory that should hold env files and logs.

    For PyInstaller onefile binaries, use the executable directory.
    For source runs, use the current working directory for developer convenience.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()
