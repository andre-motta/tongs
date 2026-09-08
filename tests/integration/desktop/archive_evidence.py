"""Produce and consume bounded receipt evidence for the desktop archive lifecycle.

This adapter wraps the already reviewed reproducible archive producer and the
existing unsigned transfer validation in the existing source-bound acceptance
receipt. It is not a packager, a receipt schema, or a signing policy. Nothing
here verifies a certificate or a signature, so nothing here may be read as a
signing claim.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Final, NoReturn

from tongs.desktop.artifact_contract import (
    FIXED_APP_ASAR_PATH,
    FIXED_LAUNCHER_PATH,
    FIXED_LICENSE_INVENTORY_PATH,
    INSTALL_MANIFEST_PATH,
    ArtifactContractError,
    parse_install_manifest,
    parse_release_manifest,
    validate_manifest_pair,
)
from tongs.desktop.installer.metadata import RELEASE_MANIFEST_NAME
from tongs.desktop.installer.models import InstallerLimits

ROOT: Final = Path(__file__).parents[3]
ADAPTER_PROGRAM: Final = "tests/integration/desktop/archive_evidence.py"
TRANSFER_VALIDATOR_PROGRAM: Final = "tests/integration/desktop/candidate_attestation.py"
RECEIPT_READER_PROGRAM: Final = ".github/scripts/verify_desktop_production.py"
ARTIFACT_CONTRACT_PACKAGE: Final = "src/tongs/desktop/artifact_contract"

DESKTOP_PACKAGE_PATH: Final = "desktop/package.json"
ARCHIVE_CONTRACT_PATH: Final = "packaging/desktop/archive/contract.json"
ELECTRON_CONFIGURATION_DIRECTORY: Final = "packaging/desktop/archive"

REPORT_PATH: Final = "reports/archive-evidence.json"
TRANSFER_MANIFEST_PATH: Final = "inputs/candidate-attestation-transfer-v1.json"
RECEIPT_PATH: Final = "archive-receipt.json"

STAGE_NAMES: Final = (
    "source-admission",
    "producer-transfer-validation",
    "archive-contract-license-validation",
    "reproducibility-output-binding",
    "source-tool-metadata-validation",
)
SCOPE: Final = {
    "covered": "reproducible-desktop-user-archive-transfer",
    "excluded": [
        "signature-and-certificate-verification",
        "application-startup",
        "hardware-gpu-function",
        "rpm-package-set",
        "sbom-component-inventory",
        "separately-installed-core-wheel",
    ],
    "transfer_identity": "unsigned",
}

MAX_LARGE_BYTES: Final = InstallerLimits().max_archive_bytes
MAX_JSON_BYTES: Final = 4 * 1024 * 1024
MAX_TEXT_BYTES: Final = 256 * 1024
MAX_TRANSFER_MANIFEST_BYTES: Final = 256 * 1024
MAX_CHECKSUM_RECORDS: Final = 256
HASH_CHUNK_BYTES: Final = 1024 * 1024

_SHA1_RE: Final = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_CHECKSUM_LINE_RE: Final = re.compile(r"([0-9a-f]{64})  \./([A-Za-z0-9._-]{1,128})\Z")
_TOOLCHAIN_KEYS: Final = frozenset(
    {
        "asar",
        "electron",
        "esbuild",
        "node",
        "npm",
        "python",
        "typescript",
        "zlib_compile",
        "zlib_runtime",
    }
)
_COMPRESSION_KEYS: Final = frozenset(
    {"gzip_filename", "gzip_level", "gzip_os", "tar_format"}
)
_INPUT_KEYS: Final = frozenset(
    {
        "TONGS_HEAD_SHA",
        "SOURCE_DATE_EPOCH",
        "RELEASE_VERSION",
        "CORE_MINIMUM",
        "CORE_MAXIMUM_EXCLUSIVE",
        "ELECTRON_ARCHIVE_SHA256",
    }
)


class ArchiveEvidenceError(ValueError):
    """Reject incomplete, stale, or semantically invalid archive evidence."""


def _load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"unable to load required module: {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


TRANSFER = _load_module(
    "desktop_archive_evidence_transfer", ROOT / TRANSFER_VALIDATOR_PROGRAM
)
RECEIPTS = _load_module(
    "desktop_archive_evidence_receipts", ROOT / RECEIPT_READER_PROGRAM
)
_TRANSFER_ORIGIN: Final = str(Path(TRANSFER.__file__).resolve())
_RECEIPTS_ORIGIN: Final = str(Path(RECEIPTS.__file__).resolve())

ARCHIVE_NAME: Final = TRANSFER.CANDIDATE_ARCHIVE_NAME
TRANSFER_MANIFEST_NAME: Final = TRANSFER.TRANSFER_MANIFEST_NAME
ELECTRON_ARCHIVE_NAME: Final = "electron-v44.2.0-linux-x64.zip"

TRANSFER_FILE_BOUNDS: Final = {
    "archive/SHA256SUMS": MAX_TEXT_BYTES,
    "archive/app-asar-inventory.json": MAX_JSON_BYTES,
    "archive/build-provenance.json": MAX_JSON_BYTES,
    "archive/desktop-install.json": MAX_JSON_BYTES,
    f"archive/{RELEASE_MANIFEST_NAME}": MAX_JSON_BYTES,
    f"archive/{ARCHIVE_NAME}": MAX_LARGE_BYTES,
    "archive/license-inventory.json": MAX_JSON_BYTES,
    "archive/prepared-source-inventory.json": MAX_JSON_BYTES,
    "archive/runtime-inventory.json": MAX_JSON_BYTES,
    "evidence/build-a.sha256": MAX_TEXT_BYTES,
    "evidence/build-b.sha256": MAX_TEXT_BYTES,
    "evidence/builder-image.json": MAX_JSON_BYTES,
    f"evidence/{ELECTRON_ARCHIVE_NAME}": MAX_LARGE_BYTES,
    "evidence/inputs.env": MAX_TEXT_BYTES,
    "evidence/rpm-nevra.txt": MAX_TEXT_BYTES,
    "evidence/rpm-sha256-check.txt": MAX_TEXT_BYTES,
    "evidence/rpm-signatures.txt": MAX_TEXT_BYTES,
    "evidence/source.tar": MAX_LARGE_BYTES,
    "evidence/toolchain.txt": MAX_TEXT_BYTES,
}
RELEASE_MANIFEST_RELATIVE: Final = f"archive/{RELEASE_MANIFEST_NAME}"
INSTALL_MANIFEST_RELATIVE: Final = "archive/desktop-install.json"
LICENSE_INVENTORY_RELATIVE: Final = "archive/license-inventory.json"
RUNTIME_INVENTORY_RELATIVE: Final = "archive/runtime-inventory.json"
ASAR_INVENTORY_RELATIVE: Final = "archive/app-asar-inventory.json"
PROVENANCE_RELATIVE: Final = "archive/build-provenance.json"
PREPARED_SOURCE_RELATIVE: Final = "archive/prepared-source-inventory.json"
CHECKSUM_FILE_NAME: Final = "SHA256SUMS"


@dataclass(frozen=True, slots=True)
class ArchiveEvidenceExpectations:
    """Caller-owned subject, archive, tool, and upstream transfer policy.

    Four identities stay separate and none is ever read from the report under
    check. The subject source identity fixes the exact clean checkout. The
    archive identity fixes the reproducible outputs. The tool identity fixes
    the trusted adapter bytes. The transfer identity fixes the authentic
    upstream execution that produced the retained transfer tree.
    """

    source_commit: str
    source_tree: str
    source_archive_sha256: str
    source_date_epoch: int
    archive_name: str
    archive_artifact_id: str
    archive_sha256: str
    release_version: str
    release_manifest_sha256: str
    install_manifest_sha256: str
    license_inventory_sha256: str
    electron_version: str
    electron_configuration_sha256: str
    electron_archive_sha256: str
    adapter_program_sha256: str
    transfer_validator_program_sha256: str
    receipt_reader_program_sha256: str
    artifact_contract_sha256: str
    transfer_repository: str
    transfer_repository_id: str
    transfer_repository_owner_id: str
    transfer_ref: str
    transfer_event: str
    transfer_run_id: str
    transfer_run_attempt: int

    def validate(self) -> None:
        """Reject malformed caller policy before reading producer evidence."""
        for label, value in (
            ("source commit", self.source_commit),
            ("source tree", self.source_tree),
        ):
            if not isinstance(value, str) or _SHA1_RE.fullmatch(value) is None:
                _fail(f"expected {label} is invalid")
        for label, value in (
            ("source archive", self.source_archive_sha256),
            ("desktop archive", self.archive_sha256),
            ("release manifest", self.release_manifest_sha256),
            ("install manifest", self.install_manifest_sha256),
            ("license inventory", self.license_inventory_sha256),
            ("Electron configuration", self.electron_configuration_sha256),
            ("Electron archive", self.electron_archive_sha256),
            ("adapter program", self.adapter_program_sha256),
            ("transfer validator program", self.transfer_validator_program_sha256),
            ("receipt reader program", self.receipt_reader_program_sha256),
            ("artifact contract", self.artifact_contract_sha256),
        ):
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                _fail(f"expected {label} SHA-256 is invalid")
        if (
            type(self.source_date_epoch) is not int
            or not 0 <= self.source_date_epoch <= 0xFFFFFFFF
        ):
            _fail("expected source epoch is invalid")
        if self.archive_name != ARCHIVE_NAME:
            _fail("expected archive name is not the reviewed archive name")
        for label, value in (
            ("archive artifact ID", self.archive_artifact_id),
            ("release version", self.release_version),
            ("Electron version", self.electron_version),
        ):
            if not isinstance(value, str) or not value:
                _fail(f"expected {label} is invalid")
        _transfer_identity(self)


@dataclass(frozen=True, slots=True)
class ArchiveEvidenceBinding:
    """Accepted archive lifecycle bindings required by parent issue #53."""

    report_path: str
    report_sha256: str
    transfer_manifest_path: str
    transfer_manifest_sha256: str
    receipt_path: str
    receipt_sha256: str
    archive_name: str
    archive_size: int
    archive_sha256: str
    archive_artifact_id: str
    release_version: str
    release_manifest_sha256: str
    install_manifest_sha256: str
    license_inventory_sha256: str
    source_commit: str
    source_tree: str


def produce_archive_evidence(
    *,
    source_root: Path,
    transfer_root: Path,
    output_root: Path,
    receipt_policy: Any,
    expectations: ArchiveEvidenceExpectations,
) -> ArchiveEvidenceBinding:
    """Validate one retained transfer and atomically publish its receipt."""
    expectations.validate()
    _validate_receipt_policy(receipt_policy, expectations)
    transfer = _resolve_transfer_root(transfer_root)
    state = _semantic_state(
        source_root=source_root, transfer_root=transfer, expectations=expectations
    )
    manifest_bytes = _read_regular_bytes(
        transfer / TRANSFER_MANIFEST_NAME,
        MAX_TRANSFER_MANIFEST_BYTES,
        "retained transfer manifest",
    )
    if _sha256(manifest_bytes) != state["transfer"]["manifest_sha256"]:
        _fail("retained transfer manifest changed during validation")

    destination = Path(os.path.abspath(output_root))
    if destination.exists() or destination.is_symlink():
        _fail("output root must not already exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        report_bytes = _canonical_json(_report_document(receipt_policy, state))
        if len(report_bytes) > RECEIPTS.MAX_JSON_REPORT_BYTES:
            _fail("archive lifecycle report exceeds its bounded size")
        receipt_bytes = _canonical_json(
            _receipt_document(
                receipt_policy,
                expectations,
                report_bytes=report_bytes,
                manifest_bytes=manifest_bytes,
            )
        )
        _write_exclusive(temporary / REPORT_PATH, report_bytes)
        _write_exclusive(temporary / TRANSFER_MANIFEST_PATH, manifest_bytes)
        _write_exclusive(temporary / RECEIPT_PATH, receipt_bytes)
        binding = consume_archive_evidence(
            evidence_root=temporary,
            receipt_path=temporary / RECEIPT_PATH,
            transfer_root=transfer,
            source_root=source_root,
            receipt_policy=receipt_policy,
            expectations=expectations,
        )
        os.replace(temporary, destination)
        return binding
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def consume_archive_evidence(
    *,
    evidence_root: Path,
    receipt_path: Path,
    transfer_root: Path,
    source_root: Path,
    receipt_policy: Any,
    expectations: ArchiveEvidenceExpectations,
) -> ArchiveEvidenceBinding:
    """Bind receipt files and require the complete archive lifecycle result."""
    expectations.validate()
    _validate_receipt_policy(receipt_policy, expectations)
    try:
        validation = RECEIPTS.validate_receipt_file(
            receipt_path, evidence_root=evidence_root, policy=receipt_policy
        )
    except RECEIPTS.ReceiptValidationError as error:
        raise ArchiveEvidenceError("archive receipt binding is invalid") from error
    if validation.receipt.get("result") != "success":
        _fail("archive receipt does not record success")
    _require_receipt_roles(validation.receipt)
    expected_files = {REPORT_PATH: "report", TRANSFER_MANIFEST_PATH: "input"}
    bound = {item.path: item for item in validation.bound_files}
    if set(bound) != set(expected_files) or any(
        bound[path].kind != kind for path, kind in expected_files.items()
    ):
        _fail("archive receipt file set is incomplete or unexpected")
    receipt_bytes = _read_regular_bytes(
        receipt_path, RECEIPTS.MAX_RECEIPT_BYTES, "archive receipt"
    )
    try:
        reread = RECEIPTS.validate_receipt(
            receipt_bytes, evidence_root=evidence_root, policy=receipt_policy
        )
    except RECEIPTS.ReceiptValidationError as error:
        raise ArchiveEvidenceError("archive receipt changed during use") from error
    if reread.receipt != validation.receipt:
        _fail("archive receipt changed between its bounded reads")
    try:
        report_bytes = RECEIPTS.read_bound_bytes(
            evidence_root, bound[REPORT_PATH], RECEIPTS.MAX_JSON_REPORT_BYTES
        )
        manifest_bytes = RECEIPTS.read_bound_bytes(
            evidence_root, bound[TRANSFER_MANIFEST_PATH], MAX_TRANSFER_MANIFEST_BYTES
        )
    except RECEIPTS.ReceiptValidationError as error:
        raise ArchiveEvidenceError(
            "archive bound evidence changed or exceeded its limit"
        ) from error

    transfer = _resolve_transfer_root(transfer_root)
    retained = _read_regular_bytes(
        transfer / TRANSFER_MANIFEST_NAME,
        MAX_TRANSFER_MANIFEST_BYTES,
        "retained transfer manifest",
    )
    if retained != manifest_bytes:
        _fail("retained transfer manifest differs from its receipt-bound bytes")
    state = _semantic_state(
        source_root=source_root, transfer_root=transfer, expectations=expectations
    )
    if (
        _read_regular_bytes(
            transfer / TRANSFER_MANIFEST_NAME,
            MAX_TRANSFER_MANIFEST_BYTES,
            "retained transfer manifest",
        )
        != manifest_bytes
    ):
        _fail("retained transfer manifest changed during revalidation")
    report = _decode_object(report_bytes, "archive lifecycle report")
    _validate_report(report, receipt_policy, state, bound)
    return ArchiveEvidenceBinding(
        report_path=REPORT_PATH,
        report_sha256=bound[REPORT_PATH].sha256,
        transfer_manifest_path=TRANSFER_MANIFEST_PATH,
        transfer_manifest_sha256=bound[TRANSFER_MANIFEST_PATH].sha256,
        receipt_path=RECEIPT_PATH,
        receipt_sha256=_sha256(receipt_bytes),
        archive_name=expectations.archive_name,
        archive_size=state["archive"]["size"],
        archive_sha256=expectations.archive_sha256,
        archive_artifact_id=expectations.archive_artifact_id,
        release_version=expectations.release_version,
        release_manifest_sha256=expectations.release_manifest_sha256,
        install_manifest_sha256=expectations.install_manifest_sha256,
        license_inventory_sha256=expectations.license_inventory_sha256,
        source_commit=expectations.source_commit,
        source_tree=expectations.source_tree,
    )


def _semantic_state(
    *,
    source_root: Path,
    transfer_root: Path,
    expectations: ArchiveEvidenceExpectations,
) -> dict[str, Any]:
    """Derive every accepted value from the source and the retained transfer."""
    tool = _require_tool_identity(expectations)
    declared = _preflight_transfer(transfer_root)
    with tempfile.TemporaryDirectory(prefix="tongs-archive-evidence-") as scratch:
        subject_archive = Path(scratch) / "subject-source.tar"
        source = _derive_source_identity(source_root, subject_archive)
        _require_expected_source(source, expectations)
        identity = _transfer_identity(expectations)
        try:
            manifest = TRANSFER.validate_transfer_manifest(
                transfer_root,
                transfer_root / TRANSFER_MANIFEST_NAME,
                identity,
                subject_archive,
            )
        except TRANSFER.CandidateAttestationError as error:
            raise ArchiveEvidenceError("retained transfer validation failed") from error
    observed = _observed_records(manifest, declared)
    archive = _archive_state(transfer_root, observed, source_root, expectations)
    reproducibility = _reproducibility_state(transfer_root, observed)
    _require_tool_identity(expectations)
    return {
        "source": source,
        "tool": tool,
        "transfer": {
            "manifest_path": TRANSFER_MANIFEST_PATH,
            "manifest_sha256": _sha256(
                _read_regular_bytes(
                    transfer_root / TRANSFER_MANIFEST_NAME,
                    MAX_TRANSFER_MANIFEST_BYTES,
                    "retained transfer manifest",
                )
            ),
            "file_count": len(observed),
            "execution": {
                "repository": expectations.transfer_repository,
                "repository_id": expectations.transfer_repository_id,
                "repository_owner_id": expectations.transfer_repository_owner_id,
                "ref": expectations.transfer_ref,
                "event": expectations.transfer_event,
                "run_id": expectations.transfer_run_id,
                "run_attempt": expectations.transfer_run_attempt,
            },
        },
        "archive": archive,
        "reproducibility": reproducibility,
    }


def _resolve_transfer_root(transfer_root: Path) -> Path:
    try:
        resolved = Path(transfer_root).resolve(strict=True)
    except OSError as error:
        raise ArchiveEvidenceError("retained transfer root is missing") from error
    details = resolved.lstat()
    if not stat.S_ISDIR(details.st_mode):
        _fail("retained transfer root must be a real directory")
    return resolved


def _preflight_transfer(transfer_root: Path) -> dict[str, int]:
    """Bound file kinds, paths, and declared sizes before any hashing."""
    observed: dict[str, int] = {}
    for path in sorted(transfer_root.rglob("*")):
        relative = path.relative_to(transfer_root).as_posix()
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            if path.is_symlink() or relative not in {"archive", "evidence"}:
                _fail("retained transfer contains an unexpected directory")
            continue
        if not stat.S_ISREG(details.st_mode) or path.is_symlink():
            _fail("retained transfer contains a nonregular file")
        _require_safe_relative_path(relative)
        if relative == TRANSFER_MANIFEST_NAME:
            maximum = MAX_TRANSFER_MANIFEST_BYTES
        else:
            maximum = TRANSFER_FILE_BOUNDS.get(relative, 0)
        if maximum == 0:
            _fail("retained transfer contains an unexpected file")
        if not 0 < details.st_size <= maximum:
            _fail("retained transfer file is outside its per-class byte bound")
        observed[relative] = details.st_size
    if set(observed) != set(TRANSFER_FILE_BOUNDS) | {TRANSFER_MANIFEST_NAME}:
        _fail("retained transfer path set is incomplete or unexpected")
    declared = _preflight_manifest(transfer_root, observed)
    return declared


def _preflight_manifest(
    transfer_root: Path, observed: Mapping[str, int]
) -> dict[str, int]:
    document = _decode_object(
        _read_regular_bytes(
            transfer_root / TRANSFER_MANIFEST_NAME,
            MAX_TRANSFER_MANIFEST_BYTES,
            "retained transfer manifest",
        ),
        "retained transfer manifest",
    )
    files = document.get("files")
    if not isinstance(files, list) or len(files) != len(TRANSFER_FILE_BOUNDS):
        _fail("retained transfer manifest file declarations are invalid")
    declared: dict[str, int] = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            _fail("retained transfer manifest file entry is invalid")
        path = item["path"]
        size = item["size"]
        if not isinstance(path, str) or path in declared:
            _fail("retained transfer manifest declares a duplicate or invalid path")
        _require_safe_relative_path(path)
        maximum = TRANSFER_FILE_BOUNDS.get(path, 0)
        if maximum == 0:
            _fail("retained transfer manifest declares an unexpected path")
        if type(size) is not int or not 0 < size <= maximum:
            _fail("declared transfer file size is outside its per-class byte bound")
        if observed.get(path) != size:
            _fail("declared transfer file size disagrees with the retained file")
        if (
            not isinstance(item["sha256"], str)
            or _SHA256_RE.fullmatch(item["sha256"]) is None
        ):
            _fail("declared transfer file digest is invalid")
        declared[path] = size
    if set(declared) != set(TRANSFER_FILE_BOUNDS):
        _fail("retained transfer manifest path set is incomplete or unexpected")
    return declared


def _observed_records(
    manifest: Mapping[str, Any], declared: Mapping[str, int]
) -> dict[str, dict[str, Any]]:
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != len(declared):
        _fail("validated transfer file records are invalid")
    records: dict[str, dict[str, Any]] = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            _fail("validated transfer file record is invalid")
        path = item["path"]
        if path in records or declared.get(path) != item["size"]:
            _fail("validated transfer file record disagrees with the preflight")
        records[path] = {"size": item["size"], "sha256": item["sha256"]}
    if set(records) != set(declared):
        _fail("validated transfer file records are incomplete")
    return records


def _bound_bytes(
    transfer_root: Path, relative: str, observed: Mapping[str, dict[str, Any]]
) -> bytes:
    record = observed.get(relative)
    if record is None:
        _fail("required transfer file was not validated")
    value = _read_regular_bytes(
        transfer_root / relative, TRANSFER_FILE_BOUNDS[relative], relative
    )
    if len(value) != record["size"] or _sha256(value) != record["sha256"]:
        _fail("retained transfer file changed after it was validated")
    return value


def _archive_state(
    transfer_root: Path,
    observed: Mapping[str, dict[str, Any]],
    source_root: Path,
    expectations: ArchiveEvidenceExpectations,
) -> dict[str, Any]:
    """Bind the archive contract, license closure, and source metadata."""
    release_bytes = _bound_bytes(transfer_root, RELEASE_MANIFEST_RELATIVE, observed)
    install_bytes = _bound_bytes(transfer_root, INSTALL_MANIFEST_RELATIVE, observed)
    try:
        release = parse_release_manifest(release_bytes)
        install = parse_install_manifest(install_bytes)
    except ArtifactContractError as error:
        raise ArchiveEvidenceError("retained archive manifests are invalid") from error
    if release.source_commit != expectations.source_commit:
        _fail("release manifest is not bound to the expected subject source")
    if release.release_version != expectations.release_version:
        _fail("release manifest version does not match caller policy")
    matches = tuple(
        artifact
        for artifact in release.artifacts
        if artifact.artifact_id == expectations.archive_artifact_id
    )
    if len(matches) != 1:
        _fail("release artifact identity is missing or ambiguous")
    artifact = matches[0]
    try:
        validate_manifest_pair(release, artifact, install)
    except ArtifactContractError as error:
        raise ArchiveEvidenceError(
            "retained release and install manifests disagree"
        ) from error
    archive_relative = f"archive/{expectations.archive_name}"
    archive_record = observed.get(archive_relative)
    if archive_record is None:
        _fail("retained transfer does not contain the expected archive")
    if (
        artifact.name != expectations.archive_name
        or artifact.sha256 != expectations.archive_sha256
        or archive_record["sha256"] != expectations.archive_sha256
        or archive_record["size"] != artifact.byte_count
    ):
        _fail("archive identity does not match caller policy")
    if (
        _sha256(release_bytes) != expectations.release_manifest_sha256
        or _sha256(install_bytes) != expectations.install_manifest_sha256
    ):
        _fail("release or install manifest identity does not match caller policy")

    declarations = {item.path: item for item in install.files}
    for required in (FIXED_LAUNCHER_PATH, FIXED_APP_ASAR_PATH):
        if required not in declarations:
            _fail("install manifest omits a required runtime path")
    if not declarations[FIXED_LAUNCHER_PATH].executable:
        _fail("install manifest launcher is not executable")
    license_declaration = declarations.get(FIXED_LICENSE_INVENTORY_PATH)
    if license_declaration is None:
        _fail("install manifest omits the runtime license inventory")
    license_record = observed[LICENSE_INVENTORY_RELATIVE]
    if (
        license_declaration.sha256 != expectations.license_inventory_sha256
        or license_declaration.sha256 != license_record["sha256"]
        or license_declaration.byte_count != license_record["size"]
    ):
        _fail("retained license inventory is not closed over the archive")
    runtime = _decode_object(
        _bound_bytes(transfer_root, RUNTIME_INVENTORY_RELATIVE, observed),
        "runtime inventory",
    )
    runtime_files = runtime.get("files")
    if not isinstance(runtime_files, list) or len(runtime_files) != len(declarations):
        _fail("runtime inventory does not cover the declared runtime file set")
    runtime_records = set()
    for item in runtime_files:
        if not isinstance(item, dict):
            _fail("runtime inventory entry is invalid")
        runtime_records.add(
            (item.get("path"), item.get("sha256"), item.get("byte_count"))
        )
    declared_records = {
        (item.path, item.sha256, item.byte_count) for item in install.files
    }
    if runtime_records != declared_records:
        _fail("runtime inventory disagrees with the install manifest declarations")
    asar = _decode_object(
        _bound_bytes(transfer_root, ASAR_INVENTORY_RELATIVE, observed),
        "application archive inventory",
    )
    asar_declaration = declarations[FIXED_APP_ASAR_PATH]
    if (
        asar.get("asar_sha256") != asar_declaration.sha256
        or asar.get("asar_byte_count") != asar_declaration.byte_count
    ):
        _fail("application archive inventory disagrees with the install manifest")

    source = _source_configuration(source_root, expectations)
    _metadata_state(
        transfer_root,
        observed,
        expectations,
        release=release,
        install=install,
        artifact=artifact,
        source=source,
        release_bytes=release_bytes,
        install_bytes=install_bytes,
    )
    return {
        "name": artifact.name,
        "artifact_id": artifact.artifact_id,
        "size": archive_record["size"],
        "sha256": archive_record["sha256"],
        "release_version": release.release_version,
        "release_manifest_path": RELEASE_MANIFEST_RELATIVE,
        "release_manifest_sha256": _sha256(release_bytes),
        "install_manifest_path": INSTALL_MANIFEST_RELATIVE,
        "install_manifest_sha256": _sha256(install_bytes),
        "install_manifest_entry": INSTALL_MANIFEST_PATH,
        "license_inventory_path": LICENSE_INVENTORY_RELATIVE,
        "license_inventory_sha256": license_declaration.sha256,
        "runtime_license_path": FIXED_LICENSE_INVENTORY_PATH,
        "runtime_file_count": len(declarations),
        "launcher_path": FIXED_LAUNCHER_PATH,
        "app_asar_path": FIXED_APP_ASAR_PATH,
        "app_asar_sha256": asar_declaration.sha256,
        "prepared_source_sha256": observed[PREPARED_SOURCE_RELATIVE]["sha256"],
        "provenance_sha256": observed[PROVENANCE_RELATIVE]["sha256"],
        "source_date_epoch": expectations.source_date_epoch,
        "source_contract_sha256": source["contract_sha256"],
        "electron_version": source["electron_version"],
        "electron_configuration_sha256": source["configuration_sha256"],
        "electron_archive_sha256": source["archive_sha256"],
        "toolchain": source["toolchain"],
        "compression": source["compression"],
    }


def _source_configuration(
    source_root: Path, expectations: ArchiveEvidenceExpectations
) -> dict[str, Any]:
    """Read the reviewed Electron configuration and source-defined toolchain."""
    root = Path(source_root)
    package = _decode_object(
        _read_regular_bytes(
            root / DESKTOP_PACKAGE_PATH, MAX_JSON_BYTES, "desktop package"
        ),
        "desktop package",
    )
    development = package.get("devDependencies")
    if not isinstance(development, dict):
        _fail("desktop package development dependencies are invalid")
    version = development.get("electron")
    if (
        not isinstance(version, str)
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None
        or version != expectations.electron_version
    ):
        _fail("desktop Electron version does not match caller policy")
    contract_bytes = _read_regular_bytes(
        root / ARCHIVE_CONTRACT_PATH, MAX_JSON_BYTES, "archive build contract"
    )
    contract = _decode_object(contract_bytes, "archive build contract")
    electron = contract.get("electron")
    if not isinstance(electron, dict) or electron.get("version") != version:
        _fail("archive build contract Electron version is inconsistent")
    toolchain = contract.get("toolchain")
    if not isinstance(toolchain, dict) or set(toolchain) != _TOOLCHAIN_KEYS:
        _fail("source defined toolchain is incomplete or unexpected")
    if toolchain.get("electron") != version:
        _fail("source defined toolchain Electron version is inconsistent")
    compression = contract.get("compression")
    if not isinstance(compression, dict) or set(compression) != _COMPRESSION_KEYS:
        _fail("source defined compression policy is incomplete or unexpected")
    inventory_name = contract.get("electron_runtime_inventory")
    if not isinstance(inventory_name, str) or inventory_name != (
        f"electron-runtime-{version}-linux-x64.json"
    ):
        _fail("archive build contract Electron inventory reference is invalid")
    configuration_bytes = _read_regular_bytes(
        root / ELECTRON_CONFIGURATION_DIRECTORY / inventory_name,
        MAX_JSON_BYTES,
        "Electron configuration",
    )
    configuration = _decode_object(configuration_bytes, "Electron configuration")
    upstream = configuration.get("upstream_archive")
    if not isinstance(upstream, dict):
        _fail("Electron upstream archive declaration is invalid")
    digest = upstream.get("sha256")
    name = upstream.get("name")
    if (
        configuration.get("electron_version") != version
        or not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
        or name != ELECTRON_ARCHIVE_NAME
    ):
        _fail("Electron configuration identity is invalid")
    configuration_sha256 = _sha256(configuration_bytes)
    if (
        configuration_sha256 != expectations.electron_configuration_sha256
        or digest != expectations.electron_archive_sha256
    ):
        _fail("reviewed Electron configuration does not match caller policy")
    return {
        "electron_version": version,
        "configuration_sha256": configuration_sha256,
        "archive_sha256": digest,
        "archive_name": name,
        "contract_sha256": _sha256(contract_bytes),
        "toolchain": dict(toolchain),
        "compression": dict(compression),
    }


def _metadata_state(
    transfer_root: Path,
    observed: Mapping[str, dict[str, Any]],
    expectations: ArchiveEvidenceExpectations,
    *,
    release: Any,
    install: Any,
    artifact: Any,
    source: Mapping[str, Any],
    release_bytes: bytes,
    install_bytes: bytes,
) -> None:
    """Cross-check provenance and input summaries against caller policy."""
    provenance = _decode_object(
        _bound_bytes(transfer_root, PROVENANCE_RELATIVE, observed), "build provenance"
    )
    expected_keys = {
        "candidate",
        "compatibility",
        "compression",
        "electron_input",
        "outputs",
        "release_version",
        "schema_version",
        "source_commit",
        "source_date_epoch",
        "toolchain",
    }
    if set(provenance) != expected_keys:
        _fail("build provenance fields are incomplete or unexpected")
    compatibility = {
        "core_minimum": release.compatibility.core_minimum,
        "core_maximum_exclusive": release.compatibility.core_maximum_exclusive,
        "rpc_api_major": release.compatibility.rpc_api_major,
        "plugin_api_major": release.compatibility.plugin_api_major,
    }
    if (
        provenance["candidate"] != "UNPUBLISHED"
        or provenance["schema_version"] != 1
        or provenance["source_commit"] != expectations.source_commit
        or provenance["source_date_epoch"] != expectations.source_date_epoch
        or provenance["release_version"] != expectations.release_version
        or provenance["compatibility"] != compatibility
    ):
        _fail("build provenance is not bound to the caller subject identity")
    if provenance["toolchain"] != source["toolchain"]:
        _fail("build provenance toolchain differs from the source defined toolchain")
    if provenance["compression"] != source["compression"]:
        _fail("build provenance compression differs from the source defined policy")
    electron_record = observed[f"evidence/{source['archive_name']}"]
    if provenance["electron_input"] != {
        "archive": {
            "byte_count": electron_record["size"],
            "name": source["archive_name"],
            "sha256": source["archive_sha256"],
        },
        "inventory_sha256": source["configuration_sha256"],
        "upstream_archive": {
            "name": source["archive_name"],
            "sha256": source["archive_sha256"],
        },
    }:
        _fail("build provenance Electron input differs from the reviewed source")
    if electron_record["sha256"] != source["archive_sha256"]:
        _fail("retained Electron archive does not match the reviewed configuration")
    if provenance["outputs"] != {
        "archive": {
            "byte_count": artifact.byte_count,
            "name": artifact.name,
            "sha256": artifact.sha256,
        },
        "install_manifest": {
            "byte_count": len(install_bytes),
            "sha256": _sha256(install_bytes),
        },
        "release_manifest": {
            "byte_count": len(release_bytes),
            "sha256": _sha256(release_bytes),
        },
    }:
        _fail("build provenance outputs differ from the validated archive records")

    prepared = _decode_object(
        _bound_bytes(transfer_root, PREPARED_SOURCE_RELATIVE, observed),
        "prepared source inventory",
    )
    if prepared.get("source_commit") != expectations.source_commit:
        _fail("prepared source inventory is not bound to the caller subject source")
    inputs = _parse_inputs(_bound_bytes(transfer_root, "evidence/inputs.env", observed))
    if inputs != {
        "TONGS_HEAD_SHA": expectations.source_commit,
        "SOURCE_DATE_EPOCH": str(expectations.source_date_epoch),
        "RELEASE_VERSION": expectations.release_version,
        "CORE_MINIMUM": install.compatibility.core_minimum,
        "CORE_MAXIMUM_EXCLUSIVE": install.compatibility.core_maximum_exclusive,
        "ELECTRON_ARCHIVE_SHA256": source["archive_sha256"],
    }:
        _fail("producer inputs do not match the caller bound subject identity")
    toolchain = source["toolchain"]
    expected_lines = [
        f"Python {toolchain['python']}",
        f"{toolchain['zlib_compile']} {toolchain['zlib_runtime']}",
        toolchain["node"],
        toolchain["npm"],
    ]
    recorded = _bound_bytes(transfer_root, "evidence/toolchain.txt", observed)
    try:
        lines = recorded.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise ArchiveEvidenceError("recorded toolchain is invalid") from error
    if lines != expected_lines:
        _fail("recorded toolchain differs from the source defined toolchain")


def _reproducibility_state(
    transfer_root: Path, observed: Mapping[str, dict[str, Any]]
) -> dict[str, Any]:
    """Close the reproducibility gap between build lists and real outputs."""
    first = _bound_bytes(transfer_root, "evidence/build-a.sha256", observed)
    second = _bound_bytes(transfer_root, "evidence/build-b.sha256", observed)
    if first != second:
        _fail("reproducibility checksum lists are not byte identical")
    records = _parse_checksum_list(first)
    if CHECKSUM_FILE_NAME not in records:
        _fail("reproducibility records omit the archive checksum file")
    actual = {
        path.removeprefix("archive/"): record["sha256"]
        for path, record in observed.items()
        if path.startswith("archive/")
    }
    if records != actual:
        _fail("reproducibility records do not equal the validated archive directory")
    return {
        "build_checksums_sha256": _sha256(first),
        "record_count": len(records),
        "records": [
            {"name": name, "sha256": records[name]} for name in sorted(records)
        ],
    }


def _parse_checksum_list(document: bytes) -> dict[str, str]:
    try:
        lines = document.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise ArchiveEvidenceError("reproducibility list is invalid") from error
    if not lines or len(lines) > MAX_CHECKSUM_RECORDS:
        _fail("reproducibility list has an invalid bounded length")
    records: dict[str, str] = {}
    for line in lines:
        match = _CHECKSUM_LINE_RE.fullmatch(line)
        if match is None:
            _fail("reproducibility list contains an invalid record")
        name = match.group(2)
        if name in {".", ".."} or name in records:
            _fail("reproducibility list contains an unsafe or duplicate name")
        records[name] = match.group(1)
    return records


def _derive_source_identity(source_root: Path, archive_path: Path) -> dict[str, Any]:
    """Independently derive the clean subject HEAD, tree, epoch, and archive."""
    try:
        root = Path(source_root).resolve(strict=True)
    except OSError as error:
        raise ArchiveEvidenceError("subject source root is missing") from error
    if not root.is_dir() or Path(source_root).is_symlink():
        _fail("subject source root must be a real directory")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        _fail("subject source checkout must be clean")
    commit = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    tree = _git(root, "rev-parse", "HEAD^{tree}").decode("ascii").strip()
    epoch_text = (
        _git(root, "show", "-s", "--format=%ct", "HEAD").decode("ascii").strip()
    )
    _git(root, "archive", "--format=tar", f"--output={archive_path}", "HEAD")
    archive_sha256 = _stream_sha256(archive_path, MAX_LARGE_BYTES)
    try:
        epoch = int(epoch_text)
    except ValueError as error:
        raise ArchiveEvidenceError("subject commit epoch is invalid") from error
    return {
        "commit": commit,
        "tree": tree,
        "archive_sha256": archive_sha256,
        "date_epoch": epoch,
    }


def _require_expected_source(
    observed: Mapping[str, Any], expectations: ArchiveEvidenceExpectations
) -> None:
    if observed != {
        "commit": expectations.source_commit,
        "tree": expectations.source_tree,
        "archive_sha256": expectations.source_archive_sha256,
        "date_epoch": expectations.source_date_epoch,
    }:
        _fail("subject source identity does not match the caller expectation")


def _require_tool_identity(
    expectations: ArchiveEvidenceExpectations,
) -> dict[str, Any]:
    """Bind the trusted adapter module origins and caller program hashes."""
    for module, origin, label in (
        (TRANSFER, _TRANSFER_ORIGIN, "transfer validator"),
        (RECEIPTS, _RECEIPTS_ORIGIN, "receipt reader"),
    ):
        loaded = getattr(module, "__file__", None)
        if not isinstance(loaded, str) or str(Path(loaded).resolve()) != origin:
            _fail(f"trusted {label} module origin is not the loaded program")
    for function, expected_module in (
        (parse_release_manifest, "tongs.desktop.artifact_contract.manifests"),
        (parse_install_manifest, "tongs.desktop.artifact_contract.manifests"),
        (validate_manifest_pair, "tongs.desktop.artifact_contract.archive"),
    ):
        if getattr(function, "__module__", None) != expected_module:
            _fail("trusted artifact contract module origin is invalid")
    adapter = _sha256(
        _read_regular_bytes(ROOT / ADAPTER_PROGRAM, MAX_JSON_BYTES, "archive adapter")
    )
    validator = _sha256(
        _read_regular_bytes(
            ROOT / TRANSFER_VALIDATOR_PROGRAM, MAX_JSON_BYTES, "transfer validator"
        )
    )
    reader = _sha256(
        _read_regular_bytes(
            ROOT / RECEIPT_READER_PROGRAM, MAX_JSON_BYTES, "receipt reader"
        )
    )
    contract = _directory_digest(ROOT / ARTIFACT_CONTRACT_PACKAGE)
    if (
        adapter != expectations.adapter_program_sha256
        or validator != expectations.transfer_validator_program_sha256
        or reader != expectations.receipt_reader_program_sha256
        or contract != expectations.artifact_contract_sha256
    ):
        _fail("trusted archive tool identity does not match caller policy")
    return {
        "adapter_program": ADAPTER_PROGRAM,
        "adapter_program_sha256": adapter,
        "transfer_validator_program": TRANSFER_VALIDATOR_PROGRAM,
        "transfer_validator_program_sha256": validator,
        "receipt_reader_program": RECEIPT_READER_PROGRAM,
        "receipt_reader_program_sha256": reader,
        "artifact_contract_package": ARTIFACT_CONTRACT_PACKAGE,
        "artifact_contract_sha256": contract,
    }


def _directory_digest(directory: Path) -> str:
    """Digest one trusted package directory without following any symlink."""
    try:
        details = directory.lstat()
    except OSError as error:
        raise ArchiveEvidenceError("trusted package directory is missing") from error
    if not stat.S_ISDIR(details.st_mode) or directory.is_symlink():
        _fail("trusted package directory must be a real directory")
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if "__pycache__" in relative.split("/"):
            continue
        entry = path.lstat()
        if stat.S_ISDIR(entry.st_mode):
            if path.is_symlink():
                _fail("trusted package directory contains a symlinked directory")
            continue
        if not stat.S_ISREG(entry.st_mode) or path.is_symlink():
            _fail("trusted package directory contains a nonregular file")
        content = _read_regular_bytes(path, MAX_JSON_BYTES, "trusted package file")
        digest.update(f"{relative}\0{len(content)}\0{_sha256(content)}\n".encode())
    return digest.hexdigest()


def _transfer_identity(expectations: ArchiveEvidenceExpectations) -> Any:
    """Build the unsigned transfer identity without any signing claim."""
    try:
        return TRANSFER.UnsignedTransferIdentity(
            repository=expectations.transfer_repository,
            repository_id=expectations.transfer_repository_id,
            repository_owner_id=expectations.transfer_repository_owner_id,
            ref=expectations.transfer_ref,
            source_commit=expectations.source_commit,
            source_tree=expectations.source_tree,
            event=expectations.transfer_event,
            run_id=expectations.transfer_run_id,
            run_attempt=expectations.transfer_run_attempt,
        )
    except TRANSFER.CandidateAttestationError as error:
        raise ArchiveEvidenceError(
            "expected transfer execution identity is invalid"
        ) from error


def _report_document(policy: Any, state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "check_id": policy.expected_check_id,
        "result": "pass",
        "source": state["source"],
        "tool": state["tool"],
        "transfer": state["transfer"],
        "archive": state["archive"],
        "reproducibility": state["reproducibility"],
        "stages": [{"name": name, "result": "pass"} for name in STAGE_NAMES],
        "scope": SCOPE,
    }


def _validate_report(
    report: Mapping[str, Any],
    policy: Any,
    state: Mapping[str, Any],
    bound: Mapping[str, Any],
) -> None:
    _require_exact_keys(
        report,
        {
            "schema_version",
            "check_id",
            "result",
            "source",
            "tool",
            "transfer",
            "archive",
            "reproducibility",
            "stages",
            "scope",
        },
        "archive lifecycle report",
    )
    if (
        report["schema_version"] != 1
        or report["check_id"] != policy.expected_check_id
        or report["result"] != "pass"
    ):
        _fail("archive lifecycle report identity or result is invalid")
    if report["source"] != state["source"]:
        _fail("archive lifecycle report source identity is stale")
    if report["tool"] != state["tool"]:
        _fail("archive lifecycle report tool identity is stale")
    expected_transfer = dict(state["transfer"])
    if expected_transfer["manifest_sha256"] != bound[TRANSFER_MANIFEST_PATH].sha256:
        _fail("bound transfer manifest differs from the retained transfer")
    if report["transfer"] != expected_transfer:
        _fail("archive lifecycle report transfer binding is stale")
    if report["archive"] != state["archive"]:
        _fail("archive lifecycle report archive binding is stale")
    if report["reproducibility"] != state["reproducibility"]:
        _fail("archive lifecycle report reproducibility binding is stale")
    if report["stages"] != [{"name": name, "result": "pass"} for name in STAGE_NAMES]:
        _fail("archive lifecycle stages are incomplete, failed, or unexpected")
    if report["scope"] != SCOPE:
        _fail("archive lifecycle scope is invalid")


def _receipt_document(
    policy: Any,
    expectations: ArchiveEvidenceExpectations,
    *,
    report_bytes: bytes,
    manifest_bytes: bytes,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "check_id": policy.expected_check_id,
        "source": {
            "commit": expectations.source_commit,
            "tree": expectations.source_tree,
        },
        "execution": {
            "repository": policy.expected_repository,
            "run_id": policy.expected_run_id,
            "attempt": policy.expected_attempt,
            "environment": policy.expected_environment,
            "provenance": policy.expected_provenance,
        },
        "result": "success",
        "reports": [
            {
                "path": REPORT_PATH,
                "size": len(report_bytes),
                "sha256": _sha256(report_bytes),
                "format": RECEIPTS.ARTIFACT_LIFECYCLE_FORMAT,
            }
        ],
        "artifacts": [],
        "inputs": [{"path": TRANSFER_MANIFEST_PATH, "sha256": _sha256(manifest_bytes)}],
    }


def _require_receipt_roles(receipt: Mapping[str, Any]) -> None:
    reports = receipt.get("reports")
    artifacts = receipt.get("artifacts")
    inputs = receipt.get("inputs")
    if (
        not isinstance(reports, list)
        or len(reports) != 1
        or not isinstance(reports[0], dict)
        or reports[0].get("path") != REPORT_PATH
        or reports[0].get("format") != RECEIPTS.ARTIFACT_LIFECYCLE_FORMAT
    ):
        _fail("archive receipt report declaration is invalid")
    if artifacts != []:
        _fail("archive receipt must not stage a copied large artifact")
    if not isinstance(inputs, list) or [
        item.get("path") for item in inputs if isinstance(item, dict)
    ] != [TRANSFER_MANIFEST_PATH]:
        _fail("archive receipt input declarations are invalid")


def _validate_receipt_policy(
    policy: Any, expectations: ArchiveEvidenceExpectations
) -> None:
    if not isinstance(policy, RECEIPTS.ReceiptPolicy):
        _fail("receipt policy must use the existing validated policy type")
    if (
        policy.expected_commit != expectations.source_commit
        or policy.expected_tree != expectations.source_tree
    ):
        _fail("receipt policy source identity differs from archive expectations")
    if policy.allowed_report_formats != frozenset({RECEIPTS.ARTIFACT_LIFECYCLE_FORMAT}):
        _fail("receipt policy must allow only artifact-lifecycle-v1")


def _parse_inputs(document: bytes) -> dict[str, str]:
    try:
        lines = document.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise ArchiveEvidenceError("producer inputs are invalid") from error
    result: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            _fail("producer inputs are invalid")
        key, value = line.split("=", 1)
        if not key or key in result:
            _fail("producer inputs are ambiguous")
        result[key] = value
    if set(result) != _INPUT_KEYS:
        _fail("producer inputs are incomplete or unexpected")
    return result


def _git(root: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=False,
            capture_output=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ArchiveEvidenceError(
            "unable to inspect the exact subject checkout"
        ) from error
    if completed.returncode != 0 or len(completed.stderr) > 64 * 1024:
        _fail("subject checkout inspection failed")
    return completed.stdout


def _require_safe_relative_path(value: str) -> None:
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        _fail("transfer path is not a safe relative path")


def _read_regular_bytes(path: Path, maximum: int, label: str) -> bytes:
    try:
        details = path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or path.is_symlink()
            or details.st_size > maximum
        ):
            _fail(f"{label} is not a bounded regular file")
        with path.open("rb") as stream:
            value = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise ArchiveEvidenceError(f"unable to read {label}") from error
    if len(value) > maximum or (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        _fail(f"{label} changed during its bounded read")
    return value


def _stream_sha256(path: Path, maximum: int) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                _fail("subject source archive is not a regular file")
            for chunk in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
                total += len(chunk)
                if total > maximum:
                    _fail("subject source archive exceeds its byte bound")
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise ArchiveEvidenceError("unable to read the subject archive") from error
    if (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ):
        _fail("subject source archive changed while it was read")
    return digest.hexdigest()


def _write_exclusive(path: Path, value: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(value)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _decode_object(document: bytes, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail(f"{label} contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        value = json.loads(
            document.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: _fail(f"{label} contains non-finite {value}"),
        )
    except ArchiveEvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ArchiveEvidenceError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        _fail(f"{label} root must be an object")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        _fail(f"{label} fields are incomplete or unexpected")


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fail(message: str) -> NoReturn:
    raise ArchiveEvidenceError(message)


def _expectations(arguments: argparse.Namespace) -> ArchiveEvidenceExpectations:
    return ArchiveEvidenceExpectations(
        source_commit=arguments.expected_source_commit,
        source_tree=arguments.expected_source_tree,
        source_archive_sha256=arguments.expected_source_archive_sha256,
        source_date_epoch=arguments.expected_source_date_epoch,
        archive_name=arguments.expected_archive_name,
        archive_artifact_id=arguments.expected_archive_artifact_id,
        archive_sha256=arguments.expected_archive_sha256,
        release_version=arguments.expected_release_version,
        release_manifest_sha256=arguments.expected_release_manifest_sha256,
        install_manifest_sha256=arguments.expected_install_manifest_sha256,
        license_inventory_sha256=arguments.expected_license_inventory_sha256,
        electron_version=arguments.expected_electron_version,
        electron_configuration_sha256=(
            arguments.expected_electron_configuration_sha256
        ),
        electron_archive_sha256=arguments.expected_electron_archive_sha256,
        adapter_program_sha256=arguments.expected_adapter_program_sha256,
        transfer_validator_program_sha256=(
            arguments.expected_transfer_validator_program_sha256
        ),
        receipt_reader_program_sha256=(
            arguments.expected_receipt_reader_program_sha256
        ),
        artifact_contract_sha256=arguments.expected_artifact_contract_sha256,
        transfer_repository=arguments.expected_transfer_repository,
        transfer_repository_id=arguments.expected_transfer_repository_id,
        transfer_repository_owner_id=arguments.expected_transfer_repository_owner_id,
        transfer_ref=arguments.expected_transfer_ref,
        transfer_event=arguments.expected_transfer_event,
        transfer_run_id=arguments.expected_transfer_run_id,
        transfer_run_attempt=arguments.expected_transfer_run_attempt,
    )


def _policy(arguments: argparse.Namespace) -> Any:
    return RECEIPTS.ReceiptPolicy(
        expected_commit=arguments.expected_source_commit,
        expected_tree=arguments.expected_source_tree,
        expected_repository=arguments.repository,
        expected_run_id=arguments.run_id,
        expected_attempt=arguments.attempt,
        expected_environment=arguments.environment,
        expected_provenance=arguments.provenance,
        expected_check_id=arguments.check_id,
        allowed_report_formats=(RECEIPTS.ARTIFACT_LIFECYCLE_FORMAT,),
    )


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--transfer-root", required=True, type=Path)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--expected-source-tree", required=True)
    parser.add_argument("--expected-source-archive-sha256", required=True)
    parser.add_argument("--expected-source-date-epoch", required=True, type=int)
    parser.add_argument("--expected-archive-name", required=True)
    parser.add_argument("--expected-archive-artifact-id", required=True)
    parser.add_argument("--expected-archive-sha256", required=True)
    parser.add_argument("--expected-release-version", required=True)
    parser.add_argument("--expected-release-manifest-sha256", required=True)
    parser.add_argument("--expected-install-manifest-sha256", required=True)
    parser.add_argument("--expected-license-inventory-sha256", required=True)
    parser.add_argument("--expected-electron-version", required=True)
    parser.add_argument("--expected-electron-configuration-sha256", required=True)
    parser.add_argument("--expected-electron-archive-sha256", required=True)
    parser.add_argument("--expected-adapter-program-sha256", required=True)
    parser.add_argument("--expected-transfer-validator-program-sha256", required=True)
    parser.add_argument("--expected-receipt-reader-program-sha256", required=True)
    parser.add_argument("--expected-artifact-contract-sha256", required=True)
    parser.add_argument("--expected-transfer-repository", required=True)
    parser.add_argument("--expected-transfer-repository-id", required=True)
    parser.add_argument("--expected-transfer-repository-owner-id", required=True)
    parser.add_argument("--expected-transfer-ref", required=True)
    parser.add_argument("--expected-transfer-event", required=True)
    parser.add_argument("--expected-transfer-run-id", required=True)
    parser.add_argument("--expected-transfer-run-attempt", required=True, type=int)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--environment", required=True)
    parser.add_argument(
        "--provenance", required=True, choices=("hosted", "local", "controlled-fixture")
    )
    parser.add_argument("--check-id", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    produce = commands.add_parser(
        "produce", help="validate one retained transfer and publish its receipt"
    )
    _common_arguments(produce)
    produce.add_argument("--output-root", required=True, type=Path)
    consume = commands.add_parser(
        "consume", help="validate one archive receipt and its lifecycle result"
    )
    _common_arguments(consume)
    consume.add_argument("--evidence-root", required=True, type=Path)
    consume.add_argument("--receipt", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the production or consumer path, failing closed."""
    arguments = _parser().parse_args(argv)
    try:
        expectations = _expectations(arguments)
        policy = _policy(arguments)
        if arguments.command == "produce":
            binding = produce_archive_evidence(
                source_root=arguments.source_root,
                transfer_root=arguments.transfer_root,
                output_root=arguments.output_root,
                receipt_policy=policy,
                expectations=expectations,
            )
        else:
            binding = consume_archive_evidence(
                evidence_root=arguments.evidence_root,
                receipt_path=arguments.receipt,
                transfer_root=arguments.transfer_root,
                source_root=arguments.source_root,
                receipt_policy=policy,
                expectations=expectations,
            )
    except (
        OSError,
        ArchiveEvidenceError,
        ArtifactContractError,
        TRANSFER.CandidateAttestationError,
        RECEIPTS.ReceiptValidationError,
    ) as error:
        print(f"desktop archive evidence failed: {error}", file=sys.stderr)
        return 1
    print(
        "desktop archive evidence passed: "
        f"archive={binding.archive_name} sha256={binding.archive_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
