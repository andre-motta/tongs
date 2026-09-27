"""Shared installed-distribution fixtures for desktop plugin tests."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from importlib.metadata import EntryPoint, distributions
from pathlib import Path

import pytest

#: Installed-distribution fixtures.  A module-level constant, so the CI lane
#: drift test sees this fixture root among what the suites read.
DESKTOP_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "desktop_plugins"


@pytest.fixture
def desktop_fixture_root(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.syspath_prepend(str(DESKTOP_FIXTURE_ROOT))
    return DESKTOP_FIXTURE_ROOT


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
