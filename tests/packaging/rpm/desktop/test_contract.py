from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[4]
PACKAGING = ROOT / "packaging" / "rpm" / "desktop"


def _load_script(name: str) -> ModuleType:
    path = PACKAGING / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"tongs_desktop_rpm_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


contract = _load_script("package_contract")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metadata_fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    compatibility = {
        "core_minimum": "0.4.2-dev.183",
        "core_maximum_exclusive": "2.0.0",
        "plugin_api_major": 1,
        "rpc_api_major": 1,
    }
    install = {
        "platform": {
            "abi": "gnu",
            "architecture": "x86_64",
            "distribution": "fedora",
            "distribution_version": "44",
            "operating_system": "linux",
        },
        "release_version": "0.5.0",
        "electron_version": "44.2.0",
        "compatibility": compatibility,
        "files": [],
    }
    documents = {
        "desktop-install.json": install,
        "desktop-manifest-v1.json": {
            "source_commit": "a" * 40,
            "release_version": "0.5.0",
            "compatibility": compatibility,
        },
        "runtime-inventory.json": {"files": []},
        "build-provenance.json": {"source_date_epoch": 100},
    }
    for name, document in documents.items():
        (tmp_path / name).write_text(json.dumps(document))
    archive = tmp_path / "desktop.tar.gz"
    archive.write_bytes(b"validated separately")
    manifest: dict[str, object] = {
        "schema_version": 1,
        "target": {
            "architecture": "x86_64",
            "distribution": "fedora",
            "release": "44",
            "python_minimum": "3.12",
        },
        "accepted_desktop": {
            "source_commit": "a" * 40,
            "archive": {
                "filename": archive.name,
                "bytes": archive.stat().st_size,
                "sha256": _digest(archive),
            },
            "evidence": {name: _digest(tmp_path / name) for name in documents},
            "release_version": "0.5.0",
            "electron_version": "44.2.0",
            "compatibility": compatibility,
        },
    }
    return manifest, documents


def _archive_fixture(
    tmp_path: Path,
    members: list[tuple[str, bytes, int, int, int, bytes | None]],
) -> tuple[Path, dict[str, object]]:
    payload = b"accepted bytes"
    install = {
        "files": [
            {
                "path": "runtime/file",
                "byte_count": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "executable": False,
            }
        ],
        "extraction_limits": {
            "max_entries": 8,
            "max_file_bytes": 1024,
            "max_path_bytes": 128,
            "max_total_bytes": 4096,
        },
    }
    archive_path = tmp_path / "candidate.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, contents, mode, uid, mtime, member_type in members:
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            member.mode = mode
            member.uid = uid
            member.gid = 0
            member.uname = "root"
            member.gname = "root"
            member.mtime = mtime
            if member_type is not None:
                member.type = member_type
            archive.addfile(member, io.BytesIO(contents))
    return archive_path, install


def test_manifest_pins_accepted_issue_51_payload() -> None:
    manifest = json.loads((PACKAGING / "manifest.json").read_text())

    contract.validate_manifest(manifest)

    accepted = manifest["accepted_desktop"]
    assert accepted["source_commit"] == "825a4217c5b5e64fc8908c3444f1bd89fd469b2e"
    assert accepted["archive"]["sha256"] == (
        "4593c1585b46e5b48181815307c29b0380bc5f14714b2c12df464dd66d365900"
    )
    assert manifest["companion_binary_count"] == 7
    assert "mcp[cli]" in manifest["mcp_requirement"]


def test_development_version_is_derived_from_exact_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    responses = iter(
        [
            "1234567890abcdef1234567890abcdef12345678",
            "v0.4.1-250-g1234567",
            "1788869486",
            "20260908",
        ]
    )
    monkeypatch.setattr(contract, "_git", lambda _checkout, *_args: next(responses))

    identity = contract.source_identity(tmp_path)

    assert identity.pep440_version == "0.4.2.dev250+g1234567890"
    assert identity.rpm_version == "0.4.2~dev250"
    assert identity.rpm_release == "0.1.20260908git1234567"


def test_payload_validator_accepts_declared_regular_files(tmp_path: Path) -> None:
    payload = b"accepted bytes"
    source_date_epoch = 100
    install = {
        "schema_version": 1,
        "release_version": "0.5.0",
        "electron_version": "44.2.0",
        "compatibility": {
            "core_minimum": "0.4.2-dev.183",
            "core_maximum_exclusive": "2.0.0",
            "plugin_api_major": 1,
            "rpc_api_major": 1,
        },
        "platform": {
            "abi": "gnu",
            "architecture": "x86_64",
            "distribution": "fedora",
            "distribution_version": "44",
            "operating_system": "linux",
        },
        "extraction_limits": {
            "max_entries": 8,
            "max_file_bytes": 1024,
            "max_path_bytes": 128,
            "max_total_bytes": 4096,
        },
        "files": [
            {
                "path": "runtime/file",
                "byte_count": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "executable": False,
            }
        ],
    }
    install_bytes = (json.dumps(install) + "\n").encode()
    (tmp_path / "desktop-install.json").write_bytes(install_bytes)
    desktop_manifest = {
        "source_commit": "a" * 40,
        "release_version": "0.5.0",
        "compatibility": install["compatibility"],
    }
    (tmp_path / "desktop-manifest-v1.json").write_text(json.dumps(desktop_manifest))
    runtime_inventory = {
        "schema_version": 1,
        "files": [
            {
                "path": "runtime/file",
                "byte_count": len(payload),
                "mode": "0644",
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    (tmp_path / "runtime-inventory.json").write_text(json.dumps(runtime_inventory))
    (tmp_path / "build-provenance.json").write_text(
        json.dumps({"source_date_epoch": source_date_epoch})
    )
    archive_path = tmp_path / "desktop.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, contents, mode in (
            ("desktop-install.json", install_bytes, 0o644),
            ("runtime/file", payload, 0o644),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            member.mode = mode
            member.uid = 0
            member.gid = 0
            member.uname = "root"
            member.gname = "root"
            member.mtime = source_date_epoch
            archive.addfile(member, io.BytesIO(contents))
    manifest = {
        "schema_version": 1,
        "target": {
            "architecture": "x86_64",
            "distribution": "fedora",
            "release": "44",
            "python_minimum": "3.12",
        },
        "accepted_desktop": {
            "source_commit": "a" * 40,
            "archive": {
                "filename": archive_path.name,
                "bytes": archive_path.stat().st_size,
                "sha256": _digest(archive_path),
            },
            "evidence": {
                "desktop-install.json": _digest(tmp_path / "desktop-install.json"),
                "desktop-manifest-v1.json": _digest(
                    tmp_path / "desktop-manifest-v1.json"
                ),
                "runtime-inventory.json": _digest(tmp_path / "runtime-inventory.json"),
                "build-provenance.json": _digest(tmp_path / "build-provenance.json"),
            },
            "release_version": "0.5.0",
            "electron_version": "44.2.0",
            "compatibility": install["compatibility"],
        },
    }

    result = contract.validate_accepted_payload(manifest, tmp_path)

    assert result == install


def test_payload_validator_rejects_links(tmp_path: Path) -> None:
    archive_path = tmp_path / "bad.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        link = tarfile.TarInfo("runtime/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)

    install = {
        "files": [],
        "extraction_limits": {
            "max_entries": 8,
            "max_file_bytes": 1024,
            "max_path_bytes": 128,
            "max_total_bytes": 4096,
        },
    }

    with pytest.raises(RuntimeError, match="unsupported accepted archive member"):
        contract._validate_archive_members(archive_path, install)


@pytest.mark.parametrize(
    "filename", ("../payload", "dir/payload", "dir\\payload", "manifest.json")
)
def test_manifest_rejects_unsafe_or_reserved_source_names(filename: str) -> None:
    manifest = json.loads((PACKAGING / "manifest.json").read_text())
    manifest["accepted_desktop"]["archive"]["filename"] = filename

    with pytest.raises(ValueError, match="filename"):
        contract.validate_manifest(manifest)


def test_manifest_rejects_wrong_target_and_exact_pair() -> None:
    manifest = json.loads((PACKAGING / "manifest.json").read_text())
    manifest["target"]["release"] = "43"
    with pytest.raises(ValueError, match="target"):
        contract.validate_manifest(manifest)

    manifest = json.loads((PACKAGING / "manifest.json").read_text())
    manifest["rpm_pairing"] = {
        "mode": "exact",
        "core_source_commit": "b" * 40,
    }
    with pytest.raises(ValueError, match="pairing mismatch"):
        contract.validate_manifest(manifest)


def test_bind_rejects_mismatched_unreviewed_fixture() -> None:
    manifest = json.loads((PACKAGING / "manifest.json").read_text())
    manifest["accepted_desktop"].pop("reviewed_fixture")
    identity = contract.SourceIdentity(
        commit="b" * 40,
        pep440_version="0.4.2.dev250+gbbbbbbbbbb",
        rpm_version="0.4.2~dev250",
        rpm_release="0.1.20260908gitbbbbbbb",
        source_date_epoch=100,
    )

    with pytest.raises(RuntimeError, match="same source commit"):
        contract.bind_manifest(manifest, identity, allow_reviewed_fixture=True)


def test_input_identity_rejects_hash_and_size_mismatch(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_bytes(b"bytes")

    with pytest.raises(RuntimeError, match="input mismatch"):
        contract._verify_file(candidate, 6, hashlib.sha256(b"other").hexdigest())


@pytest.mark.parametrize(
    ("mutator", "message"),
    (
        (
            lambda documents: documents["desktop-manifest-v1.json"].__setitem__(
                "source_commit", "b" * 40
            ),
            "source commit mismatch",
        ),
        (
            lambda documents: documents["desktop-manifest-v1.json"].__setitem__(
                "compatibility", {"rpc_api_major": 999}
            ),
            "compatibility mismatch",
        ),
        (
            lambda documents: documents["desktop-install.json"]["platform"].__setitem__(
                "distribution", "other"
            ),
            "platform mismatch",
        ),
    ),
)
def test_payload_rejects_wrong_source_compatibility_or_platform(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutator: object,
    message: str,
) -> None:
    manifest, documents = _metadata_fixture(tmp_path)
    mutator(documents)  # type: ignore[operator]
    for name, document in documents.items():
        (tmp_path / name).write_text(json.dumps(document))
        manifest["accepted_desktop"]["evidence"][name] = _digest(  # type: ignore[index]
            tmp_path / name
        )
    monkeypatch.setattr(contract, "_validate_archive_members", lambda *_args: None)

    with pytest.raises(RuntimeError, match=message):
        contract.validate_accepted_payload(manifest, tmp_path)


@pytest.mark.parametrize(
    ("members", "message"),
    (
        ([], "lacks declared files"),
        (
            [
                ("runtime/file", b"accepted bytes", 0o644, 0, 100, None),
                ("runtime/file", b"accepted bytes", 0o644, 0, 100, None),
            ],
            "duplicate accepted archive member",
        ),
        (
            [
                ("runtime/file", b"accepted bytes", 0o644, 0, 100, None),
                ("runtime/extra", b"extra", 0o644, 0, 100, None),
            ],
            "undeclared accepted archive file",
        ),
        (
            [("runtime/file", b"accepted bytes", 0o644, 1, 100, None)],
            "non-root accepted archive owner",
        ),
        (
            [("runtime/file", b"accepted bytes", 0o644, 0, 101, None)],
            "timestamp mismatch",
        ),
        (
            [("runtime/file", b"accepted bytes", 0o755, 0, 100, None)],
            "archive file mismatch",
        ),
        (
            [("runtime/file", b"accepted bytes", 0o4644, 0, 100, None)],
            "special mode in accepted archive",
        ),
        (
            [("runtime/file", b"accepted bytes", 0o644, 0, 100, tarfile.FIFOTYPE)],
            "unsupported accepted archive member",
        ),
    ),
)
def test_archive_rejects_structural_and_metadata_mismatches(
    tmp_path: Path,
    members: list[tuple[str, bytes, int, int, int, bytes | None]],
    message: str,
) -> None:
    archive_path, install = _archive_fixture(tmp_path, members)

    with pytest.raises(RuntimeError, match=message):
        contract._validate_archive_members(
            archive_path,
            install,
            {
                "runtime/file": (
                    len(b"accepted bytes"),
                    "0644",
                    hashlib.sha256(b"accepted bytes").hexdigest(),
                )
            },
            100,
        )
