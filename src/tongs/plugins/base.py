"""Plugin ABC for tongs extensions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tongs.plugins.context import PluginContext


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

    async def on_app_ready(self, ctx: PluginContext) -> None:
        """Called after the TUI app is mounted with a restricted context."""

    async def on_app_shutdown(self, ctx: PluginContext) -> None:
        """Called before app exit with a restricted context."""

    def get_commands(self) -> list[tuple[str, str, object]]:
        """Return (display, help_text, callback) tuples for the command palette."""
        return []

    def get_screens(self) -> dict[str, type]:
        """Return screen_name -> Screen class mappings."""
        return {}
