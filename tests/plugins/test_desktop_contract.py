"""Contract tests for immutable desktop plugin declarations."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from tongs.plugins.desktop import (
    MAX_JSON_DEPTH,
    DesktopAsset,
    DesktopAssetBundle,
    DesktopAssetKind,
    DesktopCommand,
    DesktopCompatibility,
    DesktopEvent,
    DesktopFocusTarget,
    DesktopMethod,
    DesktopModule,
    DesktopNavigation,
    DesktopPluginManifest,
    DesktopReadKind,
    freeze_json,
    validate_manifest,
)


def manifest(**changes: object) -> DesktopPluginManifest:
    values: dict[str, object] = {
        "plugin_id": "reviews",
        "title": "Reviews",
        "version": "1.2.dev3+gabc",
        "compatibility": DesktopCompatibility(1, "0.1.dev2+host"),
        "modules": (DesktopModule("review", "Review", "ui", "main", ("style",)),),
        "asset_bundles": (
            DesktopAssetBundle(
                "ui",
                "example.assets",
                "web",
                (
                    DesktopAsset("main", "main.mjs", DesktopAssetKind.MODULE),
                    DesktopAsset("style", "style.css", DesktopAssetKind.STYLESHEET),
                    DesktopAsset("help", "help.md", DesktopAssetKind.HELP),
                ),
            ),
        ),
        "navigation": (DesktopNavigation("review", "Review", "review"),),
        "commands": (DesktopCommand("open", "Open", "review"),),
        "methods": (DesktopMethod("refresh"),),
        "events": (DesktopEvent("refreshed"),),
        "focus_targets": (DesktopFocusTarget("editor", "Editor", "review"),),
        "help_asset_id": "help",
        "reads": (DesktopReadKind.REVIEWS,),
    }
    values.update(changes)
    return DesktopPluginManifest(**values)  # type: ignore[arg-type]


def test_manifest_accepts_pep440_dev_and_local_versions() -> None:
    # hatch-vcs development builds carry dev and local version segments.
    declaration = validate_manifest(manifest())

    assert declaration.version == "1.2.dev3+gabc"
    assert declaration.compatibility.minimum_host_version == "0.1.dev2+host"


@pytest.mark.parametrize(
    "version",
    ["not a version", "", "1.0+"],
)
def test_manifest_rejects_invalid_pep440_versions(version: str) -> None:
    with pytest.raises(ValueError, match="plugin version"):
        validate_manifest(manifest(version=version))


def test_manifest_validates_references_and_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="Duplicate method id"):
        validate_manifest(
            manifest(methods=(DesktopMethod("refresh"), DesktopMethod("refresh")))
        )
    with pytest.raises(ValueError, match="Unknown navigation target"):
        validate_manifest(
            manifest(commands=(DesktopCommand("open", "Open", "missing"),))
        )
    with pytest.raises(ValueError, match="another bundle"):
        validate_manifest(
            manifest(
                asset_bundles=(
                    *manifest().asset_bundles,
                    DesktopAssetBundle(
                        "other",
                        "example.assets",
                        ".",
                        (DesktopAsset("other", "other.mjs", DesktopAssetKind.MODULE),),
                    ),
                ),
                modules=(DesktopModule("review", "Review", "ui", "other"),),
            )
        )


@pytest.mark.parametrize(
    "path", ["../main.mjs", "/main.mjs", "a//main.mjs", "a\\main.mjs"]
)
def test_manifest_rejects_non_normalized_resource_paths(path: str) -> None:
    bundle = manifest().asset_bundles[0]
    bad_bundle = DesktopAssetBundle(
        bundle.id,
        bundle.package,
        bundle.root,
        (DesktopAsset("main", path, DesktopAssetKind.MODULE),),
    )
    with pytest.raises(ValueError, match="resource path"):
        validate_manifest(
            manifest(
                asset_bundles=(bad_bundle,),
                modules=(DesktopModule("review", "Review", "ui", "main"),),
                help_asset_id=None,
            )
        )


def test_manifest_rejects_disallowed_asset_extension() -> None:
    with pytest.raises(ValueError, match="extension"):
        validate_manifest(
            manifest(
                asset_bundles=(
                    DesktopAssetBundle(
                        "ui",
                        "example.assets",
                        ".",
                        (DesktopAsset("main", "main.py", DesktopAssetKind.MODULE),),
                    ),
                ),
                modules=(DesktopModule("review", "Review", "ui", "main"),),
                help_asset_id=None,
            )
        )


def test_manifest_rejects_duplicate_declared_reads() -> None:
    with pytest.raises(ValueError, match="Duplicate desktop plugin read"):
        validate_manifest(
            manifest(reads=(DesktopReadKind.REVIEWS, DesktopReadKind.REVIEWS))
        )


def _manifest_with_limits(
    max_file_bytes: int, max_total_bytes: int
) -> DesktopPluginManifest:
    return manifest(
        asset_bundles=(
            DesktopAssetBundle(
                "ui",
                "example.assets",
                ".",
                manifest().asset_bundles[0].assets,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
            ),
        )
    )


def test_manifest_enforces_absolute_host_size_caps() -> None:
    mib = 1024 * 1024
    validate_manifest(_manifest_with_limits(8 * mib, 32 * mib))
    with pytest.raises(ValueError, match="file limit"):
        validate_manifest(_manifest_with_limits(8 * mib + 1, 16 * mib))
    with pytest.raises(ValueError, match="total limit"):
        validate_manifest(_manifest_with_limits(8 * mib, 32 * mib + 1))


def test_freeze_json_returns_immutable_bounded_data() -> None:
    source = {"items": [1, {"ready": True}]}
    frozen = freeze_json(source)

    assert isinstance(frozen, MappingProxyType)
    source["items"].append(2)  # type: ignore[union-attr]
    assert frozen["items"] == (1, MappingProxyType({"ready": True}))
    with pytest.raises(TypeError):
        frozen["other"] = 1  # type: ignore[index]
    with pytest.raises(ValueError, match="finite"):
        freeze_json(float("inf"))
    with pytest.raises(TypeError, match="Unsupported"):
        freeze_json({1, 2})  # type: ignore[arg-type]


def _nested(levels: int) -> object:
    value: object = None
    for _ in range(levels):
        value = [value]
    return value


def test_freeze_json_rejects_excessive_depth() -> None:
    freeze_json(_nested(MAX_JSON_DEPTH))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nesting depth"):
        freeze_json(_nested(MAX_JSON_DEPTH + 1))  # type: ignore[arg-type]
