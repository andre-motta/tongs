"""Security facade for plugin access to the application."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tongs.cache.store import CacheStore
    from tongs.config import Config
    from tongs.forges.registry import ForgeRegistry
    from tongs.scanner.repo import Repo


class PluginContext:
    """Restricted view of the application exposed to plugins.

    Prevents plugins from accessing internal app state or mutating
    the TUI directly. Only safe, read-only properties and a
    notification helper are exposed.
    """

    def __init__(self, app) -> None:
        self._app = app

    @property
    def forge_registry(self) -> ForgeRegistry:
        """Read-only access to the forge registry."""
        return self._app.forge_registry

    @property
    def cache(self) -> CacheStore:
        """Read-only access to the cache store."""
        return self._app.cache

    @property
    def config(self) -> Config:
        """Read-only access to the application config."""
        return self._app.config

    @property
    def repos(self) -> list[Repo]:
        """Read-only access to discovered repositories."""
        return self._app.repos

    def notify(self, message: str, severity: str = "information") -> None:
        """Send a user-visible notification through the TUI."""
        self._app.notify(message, severity=severity)

    def push_screen(self, screen) -> None:
        """Push a screen onto the TUI screen stack."""
        self._app.push_screen(screen)

    def pop_screen(self) -> None:
        """Pop the current screen from the TUI screen stack."""
        self._app.pop_screen()

    def plugin_config(self, plugin_name: str) -> dict:
        """Return the config section for a specific plugin."""
        return dict(self._app.config.plugin_config.get(plugin_name, {}))
