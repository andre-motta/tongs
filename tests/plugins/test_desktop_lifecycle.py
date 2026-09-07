"""Lifecycle and scoped-facade tests for installed desktop providers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from importlib.metadata import EntryPoint

import pytest

from tongs.plugins.desktop import (
    DesktopCallContext,
    DesktopCancellation,
    DesktopHostFacade,
    DesktopLocation,
    DesktopNotificationSeverity,
    DesktopPluginContext,
    DesktopPluginContractError,
    DesktopPluginErrorCode,
    DesktopPluginState,
    DesktopReadKind,
    FrozenJsonObject,
    FrozenJsonValue,
    freeze_json_object,
)
from tongs.plugins.desktop_registry import DesktopPluginRegistry


class RecordingFacade:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, DesktopNotificationSeverity]] = []
        self.events: list[tuple[str, FrozenJsonObject]] = []
        self.focus_requests: list[tuple[str, FrozenJsonObject]] = []

    async def read(
        self,
        kind: DesktopReadKind,
        params: FrozenJsonObject,
        cancellation: DesktopCancellation,
    ) -> FrozenJsonValue:
        return {"kind": kind.value, "cancelled": cancellation.cancelled}

    async def notify(
        self,
        message: str,
        severity: DesktopNotificationSeverity = DesktopNotificationSeverity.INFORMATION,
    ) -> None:
        self.notifications.append((message, severity))

    async def publish_event(self, event_id: str, payload: FrozenJsonObject) -> None:
        self.events.append((event_id, payload))

    def current_location(self) -> DesktopLocation | None:
        return DesktopLocation(freeze_json_object({"screen": "review"}))

    async def focus(self, target_id: str, metadata: FrozenJsonObject) -> None:
        self.focus_requests.append((target_id, metadata))


def only_good(
    source: Callable[[str], Sequence[EntryPoint]],
) -> Callable[[str], Sequence[EntryPoint]]:
    return lambda group: tuple(item for item in source(group) if item.name == "good")


def good_and_dual(
    source: Callable[[str], Sequence[EntryPoint]],
) -> Callable[[str], Sequence[EntryPoint]]:
    return lambda group: tuple(
        item for item in source(group) if item.name in {"good", "dual"}
    )


def record(registry: DesktopPluginRegistry):
    return registry.plugins[0]


@pytest.mark.asyncio
async def test_start_call_lookup_and_stop_use_frozen_scoped_contracts(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    facade = RecordingFacade()
    registry = DesktopPluginRegistry(
        {
            "good": {
                "enabled": True,
                "nested": {"ready": True},
                "publish_on_start": True,
            }
        },
        entry_point_source=only_good(installed_entry_point_source),
        host_version="0.1.dev3+candidate",
    )
    registry.discover()

    await registry.start_all(lambda _plugin_id, _manifest: facade)
    result = await registry.call(
        "good",
        "echo",
        {"value": 42},
        DesktopCallContext("call-1", DesktopCancellation()),
    )

    assert record(registry).state is DesktopPluginState.STARTED
    assert result.ok
    assert result.value == {"method": "echo", "value": 42}
    assert registry.asset("good", "main").package == "fixture_desktop_assets"
    assert registry.command("good", "open").navigation_id == "review"
    assert registry.navigation("good", "review").module_id == "review"
    assert registry.focus_target("good", "editor").module_id == "review"
    assert isinstance(facade, DesktopHostFacade)
    assert facade.notifications == [
        ("Fixture started", DesktopNotificationSeverity.INFORMATION)
    ]
    assert facade.events == [("refreshed", {"ready": True})]
    with pytest.raises(DesktopPluginContractError):
        registry.asset("good", "other")

    await registry.stop_all()
    assert record(registry).state is DesktopPluginState.STOPPED


@pytest.mark.asyncio
async def test_context_scopes_events_and_focus_to_manifest_declarations() -> None:
    facade = RecordingFacade()
    context = DesktopPluginContext(
        "good",
        freeze_json_object({}),
        facade,
        DesktopCancellation(),
        read_kinds=frozenset({DesktopReadKind.REVIEWS}),
        event_ids=frozenset({"refreshed"}),
        focus_target_ids=frozenset({"editor"}),
    )

    result = await context.read(
        DesktopReadKind.REVIEWS, {"state": "open"}, DesktopCancellation()
    )
    await context.notify("Ready", DesktopNotificationSeverity.WARNING)
    await context.publish_event("refreshed", {"count": 1})
    await context.focus("editor", {"line": 2})

    assert facade.events == [("refreshed", {"count": 1})]
    assert facade.focus_requests == [("editor", {"line": 2})]
    assert facade.notifications == [("Ready", DesktopNotificationSeverity.WARNING)]
    assert context.current_location() == DesktopLocation(
        freeze_json_object({"screen": "review"})
    )
    assert result == {"kind": "reviews", "cancelled": False}
    with pytest.raises(DesktopPluginContractError):
        await context.read(DesktopReadKind.LOG, {}, DesktopCancellation())
    with pytest.raises(DesktopPluginContractError):
        await context.publish_event("other", {})
    with pytest.raises(DesktopPluginContractError):
        await context.focus("other", {})


@pytest.mark.asyncio
async def test_call_rejects_undeclared_duplicate_and_unbounded_data(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    undeclared = await registry.call(
        "good", "secret", {}, DesktopCallContext("call-1", DesktopCancellation())
    )
    invalid_id = await registry.call(
        "good", "echo", {}, DesktopCallContext("bad id", DesktopCancellation())
    )
    invalid_data = await registry.call(
        "good",
        "echo",
        {"value": {1, 2}},  # type: ignore[dict-item]
        DesktopCallContext("call-2", DesktopCancellation()),
    )

    assert undeclared.error.code is DesktopPluginErrorCode.UNDECLARED_METHOD  # type: ignore[union-attr]
    assert invalid_id.error.code is DesktopPluginErrorCode.INVALID_DATA  # type: ignore[union-attr]
    assert invalid_data.error.code is DesktopPluginErrorCode.INVALID_DATA  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_concurrent_duplicate_invocation_id_is_rejected(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    cancellation = DesktopCancellation()
    first = asyncio.create_task(
        registry.call("good", "wait", {}, DesktopCallContext("same-id", cancellation))
    )
    await asyncio.sleep(0)

    duplicate = await registry.call(
        "good",
        "echo",
        {},
        DesktopCallContext("same-id", DesktopCancellation()),
    )
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert duplicate.error.code is DesktopPluginErrorCode.INVALID_DATA  # type: ignore[union-attr]
    assert cancellation.cancelled


@pytest.mark.asyncio
async def test_call_failure_timeout_and_invalid_result_are_isolated(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
        call_timeout_seconds=0.01,
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    failure = await registry.call(
        "good", "explode", {}, DesktopCallContext("failure", DesktopCancellation())
    )
    cancellation = DesktopCancellation()
    timeout = await registry.call(
        "good", "wait", {}, DesktopCallContext("timeout", cancellation)
    )
    invalid = await registry.call(
        "good", "invalid", {}, DesktopCallContext("invalid", DesktopCancellation())
    )
    recovery = await registry.call(
        "good",
        "echo",
        {"value": "ready"},
        DesktopCallContext("recovery", DesktopCancellation()),
    )

    assert failure.error.code is DesktopPluginErrorCode.CALL_FAILED  # type: ignore[union-attr]
    assert timeout.error.code is DesktopPluginErrorCode.CALL_TIMEOUT  # type: ignore[union-attr]
    assert cancellation.cancelled
    assert invalid.error.code is DesktopPluginErrorCode.INVALID_DATA  # type: ignore[union-attr]
    assert recovery.ok


@pytest.mark.asyncio
async def test_provider_observes_call_cancellation_signal(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    cancellation = DesktopCancellation()
    cancellation.cancel()

    result = await registry.call(
        "good",
        "observe_cancellation",
        {},
        DesktopCallContext("observe", cancellation),
    )

    assert result.value == {"cancelled": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "expected_code"),
    [
        ({"fail_stop": True}, DesktopPluginErrorCode.STOP_FAILED),
        ({"hang_stop": True}, DesktopPluginErrorCode.CLEANUP_TIMEOUT),
    ],
)
async def test_cleanup_failure_is_bounded_and_retained(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
    config: dict[str, bool],
    expected_code: DesktopPluginErrorCode,
) -> None:
    registry = DesktopPluginRegistry(
        {"good": config},
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
        cleanup_timeout_seconds=0.01,
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    await registry.stop_all()

    assert record(registry).state is DesktopPluginState.FAILED
    assert record(registry).error.code is expected_code  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_cleanup_deadline_does_not_await_suppressed_task_cancellation(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        {"good": {"stubborn_stop": True}},
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
        cleanup_timeout_seconds=0.005,
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    loop = asyncio.get_running_loop()
    started = loop.time()

    await registry.stop_all()

    assert loop.time() - started < 0.04
    assert record(registry).error.code is DesktopPluginErrorCode.CLEANUP_TIMEOUT  # type: ignore[union-attr]
    await asyncio.sleep(0.06)


@pytest.mark.asyncio
async def test_start_failure_isolated_and_not_erased_by_cleanup(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        {"good": {"fail_start": True}},
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
    )
    registry.discover()

    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())
    await registry.stop_all()

    assert record(registry).state is DesktopPluginState.FAILED
    assert record(registry).error.code is DesktopPluginErrorCode.START_FAILED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_start_timeout_signals_lifecycle_cancellation(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        {"good": {"hang_start": True}},
        entry_point_source=only_good(installed_entry_point_source),
        host_version="1.0",
        start_timeout_seconds=0.01,
    )
    registry.discover()

    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    assert record(registry).state is DesktopPluginState.FAILED
    assert record(registry).error.code is DesktopPluginErrorCode.START_FAILED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_provider_cancelled_start_does_not_skip_other_providers(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        {"good": {"cancel_start": True}},
        entry_point_source=good_and_dual(installed_entry_point_source),
        host_version="1.0",
    )
    registry.discover()

    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    states = {item.plugin_id: item for item in registry.plugins}
    assert states["dual"].state is DesktopPluginState.STARTED
    assert states["good"].state is DesktopPluginState.FAILED
    assert states["good"].error.code is DesktopPluginErrorCode.START_FAILED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_provider_cancelled_stop_does_not_skip_other_providers(
    installed_entry_point_source: Callable[[str], Sequence[EntryPoint]],
) -> None:
    registry = DesktopPluginRegistry(
        {"good": {"cancel_stop": True}},
        entry_point_source=good_and_dual(installed_entry_point_source),
        host_version="1.0",
    )
    registry.discover()
    await registry.start_all(lambda _plugin_id, _manifest: RecordingFacade())

    await registry.stop_all()

    states = {item.plugin_id: item for item in registry.plugins}
    assert states["dual"].state is DesktopPluginState.STOPPED
    assert states["good"].state is DesktopPluginState.FAILED
    assert states["good"].error.code is DesktopPluginErrorCode.STOP_FAILED  # type: ignore[union-attr]


def test_facade_protocol_has_no_concrete_host_or_tui_capabilities() -> None:
    public_names = {
        name for name in DesktopHostFacade.__dict__ if not name.startswith("_")
    }

    assert public_names == {
        "read",
        "notify",
        "publish_event",
        "current_location",
        "focus",
    }
    assert public_names.isdisjoint(
        {"forge_registry", "cache", "token", "client", "app", "query_one"}
    )
