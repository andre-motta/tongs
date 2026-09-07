"""Discovery and isolated lifecycle management for desktop plugin providers."""

from __future__ import annotations

import asyncio
import math
import re
from collections import defaultdict
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib.metadata import EntryPoint, entry_points
from importlib.metadata import version as distribution_version

from packaging.version import InvalidVersion, Version

from tongs.plugins.desktop import (
    DESKTOP_PLUGIN_API_MAJOR,
    DesktopCallContext,
    DesktopCancellation,
    DesktopCommand,
    DesktopFocusTarget,
    DesktopHostFacade,
    DesktopNavigation,
    DesktopPluginCallResult,
    DesktopPluginContext,
    DesktopPluginContractError,
    DesktopPluginError,
    DesktopPluginErrorCode,
    DesktopPluginManifest,
    DesktopPluginProvider,
    DesktopPluginRecord,
    DesktopPluginState,
    JsonObject,
    freeze_json,
    freeze_json_object,
    validate_manifest,
)
from tongs.plugins.desktop_resources import (
    DesktopResolvedAsset,
    find_asset,
    validate_asset_resources,
)

DESKTOP_ENTRY_POINT_GROUP = "tongs.desktop_plugins"
TERMINAL_ENTRY_POINT_GROUP = "tongs.plugins"

_INVOCATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")

type EntryPointSource = Callable[[str], Sequence[EntryPoint]]
type DesktopFacadeFactory = Callable[[str, DesktopPluginManifest], DesktopHostFacade]

_OPERATION_TIMEOUT = object()
_PROVIDER_CANCELLED = object()


@dataclass(slots=True)
class _Runtime:
    provider: DesktopPluginProvider
    manifest: DesktopPluginManifest
    assets: tuple[DesktopResolvedAsset, ...]
    cancellation: DesktopCancellation = field(default_factory=DesktopCancellation)
    calls: dict[str, DesktopCancellation] = field(default_factory=dict)


def _installed_entry_points(group: str) -> Sequence[EntryPoint]:
    return tuple(entry_points(group=group))


def _consume_detached_task(task: asyncio.Task[object]) -> None:
    """Retrieve a detached plugin task outcome so asyncio does not warn."""
    if not task.cancelled():
        task.exception()


async def _bounded_plugin_operation[OperationT](
    operation: Coroutine[object, object, OperationT],
    timeout: float,
    cancellation: DesktopCancellation,
) -> OperationT | object:
    """Bound a trusted provider operation without awaiting cancellation forever.

    Providers are trusted installed code and are expected to cooperate with task
    cancellation. A provider that suppresses it is detached after the deadline so
    registry progress and cleanup of other providers remain bounded.
    """
    task = asyncio.create_task(operation)
    try:
        completed, _pending = await asyncio.wait((task,), timeout=timeout)
    except asyncio.CancelledError:
        cancellation.cancel()
        task.cancel()
        task.add_done_callback(_consume_detached_task)
        raise
    if not completed:
        cancellation.cancel()
        task.cancel()
        task.add_done_callback(_consume_detached_task)
        return _OPERATION_TIMEOUT
    if task.cancelled():
        return _PROVIDER_CANCELLED
    return task.result()


class DesktopPluginRegistry:
    """Discover explicit desktop providers without touching legacy providers."""

    def __init__(
        self,
        plugin_config: Mapping[str, Mapping[str, object]] | None = None,
        *,
        supported_api_major: int = DESKTOP_PLUGIN_API_MAJOR,
        start_timeout_seconds: float = 10.0,
        call_timeout_seconds: float = 30.0,
        cleanup_timeout_seconds: float = 2.0,
        entry_point_source: EntryPointSource | None = None,
        host_version: str | None = None,
    ) -> None:
        if type(supported_api_major) is not int or supported_api_major < 1:
            raise ValueError("Supported desktop plugin API major must be positive")
        for value, label in (
            (start_timeout_seconds, "start timeout"),
            (call_timeout_seconds, "call timeout"),
            (cleanup_timeout_seconds, "cleanup timeout"),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Desktop plugin {label} must be positive")
        self._plugin_config = {
            plugin_id: dict(config)
            for plugin_id, config in (plugin_config or {}).items()
        }
        self._supported_api_major = supported_api_major
        raw_host_version = (
            host_version if host_version is not None else distribution_version("tongs")
        )
        try:
            self._host_version = Version(raw_host_version)
        except InvalidVersion as error:
            raise ValueError(
                "Desktop host version must be a valid PEP 440 version"
            ) from error
        self._start_timeout = start_timeout_seconds
        self._call_timeout = call_timeout_seconds
        self._cleanup_timeout = cleanup_timeout_seconds
        self._entry_point_source = entry_point_source or _installed_entry_points
        self._records: dict[str, DesktopPluginRecord] = {}
        self._runtimes: dict[str, _Runtime] = {}
        self._discovered = False

    @property
    def plugins(self) -> tuple[DesktopPluginRecord, ...]:
        """Return immutable discovery/lifecycle snapshots in stable ID order."""
        return tuple(self._records[key] for key in sorted(self._records))

    def discover(self) -> tuple[DesktopPluginRecord, ...]:
        """Discover desktop entries and list terminal-only metadata.

        Terminal entries are inspected only for their names. Their modules are
        never loaded or constructed. Disabled desktop entries are likewise never
        loaded.
        """
        if self._discovered:
            raise RuntimeError("Desktop plugins have already been discovered")
        self._discovered = True

        desktop_by_name = self._group_by_name(DESKTOP_ENTRY_POINT_GROUP)
        terminal_by_name = self._group_by_name(TERMINAL_ENTRY_POINT_GROUP)
        for plugin_id in sorted(set(desktop_by_name) | set(terminal_by_name)):
            desktop_entries = desktop_by_name.get(plugin_id, ())
            terminal_entries = terminal_by_name.get(plugin_id, ())
            has_terminal = bool(terminal_entries)
            if len(plugin_id) > 80 or not _PLUGIN_ID_RE.fullmatch(plugin_id):
                self._records[plugin_id] = DesktopPluginRecord(
                    plugin_id=plugin_id,
                    state=DesktopPluginState.FAILED,
                    has_terminal_entry_point=has_terminal,
                    has_desktop_entry_point=bool(desktop_entries),
                    error=self._error(
                        plugin_id,
                        DesktopPluginErrorCode.INVALID_MANIFEST,
                        "Desktop plugin entry-point id is invalid",
                    ),
                )
                continue
            if not desktop_entries:
                self._records[plugin_id] = DesktopPluginRecord(
                    plugin_id=plugin_id,
                    state=DesktopPluginState.TERMINAL_ONLY,
                    has_terminal_entry_point=True,
                    has_desktop_entry_point=False,
                )
                continue
            if len(desktop_entries) != 1:
                self._records[plugin_id] = self._failed_record(
                    plugin_id,
                    DesktopPluginErrorCode.DUPLICATE_ID,
                    "Duplicate desktop entry-point id",
                    has_terminal=has_terminal,
                )
                continue
            if not self._plugin_config.get(plugin_id, {}).get("enabled", True):
                self._records[plugin_id] = DesktopPluginRecord(
                    plugin_id=plugin_id,
                    state=DesktopPluginState.DISABLED,
                    has_terminal_entry_point=has_terminal,
                    has_desktop_entry_point=True,
                )
                continue
            self._load_entry_point(plugin_id, desktop_entries[0], has_terminal)
        return self.plugins

    async def start_all(
        self, facade_factory: DesktopFacadeFactory
    ) -> tuple[DesktopPluginRecord, ...]:
        """Start every compatible provider while isolating individual failures."""
        self._require_discovered()
        for plugin_id in sorted(self._runtimes):
            record = self._records[plugin_id]
            if record.state is not DesktopPluginState.DISCOVERED:
                continue
            runtime = self._runtimes[plugin_id]
            try:
                facade = facade_factory(plugin_id, runtime.manifest)
                if not isinstance(facade, DesktopHostFacade):
                    raise TypeError("Facade does not implement DesktopHostFacade")
                raw_config = dict(self._plugin_config.get(plugin_id, {}))
                raw_config.pop("enabled", None)
                config = freeze_json_object(raw_config)  # type: ignore[arg-type]
                context = DesktopPluginContext(
                    plugin_id=plugin_id,
                    config=config,
                    host=facade,
                    cancellation=runtime.cancellation,
                    read_kinds=frozenset(runtime.manifest.reads),
                    event_ids=frozenset(event.id for event in runtime.manifest.events),
                    focus_target_ids=frozenset(
                        target.id for target in runtime.manifest.focus_targets
                    ),
                )
                outcome = await _bounded_plugin_operation(
                    runtime.provider.start(context),
                    self._start_timeout,
                    runtime.cancellation,
                )
                if outcome is _OPERATION_TIMEOUT:
                    self._set_error(
                        plugin_id,
                        DesktopPluginErrorCode.START_FAILED,
                        "Desktop plugin start exceeded its time limit",
                    )
                    continue
                if outcome is _PROVIDER_CANCELLED:
                    self._set_error(
                        plugin_id,
                        DesktopPluginErrorCode.START_FAILED,
                        "Desktop plugin cancelled its start operation",
                    )
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - third-party plugin boundary
                self._set_error(
                    plugin_id,
                    DesktopPluginErrorCode.START_FAILED,
                    "Desktop plugin failed to start",
                )
            else:
                self._records[plugin_id] = replace(
                    record, state=DesktopPluginState.STARTED, error=None
                )
        return self.plugins

    async def call(
        self,
        plugin_id: str,
        method: str,
        params: JsonObject,
        context: DesktopCallContext,
    ) -> DesktopPluginCallResult:
        """Invoke one allowlisted plugin method and return a safe typed result."""
        runtime = self._started_runtime(plugin_id)
        if runtime is None:
            return DesktopPluginCallResult(
                error=self._error(
                    plugin_id,
                    DesktopPluginErrorCode.UNAVAILABLE,
                    "Desktop plugin is not available",
                )
            )
        if method not in {item.id for item in runtime.manifest.methods}:
            return DesktopPluginCallResult(
                error=self._error(
                    plugin_id,
                    DesktopPluginErrorCode.UNDECLARED_METHOD,
                    "Desktop plugin method is not declared in its manifest",
                )
            )
        if not _INVOCATION_ID_RE.fullmatch(context.invocation_id):
            return DesktopPluginCallResult(
                error=self._error(
                    plugin_id,
                    DesktopPluginErrorCode.INVALID_DATA,
                    "Invalid desktop plugin invocation id",
                )
            )
        if context.invocation_id in runtime.calls:
            return DesktopPluginCallResult(
                error=self._error(
                    plugin_id,
                    DesktopPluginErrorCode.INVALID_DATA,
                    "Duplicate desktop plugin invocation id",
                )
            )
        try:
            frozen_params = freeze_json_object(params)
        except (TypeError, ValueError):
            return DesktopPluginCallResult(
                error=self._error(
                    plugin_id,
                    DesktopPluginErrorCode.INVALID_DATA,
                    "Desktop plugin parameters are not bounded JSON data",
                )
            )

        runtime.calls[context.invocation_id] = context.cancellation
        try:
            value = await _bounded_plugin_operation(
                runtime.provider.call(method, frozen_params, context),
                self._call_timeout,
                context.cancellation,
            )
            if value is _OPERATION_TIMEOUT:
                return DesktopPluginCallResult(
                    error=self._error(
                        plugin_id,
                        DesktopPluginErrorCode.CALL_TIMEOUT,
                        "Desktop plugin call exceeded its time limit",
                        retryable=True,
                    )
                )
            if value is _PROVIDER_CANCELLED:
                return DesktopPluginCallResult(
                    error=self._error(
                        plugin_id,
                        DesktopPluginErrorCode.CALL_FAILED,
                        "Desktop plugin cancelled its call",
                    )
                )
            frozen = freeze_json(value)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            raise
        except (TypeError, ValueError):
            return DesktopPluginCallResult(
                error=self._error(
                    plugin_id,
                    DesktopPluginErrorCode.INVALID_DATA,
                    "Desktop plugin returned invalid JSON data",
                )
            )
        except Exception:  # noqa: BLE001 - third-party plugin boundary
            return DesktopPluginCallResult(
                error=self._error(
                    plugin_id,
                    DesktopPluginErrorCode.CALL_FAILED,
                    "Desktop plugin call failed",
                )
            )
        finally:
            runtime.calls.pop(context.invocation_id, None)
        return DesktopPluginCallResult(value=frozen)

    async def stop_all(self) -> tuple[DesktopPluginRecord, ...]:
        """Cancel plugin work and bound every provider cleanup independently."""
        self._require_discovered()
        for plugin_id in sorted(self._runtimes, reverse=True):
            runtime = self._runtimes[plugin_id]
            record = self._records[plugin_id]
            if record.state is DesktopPluginState.STOPPED:
                continue
            runtime.cancellation.cancel()
            for cancellation in tuple(runtime.calls.values()):
                cancellation.cancel()
            try:
                outcome = await _bounded_plugin_operation(
                    runtime.provider.stop(),
                    self._cleanup_timeout,
                    runtime.cancellation,
                )
                if outcome is _OPERATION_TIMEOUT:
                    self._set_error(
                        plugin_id,
                        DesktopPluginErrorCode.CLEANUP_TIMEOUT,
                        "Desktop plugin cleanup exceeded its time limit",
                    )
                    continue
                if outcome is _PROVIDER_CANCELLED:
                    self._set_error(
                        plugin_id,
                        DesktopPluginErrorCode.STOP_FAILED,
                        "Desktop plugin cancelled its stop operation",
                    )
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - third-party plugin boundary
                self._set_error(
                    plugin_id,
                    DesktopPluginErrorCode.STOP_FAILED,
                    "Desktop plugin failed to stop",
                )
            else:
                if record.state is not DesktopPluginState.FAILED:
                    self._records[plugin_id] = replace(
                        record, state=DesktopPluginState.STOPPED, error=None
                    )
        return self.plugins

    def assets(self, plugin_id: str) -> tuple[DesktopResolvedAsset, ...]:
        runtime = self._runtime_or_raise(plugin_id)
        return runtime.assets

    def asset(self, plugin_id: str, asset_id: str) -> DesktopResolvedAsset:
        try:
            return find_asset(self.assets(plugin_id), plugin_id, asset_id)
        except LookupError as error:
            raise DesktopPluginContractError(
                self._error(
                    plugin_id,
                    DesktopPluginErrorCode.UNDECLARED_TARGET,
                    "Desktop plugin asset is not declared in its manifest",
                )
            ) from error

    def command(self, plugin_id: str, command_id: str) -> DesktopCommand:
        runtime = self._runtime_or_raise(plugin_id)
        return self._declaration_or_raise(
            plugin_id, runtime.manifest.commands, command_id, "command"
        )

    def navigation(self, plugin_id: str, navigation_id: str) -> DesktopNavigation:
        runtime = self._runtime_or_raise(plugin_id)
        return self._declaration_or_raise(
            plugin_id, runtime.manifest.navigation, navigation_id, "navigation"
        )

    def focus_target(self, plugin_id: str, target_id: str) -> DesktopFocusTarget:
        runtime = self._runtime_or_raise(plugin_id)
        return self._declaration_or_raise(
            plugin_id, runtime.manifest.focus_targets, target_id, "focus target"
        )

    def _group_by_name(self, group: str) -> dict[str, tuple[EntryPoint, ...]]:
        grouped: dict[str, list[EntryPoint]] = defaultdict(list)
        for entry_point in self._entry_point_source(group):
            if entry_point.group != group:
                continue
            grouped[entry_point.name].append(entry_point)
        return {name: tuple(values) for name, values in grouped.items()}

    def _load_entry_point(
        self, plugin_id: str, entry_point: EntryPoint, has_terminal: bool
    ) -> None:
        try:
            provider_type = entry_point.load()
        except Exception:  # noqa: BLE001 - third-party plugin boundary
            self._records[plugin_id] = self._failed_record(
                plugin_id,
                DesktopPluginErrorCode.IMPORT_FAILED,
                "Desktop plugin could not be imported",
                has_terminal=has_terminal,
            )
            return
        try:
            provider = provider_type()
        except Exception:  # noqa: BLE001 - third-party plugin boundary
            self._records[plugin_id] = self._failed_record(
                plugin_id,
                DesktopPluginErrorCode.CONSTRUCTION_FAILED,
                "Desktop plugin could not be constructed",
                has_terminal=has_terminal,
            )
            return
        if not isinstance(provider, DesktopPluginProvider):
            self._records[plugin_id] = self._failed_record(
                plugin_id,
                DesktopPluginErrorCode.INVALID_MANIFEST,
                "Desktop entry point does not implement DesktopPluginProvider",
                has_terminal=has_terminal,
            )
            return
        try:
            manifest = provider.manifest()
            if not isinstance(manifest, DesktopPluginManifest):
                raise TypeError("Manifest has the wrong type")
            validate_manifest(manifest)
        except Exception:  # noqa: BLE001 - third-party plugin boundary
            self._records[plugin_id] = self._failed_record(
                plugin_id,
                DesktopPluginErrorCode.INVALID_MANIFEST,
                "Desktop plugin manifest is invalid",
                has_terminal=has_terminal,
            )
            return
        if manifest.plugin_id != plugin_id:
            self._records[plugin_id] = self._failed_record(
                plugin_id,
                DesktopPluginErrorCode.ENTRY_POINT_MISMATCH,
                "Desktop plugin manifest id does not match its entry point",
                has_terminal=has_terminal,
                manifest=manifest,
            )
            return
        if manifest.compatibility.api_major != self._supported_api_major:
            self._records[plugin_id] = DesktopPluginRecord(
                plugin_id=plugin_id,
                state=DesktopPluginState.INCOMPATIBLE,
                has_terminal_entry_point=has_terminal,
                has_desktop_entry_point=True,
                manifest=manifest,
                error=self._error(
                    plugin_id,
                    DesktopPluginErrorCode.INCOMPATIBLE_API,
                    "Desktop plugin API major is incompatible with this host",
                ),
            )
            return
        minimum_host_version = manifest.compatibility.minimum_host_version
        if minimum_host_version is not None and self._host_version < Version(
            minimum_host_version
        ):
            self._records[plugin_id] = DesktopPluginRecord(
                plugin_id=plugin_id,
                state=DesktopPluginState.INCOMPATIBLE,
                has_terminal_entry_point=has_terminal,
                has_desktop_entry_point=True,
                manifest=manifest,
                error=self._error(
                    plugin_id,
                    DesktopPluginErrorCode.INCOMPATIBLE_HOST,
                    "Desktop plugin requires a newer Tongs host version",
                ),
            )
            return
        try:
            assets = validate_asset_resources(manifest)
        except Exception:  # noqa: BLE001 - package resource provider boundary
            self._records[plugin_id] = self._failed_record(
                plugin_id,
                DesktopPluginErrorCode.INVALID_MANIFEST,
                "Desktop plugin asset declarations are invalid",
                has_terminal=has_terminal,
                manifest=manifest,
            )
            return

        self._runtimes[plugin_id] = _Runtime(provider, manifest, assets)
        self._records[plugin_id] = DesktopPluginRecord(
            plugin_id=plugin_id,
            state=DesktopPluginState.DISCOVERED,
            has_terminal_entry_point=has_terminal,
            has_desktop_entry_point=True,
            manifest=manifest,
        )

    def _runtime_or_raise(self, plugin_id: str) -> _Runtime:
        runtime = self._runtimes.get(plugin_id)
        if runtime is None:
            raise DesktopPluginContractError(
                self._error(
                    plugin_id,
                    DesktopPluginErrorCode.UNAVAILABLE,
                    "Desktop plugin is not available",
                )
            )
        return runtime

    def _started_runtime(self, plugin_id: str) -> _Runtime | None:
        runtime = self._runtimes.get(plugin_id)
        record = self._records.get(plugin_id)
        if (
            runtime is None
            or record is None
            or record.state is not DesktopPluginState.STARTED
        ):
            return None
        return runtime

    def _declaration_or_raise[DeclarationT](
        self,
        plugin_id: str,
        values: tuple[DeclarationT, ...],
        identifier: str,
        label: str,
    ) -> DeclarationT:
        for value in values:
            if value.id == identifier:
                return value
        raise DesktopPluginContractError(
            self._error(
                plugin_id,
                DesktopPluginErrorCode.UNDECLARED_TARGET,
                f"Desktop plugin {label} is not declared in its manifest",
            )
        )

    def _failed_record(
        self,
        plugin_id: str,
        code: DesktopPluginErrorCode,
        message: str,
        *,
        has_terminal: bool,
        manifest: DesktopPluginManifest | None = None,
    ) -> DesktopPluginRecord:
        return DesktopPluginRecord(
            plugin_id=plugin_id,
            state=DesktopPluginState.FAILED,
            has_terminal_entry_point=has_terminal,
            has_desktop_entry_point=True,
            manifest=manifest,
            error=self._error(plugin_id, code, message),
        )

    def _set_error(
        self, plugin_id: str, code: DesktopPluginErrorCode, message: str
    ) -> None:
        self._records[plugin_id] = replace(
            self._records[plugin_id],
            state=DesktopPluginState.FAILED,
            error=self._error(plugin_id, code, message),
        )

    @staticmethod
    def _error(
        plugin_id: str,
        code: DesktopPluginErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> DesktopPluginError:
        return DesktopPluginError(code, message, plugin_id, retryable)

    def _require_discovered(self) -> None:
        if not self._discovered:
            raise RuntimeError("Desktop plugins have not been discovered")
