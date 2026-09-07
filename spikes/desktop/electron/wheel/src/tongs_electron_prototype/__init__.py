"""Launcher for the bundled Electron prototype runtime."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    """Replace the launcher with the bundled Electron executable."""
    package = Path(__file__).resolve().parent
    executable = package / "runtime" / "tongs-electron"
    if not executable.is_file():
        raise SystemExit("Bundled Electron runtime is missing")
    environment = os.environ.copy()
    environment["TONGS_DESKTOP_PYTHON"] = sys.executable
    chromium_args = [
        value
        for value in sys.argv[1:]
        if value == "--disable-gpu" or value.startswith("--ozone-platform=")
    ]
    app_args = [value for value in sys.argv[1:] if value not in chromium_args]
    os.execve(
        executable,
        [
            str(executable),
            *chromium_args,
            *app_args,
        ],
        environment,
    )
