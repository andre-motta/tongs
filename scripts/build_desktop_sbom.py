"""Build a deterministic SPDX 2.3 SBOM for one verified desktop archive."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import stat
import sys
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final, NoReturn
from urllib.parse import quote, urlsplit

from jsonschema import Draft7Validator, SchemaError

from tongs.desktop.artifact_contract import (
    ArtifactContractError,
    parse_install_manifest,
    parse_release_manifest,
    validate_artifact_archive,
)

_SCHEMA_SOURCE_COMMIT: Final = "aadf3b0b8dbbabdb4d880b0fc714255fea436ff7"
_SCHEMA_SHA256: Final = (
    "239208b7ac287b3cf5d9a9af23f9d69863971102a5e1587a27a398b43490b89b"
)
_MAX_JSON_BYTES: Final = 4 * 1024 * 1024
_MAX_ARCHIVE_BYTES: Final = 1024 * 1024 * 1024
_MAX_SOURCE_ARCHIVE_BYTES: Final = 512 * 1024 * 1024
_MAX_SOURCE_MEMBERS: Final = 8192
_MAX_ELECTRON_ARCHIVE_BYTES: Final = 256 * 1024 * 1024
_MAX_TRANSFER_TOTAL_BYTES: Final = 2 * 1024 * 1024 * 1024
_MAX_TRANSFER_FILES: Final = 512
_MAX_PACKAGES: Final = 4096
_HASH_CHUNK_BYTES: Final = 1024 * 1024
_SHA1_RE: Final = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_GENERATOR_VERSION_RE: Final = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_SPDX_ID_RE: Final = re.compile(r"^SPDXRef-[A-Za-z0-9.-]+$")
_LICENSE_RE: Final = re.compile(
    r"^[A-Za-z0-9.+()-]+(?: (?:AND|OR|WITH) [A-Za-z0-9.+()-]+)*$"
)
_REQUIRED_ARCHIVE_OUTPUTS: Final = frozenset(
    {
        "app-asar-inventory.json",
        "build-provenance.json",
        "desktop-install.json",
        "desktop-manifest-v1.json",
        "license-inventory.json",
        "prepared-source-inventory.json",
        "runtime-inventory.json",
    }
)
_REQUIRED_TRANSFER_PATHS: Final = frozenset(
    {f"archive/{name}" for name in _REQUIRED_ARCHIVE_OUTPUTS}
    | {
        "archive/SHA256SUMS",
        "evidence/source.tar",
    }
)
_ARCHIVE_ID: Final = "SPDXRef-Package-Tongs-Desktop-Archive"
_APPLICATION_ID: Final = "SPDXRef-Package-Tongs-Desktop-Application"
_SOURCE_ID: Final = "SPDXRef-Package-Tongs-Source"
_ELECTRON_ID: Final = "SPDXRef-Package-Electron"
_CHROMIUM_ID: Final = "SPDXRef-Package-Electron-Bundled-Third-Party"


class SbomBuildError(ValueError):
    """Reject an incomplete, stale, or semantically misleading SBOM input."""


@dataclass(frozen=True, slots=True)
class ExpectedIdentity:
    """Consumer-owned immutable identity for one producer output."""

    source_commit: str
    source_tree: str
    source_archive_sha256: str
    source_date_epoch: int
    archive_sha256: str
    electron_archive_sha256: str
    generator_version: str

    def validate(self) -> None:
        """Reject malformed identity values before using them in SPDX fields."""
        if _SHA1_RE.fullmatch(self.source_commit) is None:
            _fail("expected source commit is invalid")
        if _SHA1_RE.fullmatch(self.source_tree) is None:
            _fail("expected source tree is invalid")
        if _SHA256_RE.fullmatch(self.source_archive_sha256) is None:
            _fail("expected source archive SHA-256 is invalid")
        if (
            type(self.source_date_epoch) is not int
            or not 0 <= self.source_date_epoch <= 0xFFFFFFFF
        ):
            _fail("expected source epoch is invalid")
        if _SHA256_RE.fullmatch(self.archive_sha256) is None:
            _fail("expected archive SHA-256 is invalid")
        if _SHA256_RE.fullmatch(self.electron_archive_sha256) is None:
            _fail("expected Electron archive SHA-256 is invalid")
        if _GENERATOR_VERSION_RE.fullmatch(self.generator_version) is None:
            _fail("generator version is invalid")


@dataclass(frozen=True, slots=True)
class SemanticExpectations:
    """Exact identities used by the independent SPDX semantic validator."""

    namespace: str
    created: str
    generator_version: str
    archive_name: str
    archive_sha256: str
    release_version: str
    source_commit: str
    source_tree: str
    source_archive_sha256: str
    electron_version: str
    electron_archive_name: str
    electron_archive_sha256: str
    asar_sha256: str
    npm_packages: tuple[tuple[str, str, str, str, str, str], ...]


@dataclass(frozen=True, slots=True)
class ProducerInputs:
    """Validated producer data needed to create and audit the SPDX document."""

    release_version: str
    source_date_epoch: int
    archive_name: str
    archive_sha256: str
    source_archive_sha256: str
    electron_version: str
    electron_archive_name: str
    electron_archive_sha256: str
    asar_sha256: str
    npm_packages: tuple[Mapping[str, object], ...]


def canonical_json(value: object) -> bytes:
    """Serialize stable UTF-8 JSON bytes."""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def build_desktop_sbom(
    input_root: Path,
    schema_path: Path,
    identity: ExpectedIdentity,
) -> bytes:
    """Validate one producer transfer and return deterministic SPDX 2.3 bytes."""
    identity.validate()
    root = _real_directory(input_root, "input root")
    schema_bytes = _read_regular_file_outside_root(
        schema_path, _MAX_JSON_BYTES, "SPDX schema"
    )
    if _sha256(schema_bytes) != _SCHEMA_SHA256:
        _fail("SPDX schema does not match the pinned official SPDX 2.3 schema")
    schema = _decode_json(schema_bytes, "SPDX schema")
    producer = _validate_producer_inputs(root, identity)
    namespace = _document_namespace(identity)
    created = _spdx_timestamp(producer.source_date_epoch)
    document, expectations = _build_document(
        identity, producer, namespace=namespace, created=created
    )
    _validate_schema(document, schema)
    validate_sbom_semantics(document, expectations)
    return canonical_json(document)


def validate_sbom_semantics(
    document: Mapping[str, object], expectations: SemanticExpectations
) -> None:
    """Independently validate identities, graph closure, and checksum meaning."""
    expected_top = {
        "SPDXID",
        "creationInfo",
        "dataLicense",
        "documentDescribes",
        "documentNamespace",
        "name",
        "packages",
        "relationships",
        "spdxVersion",
    }
    if set(document) != expected_top:
        _fail("SPDX document fields are incomplete or unexpected")
    if (
        document["SPDXID"] != "SPDXRef-DOCUMENT"
        or document["dataLicense"] != "CC0-1.0"
        or document["spdxVersion"] != "SPDX-2.3"
        or document["documentNamespace"] != expectations.namespace
        or document["documentDescribes"] != [_ARCHIVE_ID]
    ):
        _fail("SPDX document identity is invalid")
    creation = _object(document["creationInfo"], "SPDX creation info")
    if creation.get("created") != expectations.created:
        _fail("SPDX creation time is not derived from the source epoch")
    creators = creation.get("creators")
    if creators != [f"Tool: tongs-desktop-sbom-{expectations.generator_version}"]:
        _fail("SPDX creator identity is invalid")

    raw_packages = document["packages"]
    if (
        not isinstance(raw_packages, list)
        or not 5 <= len(raw_packages) <= _MAX_PACKAGES
    ):
        _fail("SPDX package inventory size is invalid")
    packages: dict[str, Mapping[str, object]] = {}
    package_keys: set[tuple[object, object, object]] = set()
    for raw in raw_packages:
        package = _object(raw, "SPDX package")
        identifier = _string(package, "SPDXID", "SPDX package")
        if _SPDX_ID_RE.fullmatch(identifier) is None or identifier in packages:
            _fail("SPDX package identifiers are invalid or duplicated")
        key = (
            package.get("name"),
            package.get("versionInfo"),
            package.get("downloadLocation"),
        )
        if key in package_keys:
            _fail("SPDX package identity is duplicated")
        packages[identifier] = package
        package_keys.add(key)

    expected_ids = {
        _ARCHIVE_ID,
        _APPLICATION_ID,
        _SOURCE_ID,
        _ELECTRON_ID,
        _CHROMIUM_ID,
        *(_npm_id(path) for path, *_ in expectations.npm_packages),
    }
    if set(packages) != expected_ids:
        _fail("SPDX package closure is incomplete or unexpected")
    _expect_package(
        packages[_ARCHIVE_ID],
        name="Tongs Desktop archive",
        version=expectations.release_version,
        file_name=expectations.archive_name,
        checksum=("SHA256", expectations.archive_sha256),
        purpose="ARCHIVE",
    )
    _expect_package(
        packages[_APPLICATION_ID],
        name="Tongs Desktop application",
        version=expectations.release_version,
        file_name="app.asar",
        checksum=("SHA256", expectations.asar_sha256),
        purpose="APPLICATION",
    )
    _expect_package(
        packages[_SOURCE_ID],
        name="Tongs source",
        version=expectations.source_commit,
        file_name="source.tar",
        checksum=("SHA256", expectations.source_archive_sha256),
        purpose="SOURCE",
    )
    source_info = _string(packages[_SOURCE_ID], "sourceInfo", "Tongs source package")
    if (
        expectations.source_commit not in source_info
        or expectations.source_tree not in source_info
    ):
        _fail("Tongs source package does not bind the expected commit and tree")
    _expect_package(
        packages[_ELECTRON_ID],
        name="Electron",
        version=expectations.electron_version,
        file_name=expectations.electron_archive_name,
        checksum=("SHA256", expectations.electron_archive_sha256),
        purpose="FRAMEWORK",
    )
    chromium = packages[_CHROMIUM_ID]
    if (
        chromium.get("name") != "Chromium and Electron third-party components"
        or chromium.get("licenseDeclared") != "NOASSERTION"
        or chromium.get("licenseConcluded") != "NOASSERTION"
        or chromium.get("filesAnalyzed") is not False
        or "checksums" in chromium
    ):
        _fail("bundled Chromium coverage is overstated or incomplete")
    chromium_comment = _string(chromium, "comment", "Chromium package")
    if "not a component-level decomposition" not in chromium_comment:
        _fail("bundled Chromium coverage limit is absent")

    for (
        path,
        name,
        version,
        license_id,
        resolved,
        integrity,
    ) in expectations.npm_packages:
        package = packages[_npm_id(path)]
        if (
            package.get("name") != name
            or package.get("versionInfo") != version
            or package.get("licenseDeclared") != license_id
            or package.get("licenseConcluded") != "NOASSERTION"
            or package.get("downloadLocation") != resolved
            or package.get("filesAnalyzed") is not False
        ):
            _fail("npm package metadata does not match the production lock")
        _expect_checksum(package, "SHA512", _sri_hex(integrity))
        comment = _string(package, "comment", "npm package")
        if (
            "registry distribution tarball" not in comment
            or "not installed ASAR bytes" not in comment
        ):
            _fail("npm package checksum semantics are ambiguous")

    relationships = document["relationships"]
    if not isinstance(relationships, list):
        _fail("SPDX relationships are invalid")
    observed: set[tuple[str, str, str]] = set()
    for raw in relationships:
        relationship = _object(raw, "SPDX relationship")
        left = _string(relationship, "spdxElementId", "SPDX relationship")
        right = _string(relationship, "relatedSpdxElement", "SPDX relationship")
        kind = _string(relationship, "relationshipType", "SPDX relationship")
        if left not in packages or right not in packages:
            _fail("SPDX relationship references a missing package")
        edge = (left, kind, right)
        if edge in observed:
            _fail("SPDX relationship is duplicated")
        observed.add(edge)
    expected_edges = {
        (_ARCHIVE_ID, "CONTAINS", _APPLICATION_ID),
        (_ARCHIVE_ID, "CONTAINS", _ELECTRON_ID),
        (_APPLICATION_ID, "GENERATED_FROM", _SOURCE_ID),
        (_ELECTRON_ID, "CONTAINS", _CHROMIUM_ID),
        *(
            (_APPLICATION_ID, "DEPENDS_ON", _npm_id(path))
            for path, *_ in expectations.npm_packages
        ),
    }
    if observed != expected_edges:
        _fail("SPDX relationship graph is incomplete or unexpected")


def _validate_producer_inputs(root: Path, identity: ExpectedIdentity) -> ProducerInputs:
    transfer = _load_root_json(root, "candidate-attestation-transfer-v1.json")
    _require_exact_keys(
        transfer,
        {"schema_version", "candidate", "source", "execution", "files", "subjects"},
        "transfer manifest",
    )
    if transfer["schema_version"] != 1 or transfer["candidate"] != "UNPUBLISHED":
        _fail("transfer manifest version or candidate marker is invalid")
    if transfer["source"] != {
        "commit": identity.source_commit,
        "tree": identity.source_tree,
    }:
        _fail("transfer manifest source identity is stale")
    transferred = _validate_transfer_file_records(root, transfer["files"])
    for relative, record in sorted(transferred.items()):
        _verify_transfer_identity(root, relative, record)

    archive_root = _real_directory(root / "archive", "archive input directory")
    checksums = _validate_producer_checksums(archive_root)
    archive_names = set(checksums) - _REQUIRED_ARCHIVE_OUTPUTS
    if len(archive_names) != 1:
        _fail("producer checksums do not identify exactly one desktop archive")
    archive_name = archive_names.pop()
    archive_relative = f"archive/{archive_name}"
    required_transfer = _REQUIRED_TRANSFER_PATHS | {
        archive_relative,
        "evidence/electron-v44.2.0-linux-x64.zip",
    }
    if not required_transfer <= transferred.keys():
        _fail("transfer manifest is missing an SBOM input")
    release_bytes = _read_root_file(
        root, "archive/desktop-manifest-v1.json", _MAX_JSON_BYTES
    )
    install_bytes = _read_root_file(
        root, "archive/desktop-install.json", _MAX_JSON_BYTES
    )
    provenance = _load_root_json(root, "archive/build-provenance.json")
    prepared = _load_root_json(root, "archive/prepared-source-inventory.json")
    runtime = _load_root_json(root, "archive/runtime-inventory.json")
    asar = _load_root_json(root, "archive/app-asar-inventory.json")
    licenses = _load_root_json(root, "archive/license-inventory.json")

    try:
        release = parse_release_manifest(release_bytes)
        external_install = parse_install_manifest(install_bytes)
    except ArtifactContractError as error:
        raise SbomBuildError("desktop artifact manifest is invalid") from error
    if release.source_commit != identity.source_commit or len(release.artifacts) != 1:
        _fail("release manifest is not bound to the expected source")
    artifact = release.artifacts[0]
    if artifact.name != archive_name or artifact.sha256 != identity.archive_sha256:
        _fail("release manifest archive identity is stale")
    if checksums[archive_name] != identity.archive_sha256:
        _fail("producer checksum archive identity is stale")
    _validate_transfer_subjects(
        transfer["subjects"], root, archive_name, identity.archive_sha256
    )
    archive_path = _root_file(root, archive_relative)
    if archive_path.stat().st_size != artifact.byte_count:
        _fail("desktop archive byte count changed")
    if _stream_sha256(archive_path) != identity.archive_sha256:
        _fail("desktop archive bytes changed")
    if artifact.byte_count > _MAX_ARCHIVE_BYTES:
        _fail("desktop archive exceeds the SBOM validation memory bound")
    archive_bytes = _read_root_file(root, archive_relative, _MAX_ARCHIVE_BYTES)
    try:
        validated = validate_artifact_archive(
            archive_bytes, artifact.name, release, artifact.artifact_id
        )
    except ArtifactContractError as error:
        raise SbomBuildError("desktop archive contract is invalid") from error
    if validated.install != external_install:
        _fail("external install manifest differs from the archive manifest")

    _validate_provenance(provenance, identity, artifact.name, artifact.byte_count)
    source_files = _validate_source_inventory(prepared, identity.source_commit)
    source_archive = _root_file(root, "evidence/source.tar")
    if source_archive.stat().st_size > _MAX_SOURCE_ARCHIVE_BYTES:
        _fail("source archive exceeds its byte bound")
    if _stream_sha256(source_archive) != identity.source_archive_sha256:
        _fail("source archive does not match the independently expected checkout")
    source_documents = _read_verified_source_documents(
        source_archive,
        external_install.electron_version,
        identity.source_date_epoch,
    )
    _validate_prepared_source_documents(source_files, source_documents)
    source_lock = _npm_inventory_from_lock(
        source_documents["desktop/package-lock.json"]
    )
    runtime_files = _validate_runtime_inventory(runtime, external_install)
    _validate_archived_license_inventory(root, runtime_files)
    asar_sha256 = _validate_asar_inventory(asar, runtime_files)
    npm_packages = _validate_license_and_lock_closure(
        prepared,
        source_lock,
        licenses,
        runtime_files,
        external_install.electron_version,
    )

    electron_config_path = (
        "packaging/desktop/archive/electron-runtime-"
        f"{external_install.electron_version}-linux-x64.json"
    )
    configured_electron = _validate_source_electron_manifest(
        source_documents[electron_config_path],
        external_install.electron_version,
        identity.electron_archive_sha256,
    )
    electron_input = _object(provenance["electron_input"], "provenance electron input")
    _require_exact_keys(
        electron_input,
        {"archive", "inventory_sha256", "upstream_archive"},
        "provenance Electron input",
    )
    if electron_input["upstream_archive"] != configured_electron:
        _fail("provenance Electron upstream identity is stale")
    if electron_input["inventory_sha256"] != _sha256(
        source_documents[electron_config_path]
    ):
        _fail("provenance Electron inventory identity is stale")
    electron = _object(
        electron_input["archive"],
        "provenance Electron archive",
    )
    _require_exact_keys(
        electron, {"name", "sha256", "byte_count"}, "provenance Electron archive"
    )
    electron_archive_name = _string(electron, "name", "provenance Electron archive")
    electron_archive_sha256 = _digest(
        electron.get("sha256"), "provenance Electron archive SHA-256"
    )
    if {
        "name": electron_archive_name,
        "sha256": electron_archive_sha256,
    } != configured_electron:
        _fail("provenance Electron archive differs from the pinned source input")
    electron_path = f"evidence/{electron_archive_name}"
    if electron_path not in transferred:
        _fail("transfer manifest is missing the declared Electron archive")
    _verify_transfer_identity(root, electron_path, transferred[electron_path])
    electron_file = _root_file(root, electron_path)
    if electron_file.stat().st_size != electron["byte_count"]:
        _fail("Electron distribution archive byte count changed")
    if _stream_sha256(electron_file) != identity.electron_archive_sha256:
        _fail("Electron distribution archive bytes changed")

    return ProducerInputs(
        release_version=release.release_version,
        source_date_epoch=identity.source_date_epoch,
        archive_name=archive_name,
        archive_sha256=identity.archive_sha256,
        source_archive_sha256=identity.source_archive_sha256,
        electron_version=external_install.electron_version,
        electron_archive_name=electron_archive_name,
        electron_archive_sha256=identity.electron_archive_sha256,
        asar_sha256=asar_sha256,
        npm_packages=npm_packages,
    )


def _build_document(
    identity: ExpectedIdentity,
    producer: ProducerInputs,
    *,
    namespace: str,
    created: str,
) -> tuple[dict[str, object], SemanticExpectations]:
    npm_expectations: list[tuple[str, str, str, str, str, str]] = []
    npm_packages: list[dict[str, object]] = []
    for raw in producer.npm_packages:
        path = _string(raw, "path", "npm package")
        name = _string(raw, "name", "npm package")
        version = _string(raw, "version", "npm package")
        license_id = _string(raw, "license", "npm package")
        resolved = _string(raw, "resolved", "npm package")
        integrity = _string(raw, "integrity", "npm package")
        npm_expectations.append((path, name, version, license_id, resolved, integrity))
        npm_packages.append(
            {
                "SPDXID": _npm_id(path),
                "comment": (
                    "The SHA-512 checksum identifies the registry distribution tarball "
                    "at downloadLocation. It is not installed ASAR bytes, and this SBOM "
                    "does not assert per-package installed-file attribution inside ASAR."
                ),
                "checksums": [
                    {"algorithm": "SHA512", "checksumValue": _sri_hex(integrity)}
                ],
                "downloadLocation": resolved,
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceLocator": f"pkg:npm/{quote(name, safe='/')}@{version}",
                        "referenceType": "purl",
                    }
                ],
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": license_id,
                "name": name,
                "primaryPackagePurpose": "LIBRARY",
                "versionInfo": version,
            }
        )

    packages: list[dict[str, object]] = [
        {
            "SPDXID": _ARCHIVE_ID,
            "checksums": [
                {"algorithm": "SHA256", "checksumValue": producer.archive_sha256}
            ],
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "name": "Tongs Desktop archive",
            "packageFileName": producer.archive_name,
            "primaryPackagePurpose": "ARCHIVE",
            "versionInfo": producer.release_version,
        },
        {
            "SPDXID": _APPLICATION_ID,
            "checksums": [
                {"algorithm": "SHA256", "checksumValue": producer.asar_sha256}
            ],
            "comment": (
                "The checksum identifies runtime/resources/app.asar in the verified "
                "desktop archive. npm packages are related as the lock-derived "
                "production dependency closure without per-package ASAR attribution."
            ),
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "MIT",
            "name": "Tongs Desktop application",
            "packageFileName": "app.asar",
            "primaryPackagePurpose": "APPLICATION",
            "versionInfo": producer.release_version,
        },
        {
            "SPDXID": _SOURCE_ID,
            "checksums": [
                {
                    "algorithm": "SHA256",
                    "checksumValue": producer.source_archive_sha256,
                }
            ],
            "downloadLocation": (
                "https://github.com/andre-motta/tongs/tree/" + identity.source_commit
            ),
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceLocator": (
                        "pkg:github/andre-motta/tongs@" + identity.source_commit
                    ),
                    "referenceType": "purl",
                }
            ],
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "MIT",
            "name": "Tongs source",
            "packageFileName": "source.tar",
            "primaryPackagePurpose": "SOURCE",
            "sourceInfo": (
                f"Source archive prepared from Git commit {identity.source_commit}; "
                f"Git tree {identity.source_tree}."
            ),
            "versionInfo": identity.source_commit,
        },
        {
            "SPDXID": _ELECTRON_ID,
            "checksums": [
                {
                    "algorithm": "SHA256",
                    "checksumValue": producer.electron_archive_sha256,
                }
            ],
            "comment": (
                "The checksum identifies the exact upstream Electron distribution ZIP "
                "supplied to the producer. Actual bundled runtime bytes are covered by "
                "the desktop archive checksum and its validated runtime inventory."
            ),
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "MIT",
            "name": "Electron",
            "packageFileName": producer.electron_archive_name,
            "primaryPackagePurpose": "FRAMEWORK",
            "versionInfo": producer.electron_version,
        },
        {
            "SPDXID": _CHROMIUM_ID,
            "comment": (
                "This entry represents the retained Electron and Chromium third-party "
                "license bundle. It is not a component-level decomposition and does "
                "not assert unavailable individual Chromium versions, licenses, or "
                "checksums."
            ),
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseComments": (
                "The producer records the aggregate license value 'multiple' and "
                "runtime/LICENSES.chromium.html; no SPDX expression is inferred."
            ),
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "name": "Chromium and Electron third-party components",
            "primaryPackagePurpose": "OTHER",
            "versionInfo": f"bundled-with-electron-{producer.electron_version}",
        },
        *npm_packages,
    ]
    packages.sort(key=lambda item: str(item["SPDXID"]))
    relationships: list[dict[str, str]] = [
        {
            "spdxElementId": _ARCHIVE_ID,
            "relatedSpdxElement": _APPLICATION_ID,
            "relationshipType": "CONTAINS",
        },
        {
            "spdxElementId": _ARCHIVE_ID,
            "relatedSpdxElement": _ELECTRON_ID,
            "relationshipType": "CONTAINS",
        },
        {
            "spdxElementId": _APPLICATION_ID,
            "relatedSpdxElement": _SOURCE_ID,
            "relationshipType": "GENERATED_FROM",
        },
        {
            "spdxElementId": _ELECTRON_ID,
            "relatedSpdxElement": _CHROMIUM_ID,
            "relationshipType": "CONTAINS",
        },
        *(
            {
                "spdxElementId": _APPLICATION_ID,
                "relatedSpdxElement": _npm_id(path),
                "relationshipType": "DEPENDS_ON",
            }
            for path, *_ in npm_expectations
        ),
    ]
    relationships.sort(
        key=lambda item: (
            item["spdxElementId"],
            item["relationshipType"],
            item["relatedSpdxElement"],
        )
    )
    document: dict[str, object] = {
        "SPDXID": "SPDXRef-DOCUMENT",
        "creationInfo": {
            "created": created,
            "creators": [f"Tool: tongs-desktop-sbom-{identity.generator_version}"],
        },
        "dataLicense": "CC0-1.0",
        "documentDescribes": [_ARCHIVE_ID],
        "documentNamespace": namespace,
        "name": f"Tongs Desktop {producer.release_version} archive SBOM",
        "packages": packages,
        "relationships": relationships,
        "spdxVersion": "SPDX-2.3",
    }
    expectations = SemanticExpectations(
        namespace=namespace,
        created=created,
        generator_version=identity.generator_version,
        archive_name=producer.archive_name,
        archive_sha256=producer.archive_sha256,
        release_version=producer.release_version,
        source_commit=identity.source_commit,
        source_tree=identity.source_tree,
        source_archive_sha256=producer.source_archive_sha256,
        electron_version=producer.electron_version,
        electron_archive_name=producer.electron_archive_name,
        electron_archive_sha256=producer.electron_archive_sha256,
        asar_sha256=producer.asar_sha256,
        npm_packages=tuple(npm_expectations),
    )
    return document, expectations


def _validate_transfer_file_records(
    root: Path, raw_records: object
) -> dict[str, Mapping[str, object]]:
    if (
        not isinstance(raw_records, list)
        or not 1 <= len(raw_records) <= _MAX_TRANSFER_FILES
    ):
        _fail("transfer file inventory size is invalid")
    records: dict[str, Mapping[str, object]] = {}
    total_size = 0
    for raw in raw_records:
        record = _object(raw, "transfer file")
        _require_exact_keys(record, {"path", "sha256", "size"}, "transfer file")
        path = _string(record, "path", "transfer file")
        _validate_relative_path(path)
        if path in records:
            _fail("transfer file inventory contains a duplicate path")
        _digest(record["sha256"], "transfer file SHA-256")
        size = record["size"]
        if type(size) is not int or size < 1:
            _fail("transfer file size is invalid")
        maximum = _transfer_file_maximum(path)
        if size > maximum:
            _fail(f"transfer file exceeds its byte bound: {path}")
        total_size += size
        if total_size > _MAX_TRANSFER_TOTAL_BYTES:
            _fail("transfer file inventory exceeds its aggregate byte bound")
        records[path] = record
    return records


def _transfer_file_maximum(path: str) -> int:
    if path == "evidence/source.tar":
        return _MAX_SOURCE_ARCHIVE_BYTES
    if path.startswith("evidence/electron-v") and path.endswith("-linux-x64.zip"):
        return _MAX_ELECTRON_ARCHIVE_BYTES
    if path.startswith("evidence/"):
        return _MAX_JSON_BYTES
    if path.startswith("archive/"):
        name = path.removeprefix("archive/")
        if name.endswith(".tar.gz"):
            return _MAX_ARCHIVE_BYTES
        if name == "SHA256SUMS" or name in _REQUIRED_ARCHIVE_OUTPUTS:
            return _MAX_JSON_BYTES
    _fail(f"transfer file path is outside the supported producer layout: {path}")


def _verify_transfer_identity(
    root: Path, relative: str, record: Mapping[str, object]
) -> None:
    path = _root_file(root, relative)
    size = record["size"]
    if path.stat().st_size != size or _stream_sha256(path) != record["sha256"]:
        _fail(f"transfer file identity changed: {relative}")


def _validate_transfer_subjects(
    raw_subjects: object,
    root: Path,
    archive_name: str,
    archive_sha256: str,
) -> None:
    release_path = "archive/desktop-manifest-v1.json"
    expected = {
        release_path: {
            "name": "desktop-manifest-v1.json",
            "path": release_path,
            "size": _root_file(root, release_path).stat().st_size,
            "sha256": _stream_sha256(_root_file(root, release_path)),
        },
        f"archive/{archive_name}": {
            "name": archive_name,
            "path": f"archive/{archive_name}",
            "size": _root_file(root, f"archive/{archive_name}").stat().st_size,
            "sha256": archive_sha256,
        },
    }
    if not isinstance(raw_subjects, list) or len(raw_subjects) != 2:
        _fail("transfer manifest subjects are invalid")
    observed: dict[str, Mapping[str, object]] = {}
    for raw in raw_subjects:
        subject = _object(raw, "transfer subject")
        _require_exact_keys(
            subject, {"name", "path", "size", "sha256"}, "transfer subject"
        )
        path = _string(subject, "path", "transfer subject")
        if path in observed:
            _fail("transfer manifest subjects contain a duplicate path")
        observed[path] = subject
    if observed != expected:
        _fail("transfer manifest subjects do not match the producer output")


def _validate_producer_checksums(root: Path) -> dict[str, str]:
    document = _read_regular_file_outside_root(
        root / "SHA256SUMS", _MAX_JSON_BYTES, "producer SHA256SUMS"
    )
    try:
        lines = document.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise SbomBuildError("producer SHA256SUMS is not ASCII") from error
    checksums: dict[str, str] = {}
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/]+)", line)
        if match is None or match.group(2) in checksums:
            _fail("producer SHA256SUMS is invalid")
        checksums[match.group(2)] = match.group(1)
    if not _REQUIRED_ARCHIVE_OUTPUTS < set(checksums):
        _fail("producer SHA256SUMS path set is incomplete")
    actual_names = {
        path.name
        for path in root.iterdir()
        if path.name != "SHA256SUMS" and path.is_file() and not path.is_symlink()
    }
    if set(checksums) != actual_names:
        _fail("producer SHA256SUMS does not cover the exact archive output set")
    for name, expected in checksums.items():
        if _stream_sha256(_root_file(root.parent, f"archive/{name}")) != expected:
            _fail(f"producer output checksum changed: {name}")
    return checksums


def _validate_provenance(
    provenance: Mapping[str, object],
    identity: ExpectedIdentity,
    archive_name: str,
    archive_size: int,
) -> None:
    if (
        provenance.get("schema_version") != 1
        or provenance.get("candidate") != "UNPUBLISHED"
        or provenance.get("source_commit") != identity.source_commit
    ):
        _fail("build provenance is not source-bound")
    if provenance.get("source_date_epoch") != identity.source_date_epoch:
        _fail("build provenance source epoch is stale")
    output = _object(
        _object(provenance.get("outputs"), "build provenance outputs").get("archive"),
        "build provenance archive",
    )
    if output != {
        "name": archive_name,
        "sha256": identity.archive_sha256,
        "byte_count": archive_size,
    }:
        _fail("build provenance archive identity is stale")


def _validate_source_inventory(
    prepared: Mapping[str, object], source_commit: str
) -> dict[str, Mapping[str, object]]:
    if (
        prepared.get("schema_version") != 1
        or prepared.get("source_commit") != source_commit
    ):
        _fail("prepared source inventory is not source-bound")
    files = prepared.get("files")
    if not isinstance(files, list) or not files:
        _fail("prepared source file inventory is empty")
    observed: dict[str, Mapping[str, object]] = {}
    for raw in files:
        item = _object(raw, "prepared source file")
        _require_exact_keys(
            item, {"path", "sha256", "byte_count"}, "prepared source file"
        )
        path = _string(item, "path", "prepared source file")
        _validate_relative_path(path)
        if path in observed:
            _fail("prepared source inventory has a duplicate path")
        observed[path] = item
        _digest(item["sha256"], "prepared source file SHA-256")
        if type(item["byte_count"]) is not int or item["byte_count"] < 0:
            _fail("prepared source file byte count is invalid")
    if "desktop/package-lock.json" not in observed or "LICENSE" not in observed:
        _fail("prepared source inventory lacks required source inputs")
    return observed


def _read_verified_source_documents(
    source_archive: Path,
    electron_version: str,
    source_date_epoch: int,
) -> dict[str, bytes]:
    """Read only the bounded source files needed for independent SBOM semantics."""
    electron_path = (
        f"packaging/desktop/archive/electron-runtime-{electron_version}-linux-x64.json"
    )
    required = {"LICENSE", "desktop/package-lock.json", electron_path}
    observed: dict[str, bytes] = {}
    try:
        with tarfile.open(source_archive, mode="r:") as archive:
            for count, member in enumerate(archive, start=1):
                if count > _MAX_SOURCE_MEMBERS:
                    _fail("source archive exceeds its member bound")
                if member.name not in required:
                    continue
                if member.name in observed:
                    _fail("source archive contains a duplicate required member")
                if (
                    not member.isfile()
                    or member.size < 1
                    or member.size > _MAX_JSON_BYTES
                    or member.mtime != source_date_epoch
                ):
                    _fail("source archive required member is invalid")
                stream = archive.extractfile(member)
                if stream is None:
                    _fail("source archive required member cannot be read")
                document = stream.read(member.size + 1)
                if len(document) != member.size:
                    _fail("source archive required member size changed")
                observed[member.name] = document
    except (OSError, tarfile.TarError) as error:
        raise SbomBuildError(
            "source archive is not a supported Git tar archive"
        ) from error
    if set(observed) != required:
        _fail("source archive lacks a required SBOM source input")
    return observed


def _validate_prepared_source_documents(
    source_files: Mapping[str, Mapping[str, object]],
    documents: Mapping[str, bytes],
) -> None:
    for path, document in documents.items():
        prepared = source_files.get(path)
        if prepared is None or prepared != {
            "path": path,
            "sha256": _sha256(document),
            "byte_count": len(document),
        }:
            _fail(f"prepared source inventory differs from source archive: {path}")


def _npm_inventory_from_lock(document: bytes) -> tuple[Mapping[str, object], ...]:
    lock = _decode_json(document, "source package lock")
    packages = lock.get("packages")
    if not isinstance(packages, dict) or not 1 <= len(packages) <= _MAX_PACKAGES:
        _fail("source package lock inventory is invalid or exceeds its bound")
    values: list[Mapping[str, object]] = []
    for path, raw in sorted(packages.items()):
        if not path:
            continue
        if not isinstance(path, str):
            _fail("source package lock path is invalid")
        item = _object(raw, "source npm package")
        values.append(
            {
                key: item[key]
                for key in ("version", "resolved", "integrity", "license", "dev")
                if key in item
            }
            | {"path": path}
        )
    if not values:
        _fail("source package lock inventory is empty")
    return tuple(values)


def _validate_source_electron_manifest(
    document: bytes, electron_version: str, electron_sha256: str
) -> Mapping[str, object]:
    manifest = _decode_json(document, "source Electron manifest")
    _require_exact_keys(
        manifest,
        {"schema_version", "electron_version", "platform", "upstream_archive", "files"},
        "source Electron manifest",
    )
    if (
        manifest["schema_version"] != 1
        or manifest["electron_version"] != electron_version
        or manifest["platform"] != "linux-x64"
    ):
        _fail("source Electron manifest identity is stale")
    upstream = _object(manifest["upstream_archive"], "source Electron upstream archive")
    _require_exact_keys(
        upstream, {"name", "sha256"}, "source Electron upstream archive"
    )
    name = _string(upstream, "name", "source Electron upstream archive")
    if name != f"electron-v{electron_version}-linux-x64.zip":
        _fail("source Electron upstream archive name is invalid")
    if (
        _digest(upstream.get("sha256"), "source Electron upstream archive SHA-256")
        != electron_sha256
    ):
        _fail(
            "source Electron archive does not match the independently expected digest"
        )
    return upstream


def _validate_runtime_inventory(
    runtime: Mapping[str, object], install: object
) -> dict[str, Mapping[str, object]]:
    if runtime.get("schema_version") != 1:
        _fail("runtime inventory version is unsupported")
    raw_files = runtime.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        _fail("runtime inventory is empty")
    files: dict[str, Mapping[str, object]] = {}
    for raw in raw_files:
        item = _object(raw, "runtime file")
        _require_exact_keys(
            item, {"path", "sha256", "byte_count", "mode"}, "runtime file"
        )
        path = _string(item, "path", "runtime file")
        _validate_relative_path(path)
        if path in files:
            _fail("runtime inventory contains a duplicate path")
        _digest(item["sha256"], "runtime file SHA-256")
        if type(item["byte_count"]) is not int or item["byte_count"] < 0:
            _fail("runtime file byte count is invalid")
        if item["mode"] not in {"0644", "0755"}:
            _fail("runtime file mode is invalid")
        files[path] = item
    declarations = {
        item.path: (item.sha256, item.byte_count, "0755" if item.executable else "0644")
        for item in install.files
    }
    observed = {
        path: (item["sha256"], item["byte_count"], item["mode"])
        for path, item in files.items()
    }
    if observed != declarations:
        _fail("runtime inventory disagrees with the install manifest")
    return files


def _validate_asar_inventory(
    asar: Mapping[str, object], runtime: Mapping[str, Mapping[str, object]]
) -> str:
    if asar.get("schema_version") != 1:
        _fail("ASAR inventory version is unsupported")
    digest = _digest(asar.get("asar_sha256"), "ASAR SHA-256")
    size = asar.get("asar_byte_count")
    if type(size) is not int or size < 1:
        _fail("ASAR byte count is invalid")
    entry = runtime.get("runtime/resources/app.asar")
    if entry is None or entry["sha256"] != digest or entry["byte_count"] != size:
        _fail("ASAR inventory disagrees with the installed runtime bytes")
    files = asar.get("files")
    if not isinstance(files, list) or not files:
        _fail("ASAR source inventory is empty")
    observed: set[str] = set()
    for raw in files:
        item = _object(raw, "ASAR source file")
        _require_exact_keys(item, {"path", "sha256", "byte_count"}, "ASAR source file")
        path = _string(item, "path", "ASAR source file")
        _validate_relative_path(path)
        if path in observed:
            _fail("ASAR source inventory contains a duplicate path")
        observed.add(path)
        _digest(item["sha256"], "ASAR source file SHA-256")
        if type(item["byte_count"]) is not int or item["byte_count"] < 0:
            _fail("ASAR source file byte count is invalid")
    return digest


def _validate_archived_license_inventory(
    root: Path, runtime: Mapping[str, Mapping[str, object]]
) -> None:
    entry = runtime.get("runtime/LICENSES.json")
    if entry is None:
        _fail("runtime inventory is missing the archived license inventory")
    path = _root_file(root, "archive/license-inventory.json")
    if (
        path.stat().st_size != entry["byte_count"]
        or _stream_sha256(path) != entry["sha256"]
    ):
        _fail("license inventory bytes differ from the archived runtime inventory")


def _validate_license_and_lock_closure(
    prepared: Mapping[str, object],
    source_lock: tuple[Mapping[str, object], ...],
    licenses: Mapping[str, object],
    runtime: Mapping[str, Mapping[str, object]],
    electron_version: str,
) -> tuple[Mapping[str, object], ...]:
    if licenses.get("schema_version") != 1:
        _fail("license inventory version is unsupported")
    raw_packages = prepared.get("npm_packages")
    raw_components = licenses.get("components")
    if not isinstance(raw_packages, list) or not isinstance(raw_components, list):
        _fail("npm or license inventory is invalid")
    if raw_packages != list(source_lock):
        _fail("prepared npm inventory differs from the verified source package lock")
    if (
        not 1 <= len(raw_packages) <= _MAX_PACKAGES
        or not 3 <= len(raw_components) <= _MAX_PACKAGES
    ):
        _fail("npm or license inventory exceeds its bound")

    lock: dict[str, Mapping[str, object]] = {}
    production_paths: set[str] = set()
    for raw in raw_packages:
        item = _object(raw, "npm lock package")
        allowed = {"path", "version", "resolved", "integrity", "license", "dev"}
        if not set(item) <= allowed:
            _fail("npm lock package fields are unexpected")
        path = _string(item, "path", "npm lock package")
        _validate_npm_lock_path(path)
        if path in lock:
            _fail("npm lock inventory contains a duplicate path")
        lock[path] = item
        if item.get("dev") is not True:
            for key in ("version", "resolved", "integrity", "license"):
                _string(item, key, "production npm lock package")
            _validate_registry_source(item)
            production_paths.add(path)

    special: dict[str, Mapping[str, object]] = {}
    npm_licenses: dict[str, Mapping[str, object]] = {}
    for raw in raw_components:
        component = _object(raw, "license component")
        name = _string(component, "name", "license component")
        version = _string(component, "version", "license component")
        license_id = _string(component, "license", "license component")
        paths = component.get("license_paths")
        if not isinstance(paths, list) or not paths:
            _fail("license component paths are invalid")
        for path in paths:
            if not isinstance(path, str):
                _fail("license component path is invalid")
            _validate_relative_path(path)
            if path not in runtime:
                _fail("license component references a missing runtime license")
        lock_path = component.get("lock_path")
        if lock_path is None:
            if name in special:
                _fail("license inventory contains a duplicate top-level component")
            special[name] = component
            continue
        if not isinstance(lock_path, str):
            _fail("license inventory contains an invalid npm lock path")
        if lock_path in npm_licenses:
            _fail("license inventory contains a duplicate npm lock path")
        npm_licenses[lock_path] = component
        item = lock.get(lock_path)
        if item is None or item.get("dev") is True:
            _fail("license inventory includes a development or missing npm package")
        if (
            name != _npm_name(lock_path)
            or version != item.get("version")
            or license_id != item.get("license")
        ):
            _fail("license inventory disagrees with the production npm lock")
        _license_expression(license_id)

    if set(npm_licenses) != production_paths:
        _fail("license inventory does not exactly match the production npm closure")
    if set(special) != {
        "Tongs",
        "Electron",
        "Chromium and Electron third-party components",
    }:
        _fail("license inventory top-level component set is invalid")
    if (
        special["Tongs"].get("license") != "MIT"
        or special["Tongs"].get("version") != "source"
        or special["Electron"].get("license") != "MIT"
        or special["Electron"].get("version") != electron_version
        or special["Chromium and Electron third-party components"].get("license")
        != "multiple"
        or special["Chromium and Electron third-party components"].get("version")
        != f"bundled-with-electron-{electron_version}"
    ):
        _fail("license inventory top-level identities are invalid")
    return tuple(
        {
            "path": path,
            "name": _npm_name(path),
            "version": item["version"],
            "license": item["license"],
            "resolved": item["resolved"],
            "integrity": item["integrity"],
        }
        for path, item in sorted(lock.items())
        if path in production_paths
    )


def _validate_registry_source(item: Mapping[str, object]) -> None:
    resolved = _string(item, "resolved", "production npm package")
    split = urlsplit(resolved)
    if (
        split.scheme != "https"
        or split.hostname != "registry.npmjs.org"
        or split.username is not None
        or split.password is not None
        or split.query
        or split.fragment
        or not split.path.endswith(".tgz")
    ):
        _fail("production npm package source is not the expected registry tarball")
    _sri_hex(_string(item, "integrity", "production npm package"))
    _license_expression(_string(item, "license", "production npm package"))


def _document_namespace(identity: ExpectedIdentity) -> str:
    material = canonical_json(
        {
            "archive_sha256": identity.archive_sha256,
            "generator_version": identity.generator_version,
            "schema_sha256": _SCHEMA_SHA256,
            "source_commit": identity.source_commit,
            "source_tree": identity.source_tree,
            "source_archive_sha256": identity.source_archive_sha256,
            "source_date_epoch": identity.source_date_epoch,
            "electron_archive_sha256": identity.electron_archive_sha256,
        }
    )
    return "https://tongs.tools/spdx/desktop/" + _sha256(material)


def _spdx_timestamp(epoch: int) -> str:
    try:
        return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError) as error:
        raise SbomBuildError(
            "source epoch cannot be represented as an SPDX time"
        ) from error


def _validate_schema(
    document: Mapping[str, object], schema: Mapping[str, object]
) -> None:
    try:
        Draft7Validator.check_schema(schema)
    except SchemaError as error:
        raise SbomBuildError("pinned SPDX schema is invalid") from error
    errors = sorted(
        Draft7Validator(schema).iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        location = "/".join(str(part) for part in errors[0].absolute_path) or "document"
        _fail(f"generated SPDX document fails the official schema at {location}")


def _expect_package(
    package: Mapping[str, object],
    *,
    name: str,
    version: str,
    file_name: str,
    checksum: tuple[str, str],
    purpose: str,
) -> None:
    if (
        package.get("name") != name
        or package.get("versionInfo") != version
        or package.get("packageFileName") != file_name
        or package.get("primaryPackagePurpose") != purpose
        or package.get("filesAnalyzed") is not False
    ):
        _fail(f"SPDX package identity is invalid: {name}")
    _expect_checksum(package, *checksum)


def _expect_checksum(package: Mapping[str, object], algorithm: str, value: str) -> None:
    if package.get("checksums") != [{"algorithm": algorithm, "checksumValue": value}]:
        _fail("SPDX package checksum identity or meaning is invalid")


def _sri_hex(integrity: str) -> str:
    if not integrity.startswith("sha512-"):
        _fail("production npm integrity is not SHA-512 SRI")
    encoded = integrity.removeprefix("sha512-")
    try:
        digest = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SbomBuildError("production npm integrity is invalid base64") from error
    if len(digest) != 64:
        _fail("production npm integrity has the wrong SHA-512 length")
    return digest.hex()


def _npm_id(path: str) -> str:
    return "SPDXRef-Package-npm-" + hashlib.sha256(path.encode()).hexdigest()[:24]


def _npm_name(path: str) -> str:
    marker = "node_modules/"
    if not path.startswith(marker):
        _fail("npm lock path is invalid")
    return path.rsplit(marker, 1)[1]


def _validate_npm_lock_path(path: str) -> None:
    _validate_relative_path(path)
    if not path.startswith("node_modules/") or path.endswith("/node_modules"):
        _fail("npm lock path is invalid")
    name = _npm_name(path)
    if not name or name.startswith("/") or "node_modules/" in name:
        _fail("npm lock package name is invalid")


def _license_expression(value: str) -> str:
    if _LICENSE_RE.fullmatch(value) is None or value in {"NOASSERTION", "NONE"}:
        _fail("production npm license is not a supported SPDX expression")
    return value


def _load_root_json(root: Path, relative: str) -> Mapping[str, object]:
    return _decode_json(_read_root_file(root, relative, _MAX_JSON_BYTES), relative)


def _decode_json(document: bytes, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(
            document,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SbomBuildError(f"{label} is not valid UTF-8 JSON") from error
    return _object(value, label)


def _object_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            _fail(f"JSON object contains duplicate key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> NoReturn:
    _fail(f"JSON contains unsupported numeric constant: {value}")


def _read_root_file(root: Path, relative: str, maximum: int) -> bytes:
    path = _root_file(root, relative)
    return _read_regular_file_outside_root(path, maximum, relative)


def _root_file(root: Path, relative: str) -> Path:
    _validate_relative_path(relative)
    current = root
    for index, part in enumerate(PurePosixPath(relative).parts):
        current = current / part
        try:
            details = current.lstat()
        except OSError as error:
            raise SbomBuildError(f"required input is missing: {relative}") from error
        if stat.S_ISLNK(details.st_mode):
            _fail(f"input path contains a symlink: {relative}")
        if index < len(PurePosixPath(relative).parts) - 1:
            if not stat.S_ISDIR(details.st_mode):
                _fail(f"input parent is not a directory: {relative}")
        elif not stat.S_ISREG(details.st_mode):
            _fail(f"input is not a regular file: {relative}")
    return current


def _read_regular_file_outside_root(path: Path, maximum: int, label: str) -> bytes:
    try:
        details = path.lstat()
    except OSError as error:
        raise SbomBuildError(f"{label} is missing") from error
    if not stat.S_ISREG(details.st_mode) or path.is_symlink():
        _fail(f"{label} is not a regular file")
    if not 0 < details.st_size <= maximum:
        _fail(f"{label} exceeds its byte bound")
    return path.read_bytes()


def _real_directory(path: Path, label: str) -> Path:
    try:
        details = path.lstat()
    except OSError as error:
        raise SbomBuildError(f"{label} is missing") from error
    if not stat.S_ISDIR(details.st_mode) or path.is_symlink():
        _fail(f"{label} is not a real directory")
    return path.resolve(strict=True)


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256(document: bytes) -> str:
    return hashlib.sha256(document).hexdigest()


def _validate_relative_path(value: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise SbomBuildError("input path is not valid UTF-8") from error
    path = PurePosixPath(value)
    if (
        not value
        or len(value.encode()) > 512
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or "\\" in value
    ):
        _fail("input path is unsafe")


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        _fail(f"{label} is not an object")
    return value


def _string(item: Mapping[str, object], key: str, label: str) -> str:
    value = item.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 4096
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        _fail(f"{label} {key} is invalid")
    return value


def _integer(item: Mapping[str, object], key: str, label: str) -> int:
    value = item.get(key)
    if type(value) is not int:
        _fail(f"{label} {key} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _fail(f"{label} is invalid")
    return value


def _require_exact_keys(
    item: Mapping[str, object], expected: set[str], label: str
) -> None:
    if set(item) != expected:
        _fail(f"{label} fields are incomplete or unexpected")


def _fail(message: str) -> NoReturn:
    raise SbomBuildError(message)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--expected-source-tree", required=True)
    parser.add_argument(
        "--expected-source-archive-sha256",
        required=True,
        help="SHA-256 independently computed from the exact checkout's git archive",
    )
    parser.add_argument(
        "--expected-source-date-epoch",
        required=True,
        type=int,
        help="commit epoch independently read from the exact checked-out source",
    )
    parser.add_argument("--expected-archive-sha256", required=True)
    parser.add_argument(
        "--expected-electron-archive-sha256",
        required=True,
        help="SHA-256 from the reviewed Electron input configuration in source",
    )
    parser.add_argument("--generator-version", required=True)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fail-closed desktop SPDX generator."""
    arguments = _parser().parse_args(argv)
    identity = ExpectedIdentity(
        source_commit=arguments.expected_source_commit,
        source_tree=arguments.expected_source_tree,
        source_archive_sha256=arguments.expected_source_archive_sha256,
        source_date_epoch=arguments.expected_source_date_epoch,
        archive_sha256=arguments.expected_archive_sha256,
        electron_archive_sha256=arguments.expected_electron_archive_sha256,
        generator_version=arguments.generator_version,
    )
    try:
        document = build_desktop_sbom(arguments.input_root, arguments.schema, identity)
        output = arguments.output
        if output.exists() or output.is_symlink():
            _fail("output path already exists")
        output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        output.write_bytes(document)
    except (OSError, SbomBuildError) as error:
        print(f"desktop SBOM generation failed: {error}", file=sys.stderr)
        return 1
    print(f"desktop SPDX 2.3 SBOM written: sha256={_sha256(document)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
