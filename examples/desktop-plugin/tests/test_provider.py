"""Focused lifecycle and manifest checks for the installed example provider."""

from __future__ import annotations

import asyncio

from tongs_example_dashboard.desktop import ExampleDashboardProvider

from tongs.plugins.desktop import (
    DesktopCallContext,
    DesktopCancellation,
    DesktopLocation,
    DesktopNotificationSeverity,
    DesktopPluginContext,
    FrozenJsonObject,
    JsonValue,
    freeze_json_object,
)


class RecordingHost:
    def __init__(self) -> None:
        self.events: list[tuple[str, FrozenJsonObject]] = []
        self.notifications: list[tuple[str, DesktopNotificationSeverity]] = []

    async def read(self, kind, params, cancellation):
        raise AssertionError(f"unexpected host read: {kind}, {params}, {cancellation}")

    async def invoke(self, method, params, context):
        raise AssertionError(f"unexpected host invoke: {method}, {params}, {context}")

    async def notify(self, message, severity=DesktopNotificationSeverity.INFORMATION):
        self.notifications.append((message, severity))

    async def publish_event(self, event_id, payload):
        self.events.append((event_id, payload))

    def current_location(self):
        return DesktopLocation(freeze_json_object({"kind": "plugin"}))

    async def focus(self, target_id, metadata):
        raise AssertionError(f"unexpected host focus: {target_id}, {metadata}")


def make_context(host: RecordingHost) -> DesktopPluginContext:
    return DesktopPluginContext(
        plugin_id="example_dashboard",
        config=freeze_json_object({}),
        host=host,
        cancellation=DesktopCancellation(),
        event_ids=frozenset({"refreshed"}),
    )


def test_manifest_declares_stable_example_surface() -> None:
    manifest = ExampleDashboardProvider().manifest()

    assert manifest.plugin_id == "example_dashboard"
    assert [item.id for item in manifest.modules] == ["dashboard"]
    assert [item.id for item in manifest.navigation] == ["dashboard"]
    assert [item.id for item in manifest.commands] == ["open-dashboard"]
    assert [item.id for item in manifest.methods] == ["refresh"]
    assert [item.id for item in manifest.events] == ["refreshed"]
    assert [item.id for item in manifest.focus_targets] == ["summary"]
    assert manifest.help_asset_id == "dashboard-help"
    assert {item.path for item in manifest.asset_bundles[0].assets} == {
        "dashboard.mjs",
        "dashboard.css",
        "help.md",
    }


def test_terminal_surface_does_not_import_desktop_module() -> None:
    import sys

    sys.modules.pop("tongs_example_dashboard.desktop", None)
    from tongs_example_dashboard.terminal import ExampleDashboardTerminalPlugin

    plugin = ExampleDashboardTerminalPlugin()
    assert plugin.name == "example_dashboard"
    assert "tongs_example_dashboard.desktop" not in sys.modules


def test_refresh_is_deterministic_and_publishes_scoped_event() -> None:
    async def run() -> tuple[JsonValue, RecordingHost]:
        provider = ExampleDashboardProvider()
        host = RecordingHost()
        await provider.start(make_context(host))
        value = await provider.call(
            "refresh",
            freeze_json_object({}),
            DesktopCallContext("refresh-1", DesktopCancellation()),
        )
        await provider.stop()
        return value, host

    value, host = asyncio.run(run())
    assert host.events[0][1] == freeze_json_object(value)  # type: ignore[arg-type]
    assert host.events[0][0] == "refreshed"
    assert value["generated_at"] == "2026-01-01T00:00:00Z"  # type: ignore[index]
    assert len(value["reviews"]) == 3  # type: ignore[index]
