"""Shared installed-distribution fixtures for desktop plugin tests."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from importlib.metadata import EntryPoint, distributions
from pathlib import Path

import pytest


@pytest.fixture
def desktop_fixture_root(monkeypatch: pytest.MonkeyPatch) -> Path:
    root = Path(__file__).parents[1] / "fixtures" / "desktop_plugins"
    monkeypatch.syspath_prepend(str(root))
    return root


@pytest.fixture
def installed_entry_point_source(
    desktop_fixture_root: Path,
) -> Callable[[str], Sequence[EntryPoint]]:
    entry_points = tuple(
        entry_point
        for distribution in distributions(path=[str(desktop_fixture_root)])
        for entry_point in distribution.entry_points
    )

    def source(group: str) -> Sequence[EntryPoint]:
        return tuple(item for item in entry_points if item.group == group)

    return source
