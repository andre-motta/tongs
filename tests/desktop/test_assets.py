from __future__ import annotations

import base64
import importlib
import sys
from pathlib import Path

import pytest

from tongs.desktop.assets import AssetCatalog, CoreAssetSpec
from tongs.desktop.protocol.messages import ProtocolError, ProtocolErrorCode


def _package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package = tmp_path / "sidecar_asset_fixture"
    assets = package / "assets"
    assets.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (assets / "app.mjs").write_text("export const ready = true;", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("sidecar_asset_fixture", None)
    importlib.invalidate_caches()
    return package


def test_core_assets_are_staged_behind_opaque_chunk_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _package(tmp_path, monkeypatch)
    catalog = AssetCatalog(token_source=lambda _length=24: "opaque-asset-token")

    catalog.stage_core(
        (CoreAssetSpec("app", "sidecar_asset_fixture", "assets", "app.mjs"),)
    )

    descriptor = catalog.descriptors[0]
    assert descriptor.handle == "opaque-asset-token"
    assert descriptor.media_type == "text/javascript; charset=utf-8"
    assert not hasattr(descriptor, "package")
    assert not hasattr(descriptor, "path")
    chunk = catalog.read(descriptor.handle, 0, 8)
    assert base64.b64decode(chunk.data_base64) == b"export c"
    assert chunk.next_offset == 8


def test_asset_read_rejects_changed_staged_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(tmp_path, monkeypatch)
    catalog = AssetCatalog(token_source=lambda _length=24: "opaque-asset-token")
    catalog.stage_core(
        (CoreAssetSpec("app", "sidecar_asset_fixture", "assets", "app.mjs"),)
    )
    (package / "assets" / "app.mjs").write_text("changed", encoding="utf-8")

    with pytest.raises(ProtocolError) as caught:
        catalog.read("opaque-asset-token", 0, 8)

    assert caught.value.code is ProtocolErrorCode.INVALID_HANDLE


@pytest.mark.parametrize("path", ["../secret.mjs", "/tmp/secret.mjs", "a//b.mjs"])
def test_core_asset_paths_must_be_normalized(path: str) -> None:
    catalog = AssetCatalog()

    with pytest.raises(ValueError, match="resource path"):
        catalog.stage_core((CoreAssetSpec("app", "fixture", ".", path),))


def test_asset_chunk_size_is_bounded() -> None:
    catalog = AssetCatalog()

    with pytest.raises(ProtocolError) as caught:
        catalog.read("unknown", 0, 1024 * 1024)

    assert caught.value.code is ProtocolErrorCode.INVALID_HANDLE
