"""Tests for the plugin system."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tongs.plugins.base import TongsPlugin
from tongs.plugins.context import PluginContext
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
    def test_minimal_plugin_contributes_nothing(self):
        class MinimalPlugin(TongsPlugin):
            @property
            def name(self):
                return "minimal"

        plugin = MinimalPlugin()
        assert plugin.get_commands() == []
        assert plugin.get_screens() == {}


class TestPluginRegistry:
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
        ep = self._make_entry_point("dummy", DummyPlugin)
        mock_eps.return_value = [ep]
        reg = PluginRegistry()
        reg.discover({"dummy": {"enabled": False}})
        assert reg.plugins == []
        # A disabled plugin must never be imported.
        ep.load.assert_not_called()

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


def _tracking_plugin(name: str, calls: list) -> TongsPlugin:
    class TrackingPlugin(TongsPlugin):
        @property
        def name(self):
            return name

        async def on_app_ready(self, ctx):
            calls.append(("ready", name, ctx))

        async def on_app_shutdown(self, ctx):
            calls.append(("shutdown", name, ctx))

    return TrackingPlugin()


class FailPlugin(TongsPlugin):
    @property
    def name(self):
        return "fail"

    async def on_app_ready(self, ctx):
        raise RuntimeError("boom")

    async def on_app_shutdown(self, ctx):
        raise RuntimeError("boom")


@pytest.mark.asyncio
class TestPluginLifecycle:
    @pytest.mark.parametrize("hook", ["on_app_ready", "on_app_shutdown"])
    async def test_hook_receives_plugin_context_not_app(self, hook):
        calls = []
        app = SimpleNamespace(config=object())
        reg = PluginRegistry()
        reg._plugins.append(_tracking_plugin("tracker", calls))
        await getattr(reg, hook)(app)
        assert len(calls) == 1
        ctx = calls[0][2]
        assert isinstance(ctx, PluginContext)
        assert ctx is not app
        assert ctx.config is app.config

    @pytest.mark.parametrize("hook", ["on_app_ready", "on_app_shutdown"])
    async def test_failing_hook_does_not_stop_later_plugins(self, hook, caplog):
        calls = []
        reg = PluginRegistry()
        reg._plugins.append(FailPlugin())
        reg._plugins.append(_tracking_plugin("healthy", calls))
        with caplog.at_level(logging.WARNING, logger="tongs.plugins.registry"):
            await getattr(reg, hook)(SimpleNamespace())
        assert [(kind, name) for kind, name, _ in calls] == [
            (hook.removeprefix("on_app_"), "healthy")
        ]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert [r.getMessage() for r in warnings] == [f"Plugin fail.{hook} failed"]


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
        return PluginContext(app)

    @pytest.mark.parametrize("field", ["forge_registry", "cache", "config"])
    def test_read_only_field_is_app_field(self, ctx, app, field):
        assert getattr(ctx, field) is getattr(app, field)

    def test_repos_lists_app_repos(self, ctx, app):
        assert list(ctx.repos) == app.repos
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
    def test_mcp_plugin_commands(self, monkeypatch):
        from tongs.mcp import plugin

        monkeypatch.setattr(plugin, "_mcp_available", lambda: True)

        mcp_plugin = plugin.MCPPlugin()
        commands = mcp_plugin.get_commands()
        assert len(commands) == 1
        assert commands[0][0] == "Start MCP Server"
        assert commands[0][1]
        assert commands[0][2] == mcp_plugin._start_server

    def test_mcp_plugin_hides_command_when_dependency_is_missing(self, monkeypatch):
        from tongs.mcp import plugin

        monkeypatch.setattr(plugin, "_mcp_available", lambda: False)

        assert plugin.MCPPlugin().get_commands() == []

    def test_mcp_plugin_refuses_launch_when_dependency_disappears(self, monkeypatch):
        from tongs.mcp import plugin

        monkeypatch.setattr(plugin, "_mcp_available", lambda: False)

        with pytest.raises(RuntimeError, match="optional MCP dependency"):
            plugin.MCPPlugin()._start_server()
