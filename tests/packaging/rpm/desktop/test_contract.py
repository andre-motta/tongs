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
            "core_maximum_exclusive": "0.5.0",
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
