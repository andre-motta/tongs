"""Validation for package-resource descriptors declared by desktop plugins."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from tongs.plugins.desktop import (
    DesktopAsset,
    DesktopAssetKind,
    DesktopPluginManifest,
    validate_manifest,
)

_MEDIA_TYPES = {
    DesktopAssetKind.MODULE: "text/javascript; charset=utf-8",
    DesktopAssetKind.STYLESHEET: "text/css; charset=utf-8",
    DesktopAssetKind.HELP: "text/markdown; charset=utf-8",
}

HOST_MAX_ASSET_FILE_BYTES = 8 * 1024 * 1024
HOST_MAX_ASSET_BUNDLE_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DesktopResolvedAsset:
    """Validated logical resource identity without a caller-selected path."""

    plugin_id: str
    bundle_id: str
    asset_id: str
    package: str
    root: str
    path: str
    kind: DesktopAssetKind
    media_type: str
    byte_count: int


def validate_asset_resources(
    manifest: DesktopPluginManifest,
) -> tuple[DesktopResolvedAsset, ...]:
    """Resolve and size every declared resource inside its package root.

    This validates descriptors only.  S6 will perform the final resource read
    when serving a validated descriptor through the desktop asset protocol.
    """
    validate_manifest(manifest)
    resolved: list[DesktopResolvedAsset] = []
    for bundle in manifest.asset_bundles:
        package_root = files(bundle.package)
        resource_root = (
            package_root
            if bundle.root == "."
            else package_root.joinpath(*bundle.root.split("/"))
        )
        _validate_containment(package_root, resource_root)
        if not resource_root.is_dir():
            raise ValueError(f"Asset bundle root does not exist: {bundle.id}")

        total_bytes = 0
        normalized_paths: set[str] = set()
        for asset in bundle.assets:
            normalized = unicodedata.normalize("NFC", asset.path).casefold()
            if normalized in normalized_paths:
                raise ValueError(
                    f"Duplicate normalized asset path in bundle {bundle.id}"
                )
            normalized_paths.add(normalized)

            resource = resource_root.joinpath(*asset.path.split("/"))
            _validate_containment(resource_root, resource)
            if not resource.is_file():
                raise ValueError(f"Declared asset is not a regular file: {asset.id}")
            file_limit = min(bundle.max_file_bytes, HOST_MAX_ASSET_FILE_BYTES)
            total_limit = min(bundle.max_total_bytes, HOST_MAX_ASSET_BUNDLE_BYTES)
            size = _bounded_size(resource, file_limit)
            total_bytes += size
            if total_bytes > total_limit:
                raise ValueError(f"Asset bundle exceeds total size limit: {bundle.id}")
            resolved.append(
                DesktopResolvedAsset(
                    plugin_id=manifest.plugin_id,
                    bundle_id=bundle.id,
                    asset_id=asset.id,
                    package=bundle.package,
                    root=bundle.root,
                    path=asset.path,
                    kind=asset.kind,
                    media_type=_MEDIA_TYPES[asset.kind],
                    byte_count=size,
                )
            )
    return tuple(resolved)


def _bounded_size(resource: Traversable, limit: int) -> int:
    size = 0
    with resource.open("rb") as stream:
        while chunk := stream.read(min(64 * 1024, limit + 1 - size)):
            size += len(chunk)
            if size > limit:
                raise ValueError("Declared asset exceeds per-file size limit")
    return size


def _validate_containment(root: Traversable, resource: Traversable) -> None:
    """Reject filesystem symlinks and resources escaping the declared root."""
    if not isinstance(root, Path) or not isinstance(resource, Path):
        # Non-filesystem Traversables are joined only from validated path parts.
        # They do not expose symlink traversal through the Traversable protocol.
        return

    try:
        relative = resource.relative_to(root)
    except ValueError as error:
        raise ValueError("Asset resource escapes its declared package root") from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Asset resource path contains a symlink")

    root_resolved = root.resolve(strict=True)
    resource_resolved = resource.resolve(strict=True)
    try:
        resource_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("Asset resource escapes its declared package root") from exc


def find_asset(
    assets: tuple[DesktopResolvedAsset, ...], plugin_id: str, asset_id: str
) -> DesktopResolvedAsset:
    """Resolve an asset only within the named plugin namespace."""
    matches = [
        asset
        for asset in assets
        if asset.plugin_id == plugin_id and asset.asset_id == asset_id
    ]
    if len(matches) != 1:
        raise LookupError("Unknown or ambiguous desktop plugin asset")
    return matches[0]


def asset_for_declaration(
    assets: tuple[DesktopResolvedAsset, ...],
    plugin_id: str,
    declaration: DesktopAsset,
) -> DesktopResolvedAsset:
    """Find the validated descriptor corresponding to a manifest declaration."""
    resolved = find_asset(assets, plugin_id, declaration.id)
    if resolved.path != declaration.path or resolved.kind is not declaration.kind:
        raise LookupError("Desktop plugin asset declaration changed after validation")
    return resolved
