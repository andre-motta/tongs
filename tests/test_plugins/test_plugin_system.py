"""Tests for the plugin system."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tongs.plugins.base import TongsPlugin
from tongs.plugins.registry import PluginRegistry


class DummyPlugin(TongsPlugin):
    @property
    def name(self) -> str:
        return "dummy"

    @property
    def version(self) -> str:
        return "1.0.0"

    def get_commands(self) -> list[tuple[str, str, object]]:
        return [("Dummy Command", "Does nothing", lambda: None)]

    def get_screens(self) -> dict[str, type]:
        return {"dummy-screen": object}


class BrokenPlugin(TongsPlugin):
    @property
    def name(self) -> str:
        return "broken"

    def get_commands(self):
        raise RuntimeError("broken")

    def get_screens(self):
        raise RuntimeError("broken screens")


class TestTongsPluginABC:
    def test_name_is_abstract(self):
        with pytest.raises(TypeError):
            TongsPlugin()

    def test_concrete_plugin(self):
        p = DummyPlugin()
        assert p.name == "dummy"
        assert p.version == "1.0.0"

    def test_default_version(self):
        class MinimalPlugin(TongsPlugin):
            @property
            def name(self):
                return "minimal"

        p = MinimalPlugin()
        assert p.version == "0.0.0"

    def test_default_commands_empty(self):
        class MinimalPlugin(TongsPlugin):
            @property
            def name(self):
                return "minimal"

        assert MinimalPlugin().get_commands() == []

    def test_default_screens_empty(self):
        class MinimalPlugin(TongsPlugin):
            @property
            def name(self):
                return "minimal"

        assert MinimalPlugin().get_screens() == {}


class TestPluginRegistry:
    def test_empty_registry(self):
        reg = PluginRegistry()
        assert reg.plugins == []

    def test_get_all_commands_empty(self):
        reg = PluginRegistry()
        assert reg.get_all_commands() == []

    def test_get_all_commands_with_plugins(self):
        reg = PluginRegistry()
        reg._plugins.append(DummyPlugin())
        commands = reg.get_all_commands()
        assert len(commands) == 1
        assert commands[0][0] == "Dummy Command"

    def test_get_all_screens(self):
        reg = PluginRegistry()
        reg._plugins.append(DummyPlugin())
        screens = reg.get_all_screens()
        assert "dummy-screen" in screens

    def test_broken_plugin_commands_handled(self):
        reg = PluginRegistry()
        reg._plugins.append(BrokenPlugin())
        commands = reg.get_all_commands()
        assert commands == []

    def test_broken_plugin_screens_handled(self):
        reg = PluginRegistry()
        reg._plugins.append(BrokenPlugin())
        screens = reg.get_all_screens()
        assert screens == {}

    def test_multi_plugin_commands_merged(self):
        reg = PluginRegistry()
        reg._plugins.append(DummyPlugin())
        reg._plugins.append(DummyPlugin())
        commands = reg.get_all_commands()
        assert len(commands) == 2


class TestPluginDiscovery:
    def _make_entry_point(self, name, plugin_cls):
        ep = MagicMock()
        ep.name = name
        ep.load.return_value = plugin_cls
        return ep

    @patch("tongs.plugins.registry.entry_points")
    def test_discover_loads_plugin(self, mock_eps):
        mock_eps.return_value = [self._make_entry_point("dummy", DummyPlugin)]
        reg = PluginRegistry()
        reg.discover()
        assert len(reg.plugins) == 1
        assert reg.plugins[0].name == "dummy"

    @patch("tongs.plugins.registry.entry_points")
    def test_discover_filters_disabled(self, mock_eps):
        mock_eps.return_value = [self._make_entry_point("dummy", DummyPlugin)]
        reg = PluginRegistry()
        reg.discover({"dummy": {"enabled": False}})
        assert reg.plugins == []

    @patch("tongs.plugins.registry.entry_points")
    def test_discover_load_failure_skipped(self, mock_eps):
        ep = MagicMock()
        ep.name = "bad"
        ep.load.side_effect = ImportError("no module")
        mock_eps.return_value = [ep]
        reg = PluginRegistry()
        reg.discover()
        assert reg.plugins == []

    @patch("tongs.plugins.registry.entry_points")
    def test_discover_non_plugin_skipped(self, mock_eps):
        mock_eps.return_value = [self._make_entry_point("notaplugin", object)]
        reg = PluginRegistry()
        reg.discover()
        assert reg.plugins == []


@pytest.mark.asyncio
class TestPluginLifecycle:
    async def test_on_app_ready(self):
        called = []

        class TrackingPlugin(TongsPlugin):
            @property
            def name(self):
                return "tracker"

            async def on_app_ready(self, app):
                called.append("ready")

        reg = PluginRegistry()
        reg._plugins.append(TrackingPlugin())
        await reg.on_app_ready(None)
        assert called == ["ready"]

    async def test_on_app_shutdown(self):
        called = []

        class TrackingPlugin(TongsPlugin):
            @property
            def name(self):
                return "tracker"

            async def on_app_shutdown(self, app):
                called.append("shutdown")

        reg = PluginRegistry()
        reg._plugins.append(TrackingPlugin())
        await reg.on_app_shutdown(None)
        assert called == ["shutdown"]

    async def test_broken_on_app_ready_handled(self):
        class FailPlugin(TongsPlugin):
            @property
            def name(self):
                return "fail"

            async def on_app_ready(self, app):
                raise RuntimeError("boom")

        reg = PluginRegistry()
        reg._plugins.append(FailPlugin())
        await reg.on_app_ready(None)

    async def test_broken_on_app_shutdown_handled(self):
        class FailPlugin(TongsPlugin):
            @property
            def name(self):
                return "fail"

            async def on_app_shutdown(self, app):
                raise RuntimeError("boom")

        reg = PluginRegistry()
        reg._plugins.append(FailPlugin())
        await reg.on_app_shutdown(None)


class TestMCPPlugin:
    def test_mcp_plugin_available(self):
        try:
            from tongs.mcp.plugin import MCPPlugin

            p = MCPPlugin()
            assert p.name == "mcp"
            assert p.version == "0.2.0"
        except ImportError:
            pytest.skip("mcp not installed")

    def test_mcp_plugin_commands(self):
        try:
            from tongs.mcp.plugin import MCPPlugin

            commands = MCPPlugin().get_commands()
            assert len(commands) == 1
            assert commands[0][0] == "Start MCP Server"
            assert commands[0][1]
            assert callable(commands[0][2])
        except ImportError:
            pytest.skip("mcp not installed")
