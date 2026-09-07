"""Deterministic builder for the clearly synthetic contract fixture.

This test helper does not build, sign, publish, or install a desktop product.
Every payload file is inert placeholder text retained beside the test.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
INPUT_ROOT = FIXTURE_ROOT / "synthetic-input"
ARCHIVE_NAME = "tongs-desktop-1.2.3-fedora44-x86_64.synthetic.tar.gz"
SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
LIMITS = {
    "max_entries": 64,
    "max_total_bytes": 1024 * 1024,
    "max_file_bytes": 512 * 1024,
    "max_path_bytes": 256,
}
PLATFORM = {
    "operating_system": "linux",
    "architecture": "x86_64",
    "distribution": "fedora",
    "distribution_version": "44",
    "abi": "gnu",
}
COMPATIBILITY = {
    "core_minimum": "1.2.0",
    "core_maximum_exclusive": "2.0.0",
    "rpc_api_major": 1,
    "plugin_api_major": 1,
}
FILE_INPUTS = {
    "runtime/LICENSES.json": "LICENSES.synthetic.json",
    "runtime/chrome-sandbox": "chrome-sandbox.placeholder",
    "runtime/libEGL.so": "libEGL.so.placeholder",
    "runtime/resources/app.asar": "app.asar.placeholder",
    "runtime/tongs-desktop": "tongs-desktop.placeholder",
    "runtime/version": "electron-version.txt",
}
EXECUTABLES = frozenset({"runtime/chrome-sandbox", "runtime/tongs-desktop"})


@dataclass(frozen=True, slots=True)
class ReferenceArtifact:
    archive: bytes
    install_manifest: bytes
    release_manifest: bytes


def build_reference_artifact() -> ReferenceArtifact:
    files = {
        path: (INPUT_ROOT / source).read_bytes() for path, source in FILE_INPUTS.items()
    }
    install = {
        "schema_version": 1,
        "release_version": "1.2.3",
        "package_kind": "user-archive",
        "ownership": "per-user",
        "platform": PLATFORM,
        "compatibility": COMPATIBILITY,
        "electron_version": "44.2.0",
        "launcher_path": "runtime/tongs-desktop",
        "extraction_limits": LIMITS,
        "files": [
            {
                "path": path,
                "byte_count": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "executable": path in EXECUTABLES,
            }
            for path, content in sorted(files.items())
        ],
    }
    install_document = canonical_json(install)
    archive = _build_tar_gzip(install_document, files)
    release = {
        "schema_version": 1,
        "release_version": "1.2.3",
        "source_commit": SOURCE_COMMIT,
        "compatibility": COMPATIBILITY,
        "artifacts": [
            {
                "artifact_id": "fedora-44-x86_64-user-archive",
                "name": ARCHIVE_NAME,
                "package_kind": "user-archive",
                "ownership": "per-user",
                "platform": PLATFORM,
                "byte_count": len(archive),
                "sha256": hashlib.sha256(archive).hexdigest(),
                "extraction_limits": LIMITS,
            }
        ],
    }
    return ReferenceArtifact(archive, install_document, canonical_json(release))


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def _build_tar_gzip(install_document: bytes, files: dict[str, bytes]) -> bytes:
    members: dict[str, tuple[bytes | None, int]] = {
        "desktop-install.json": (install_document, 0o644),
        "runtime": (None, 0o755),
        "runtime/resources": (None, 0o755),
    }
    members.update(
        {
            path: (content, 0o755 if path in EXECUTABLES else 0o644)
            for path, content in files.items()
        }
    )
    tar_bytes = io.BytesIO()
    with tarfile.open(
        fileobj=tar_bytes, mode="w", format=tarfile.PAX_FORMAT
    ) as archive:
        for path in sorted(members):
            content, mode = members[path]
            info = tarfile.TarInfo(path)
            info.mode = mode
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.pax_headers = {}
            if content is None:
                info.type = tarfile.DIRTYPE
                info.size = 0
                archive.addfile(info)
            else:
                info.type = tarfile.REGTYPE
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    output = io.BytesIO()
    with gzip.GzipFile(
        fileobj=output,
        mode="wb",
        filename="",
        compresslevel=9,
        mtime=0,
    ) as compressor:
        compressor.write(tar_bytes.getvalue())
    return output.getvalue()
