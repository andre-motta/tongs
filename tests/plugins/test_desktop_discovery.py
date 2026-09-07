"""Discovery tests using installed fixture distribution metadata."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from importlib.metadata import EntryPoint, distributions
from pathlib import Path

from tongs.plugins.desktop import DesktopPluginErrorCode, DesktopPluginState
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.plugins.registry import PluginRegistry


def by_id(registry: DesktopPluginRegistry, plugin_id: str):
    return next(record for record in registry.plugins if record.plugin_id == plugin_id)


def test_discovery_uses_companion_group_without_importing_disabled_or_legacy(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        {"disabled": {"enabled": False}},
        entry_point_source=installed_entry_point_source,
        host_version="0.1.dev3+candidate",
    )

    registry.discover()

    assert by_id(registry, "good").state is DesktopPluginState.DISCOVERED
    assert by_id(registry, "dual").has_terminal_entry_point is True
    assert by_id(registry, "disabled").state is DesktopPluginState.DISABLED
    assert by_id(registry, "terminal_only").state is DesktopPluginState.TERMINAL_ONLY
    assert "fixture_disabled_sentinel" not in sys.modules
    assert "fixture_terminal_sentinel" not in sys.modules
    assert "fixture_terminal_dual" not in sys.modules


def test_tui_registry_never_loads_desktop_entry_point_group(
    desktop_fixture_root: Path,
) -> None:
    sys.modules.pop("fixture_desktop_only_sentinel", None)
    registry = PluginRegistry()

    registry.discover({"terminal_only": {"enabled": False}})

    assert "fixture_desktop_only_sentinel" not in sys.modules
    assert any(plugin.name == "dual" for plugin in registry.plugins)


def test_discovery_reports_safe_identity_import_manifest_and_compatibility_errors(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        entry_point_source=installed_entry_point_source,
        host_version="0.1.dev3+candidate",
    )

    registry.discover()

    assert by_id(registry, "broken").error.code is DesktopPluginErrorCode.IMPORT_FAILED  # type: ignore[union-attr]
    assert (
        by_id(registry, "mismatch").error.code
        is DesktopPluginErrorCode.ENTRY_POINT_MISMATCH
    )  # type: ignore[union-attr]
    assert (
        by_id(registry, "bad_manifest").error.code
        is DesktopPluginErrorCode.INVALID_MANIFEST
    )  # type: ignore[union-attr]
    assert by_id(registry, "api2").state is DesktopPluginState.INCOMPATIBLE
    assert by_id(registry, "api2").error.code is DesktopPluginErrorCode.INCOMPATIBLE_API  # type: ignore[union-attr]
    assert (
        by_id(registry, "future").error.code is DesktopPluginErrorCode.INCOMPATIBLE_HOST
    )  # type: ignore[union-attr]
    assert "fixture_missing" not in sys.modules


def test_discovery_rejects_duplicate_entry_point_identity(
    desktop_fixture_root: Path,
) -> None:
    roots = (desktop_fixture_root, desktop_fixture_root / "duplicate")
    entry_points = tuple(
        entry_point
        for root in roots
        for distribution in distributions(path=[str(root)])
        for entry_point in distribution.entry_points
    )

    registry = DesktopPluginRegistry(
        entry_point_source=lambda group: tuple(
            item for item in entry_points if item.group == group
        ),
        host_version="1.0",
    )
    registry.discover()

    record = by_id(registry, "good")
    assert record.state is DesktopPluginState.FAILED
    assert record.error is not None
    assert record.error.code is DesktopPluginErrorCode.DUPLICATE_ID
