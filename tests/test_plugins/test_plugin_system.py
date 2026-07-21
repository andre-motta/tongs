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


class TestPluginContext:
    @pytest.fixture()
    def app(self):
        mock_app = MagicMock()
        mock_app.forge_registry = MagicMock(name="forge_registry")
        mock_app.cache = MagicMock(name="cache")
        mock_app.config = MagicMock(name="config")
        mock_app.config.plugin_config = {
            "mcp": {"enabled": True, "port": 8080},
            "other": {"key": "value"},
        }
        mock_app.repos = [MagicMock(name="repo1"), MagicMock(name="repo2")]
        return mock_app

    @pytest.fixture()
    def ctx(self, app):
        from tongs.plugins.context import PluginContext

        return PluginContext(app)

    def test_forge_registry_returns_app_registry(self, ctx, app):
        assert ctx.forge_registry is app.forge_registry

    def test_cache_returns_app_cache(self, ctx, app):
        assert ctx.cache is app.cache

    def test_config_returns_app_config(self, ctx, app):
        assert ctx.config is app.config

    def test_repos_returns_app_repos(self, ctx, app):
        assert ctx.repos is app.repos
        assert len(ctx.repos) == 2

    def test_notify_calls_app_notify(self, ctx, app):
        ctx.notify("hello", severity="warning")
        app.notify.assert_called_once_with("hello", severity="warning")

    def test_notify_default_severity(self, ctx, app):
        ctx.notify("info message")
        app.notify.assert_called_once_with("info message", severity="information")

    def test_push_screen_calls_app_push_screen(self, ctx, app):
        screen = MagicMock()
        ctx.push_screen(screen)
        app.push_screen.assert_called_once_with(screen)

    def test_pop_screen_calls_app_pop_screen(self, ctx, app):
        ctx.pop_screen()
        app.pop_screen.assert_called_once()

    def test_plugin_config_returns_correct_dict(self, ctx):
        result = ctx.plugin_config("mcp")
        assert result == {"enabled": True, "port": 8080}

    def test_plugin_config_returns_copy(self, ctx):
        """Returned dict is a copy, not the original."""
        result = ctx.plugin_config("mcp")
        result["extra"] = "injected"
        assert "extra" not in ctx.plugin_config("mcp")

    def test_plugin_config_missing_returns_empty(self, ctx):
        result = ctx.plugin_config("nonexistent")
        assert result == {}


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
