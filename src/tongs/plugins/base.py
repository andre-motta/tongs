"""Plugin ABC for tongs extensions."""

from __future__ import annotations

from abc import ABC, abstractmethod


class TongsPlugin(ABC):
    """Base class for tongs plugins.

    Plugins are discovered via the ``tongs.plugins`` entry point group
    and can register commands, screens, and lifecycle hooks.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique plugin identifier."""
        ...

    @property
    def version(self) -> str:
        return "0.0.0"

    async def on_app_ready(self, app) -> None:
        """Called after the TUI app is mounted."""

    async def on_app_shutdown(self, app) -> None:
        """Called before app exit."""

    def get_commands(self) -> list[tuple[str, str, object]]:
        """Return (display, help_text, callback) tuples for the command palette."""
        return []

    def get_screens(self) -> dict[str, type]:
        """Return screen_name -> Screen class mappings."""
        return {}
