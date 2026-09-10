"""Opt-in desktop and legacy terminal fixtures, distributed independently."""

from __future__ import annotations

from importlib.resources import files

from tongs.plugins.base import TongsPlugin


class TerminalPlugin(TongsPlugin):
    @property
    def name(self) -> str:
        return "sample-terminal"

    def get_commands(self) -> list[tuple[str, str, object]]:
        return [("Sample terminal action", "Legacy terminal fixture", lambda: None)]


class DesktopPlugin(TerminalPlugin):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "sample-desktop"

    def get_desktop_modules(self) -> list[dict]:
        return [
            {
                "id": "inspector",
                "title": "Plugin inspector",
                "api_version": "prototype-1",
                "asset_dir": str(files(__package__) / "assets"),
                "entry": "module.js",
                "help": "help.md",
            }
        ]

    def desktop_call(self, method: str, params: dict) -> dict:
        if (
            method != "echo"
            or not isinstance(params, dict)
            or not isinstance(params.get("text"), str)
        ):
            raise ValueError("Use echo with a text string")
        self.calls += 1
        return {
            "message": f"Python plugin received: {params['text']}",
            "calls": self.calls,
        }
