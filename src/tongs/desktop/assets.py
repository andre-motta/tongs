"""Validated package-asset catalog for the Electron application protocol.

The renderer receives only session-local asset handles and metadata. Package
names and resource paths are supplied by trusted host/plugin declarations and
never accepted from protocol requests.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Protocol

from tongs.desktop.protocol.messages import ProtocolError, ProtocolErrorCode
from tongs.plugins.desktop import DesktopPluginState
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.plugins.desktop_resources import (
    HOST_MAX_ASSET_FILE_BYTES,
    DesktopResolvedAsset,
)

ASSET_CHUNK_BYTES = 512 * 1024
_PACKAGE_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_ASSET_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_CORE_MEDIA_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".wasm": "application/wasm",
}


@dataclass(frozen=True, slots=True)
class CoreAssetSpec:
    """Trusted package declaration for a staged core desktop asset."""

    asset_id: str
    package: str
    root: str
    path: str
    media_type: str | None = None
    max_bytes: int = HOST_MAX_ASSET_FILE_BYTES


@dataclass(frozen=True, slots=True)
class AssetDescriptor:
    """Wire-safe asset metadata without a package or filesystem path."""

    handle: str
    source: str
    asset_id: str
    plugin_id: str | None
    kind: str
    media_type: str
    byte_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AssetChunk:
    handle: str
    offset: int
    next_offset: int | None
    data_base64: str


@dataclass(frozen=True, slots=True)
class _AssetSource:
    descriptor: AssetDescriptor
    package: str
    root: str
    path: str
    max_bytes: int


class TokenSource(Protocol):
    def __call__(self, length: int = 24) -> str: ...


class AssetCatalog:
    """Stage validated core/plugin resources behind unpredictable handles."""

    def __init__(self, *, token_source: TokenSource | None = None) -> None:
        if token_source is None:
            token_source = secrets.token_urlsafe
        self._token_source = token_source
        self._assets: dict[str, _AssetSource] = {}

    @property
    def descriptors(self) -> tuple[AssetDescriptor, ...]:
        return tuple(
            item.descriptor
            for item in sorted(
                self._assets.values(),
                key=lambda item: (
                    item.descriptor.source,
                    item.descriptor.plugin_id or "",
                    item.descriptor.asset_id,
                ),
            )
        )

    def stage_core(self, specs: tuple[CoreAssetSpec, ...]) -> None:
        seen: set[str] = set()
        for spec in specs:
            if spec.asset_id in seen:
                raise ValueError(f"Duplicate core asset id: {spec.asset_id}")
            seen.add(spec.asset_id)
            _validate_id(spec.asset_id, "core asset")
            _validate_package(spec.package)
            _validate_resource_path(spec.root, allow_dot=True)
            _validate_resource_path(spec.path)
            if not isinstance(spec.max_bytes, int) or isinstance(spec.max_bytes, bool):
                raise TypeError("Core asset size limit must be an integer")
            if not 1 <= spec.max_bytes <= HOST_MAX_ASSET_FILE_BYTES:
                raise ValueError("Core asset size limit is outside supported bounds")
            suffix = Path(spec.path).suffix.lower()
            expected_media_type = _CORE_MEDIA_TYPES.get(suffix)
            if expected_media_type is None:
                raise ValueError("Unsupported core desktop asset extension")
            if spec.media_type is not None and spec.media_type != expected_media_type:
                raise ValueError(
                    "Core desktop asset media type does not match its path"
                )
            self._stage(
                source="core",
                asset_id=spec.asset_id,
                plugin_id=None,
                kind="core",
                media_type=expected_media_type,
                package=spec.package,
                root=spec.root,
                path=spec.path,
                max_bytes=spec.max_bytes,
            )

    def stage_plugins(self, registry: DesktopPluginRegistry) -> None:
        for record in registry.plugins:
            if record.state is not DesktopPluginState.STARTED:
                continue
            for asset in registry.assets(record.plugin_id):
                self._stage_plugin(asset)

    def read(self, handle: object, offset: object, length: object) -> AssetChunk:
        source = self._resolve(handle)
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(length, int)
            or isinstance(length, bool)
            or not 1 <= length <= ASSET_CHUNK_BYTES
        ):
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "The asset offset or chunk size is invalid.",
            )
        content = _read_declared_resource(
            source.package, source.root, source.path, source.max_bytes
        )
        if (
            len(content) != source.descriptor.byte_count
            or hashlib.sha256(content).hexdigest() != source.descriptor.sha256
        ):
            raise ProtocolError(
                ProtocolErrorCode.INVALID_HANDLE,
                "The staged asset changed and must be reloaded.",
                retryable=True,
            )
        if offset > len(content):
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "The asset offset is outside the staged resource.",
            )
        end = min(offset + length, len(content))
        next_offset = end if end < len(content) else None
        return AssetChunk(
            source.descriptor.handle,
            offset,
            next_offset,
            base64.b64encode(content[offset:end]).decode("ascii"),
        )

    def clear(self) -> None:
        self._assets.clear()

    def _stage_plugin(self, asset: DesktopResolvedAsset) -> None:
        self._stage(
            source="plugin",
            asset_id=asset.asset_id,
            plugin_id=asset.plugin_id,
            kind=asset.kind.value,
            media_type=asset.media_type,
            package=asset.package,
            root=asset.root,
            path=asset.path,
            max_bytes=min(asset.byte_count, HOST_MAX_ASSET_FILE_BYTES),
        )

    def _stage(
        self,
        *,
        source: str,
        asset_id: str,
        plugin_id: str | None,
        kind: str,
        media_type: str,
        package: str,
        root: str,
        path: str,
        max_bytes: int,
    ) -> None:
        content = _read_declared_resource(package, root, path, max_bytes)
        handle = self._new_handle()
        descriptor = AssetDescriptor(
            handle,
            source,
            asset_id,
            plugin_id,
            kind,
            media_type,
            len(content),
            hashlib.sha256(content).hexdigest(),
        )
        self._assets[handle] = _AssetSource(descriptor, package, root, path, max_bytes)

    def _resolve(self, handle: object) -> _AssetSource:
        if not isinstance(handle, str) or handle not in self._assets:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_HANDLE,
                "The asset handle is invalid or expired.",
            )
        return self._assets[handle]

    def _new_handle(self) -> str:
        while True:
            handle = self._token_source(24)
            if handle not in self._assets:
                return handle


def _read_declared_resource(
    package: str, root: str, path: str, max_bytes: int
) -> bytes:
    package_root = files(package)
    resource_root = (
        package_root if root == "." else package_root.joinpath(*root.split("/"))
    )
    resource = resource_root.joinpath(*path.split("/"))
    _validate_containment(package_root, resource_root)
    _validate_containment(resource_root, resource)
    if not resource.is_file():
        raise ValueError("Declared desktop asset is not a regular file")
    with resource.open("rb") as stream:
        content = stream.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError("Declared desktop asset exceeds its size limit")
    return content


def _validate_containment(root: Traversable, resource: Traversable) -> None:
    if not isinstance(root, Path) or not isinstance(resource, Path):
        return
    try:
        relative = resource.relative_to(root)
    except ValueError as error:
        raise ValueError("Desktop asset escapes its declared package root") from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Desktop asset path contains a symlink")
    resolved_root = root.resolve(strict=True)
    resolved_resource = resource.resolve(strict=True)
    try:
        resolved_resource.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError("Desktop asset escapes its declared package root") from error


def _validate_package(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 200
        or not _PACKAGE_RE.fullmatch(value)
    ):
        raise ValueError("Invalid desktop asset package")


def _validate_id(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 80
        or not _ASSET_ID_RE.fullmatch(value)
    ):
        raise ValueError(f"Invalid {label} id")


def _validate_resource_path(value: str, *, allow_dot: bool = False) -> None:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid desktop asset resource path")
    parts = value.split("/")
    if value.startswith("/") or any(part in {"", ".."} for part in parts):
        raise ValueError("Desktop asset resource path must be relative")
    if (not allow_dot and any(part == "." for part in parts)) or (
        allow_dot and value != "." and any(part == "." for part in parts)
    ):
        raise ValueError("Desktop asset resource path must be normalized")


__all__ = [
    "ASSET_CHUNK_BYTES",
    "AssetCatalog",
    "AssetChunk",
    "AssetDescriptor",
    "CoreAssetSpec",
]
