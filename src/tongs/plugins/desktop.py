"""Public declarations for production desktop plugins.

Desktop plugins are installed, trusted Python and UI code.  These declarations
scope accidental cross-plugin access and give the desktop host a validation
boundary; they are not a sandbox for hostile extensions.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from packaging.version import InvalidVersion, Version

DESKTOP_PLUGIN_API_MAJOR = 1
MAX_JSON_DEPTH = 16
MAX_JSON_ITEMS = 4096
MAX_JSON_STRING_BYTES = 256 * 1024

_LOCAL_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_PACKAGE_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type FrozenJsonValue = (
    JsonScalar | tuple[FrozenJsonValue, ...] | Mapping[str, FrozenJsonValue]
)
type FrozenJsonObject = Mapping[str, FrozenJsonValue]


class DesktopAssetKind(StrEnum):
    """Supported packaged resource roles."""

    MODULE = "module"
    STYLESHEET = "stylesheet"
    HELP = "help"


class DesktopReadKind(StrEnum):
    """Host reads a desktop plugin may request through its scoped facade."""

    REPOSITORIES = "repositories"
    REVIEWS = "reviews"
    REVIEW = "review"
    DISCUSSIONS = "discussions"
    COMMITS = "commits"
    PIPELINES = "pipelines"
    JOBS = "jobs"
    LOG = "log"


class DesktopNotificationSeverity(StrEnum):
    INFORMATION = "information"
    WARNING = "warning"
    ERROR = "error"


class DesktopPluginState(StrEnum):
    """Observable state for one entry-point identity."""

    DISCOVERED = "discovered"
    STARTED = "started"
    DISABLED = "disabled"
    TERMINAL_ONLY = "terminal_only"
    INCOMPATIBLE = "incompatible"
    FAILED = "failed"
    STOPPED = "stopped"


class DesktopPluginErrorCode(StrEnum):
    DUPLICATE_ID = "duplicate_id"
    ENTRY_POINT_MISMATCH = "entry_point_mismatch"
    INVALID_MANIFEST = "invalid_manifest"
    INCOMPATIBLE_API = "incompatible_api"
    INCOMPATIBLE_HOST = "incompatible_host"
    IMPORT_FAILED = "import_failed"
    CONSTRUCTION_FAILED = "construction_failed"
    START_FAILED = "start_failed"
    CALL_FAILED = "call_failed"
    CALL_TIMEOUT = "call_timeout"
    STOP_FAILED = "stop_failed"
    CLEANUP_TIMEOUT = "cleanup_timeout"
    UNAVAILABLE = "unavailable"
    UNDECLARED_METHOD = "undeclared_method"
    UNDECLARED_READ = "undeclared_read"
    UNDECLARED_TARGET = "undeclared_target"
    INVALID_DATA = "invalid_data"


@dataclass(frozen=True, slots=True)
class DesktopPluginError:
    """Safe plugin failure suitable for serialization and display."""

    code: DesktopPluginErrorCode
    message: str
    plugin_id: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class DesktopCompatibility:
    api_major: int
    minimum_host_version: str | None = None


@dataclass(frozen=True, slots=True)
class DesktopAsset:
    id: str
    path: str
    kind: DesktopAssetKind


@dataclass(frozen=True, slots=True)
class DesktopAssetBundle:
    id: str
    package: str
    root: str
    assets: tuple[DesktopAsset, ...]
    max_file_bytes: int = 1024 * 1024
    max_total_bytes: int = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DesktopModule:
    id: str
    title: str
    bundle_id: str
    entry_asset_id: str
    stylesheet_asset_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DesktopNavigation:
    id: str
    title: str
    module_id: str


@dataclass(frozen=True, slots=True)
class DesktopCommand:
    id: str
    title: str
    navigation_id: str
    help_text: str = ""


@dataclass(frozen=True, slots=True)
class DesktopMethod:
    id: str


@dataclass(frozen=True, slots=True)
class DesktopEvent:
    id: str


@dataclass(frozen=True, slots=True)
class DesktopFocusTarget:
    id: str
    title: str
    module_id: str


@dataclass(frozen=True, slots=True)
class DesktopPluginManifest:
    plugin_id: str
    title: str
    version: str
    compatibility: DesktopCompatibility
    modules: tuple[DesktopModule, ...]
    asset_bundles: tuple[DesktopAssetBundle, ...]
    navigation: tuple[DesktopNavigation, ...] = ()
    commands: tuple[DesktopCommand, ...] = ()
    methods: tuple[DesktopMethod, ...] = ()
    events: tuple[DesktopEvent, ...] = ()
    focus_targets: tuple[DesktopFocusTarget, ...] = ()
    help_asset_id: str | None = None
    reads: tuple[DesktopReadKind, ...] = ()


@dataclass(frozen=True, slots=True)
class DesktopLocation:
    """Service-independent, JSON-safe current-location metadata."""

    value: FrozenJsonObject


class DesktopCancellation:
    """Cancellation signal owned by the host for one plugin or call."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    def cancel(self) -> None:
        """Signal cancellation without cancelling the caller's task."""
        self._event.set()


@dataclass(frozen=True, slots=True)
class DesktopCallContext:
    invocation_id: str
    cancellation: DesktopCancellation
    location: DesktopLocation | None = None


@runtime_checkable
class DesktopHostFacade(Protocol):
    """Small surface-independent host facade, concretely bound by S6.

    An implementation is created for exactly one plugin and enforces that
    plugin's declared events and focus targets.  It never exposes a forge
    registry, cache, token, raw client, Textual app, or caller-selected origin.
    """

    async def read(
        self,
        kind: DesktopReadKind,
        params: FrozenJsonObject,
        cancellation: DesktopCancellation,
    ) -> FrozenJsonValue: ...

    async def notify(
        self,
        message: str,
        severity: DesktopNotificationSeverity = DesktopNotificationSeverity.INFORMATION,
    ) -> None: ...

    async def publish_event(self, event_id: str, payload: FrozenJsonObject) -> None: ...

    def current_location(self) -> DesktopLocation | None: ...

    async def focus(self, target_id: str, metadata: FrozenJsonObject) -> None: ...


@dataclass(frozen=True, slots=True)
class DesktopPluginContext:
    plugin_id: str
    config: FrozenJsonObject
    host: DesktopHostFacade
    cancellation: DesktopCancellation
    read_kinds: frozenset[DesktopReadKind] = field(default_factory=frozenset)
    event_ids: frozenset[str] = field(default_factory=frozenset)
    focus_target_ids: frozenset[str] = field(default_factory=frozenset)

    async def notify(
        self,
        message: str,
        severity: DesktopNotificationSeverity = DesktopNotificationSeverity.INFORMATION,
    ) -> None:
        """Send a bounded notification through the scoped facade."""
        _validate_text(message, "plugin notification")
        await self.host.notify(message, severity)

    def current_location(self) -> DesktopLocation | None:
        """Read the host-validated JSON-safe location snapshot."""
        return self.host.current_location()

    async def read(
        self,
        kind: DesktopReadKind,
        params: JsonObject,
        cancellation: DesktopCancellation,
    ) -> FrozenJsonValue:
        """Perform a manifest-declared read through the scoped facade."""
        if kind not in self.read_kinds:
            raise DesktopPluginContractError(
                DesktopPluginError(
                    DesktopPluginErrorCode.UNDECLARED_READ,
                    "Plugin read is not declared in its manifest",
                    self.plugin_id,
                )
            )
        return await self.host.read(kind, freeze_json_object(params), cancellation)

    async def publish_event(self, event_id: str, payload: JsonObject) -> None:
        """Publish a manifest-declared event through the scoped facade."""
        if event_id not in self.event_ids:
            raise DesktopPluginContractError(
                DesktopPluginError(
                    DesktopPluginErrorCode.UNDECLARED_TARGET,
                    "Plugin event is not declared in its manifest",
                    self.plugin_id,
                )
            )
        await self.host.publish_event(event_id, freeze_json_object(payload))

    async def focus(self, target_id: str, metadata: JsonObject) -> None:
        """Focus a manifest-declared plugin target."""
        if target_id not in self.focus_target_ids:
            raise DesktopPluginContractError(
                DesktopPluginError(
                    DesktopPluginErrorCode.UNDECLARED_TARGET,
                    "Plugin focus target is not declared in its manifest",
                    self.plugin_id,
                )
            )
        await self.host.focus(target_id, freeze_json_object(metadata))


@runtime_checkable
class DesktopPluginProvider(Protocol):
    """Provider loaded from the ``tongs.desktop_plugins`` entry-point group."""

    def manifest(self) -> DesktopPluginManifest: ...

    async def start(self, context: DesktopPluginContext) -> None: ...

    async def call(
        self,
        method: str,
        params: FrozenJsonObject,
        context: DesktopCallContext,
    ) -> JsonValue: ...

    async def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DesktopPluginRecord:
    plugin_id: str
    state: DesktopPluginState
    has_terminal_entry_point: bool
    has_desktop_entry_point: bool
    manifest: DesktopPluginManifest | None = None
    error: DesktopPluginError | None = None


@dataclass(frozen=True, slots=True)
class DesktopPluginCallResult:
    value: FrozenJsonValue = None
    error: DesktopPluginError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class DesktopPluginContractError(ValueError):
    """Raised for a safe, typed desktop plugin contract failure."""

    def __init__(self, error: DesktopPluginError) -> None:
        super().__init__(error.message)
        self.error = error


def freeze_json(
    value: JsonValue, *, _depth: int = 0, _budget: list[int] | None = None
) -> FrozenJsonValue:
    """Validate and recursively freeze a bounded JSON value."""
    if _budget is None:
        _budget = [MAX_JSON_ITEMS]
    if _depth > MAX_JSON_DEPTH:
        raise ValueError("JSON value exceeds maximum nesting depth")
    _budget[0] -= 1
    if _budget[0] < 0:
        raise ValueError("JSON value exceeds maximum item count")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_JSON_STRING_BYTES:
            raise ValueError("JSON string exceeds maximum size")
        return value
    if isinstance(value, list):
        return tuple(
            freeze_json(item, _depth=_depth + 1, _budget=_budget) for item in value
        )
    if isinstance(value, dict):
        frozen: dict[str, FrozenJsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            if len(key.encode("utf-8")) > MAX_JSON_STRING_BYTES:
                raise ValueError("JSON object key exceeds maximum size")
            frozen[key] = freeze_json(item, _depth=_depth + 1, _budget=_budget)
        return MappingProxyType(frozen)
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


def freeze_json_object(value: JsonObject) -> FrozenJsonObject:
    """Validate and recursively freeze a JSON object."""
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("Expected a JSON object")
    return frozen


def validate_manifest(manifest: DesktopPluginManifest) -> DesktopPluginManifest:
    """Validate a provider manifest and all internal references."""
    _validate_plugin_id(manifest.plugin_id)
    _validate_title(manifest.title, "plugin title")
    _validate_version(manifest.version, "plugin version")
    if (
        type(manifest.compatibility.api_major) is not int
        or manifest.compatibility.api_major < 1
    ):
        raise ValueError("Desktop plugin API major must be positive")
    if manifest.compatibility.minimum_host_version is not None:
        _validate_version(
            manifest.compatibility.minimum_host_version, "minimum host version"
        )

    bundle_ids = _unique_ids(manifest.asset_bundles, "asset bundle")
    module_ids = _unique_ids(manifest.modules, "module")
    navigation_ids = _unique_ids(manifest.navigation, "navigation")
    _unique_ids(manifest.commands, "command")
    _unique_ids(manifest.methods, "method")
    _unique_ids(manifest.events, "event")
    _unique_ids(manifest.focus_targets, "focus target")
    if len(set(manifest.reads)) != len(manifest.reads):
        raise ValueError("Duplicate desktop plugin read declaration")
    if any(not isinstance(read, DesktopReadKind) for read in manifest.reads):
        raise TypeError("Desktop plugin reads must use DesktopReadKind")

    assets: dict[str, DesktopAsset] = {}
    asset_bundle_by_id: dict[str, str] = {}
    for bundle in manifest.asset_bundles:
        _validate_package(bundle.package)
        _validate_resource_path(bundle.root, allow_dot=True)
        if (
            type(bundle.max_file_bytes) is not int
            or not 1 <= bundle.max_file_bytes <= 8 * 1024 * 1024
        ):
            raise ValueError("Asset bundle file limit is outside supported bounds")
        if (
            type(bundle.max_total_bytes) is not int
            or not bundle.max_file_bytes <= bundle.max_total_bytes <= 32 * 1024 * 1024
        ):
            raise ValueError("Asset bundle total limit is outside supported bounds")
        for asset in bundle.assets:
            _validate_local_id(asset.id, "asset")
            if asset.id in assets:
                raise ValueError(f"Duplicate asset id: {asset.id}")
            _validate_resource_path(asset.path)
            _validate_asset_extension(asset)
            assets[asset.id] = asset
            asset_bundle_by_id[asset.id] = bundle.id

    if not manifest.modules:
        raise ValueError("Desktop plugin manifest must declare at least one module")
    for module in manifest.modules:
        _validate_title(module.title, "module title")
        if module.bundle_id not in bundle_ids:
            raise ValueError(f"Unknown asset bundle for module {module.id}")
        entry = assets.get(module.entry_asset_id)
        if entry is None or entry.kind is not DesktopAssetKind.MODULE:
            raise ValueError(
                f"Module {module.id} entry asset is missing or not a module"
            )
        if asset_bundle_by_id[module.entry_asset_id] != module.bundle_id:
            raise ValueError(
                f"Module {module.id} entry asset belongs to another bundle"
            )
        for asset_id in module.stylesheet_asset_ids:
            style = assets.get(asset_id)
            if style is None or style.kind is not DesktopAssetKind.STYLESHEET:
                raise ValueError(f"Module {module.id} stylesheet is missing or invalid")
            if asset_bundle_by_id[asset_id] != module.bundle_id:
                raise ValueError(
                    f"Module {module.id} stylesheet belongs to another bundle"
                )

    for navigation in manifest.navigation:
        _validate_title(navigation.title, "navigation title")
        if navigation.module_id not in module_ids:
            raise ValueError(f"Unknown module for navigation {navigation.id}")
    for command in manifest.commands:
        _validate_title(command.title, "command title")
        _validate_text(command.help_text, "command help", allow_empty=True)
        if command.navigation_id not in navigation_ids:
            raise ValueError(f"Unknown navigation target for command {command.id}")
    for target in manifest.focus_targets:
        _validate_title(target.title, "focus target title")
        if target.module_id not in module_ids:
            raise ValueError(f"Unknown module for focus target {target.id}")
    if manifest.help_asset_id is not None:
        help_asset = assets.get(manifest.help_asset_id)
        if help_asset is None or help_asset.kind is not DesktopAssetKind.HELP:
            raise ValueError("Help asset is missing or not a help resource")
    return manifest


def _unique_ids(values: tuple[object, ...], label: str) -> set[str]:
    found: set[str] = set()
    for value in values:
        identifier = getattr(value, "id", None)
        if not isinstance(identifier, str):
            raise TypeError(f"{label.title()} id must be text")
        _validate_local_id(identifier, label)
        if identifier in found:
            raise ValueError(f"Duplicate {label} id: {identifier}")
        found.add(identifier)
    return found


def _validate_plugin_id(value: str) -> None:
    if len(value) > 80 or not _PLUGIN_ID_RE.fullmatch(value):
        raise ValueError("Invalid desktop plugin id")


def _validate_local_id(value: str, label: str) -> None:
    if len(value) > 80 or not _LOCAL_ID_RE.fullmatch(value):
        raise ValueError(f"Invalid {label} id")


def _validate_package(value: str) -> None:
    if len(value) > 200 or not _PACKAGE_RE.fullmatch(value):
        raise ValueError("Invalid asset package name")


def _validate_title(value: str, label: str) -> None:
    _validate_text(value, label)


def _validate_text(value: str, label: str, *, allow_empty: bool = False) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 500
        or (not allow_empty and not value.strip())
    ):
        raise ValueError(f"Invalid {label}")
    if any(ord(character) < 32 and character not in "\t\n" for character in value):
        raise ValueError(f"Invalid control character in {label}")


def _validate_version(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError(f"Invalid {label}")
    try:
        Version(value)
    except InvalidVersion as error:
        raise ValueError(f"Invalid {label}") from error


def _validate_resource_path(value: str, *, allow_dot: bool = False) -> None:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid package resource path")
    parts = value.split("/")
    if value.startswith("/") or any(part in {"", ".."} for part in parts):
        raise ValueError("Package resource path must be relative and normalized")
    if not allow_dot and any(part == "." for part in parts):
        raise ValueError("Package resource path must be relative and normalized")
    if allow_dot and value != "." and any(part == "." for part in parts):
        raise ValueError("Package resource path must be relative and normalized")


def _validate_asset_extension(asset: DesktopAsset) -> None:
    suffix = "." + asset.path.rsplit(".", 1)[-1].lower() if "." in asset.path else ""
    allowed = {
        DesktopAssetKind.MODULE: {".js", ".mjs"},
        DesktopAssetKind.STYLESHEET: {".css"},
        DesktopAssetKind.HELP: {".md"},
    }[asset.kind]
    if suffix not in allowed:
        raise ValueError(f"Unsupported {asset.kind.value} asset extension")
