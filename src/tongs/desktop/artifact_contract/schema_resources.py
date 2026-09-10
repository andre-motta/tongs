"""Access the exact packaged JSON schema bytes used by later build stages."""

from __future__ import annotations

from importlib.resources import files

_SCHEMA_PACKAGE = "tongs.desktop.artifact_contract.schemas"
RELEASE_SCHEMA_NAME = "desktop-release-manifest-v1.schema.json"
INSTALL_SCHEMA_NAME = "desktop-install-manifest-v1.schema.json"


def release_schema_bytes() -> bytes:
    return files(_SCHEMA_PACKAGE).joinpath(RELEASE_SCHEMA_NAME).read_bytes()


def install_schema_bytes() -> bytes:
    return files(_SCHEMA_PACKAGE).joinpath(INSTALL_SCHEMA_NAME).read_bytes()
