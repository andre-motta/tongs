"""Plugin discovery and lifecycle management."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from tongs.plugins.base import TongsPlugin

log = logging.getLogger(__name__)


class PluginRegistry:
    """Discovers and manages tongs plugins."""

    def __init__(self) -> None:
        self._plugins: list[TongsPlugin] = []

    @property
    def plugins(self) -> list[TongsPlugin]:
        return list(self._plugins)

    def discover(self, plugin_config: dict[str, dict] | None = None) -> None:
        """Discover plugins via entry points, filtered by config."""
        config = plugin_config or {}
        eps = entry_points(group="tongs.plugins")
        for ep in eps:
            plugin_conf = config.get(ep.name, {})
            if not plugin_conf.get("enabled", True):
                log.info("Plugin %s disabled by config", ep.name)
                continue
            try:
                plugin_cls = ep.load()
                plugin = plugin_cls()
                if not isinstance(plugin, TongsPlugin):
                    log.warning(
                        "Plugin %s is not a TongsPlugin subclass, skipping",
                        ep.name,
                    )
                    continue
                self._plugins.append(plugin)
                log.info("Loaded plugin: %s v%s", plugin.name, plugin.version)
            except Exception:
                log.warning("Failed to load plugin %s", ep.name, exc_info=True)

    async def on_app_ready(self, app) -> None:
        for plugin in self._plugins:
            try:
                await plugin.on_app_ready(app)
            except Exception:
                log.warning("Plugin %s.on_app_ready failed", plugin.name, exc_info=True)

    async def on_app_shutdown(self, app) -> None:
        for plugin in self._plugins:
            try:
                await plugin.on_app_shutdown(app)
            except Exception:
                log.warning(
                    "Plugin %s.on_app_shutdown failed",
                    plugin.name,
                    exc_info=True,
                )

    def get_all_commands(self) -> list[tuple[str, str, object]]:
        commands: list[tuple[str, str, object]] = []
        for plugin in self._plugins:
            try:
                commands.extend(plugin.get_commands())
            except Exception:
                log.warning("Plugin %s.get_commands failed", plugin.name, exc_info=True)
        return commands

    def get_all_screens(self) -> dict[str, type]:
        screens: dict[str, type] = {}
        for plugin in self._plugins:
            try:
                screens.update(plugin.get_screens())
            except Exception:
                log.warning("Plugin %s.get_screens failed", plugin.name, exc_info=True)
        return screens
