"""Launcher for the bundled Electron prototype runtime."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _split_arguments(
    arguments: list[str], *, session_type: str | None
) -> tuple[list[str], list[str]]:
    chromium_args: list[str] = []
    app_args: list[str] = []
    value_options = {"--ozone-platform", "--use-angle", "--use-gl"}
    flag_options = {
        "--disable-gpu",
        "--disable-vulkan",
        "--enable-gpu-sandbox",
        "--gpu-sandbox-start-early",
    }
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value in value_options and index + 1 < len(arguments):
            chromium_args.extend((value, arguments[index + 1]))
            index += 2
            continue
        if value in flag_options or value.startswith(
            ("--ozone-platform=", "--use-angle=", "--use-gl=")
        ):
            chromium_args.append(value)
        else:
            app_args.append(value)
        index += 1
    if session_type == "wayland" and not any(
        value == "--ozone-platform" or value.startswith("--ozone-platform=")
        for value in chromium_args
    ):
        chromium_args.insert(0, "--ozone-platform=x11")
    return chromium_args, app_args


def main() -> None:
    """Replace the launcher with the bundled Electron executable."""
    package = Path(__file__).resolve().parent
    executable = package / "runtime" / "tongs-electron"
    if not executable.is_file():
        raise SystemExit("Bundled Electron runtime is missing")
    environment = os.environ.copy()
    environment["TONGS_DESKTOP_PYTHON"] = sys.executable
    chromium_args, app_args = _split_arguments(
        sys.argv[1:], session_type=environment.get("XDG_SESSION_TYPE")
    )
    os.execve(
        executable,
        [
            str(executable),
            *chromium_args,
            *app_args,
        ],
        environment,
    )
