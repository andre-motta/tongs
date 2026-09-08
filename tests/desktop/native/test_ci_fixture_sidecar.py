"""Contract checks for the controlled native utility proof session."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from tongs.desktop.protocol.state import HandleRegistry
from tongs.desktop.protocol.utility_operations import UtilityOperations
from tongs.services import EditorReservation, ServiceEventKind


def _load_fixture() -> ModuleType:
    fixture_path = Path(__file__).with_name("ci_fixture_sidecar.py")
    spec = importlib.util.spec_from_file_location("ci_fixture_sidecar", fixture_path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError("native fixture module is not loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_FixtureSession = cast(type, _load_fixture()._FixtureSession)


class _UnavailableReservations:
    async def reserve(
        self, job_id: int, token: str
    ) -> EditorReservation | None:  # pragma: no cover - cache path only
        raise AssertionError((job_id, token))

    async def release(
        self, slot: int, token: str
    ) -> bool:  # pragma: no cover - cache path only
        raise AssertionError((slot, token))


@pytest.mark.asyncio
async def test_fixture_session_exposes_utility_cache_resync(tmp_path: Path) -> None:
    """Exercise the real fixture facade used by DesktopSidecarServer."""
    session = _FixtureSession(tmp_path / "actions.jsonl")
    operations = UtilityOperations(
        session=session,
        handles=HandleRegistry(),
        reservations=_UnavailableReservations(),
        environment={},
    )

    assert await operations.cache_clear({}, object()) == {"cleared": True}
    events = session.events()
    event = await anext(events)
    await events.aclose()

    assert event.kind is ServiceEventKind.RESYNC_REQUIRED
    assert event.resource is None
