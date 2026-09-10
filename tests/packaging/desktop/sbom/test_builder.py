"""Focused tests for the source-bound desktop SPDX 2.3 generator."""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

ROOT = Path(__file__).parents[4]
SCHEMA = ROOT / "tests/packaging/desktop/sbom/schema/spdx-2.3.schema.json"
SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
SOURCE_TREE = "89abcdef0123456789abcdef0123456789abcdef"
SOURCE_EPOCH = 1_788_884_288
GENERATOR_VERSION = "1.0.0"


def _load_script(name: str, path: Path) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sbom = _load_script("build_desktop_sbom", ROOT / "scripts/build_desktop_sbom.py")
archive_builder = _load_script(
    "build_desktop_archive_for_sbom", ROOT / "scripts/build_desktop_archive.py"
)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def _digest(document: bytes) -> str:
    return hashlib.sha256(document).hexdigest()


def _identity(root: Path) -> object:
    archive = next((root / "archive").glob("*.tar.gz"))
    source_archive = root / "evidence/source.tar"
    electron_archive = root / "evidence/electron-v44.2.0-linux-x64.zip"
    return sbom.ExpectedIdentity(
        source_commit=SOURCE_COMMIT,
        source_tree=SOURCE_TREE,
        source_archive_sha256=_digest(source_archive.read_bytes()),
        source_date_epoch=SOURCE_EPOCH,
        archive_sha256=_digest(archive.read_bytes()),
        electron_archive_sha256=_digest(electron_archive.read_bytes()),
        generator_version=GENERATOR_VERSION,
    )


def _source_archive(documents: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path, document in sorted(documents.items()):
            member = tarfile.TarInfo(path)
            member.size = len(document)
            member.mode = 0o644
            member.mtime = SOURCE_EPOCH
            member.pax_headers = {"comment": SOURCE_COMMIT}
            archive.addfile(member, io.BytesIO(document))
    return output.getvalue()


def _source_documents(root: Path) -> dict[str, bytes]:
    documents: dict[str, bytes] = {}
    with tarfile.open(root / "evidence/source.tar", mode="r:") as archive:
        for member in archive:
            stream = archive.extractfile(member)
            assert stream is not None
            documents[member.name] = stream.read()
    return documents


def _fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "transfer"
    archive_root = root / "archive"
    evidence = root / "evidence"
    archive_root.mkdir(parents=True)
    evidence.mkdir()

    production = {
        "path": "node_modules/example",
        "version": "1.2.3",
        "resolved": "https://registry.npmjs.org/example/-/example-1.2.3.tgz",
        "integrity": "sha512-"
        + base64.b64encode(hashlib.sha512(b"registry tarball").digest()).decode(),
        "license": "MIT",
    }
    development = {
        "path": "node_modules/dev-only",
        "version": "9.0.0",
        "resolved": "https://registry.npmjs.org/dev-only/-/dev-only-9.0.0.tgz",
        "integrity": "sha512-"
        + base64.b64encode(hashlib.sha512(b"dev tarball").digest()).decode(),
        "license": "ISC",
        "dev": True,
    }
    package_lock_bytes = _canonical(
        {
            "lockfileVersion": 3,
            "name": "tongs-desktop-shell",
            "packages": {
                "": {"name": "tongs-desktop-shell", "version": "0.1.0"},
                production["path"]: {
                    key: value for key, value in production.items() if key != "path"
                },
                development["path"]: {
                    key: value for key, value in development.items() if key != "path"
                },
            },
            "requires": True,
            "version": "0.1.0",
        }
    )
    licenses = {
        "schema_version": 1,
        "components": [
            {
                "name": "Tongs",
                "version": "source",
                "license": "MIT",
                "license_paths": ["runtime/licenses/tongs/LICENSE"],
            },
            {
                "name": "Electron",
                "version": "44.2.0",
                "license": "MIT",
                "license_paths": ["runtime/LICENSE"],
            },
            {
                "name": "Chromium and Electron third-party components",
                "version": "bundled-with-electron-44.2.0",
                "license": "multiple",
                "license_paths": ["runtime/LICENSES.chromium.html"],
            },
            {
                "name": "example",
                "version": "1.2.3",
                "license": "MIT",
                "license_paths": ["runtime/licenses/npm/THIRD_PARTY_NOTICES.txt"],
                "lock_path": "node_modules/example",
                "source_license_files": ["LICENSE"],
            },
        ],
    }
    license_bytes = _canonical(licenses)
    payload = {
        "runtime/LICENSE": (b"electron license\n", 0o644),
        "runtime/LICENSES.chromium.html": (b"third-party licenses\n", 0o644),
        "runtime/LICENSES.json": (license_bytes, 0o644),
        "runtime/licenses/npm/THIRD_PARTY_NOTICES.txt": (b"npm notices\n", 0o644),
        "runtime/licenses/tongs/LICENSE": (b"tongs license\n", 0o644),
        "runtime/resources/app.asar": (b"synthetic app asar", 0o644),
        "runtime/tongs-desktop": (b"#!/bin/sh\n", 0o755),
    }
    parameters = archive_builder.BuildParameters(
        "0.5.0", "0.4.2-dev.183", "2.0.0", SOURCE_COMMIT, SOURCE_EPOCH
    )
    contract = json.loads(
        (ROOT / "packaging/desktop/archive/contract.json").read_bytes()
    )
    built = archive_builder.build_contract_documents(payload, parameters, contract)
    archive_path = archive_root / built.archive_name
    archive_path.write_bytes(built.archive)
    (archive_root / "desktop-install.json").write_bytes(built.install_manifest)
    (archive_root / "desktop-manifest-v1.json").write_bytes(built.release_manifest)
    runtime = {
        "schema_version": 1,
        "files": [
            {
                "path": path,
                "byte_count": len(content),
                "sha256": _digest(content),
                "mode": f"{mode:04o}",
            }
            for path, (content, mode) in sorted(payload.items())
        ],
    }
    electron_bytes = b"synthetic Electron ZIP"
    electron_name = "electron-v44.2.0-linux-x64.zip"
    electron_manifest_path = (
        "packaging/desktop/archive/electron-runtime-44.2.0-linux-x64.json"
    )
    electron_manifest_bytes = _canonical(
        {
            "schema_version": 1,
            "electron_version": "44.2.0",
            "platform": "linux-x64",
            "upstream_archive": {
                "name": electron_name,
                "sha256": _digest(electron_bytes),
            },
            "files": [],
        }
    )
    source_documents = {
        "LICENSE": b"tongs license\n",
        "desktop/package-lock.json": package_lock_bytes,
        electron_manifest_path: electron_manifest_bytes,
    }
    prepared = {
        "schema_version": 1,
        "source_commit": SOURCE_COMMIT,
        "files": [
            {
                "path": path,
                "byte_count": len(document),
                "sha256": _digest(document),
            }
            for path, document in sorted(source_documents.items())
        ],
        "npm_packages": sorted(
            [production, development], key=lambda item: item["path"]
        ),
    }
    asar = {
        "schema_version": 1,
        "asar_sha256": _digest(payload["runtime/resources/app.asar"][0]),
        "asar_byte_count": len(payload["runtime/resources/app.asar"][0]),
        "files": [{"path": "dist/app.js", "byte_count": 3, "sha256": "3" * 64}],
    }
    (evidence / electron_name).write_bytes(electron_bytes)
    (evidence / "source.tar").write_bytes(_source_archive(source_documents))
    provenance = {
        "schema_version": 1,
        "candidate": "UNPUBLISHED",
        "release_version": "0.5.0",
        "source_commit": SOURCE_COMMIT,
        "source_date_epoch": SOURCE_EPOCH,
        "electron_input": {
            "archive": {
                "name": electron_name,
                "byte_count": len(electron_bytes),
                "sha256": _digest(electron_bytes),
            },
            "inventory_sha256": _digest(electron_manifest_bytes),
            "upstream_archive": {
                "name": electron_name,
                "sha256": _digest(electron_bytes),
            },
        },
        "outputs": {
            "archive": {
                "name": built.archive_name,
                "byte_count": len(built.archive),
                "sha256": _digest(built.archive),
            }
        },
    }
    documents = {
        "app-asar-inventory.json": asar,
        "build-provenance.json": provenance,
        "license-inventory.json": licenses,
        "prepared-source-inventory.json": prepared,
        "runtime-inventory.json": runtime,
    }
    for name, document in documents.items():
        (archive_root / name).write_bytes(_canonical(document))
    _rebind(root)
    return root


def _rebind(root: Path) -> None:
    archive_root = root / "archive"
    output_names = sorted(
        path.name for path in archive_root.iterdir() if path.name != "SHA256SUMS"
    )
    checksums = "".join(
        f"{_digest((archive_root / name).read_bytes())}  {name}\n"
        for name in output_names
    )
    (archive_root / "SHA256SUMS").write_text(checksums)
    records = []
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        if path.name == "candidate-attestation-transfer-v1.json":
            continue
        document = path.read_bytes()
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _digest(document),
                "size": len(document),
            }
        )
    archive = next(archive_root.glob("*.tar.gz"))
    release = archive_root / "desktop-manifest-v1.json"
    transfer = {
        "schema_version": 1,
        "candidate": "UNPUBLISHED",
        "source": {"commit": SOURCE_COMMIT, "tree": SOURCE_TREE},
        "execution": {"fixture": "offline"},
        "files": records,
        "subjects": [
            {
                "name": release.name,
                "path": f"archive/{release.name}",
                "sha256": _digest(release.read_bytes()),
                "size": release.stat().st_size,
            },
            {
                "name": archive.name,
                "path": f"archive/{archive.name}",
                "sha256": _digest(archive.read_bytes()),
                "size": archive.stat().st_size,
            },
        ],
    }
    (root / "candidate-attestation-transfer-v1.json").write_bytes(_canonical(transfer))


def _mutate_json(
    root: Path, relative: str, mutate: Callable[[dict[str, object]], None]
) -> None:
    path = root / relative
    value = json.loads(path.read_bytes())
    mutate(value)
    path.write_bytes(_canonical(value))
    _rebind(root)


def test_build_is_deterministic_schema_valid_and_semantically_bounded(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    first = sbom.build_desktop_sbom(root, SCHEMA, _identity(root))
    second = sbom.build_desktop_sbom(root, SCHEMA, _identity(root))
    assert first == second
    document = json.loads(first)
    Draft7Validator(json.loads(SCHEMA.read_bytes())).validate(document)
    packages = document["packages"]
    assert len(packages) == 6
    assert not any(package["name"] == "dev-only" for package in packages)
    npm = next(package for package in packages if package["name"] == "example")
    assert npm["checksums"][0]["algorithm"] == "SHA512"
    assert "not installed ASAR bytes" in npm["comment"]
    chromium = next(
        package
        for package in packages
        if package["name"] == "Chromium and Electron third-party components"
    )
    assert chromium["licenseDeclared"] == "NOASSERTION"
    assert "checksums" not in chromium
    assert document["creationInfo"]["created"] == "2026-09-08T16:18:08Z"


@pytest.mark.parametrize(
    ("relative", "mutate", "message"),
    [
        (
            "archive/prepared-source-inventory.json",
            lambda value: value["npm_packages"][0].update(version="7.7.7"),
            "differs from the verified source package lock",
        ),
        (
            "archive/prepared-source-inventory.json",
            lambda value: value["npm_packages"][0].update(integrity="sha256-Zm9v"),
            "differs from the verified source package lock",
        ),
    ],
)
def test_changed_lock_or_license_closure_is_rejected(
    tmp_path: Path,
    relative: str,
    mutate: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    root = _fixture_root(tmp_path)
    _mutate_json(root, relative, mutate)
    with pytest.raises(sbom.SbomBuildError, match=message):
        sbom.build_desktop_sbom(root, SCHEMA, _identity(root))


def test_changed_license_manifest_bytes_are_rejected(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _mutate_json(
        root,
        "archive/license-inventory.json",
        lambda value: value["components"][-1].update(version="7.7.7"),
    )
    with pytest.raises(sbom.SbomBuildError, match="archived runtime inventory"):
        sbom.build_desktop_sbom(root, SCHEMA, _identity(root))


def test_fabricated_registry_identity_rebound_in_producer_metadata_is_rejected(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)

    def fabricate(value: dict[str, object]) -> None:
        package = value["npm_packages"][0]
        assert isinstance(package, dict)
        package["resolved"] = (
            "https://registry.npmjs.org/fabricated/-/fabricated-9.9.9.tgz"
        )
        package["integrity"] = (
            "sha512-"
            + base64.b64encode(
                hashlib.sha512(b"fabricated registry distribution").digest()
            ).decode()
        )

    _mutate_json(root, "archive/prepared-source-inventory.json", fabricate)
    with pytest.raises(sbom.SbomBuildError, match="verified source package lock"):
        sbom.build_desktop_sbom(root, SCHEMA, _identity(root))


def test_replaced_source_archive_with_rehashed_transfer_is_rejected(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    identity = _identity(root)
    (root / "evidence/source.tar").write_bytes(b"not the expected source archive")
    _rebind(root)
    with pytest.raises(sbom.SbomBuildError, match="independently expected checkout"):
        sbom.build_desktop_sbom(root, SCHEMA, identity)


def test_verified_source_archive_must_contain_the_authoritative_lock(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    documents = _source_documents(root)
    documents.pop("desktop/package-lock.json")
    (root / "evidence/source.tar").write_bytes(_source_archive(documents))
    _rebind(root)
    identity = replace(
        _identity(root),
        source_archive_sha256=_digest((root / "evidence/source.tar").read_bytes()),
    )
    with pytest.raises(sbom.SbomBuildError, match="lacks a required"):
        sbom.build_desktop_sbom(root, SCHEMA, identity)


def test_replaced_electron_archive_with_rebound_producer_hashes_is_rejected(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    identity = _identity(root)
    replacement = b"not the expected Electron ZIP"
    electron_path = root / "evidence/electron-v44.2.0-linux-x64.zip"
    electron_path.write_bytes(replacement)
    provenance_path = root / "archive/build-provenance.json"
    provenance = json.loads(provenance_path.read_bytes())
    provenance["electron_input"]["archive"].update(
        byte_count=len(replacement), sha256=_digest(replacement)
    )
    provenance_path.write_bytes(_canonical(provenance))
    _rebind(root)
    with pytest.raises(sbom.SbomBuildError, match="pinned source input"):
        sbom.build_desktop_sbom(root, SCHEMA, identity)


def test_rebound_producer_source_epoch_is_rejected(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _mutate_json(
        root,
        "archive/build-provenance.json",
        lambda value: value.update(source_date_epoch=SOURCE_EPOCH + 1),
    )
    with pytest.raises(sbom.SbomBuildError, match="source epoch is stale"):
        sbom.build_desktop_sbom(root, SCHEMA, _identity(root))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value["components"].append(
                copy.deepcopy(value["components"][-1])
            ),
            "duplicate",
        ),
        (lambda value: value["components"].pop(), "exactly match"),
        (
            lambda value: value["components"][-1].update(
                lock_path="node_modules/dev-only",
                name="dev-only",
                version="9.0.0",
                license="ISC",
            ),
            "development or missing",
        ),
    ],
)
def test_license_closure_rejects_duplicate_missing_or_development_components(
    tmp_path: Path,
    mutate: Callable[[dict[str, object]], None],
    message: str,
) -> None:
    root = _fixture_root(tmp_path)
    prepared = json.loads(
        (root / "archive/prepared-source-inventory.json").read_bytes()
    )
    licenses = json.loads((root / "archive/license-inventory.json").read_bytes())
    runtime_document = json.loads(
        (root / "archive/runtime-inventory.json").read_bytes()
    )
    runtime = {item["path"]: item for item in runtime_document["files"]}
    mutate(licenses)
    with pytest.raises(sbom.SbomBuildError, match=message):
        sbom._validate_license_and_lock_closure(
            prepared,
            tuple(prepared["npm_packages"]),
            licenses,
            runtime,
            "44.2.0",
        )


def test_stale_source_and_archive_identities_are_rejected(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    good = _identity(root)
    with pytest.raises(sbom.SbomBuildError, match="source identity is stale"):
        sbom.build_desktop_sbom(
            root,
            SCHEMA,
            replace(good, source_commit="f" * 40),
        )
    with pytest.raises(sbom.SbomBuildError, match="archive identity is stale"):
        sbom.build_desktop_sbom(
            root,
            SCHEMA,
            replace(good, archive_sha256="f" * 64),
        )


def test_unsafe_or_missing_transfer_input_is_rejected(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    transfer_path = root / "candidate-attestation-transfer-v1.json"
    transfer = json.loads(transfer_path.read_bytes())
    transfer["files"][0]["path"] = "../escape"
    transfer_path.write_bytes(_canonical(transfer))
    with pytest.raises(sbom.SbomBuildError, match="unsafe"):
        sbom.build_desktop_sbom(root, SCHEMA, _identity(root))

    root = _fixture_root(tmp_path / "missing")
    identity = _identity(root)
    (root / "evidence/source.tar").unlink()
    with pytest.raises(sbom.SbomBuildError, match="missing"):
        sbom.build_desktop_sbom(root, SCHEMA, identity)


def test_transfer_size_bounds_are_checked_before_file_hashing(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    transfer_path = root / "candidate-attestation-transfer-v1.json"
    transfer = json.loads(transfer_path.read_bytes())
    record = next(
        item
        for item in transfer["files"]
        if item["path"] == "archive/build-provenance.json"
    )
    record["size"] = sbom._MAX_JSON_BYTES + 1
    transfer_path.write_bytes(_canonical(transfer))
    with pytest.raises(sbom.SbomBuildError, match="exceeds its byte bound"):
        sbom.build_desktop_sbom(root, SCHEMA, _identity(root))


def test_mutated_schema_and_semantically_malformed_output_are_rejected(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    mutated_schema = tmp_path / "schema.json"
    mutated_schema.write_bytes(SCHEMA.read_bytes() + b" ")
    with pytest.raises(sbom.SbomBuildError, match="pinned official"):
        sbom.build_desktop_sbom(root, mutated_schema, _identity(root))

    producer = sbom._validate_producer_inputs(root.resolve(), _identity(root))
    namespace = sbom._document_namespace(_identity(root))
    document, expectations = sbom._build_document(
        _identity(root),
        producer,
        namespace=namespace,
        created=sbom._spdx_timestamp(producer.source_date_epoch),
    )
    document["packages"][0]["checksums"][0]["checksumValue"] = "f" * 64
    with pytest.raises(sbom.SbomBuildError, match="checksum"):
        sbom.validate_sbom_semantics(document, expectations)


def test_cli_refuses_to_overwrite_an_existing_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fixture_root(tmp_path)
    output = tmp_path / "existing.spdx.json"
    output.write_text("preserve\n")
    identity = _identity(root)
    result = sbom.main(
        [
            "--input-root",
            str(root),
            "--expected-source-commit",
            identity.source_commit,
            "--expected-source-tree",
            identity.source_tree,
            "--expected-source-archive-sha256",
            identity.source_archive_sha256,
            "--expected-source-date-epoch",
            str(identity.source_date_epoch),
            "--expected-archive-sha256",
            identity.archive_sha256,
            "--expected-electron-archive-sha256",
            identity.electron_archive_sha256,
            "--generator-version",
            identity.generator_version,
            "--schema",
            str(SCHEMA),
            "--output",
            str(output),
        ]
    )
    assert result == 1
    assert output.read_text() == "preserve\n"
    assert "already exists" in capsys.readouterr().err
