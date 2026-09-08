"""Produce and consume bounded receipt evidence for the desktop archive SBOM."""

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

ROOT: Final = Path(__file__).parents[3]
DEFAULT_SCHEMA: Final = (
    ROOT / "tests/packaging/desktop/sbom/schema/spdx-2.3.schema.json"
)
REPORT_PATH: Final = "reports/sbom-evidence.json"
SBOM_PATH: Final = "artifacts/tongs-desktop.spdx.json"
ARCHIVE_RECEIPT_PATH: Final = "inputs/archive-receipt.json"
TRANSFER_MANIFEST_PATH: Final = "inputs/candidate-attestation-transfer-v1.json"
RECEIPT_PATH: Final = "sbom-receipt.json"
MAX_SBOM_BYTES: Final = 4 * 1024 * 1024
MAX_INPUT_RECEIPT_BYTES: Final = 256 * 1024
MAX_TRANSFER_MANIFEST_BYTES: Final = 256 * 1024
GENERATOR_PROGRAM: Final = "scripts/build_desktop_sbom.py"
ADAPTER_PROGRAM: Final = "tests/integration/desktop/sbom_evidence.py"
RECEIPT_READER_PROGRAM: Final = ".github/scripts/verify_desktop_production.py"
GENERATOR_SCHEMA: Final = "tests/packaging/desktop/sbom/schema/spdx-2.3.schema.json"
STAGE_NAMES: Final = (
    "source-admission",
    "producer-transfer-validation",
    "semantic-generation",
    "determinism",
)
_SHA1_RE: Final = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")


class SbomEvidenceError(ValueError):
    """Reject incomplete, stale, or semantically invalid SBOM evidence."""


def _load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"unable to load required module: {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


SBOM = _load_module("desktop_sbom_evidence_generator", ROOT / GENERATOR_PROGRAM)
RECEIPTS = _load_module(
    "desktop_sbom_evidence_receipts",
    ROOT / ".github/scripts/verify_desktop_production.py",
)


@dataclass(frozen=True, slots=True)
class EvidenceExpectations:
    """Caller-owned source, archive, and generator expectations."""

    source_commit: str
    source_tree: str
    source_archive_sha256: str
    source_date_epoch: int
    archive_sha256: str
    electron_archive_sha256: str
    generator_version: str
    tool_commit: str
    tool_tree: str
    adapter_program_sha256: str
    generator_program_sha256: str
    receipt_reader_program_sha256: str
    schema_sha256: str
    electron_configuration_sha256: str
    transfer_repository: str
    transfer_repository_id: str
    transfer_repository_owner_id: str
    transfer_ref: str
    transfer_event: str
    transfer_run_id: str
    transfer_run_attempt: int
    archive_receipt_sha256: str

    def validate(self) -> None:
        """Reject malformed caller policy before reading producer evidence."""
        if _SHA1_RE.fullmatch(self.source_commit) is None:
            _fail("expected source commit is invalid")
        if _SHA1_RE.fullmatch(self.source_tree) is None:
            _fail("expected source tree is invalid")
        if _SHA1_RE.fullmatch(self.tool_commit) is None:
            _fail("expected tool commit is invalid")
        if _SHA1_RE.fullmatch(self.tool_tree) is None:
            _fail("expected tool tree is invalid")
        for label, value in (
            ("source archive", self.source_archive_sha256),
            ("desktop archive", self.archive_sha256),
            ("Electron archive", self.electron_archive_sha256),
            ("adapter program", self.adapter_program_sha256),
            ("generator program", self.generator_program_sha256),
            ("receipt reader program", self.receipt_reader_program_sha256),
            ("SPDX schema", self.schema_sha256),
            ("Electron configuration", self.electron_configuration_sha256),
            ("archive receipt", self.archive_receipt_sha256),
        ):
            if _SHA256_RE.fullmatch(value) is None:
                _fail(f"expected {label} SHA-256 is invalid")
        if (
            type(self.source_date_epoch) is not int
            or not 0 <= self.source_date_epoch <= 0xFFFFFFFF
        ):
            _fail("expected source epoch is invalid")
        if not isinstance(self.generator_version, str) or not self.generator_version:
            _fail("expected generator version is invalid")
        for label, value in (
            ("transfer repository ID", self.transfer_repository_id),
            ("transfer repository owner ID", self.transfer_repository_owner_id),
            ("transfer ref", self.transfer_ref),
            ("transfer event", self.transfer_event),
            ("transfer repository", self.transfer_repository),
            ("transfer run ID", self.transfer_run_id),
        ):
            if not isinstance(value, str) or not value:
                _fail(f"expected {label} is invalid")
        if type(self.transfer_run_attempt) is not int or self.transfer_run_attempt < 1:
            _fail("expected transfer run attempt is invalid")


@dataclass(frozen=True, slots=True)
class SbomEvidenceBinding:
    """Semantically accepted output identities for parent issue #53."""

    sbom_path: str
    sbom_size: int
    sbom_sha256: str
    archive_receipt_path: str
    archive_receipt_sha256: str
    transfer_manifest_path: str
    transfer_manifest_sha256: str
    archive_sha256: str
    source_commit: str
    source_tree: str
    generator_version: str


def produce_sbom_evidence(
    *,
    source_root: Path,
    input_root: Path,
    output_root: Path,
    archive_receipt_bytes: bytes,
    receipt_policy: Any,
    expectations: EvidenceExpectations,
    schema_path: Path = DEFAULT_SCHEMA,
) -> SbomEvidenceBinding:
    """Generate the SBOM twice and atomically publish a validated receipt."""
    expectations.validate()
    _validate_receipt_policy(receipt_policy, expectations)
    if not isinstance(archive_receipt_bytes, bytes):
        _fail("archive receipt must be immutable bytes")
    if not 0 < len(archive_receipt_bytes) <= MAX_INPUT_RECEIPT_BYTES:
        _fail("archive receipt is outside its byte bound")
    if _sha256(archive_receipt_bytes) != expectations.archive_receipt_sha256:
        _fail("archive receipt bytes do not match the caller expectation")

    source = _derive_source_identity(source_root)
    _require_expected_source(source, expectations)
    _require_tool_identity(expectations)
    electron_config_sha256, electron_archive_sha256 = _electron_identity(source_root)
    if electron_config_sha256 != expectations.electron_configuration_sha256:
        _fail("reviewed Electron configuration does not match caller policy")
    if electron_archive_sha256 != expectations.electron_archive_sha256:
        _fail(
            "reviewed Electron archive identity does not match the caller expectation"
        )
    transfer_bytes = _read_regular_bytes(
        input_root / "candidate-attestation-transfer-v1.json",
        MAX_TRANSFER_MANIFEST_BYTES,
        "candidate transfer manifest",
    )
    schema_bytes = _read_regular_bytes(schema_path, MAX_SBOM_BYTES, "SPDX schema")
    if _sha256(schema_bytes) != expectations.schema_sha256:
        _fail("SPDX schema does not match caller policy")

    destination = Path(os.path.abspath(output_root))
    if destination.exists() or destination.is_symlink():
        _fail("output root must not already exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        generator_identity = SBOM.ExpectedIdentity(
            source_commit=expectations.source_commit,
            source_tree=expectations.source_tree,
            source_archive_sha256=expectations.source_archive_sha256,
            source_date_epoch=expectations.source_date_epoch,
            archive_sha256=expectations.archive_sha256,
            electron_archive_sha256=expectations.electron_archive_sha256,
            generator_version=expectations.generator_version,
        )
        first = SBOM.build_desktop_sbom(input_root, schema_path, generator_identity)
        second = SBOM.build_desktop_sbom(input_root, schema_path, generator_identity)
        if not isinstance(first, bytes) or not isinstance(second, bytes):
            _fail("SBOM generator did not return bytes")
        if first != second:
            _fail("desktop SBOM generation is not byte deterministic")
        if not 0 < len(first) <= MAX_SBOM_BYTES:
            _fail("generated desktop SBOM is outside its byte bound")

        _require_expected_source(_derive_source_identity(source_root), expectations)
        _require_tool_identity(expectations)
        if (
            _read_regular_bytes(
                input_root / "candidate-attestation-transfer-v1.json",
                MAX_TRANSFER_MANIFEST_BYTES,
                "candidate transfer manifest",
            )
            != transfer_bytes
        ):
            _fail("candidate transfer manifest changed during SBOM generation")
        if (
            _read_regular_bytes(schema_path, MAX_SBOM_BYTES, "SPDX schema")
            != schema_bytes
        ):
            _fail("SPDX schema changed during SBOM generation")
        spdx = _decode_object(first, "generated SPDX document")
        output_summary = _spdx_summary(spdx, expectations)
        report = {
            "schema_version": 1,
            "check_id": receipt_policy.expected_check_id,
            "result": "pass",
            "source": {
                "commit": expectations.source_commit,
                "tree": expectations.source_tree,
                "archive_sha256": expectations.source_archive_sha256,
                "date_epoch": expectations.source_date_epoch,
            },
            "generator": {
                "program": GENERATOR_PROGRAM,
                "program_sha256": expectations.generator_program_sha256,
                "adapter_program_sha256": expectations.adapter_program_sha256,
                "receipt_reader_program_sha256": (
                    expectations.receipt_reader_program_sha256
                ),
                "version": expectations.generator_version,
                "schema": GENERATOR_SCHEMA,
                "schema_sha256": expectations.schema_sha256,
                "electron_configuration_sha256": electron_config_sha256,
                "electron_archive_sha256": expectations.electron_archive_sha256,
                "tool_commit": expectations.tool_commit,
                "tool_tree": expectations.tool_tree,
            },
            "inputs": {
                "archive_receipt": {
                    "path": ARCHIVE_RECEIPT_PATH,
                    "sha256": expectations.archive_receipt_sha256,
                },
                "candidate_transfer_manifest": {
                    "path": TRANSFER_MANIFEST_PATH,
                    "sha256": _sha256(transfer_bytes),
                },
                "desktop_archive_sha256": expectations.archive_sha256,
            },
            "output": {
                "path": SBOM_PATH,
                "size": len(first),
                "sha256": _sha256(first),
                **output_summary,
            },
            "stages": [{"name": name, "result": "pass"} for name in STAGE_NAMES],
            "scope": {
                "covered": "desktop-user-archive",
                "excluded": [
                    "separately-installed-core-wheel",
                    "optional-plugins",
                    "host-operating-system",
                    "rpm-package-set",
                ],
                "npm_integrity_meaning": "registry-distribution",
            },
        }
        report_bytes = _canonical_json(report)
        receipt = _receipt_document(
            receipt_policy,
            expectations,
            report_bytes=report_bytes,
            sbom_bytes=first,
            transfer_bytes=transfer_bytes,
        )
        _write_exclusive(temporary / REPORT_PATH, report_bytes)
        _write_exclusive(temporary / SBOM_PATH, first)
        _write_exclusive(temporary / ARCHIVE_RECEIPT_PATH, archive_receipt_bytes)
        _write_exclusive(temporary / TRANSFER_MANIFEST_PATH, transfer_bytes)
        _write_exclusive(temporary / RECEIPT_PATH, _canonical_json(receipt))
        binding = consume_sbom_evidence(
            evidence_root=temporary,
            receipt_path=temporary / RECEIPT_PATH,
            receipt_policy=receipt_policy,
            expectations=expectations,
            input_root=input_root,
            schema_path=schema_path,
        )
        os.replace(temporary, destination)
        return binding
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def consume_sbom_evidence(
    *,
    evidence_root: Path,
    receipt_path: Path,
    receipt_policy: Any,
    expectations: EvidenceExpectations,
    input_root: Path,
    schema_path: Path = DEFAULT_SCHEMA,
) -> SbomEvidenceBinding:
    """Bind receipt files and require the complete successful SBOM semantics."""
    expectations.validate()
    _validate_receipt_policy(receipt_policy, expectations)
    try:
        validation = RECEIPTS.validate_receipt_file(
            receipt_path,
            evidence_root=evidence_root,
            policy=receipt_policy,
        )
    except RECEIPTS.ReceiptValidationError as error:
        raise SbomEvidenceError("SBOM receipt binding is invalid") from error
    if validation.receipt.get("result") != "success":
        _fail("SBOM receipt does not record success")
    expected_files = {
        REPORT_PATH: "report",
        SBOM_PATH: "artifact",
        ARCHIVE_RECEIPT_PATH: "input",
        TRANSFER_MANIFEST_PATH: "input",
    }
    bound = {item.path: item for item in validation.bound_files}
    if set(bound) != set(expected_files) or any(
        bound[path].kind != kind for path, kind in expected_files.items()
    ):
        _fail("SBOM receipt file set is incomplete or unexpected")
    _require_receipt_roles(validation.receipt)
    try:
        report_bytes = RECEIPTS.read_bound_bytes(
            evidence_root, bound[REPORT_PATH], RECEIPTS.MAX_JSON_REPORT_BYTES
        )
        sbom_bytes = RECEIPTS.read_bound_bytes(
            evidence_root, bound[SBOM_PATH], MAX_SBOM_BYTES
        )
        archive_receipt_bytes = RECEIPTS.read_bound_bytes(
            evidence_root, bound[ARCHIVE_RECEIPT_PATH], MAX_INPUT_RECEIPT_BYTES
        )
        transfer_bytes = RECEIPTS.read_bound_bytes(
            evidence_root, bound[TRANSFER_MANIFEST_PATH], MAX_TRANSFER_MANIFEST_BYTES
        )
    except RECEIPTS.ReceiptValidationError as error:
        raise SbomEvidenceError(
            "SBOM bound evidence changed or exceeded its limit"
        ) from error
    if _sha256(archive_receipt_bytes) != expectations.archive_receipt_sha256:
        _fail("bound archive receipt does not match the caller expectation")

    report = _decode_object(report_bytes, "SBOM lifecycle report")
    spdx = _decode_object(sbom_bytes, "bound SPDX document")
    transfer = _decode_object(transfer_bytes, "bound candidate transfer manifest")
    _validate_report(report, receipt_policy, expectations, bound, spdx, transfer)
    _require_tool_identity(expectations)
    if (
        _sha256(_read_regular_bytes(schema_path, MAX_SBOM_BYTES, "SPDX schema"))
        != expectations.schema_sha256
    ):
        _fail("SPDX schema does not match caller policy")
    external_transfer = _read_regular_bytes(
        input_root / "candidate-attestation-transfer-v1.json",
        MAX_TRANSFER_MANIFEST_BYTES,
        "candidate transfer manifest",
    )
    if external_transfer != transfer_bytes:
        _fail("retained transfer manifest differs from its receipt-bound bytes")
    generator_identity = SBOM.ExpectedIdentity(
        source_commit=expectations.source_commit,
        source_tree=expectations.source_tree,
        source_archive_sha256=expectations.source_archive_sha256,
        source_date_epoch=expectations.source_date_epoch,
        archive_sha256=expectations.archive_sha256,
        electron_archive_sha256=expectations.electron_archive_sha256,
        generator_version=expectations.generator_version,
    )
    try:
        reproduced = SBOM.build_desktop_sbom(
            input_root, schema_path, generator_identity
        )
    except (OSError, SBOM.SbomBuildError) as error:
        raise SbomEvidenceError("SBOM semantic reproduction failed") from error
    if reproduced != sbom_bytes:
        _fail("bound SPDX bytes differ from exact semantic reproduction")
    if (
        _read_regular_bytes(
            input_root / "candidate-attestation-transfer-v1.json",
            MAX_TRANSFER_MANIFEST_BYTES,
            "candidate transfer manifest",
        )
        != transfer_bytes
    ):
        _fail("retained transfer manifest changed during semantic reproduction")
    _require_tool_identity(expectations)
    return SbomEvidenceBinding(
        sbom_path=SBOM_PATH,
        sbom_size=bound[SBOM_PATH].size,
        sbom_sha256=bound[SBOM_PATH].sha256,
        archive_receipt_path=ARCHIVE_RECEIPT_PATH,
        archive_receipt_sha256=bound[ARCHIVE_RECEIPT_PATH].sha256,
        transfer_manifest_path=TRANSFER_MANIFEST_PATH,
        transfer_manifest_sha256=bound[TRANSFER_MANIFEST_PATH].sha256,
        archive_sha256=expectations.archive_sha256,
        source_commit=expectations.source_commit,
        source_tree=expectations.source_tree,
        generator_version=expectations.generator_version,
    )


def _validate_report(
    report: Mapping[str, object],
    receipt_policy: Any,
    expectations: EvidenceExpectations,
    bound: Mapping[str, Any],
    spdx: Mapping[str, object],
    transfer: Mapping[str, object],
) -> None:
    _require_exact_keys(
        report,
        {
            "schema_version",
            "check_id",
            "result",
            "source",
            "generator",
            "inputs",
            "output",
            "stages",
            "scope",
        },
        "SBOM lifecycle report",
    )
    if (
        report["schema_version"] != 1
        or report["check_id"] != receipt_policy.expected_check_id
        or report["result"] != "pass"
    ):
        _fail("SBOM lifecycle report identity or result is invalid")
    source = _require_object(report["source"], "SBOM report source")
    if source != {
        "commit": expectations.source_commit,
        "tree": expectations.source_tree,
        "archive_sha256": expectations.source_archive_sha256,
        "date_epoch": expectations.source_date_epoch,
    }:
        _fail("SBOM lifecycle report source identity is stale")
    generator = _require_object(report["generator"], "SBOM report generator")
    _require_exact_keys(
        generator,
        {
            "program",
            "program_sha256",
            "adapter_program_sha256",
            "receipt_reader_program_sha256",
            "version",
            "schema",
            "schema_sha256",
            "electron_configuration_sha256",
            "electron_archive_sha256",
            "tool_commit",
            "tool_tree",
        },
        "SBOM report generator",
    )
    if (
        generator["program"] != GENERATOR_PROGRAM
        or generator["program_sha256"] != expectations.generator_program_sha256
        or generator["adapter_program_sha256"] != expectations.adapter_program_sha256
        or generator["receipt_reader_program_sha256"]
        != expectations.receipt_reader_program_sha256
        or generator["version"] != expectations.generator_version
        or generator["schema"] != GENERATOR_SCHEMA
        or generator["schema_sha256"] != expectations.schema_sha256
        or generator["electron_configuration_sha256"]
        != expectations.electron_configuration_sha256
        or generator["electron_archive_sha256"] != expectations.electron_archive_sha256
        or generator["tool_commit"] != expectations.tool_commit
        or generator["tool_tree"] != expectations.tool_tree
    ):
        _fail("SBOM lifecycle report generator identity is invalid")
    inputs = _require_object(report["inputs"], "SBOM report inputs")
    expected_inputs = {
        "archive_receipt": {
            "path": ARCHIVE_RECEIPT_PATH,
            "sha256": expectations.archive_receipt_sha256,
        },
        "candidate_transfer_manifest": {
            "path": TRANSFER_MANIFEST_PATH,
            "sha256": bound[TRANSFER_MANIFEST_PATH].sha256,
        },
        "desktop_archive_sha256": expectations.archive_sha256,
    }
    if inputs != expected_inputs:
        _fail("SBOM lifecycle report input bindings are invalid")
    _require_exact_keys(
        transfer,
        {"schema_version", "candidate", "source", "execution", "files", "subjects"},
        "candidate transfer manifest",
    )
    if transfer["schema_version"] != 1 or transfer["candidate"] != "UNPUBLISHED":
        _fail("bound candidate transfer marker is invalid")
    transfer_source = _require_object(transfer["source"], "candidate transfer source")
    if transfer_source != {
        "commit": expectations.source_commit,
        "tree": expectations.source_tree,
    }:
        _fail("bound candidate transfer source identity is stale")
    transfer_execution = _require_object(
        transfer["execution"], "candidate transfer execution"
    )
    if transfer_execution != {
        "repository": expectations.transfer_repository,
        "repository_id": expectations.transfer_repository_id,
        "repository_owner_id": expectations.transfer_repository_owner_id,
        "ref": expectations.transfer_ref,
        "event": expectations.transfer_event,
        "run_id": expectations.transfer_run_id,
        "run_attempt": expectations.transfer_run_attempt,
    }:
        _fail("bound candidate transfer execution identity is stale")
    output = _require_object(report["output"], "SBOM report output")
    summary = _spdx_summary(spdx, expectations)
    expected_output = {
        "path": SBOM_PATH,
        "size": bound[SBOM_PATH].size,
        "sha256": bound[SBOM_PATH].sha256,
        **summary,
    }
    if output != expected_output:
        _fail("SBOM lifecycle output binding or semantic summary is invalid")
    stages = report["stages"]
    if stages != [{"name": name, "result": "pass"} for name in STAGE_NAMES]:
        _fail("SBOM lifecycle stages are incomplete, failed, or unexpected")
    scope = _require_object(report["scope"], "SBOM report scope")
    if scope != {
        "covered": "desktop-user-archive",
        "excluded": [
            "separately-installed-core-wheel",
            "optional-plugins",
            "host-operating-system",
            "rpm-package-set",
        ],
        "npm_integrity_meaning": "registry-distribution",
    }:
        _fail("SBOM lifecycle scope is invalid")


def _spdx_summary(
    document: Mapping[str, object], expectations: EvidenceExpectations
) -> dict[str, object]:
    creation = _require_object(document.get("creationInfo"), "SPDX creation info")
    packages = document.get("packages")
    relationships = document.get("relationships")
    if not isinstance(packages, list) or not packages:
        _fail("SPDX package inventory is empty or invalid")
    if not isinstance(relationships, list) or not relationships:
        _fail("SPDX relationship inventory is empty or invalid")
    creators = creation.get("creators")
    expected_creator = f"Tool: tongs-desktop-sbom-{expectations.generator_version}"
    if (
        document.get("spdxVersion") != "SPDX-2.3"
        or document.get("dataLicense") != "CC0-1.0"
        or document.get("documentDescribes")
        != ["SPDXRef-Package-Tongs-Desktop-Archive"]
        or creators != [expected_creator]
    ):
        _fail("SPDX top-level identity is invalid")
    namespace = document.get("documentNamespace")
    created = creation.get("created")
    if not isinstance(namespace, str) or not namespace:
        _fail("SPDX document namespace is invalid")
    if not isinstance(created, str) or not created:
        _fail("SPDX creation timestamp is invalid")
    return {
        "spdx_version": "SPDX-2.3",
        "data_license": "CC0-1.0",
        "document_namespace": namespace,
        "created": created,
        "creator": expected_creator,
        "document_subject": "SPDXRef-Package-Tongs-Desktop-Archive",
        "package_count": len(packages),
        "relationship_count": len(relationships),
    }


def _derive_source_identity(source_root: Path) -> dict[str, object]:
    root = source_root.resolve(strict=True)
    if not root.is_dir() or source_root.is_symlink():
        _fail("source root must be a real directory")
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        _fail("source checkout must be clean")
    commit = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    tree = _git(root, "rev-parse", "HEAD^{tree}").decode("ascii").strip()
    epoch_text = (
        _git(root, "show", "-s", "--format=%ct", "HEAD").decode("ascii").strip()
    )
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix="tongs-sbom-source-", suffix=".tar"
    )
    os.close(temporary_fd)
    archive_path = Path(temporary_name)
    try:
        archive_path.unlink()
        _git(root, "archive", "--format=tar", f"--output={archive_path}", "HEAD")
        archive_sha256 = _stream_sha256(archive_path)
    finally:
        archive_path.unlink(missing_ok=True)
    try:
        epoch = int(epoch_text)
    except ValueError as error:
        raise SbomEvidenceError("source commit epoch is invalid") from error
    return {
        "commit": commit,
        "tree": tree,
        "archive_sha256": archive_sha256,
        "date_epoch": epoch,
    }


def _electron_identity(source_root: Path) -> tuple[str, str]:
    desktop_package = _decode_object(
        _read_regular_bytes(
            source_root / "desktop/package.json", MAX_SBOM_BYTES, "desktop package"
        ),
        "desktop package",
    )
    development = _require_object(
        desktop_package.get("devDependencies"), "desktop devDependencies"
    )
    version = development.get("electron")
    if (
        not isinstance(version, str)
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None
    ):
        _fail("desktop Electron version is invalid")
    configuration_path = (
        source_root
        / f"packaging/desktop/archive/electron-runtime-{version}-linux-x64.json"
    )
    configuration_bytes = _read_regular_bytes(
        configuration_path, MAX_SBOM_BYTES, "Electron configuration"
    )
    configuration = _decode_object(configuration_bytes, "Electron configuration")
    upstream = _require_object(
        configuration.get("upstream_archive"), "Electron upstream archive"
    )
    digest = upstream.get("sha256")
    if (
        configuration.get("electron_version") != version
        or not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
    ):
        _fail("Electron configuration identity is invalid")
    return _sha256(configuration_bytes), digest


def _require_expected_source(
    observed: Mapping[str, object], expectations: EvidenceExpectations
) -> None:
    expected = {
        "commit": expectations.source_commit,
        "tree": expectations.source_tree,
        "archive_sha256": expectations.source_archive_sha256,
        "date_epoch": expectations.source_date_epoch,
    }
    if observed != expected:
        _fail("source checkout identity does not match the caller expectation")


def _require_tool_identity(expectations: EvidenceExpectations) -> None:
    """Bind the trusted adapter tool checkout separately from subject source."""
    commit = _git(ROOT, "rev-parse", "HEAD").decode("ascii").strip()
    tree = _git(ROOT, "rev-parse", "HEAD^{tree}").decode("ascii").strip()
    adapter_sha256 = _sha256(
        _read_regular_bytes(ROOT / ADAPTER_PROGRAM, MAX_SBOM_BYTES, "SBOM adapter")
    )
    generator_sha256 = _sha256(
        _read_regular_bytes(ROOT / GENERATOR_PROGRAM, MAX_SBOM_BYTES, "SBOM generator")
    )
    receipt_reader_sha256 = _sha256(
        _read_regular_bytes(
            ROOT / RECEIPT_READER_PROGRAM,
            MAX_SBOM_BYTES,
            "desktop receipt reader",
        )
    )
    if (
        commit != expectations.tool_commit
        or tree != expectations.tool_tree
        or adapter_sha256 != expectations.adapter_program_sha256
        or generator_sha256 != expectations.generator_program_sha256
        or receipt_reader_sha256 != expectations.receipt_reader_program_sha256
    ):
        _fail("trusted SBOM tool identity does not match caller policy")


def _receipt_document(
    policy: Any,
    expectations: EvidenceExpectations,
    *,
    report_bytes: bytes,
    sbom_bytes: bytes,
    transfer_bytes: bytes,
) -> dict[str, object]:
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
        "artifacts": [
            {
                "path": SBOM_PATH,
                "size": len(sbom_bytes),
                "sha256": _sha256(sbom_bytes),
                "role": "desktop-archive-sbom",
            }
        ],
        "inputs": [
            {
                "path": ARCHIVE_RECEIPT_PATH,
                "sha256": expectations.archive_receipt_sha256,
            },
            {"path": TRANSFER_MANIFEST_PATH, "sha256": _sha256(transfer_bytes)},
        ],
    }


def _require_receipt_roles(receipt: Mapping[str, object]) -> None:
    reports = receipt.get("reports")
    artifacts = receipt.get("artifacts")
    inputs = receipt.get("inputs")
    if not isinstance(reports, list) or reports != [
        {
            "path": REPORT_PATH,
            "size": reports[0].get("size")
            if reports and isinstance(reports[0], dict)
            else None,
            "sha256": reports[0].get("sha256")
            if reports and isinstance(reports[0], dict)
            else None,
            "format": RECEIPTS.ARTIFACT_LIFECYCLE_FORMAT,
        }
    ]:
        _fail("SBOM receipt report declaration is invalid")
    if (
        not isinstance(artifacts, list)
        or len(artifacts) != 1
        or artifacts[0].get("path") != SBOM_PATH
        or artifacts[0].get("role") != "desktop-archive-sbom"
    ):
        _fail("SBOM receipt artifact role is invalid")
    if not isinstance(inputs, list) or [item.get("path") for item in inputs] != [
        ARCHIVE_RECEIPT_PATH,
        TRANSFER_MANIFEST_PATH,
    ]:
        _fail("SBOM receipt input declarations are invalid")


def _validate_receipt_policy(policy: Any, expectations: EvidenceExpectations) -> None:
    if not isinstance(policy, RECEIPTS.ReceiptPolicy):
        _fail("receipt policy must use the existing validated policy type")
    if (
        policy.expected_commit != expectations.source_commit
        or policy.expected_tree != expectations.source_tree
    ):
        _fail("receipt policy source identity differs from SBOM expectations")
    if policy.allowed_report_formats != frozenset({RECEIPTS.ARTIFACT_LIFECYCLE_FORMAT}):
        _fail("receipt policy must allow only artifact-lifecycle-v1")


def _git(root: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=False,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SbomEvidenceError("unable to inspect exact source checkout") from error
    if completed.returncode != 0 or len(completed.stderr) > 64 * 1024:
        _fail("source checkout inspection failed")
    return completed.stdout


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
        raise SbomEvidenceError(f"unable to read {label}") from error
    if len(value) > maximum or (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        _fail(f"{label} changed during its bounded read")
    return value


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


def _decode_object(document: bytes, label: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
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
    except SbomEvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise SbomEvidenceError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        _fail(f"{label} root must be an object")
    return value


def _require_object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _require_exact_keys(
    value: Mapping[str, object], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        _fail(f"{label} fields are incomplete or unexpected")


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fail(message: str) -> NoReturn:
    raise SbomEvidenceError(message)


def _expectations(arguments: argparse.Namespace) -> EvidenceExpectations:
    return EvidenceExpectations(
        source_commit=arguments.expected_source_commit,
        source_tree=arguments.expected_source_tree,
        source_archive_sha256=arguments.expected_source_archive_sha256,
        source_date_epoch=arguments.expected_source_date_epoch,
        archive_sha256=arguments.expected_archive_sha256,
        electron_archive_sha256=arguments.expected_electron_archive_sha256,
        generator_version=arguments.generator_version,
        tool_commit=arguments.expected_tool_commit,
        tool_tree=arguments.expected_tool_tree,
        adapter_program_sha256=arguments.expected_adapter_program_sha256,
        generator_program_sha256=arguments.expected_generator_program_sha256,
        receipt_reader_program_sha256=(
            arguments.expected_receipt_reader_program_sha256
        ),
        schema_sha256=arguments.expected_schema_sha256,
        electron_configuration_sha256=(
            arguments.expected_electron_configuration_sha256
        ),
        transfer_repository=arguments.expected_transfer_repository,
        transfer_repository_id=arguments.expected_transfer_repository_id,
        transfer_repository_owner_id=(arguments.expected_transfer_repository_owner_id),
        transfer_ref=arguments.expected_transfer_ref,
        transfer_event=arguments.expected_transfer_event,
        transfer_run_id=arguments.expected_transfer_run_id,
        transfer_run_attempt=arguments.expected_transfer_run_attempt,
        archive_receipt_sha256=arguments.expected_archive_receipt_sha256,
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
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--expected-source-tree", required=True)
    parser.add_argument("--expected-source-archive-sha256", required=True)
    parser.add_argument("--expected-source-date-epoch", required=True, type=int)
    parser.add_argument("--expected-archive-sha256", required=True)
    parser.add_argument("--expected-electron-archive-sha256", required=True)
    parser.add_argument("--expected-archive-receipt-sha256", required=True)
    parser.add_argument("--generator-version", required=True)
    parser.add_argument("--expected-tool-commit", required=True)
    parser.add_argument("--expected-tool-tree", required=True)
    parser.add_argument("--expected-adapter-program-sha256", required=True)
    parser.add_argument("--expected-generator-program-sha256", required=True)
    parser.add_argument("--expected-receipt-reader-program-sha256", required=True)
    parser.add_argument("--expected-schema-sha256", required=True)
    parser.add_argument("--expected-electron-configuration-sha256", required=True)
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
        "produce", help="generate and receipt one archive SBOM"
    )
    _common_arguments(produce)
    produce.add_argument("--source-root", required=True, type=Path)
    produce.add_argument("--input-root", required=True, type=Path)
    produce.add_argument("--output-root", required=True, type=Path)
    produce.add_argument("--archive-receipt", required=True, type=Path)
    produce.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    consume = commands.add_parser(
        "consume", help="validate one SBOM receipt and semantic result"
    )
    _common_arguments(consume)
    consume.add_argument("--evidence-root", required=True, type=Path)
    consume.add_argument("--receipt", required=True, type=Path)
    consume.add_argument("--input-root", required=True, type=Path)
    consume.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the production or semantic consumer path, failing closed."""
    arguments = _parser().parse_args(argv)
    try:
        expectations = _expectations(arguments)
        policy = _policy(arguments)
        if arguments.command == "produce":
            archive_receipt = _read_regular_bytes(
                arguments.archive_receipt,
                MAX_INPUT_RECEIPT_BYTES,
                "archive receipt",
            )
            binding = produce_sbom_evidence(
                source_root=arguments.source_root,
                input_root=arguments.input_root,
                output_root=arguments.output_root,
                archive_receipt_bytes=archive_receipt,
                receipt_policy=policy,
                expectations=expectations,
                schema_path=arguments.schema,
            )
        else:
            binding = consume_sbom_evidence(
                evidence_root=arguments.evidence_root,
                receipt_path=arguments.receipt,
                receipt_policy=policy,
                expectations=expectations,
                input_root=arguments.input_root,
                schema_path=arguments.schema,
            )
    except (
        OSError,
        SbomEvidenceError,
        SBOM.SbomBuildError,
        RECEIPTS.ReceiptValidationError,
    ) as error:
        print(f"desktop SBOM evidence failed: {error}", file=sys.stderr)
        return 1
    print(
        "desktop SBOM evidence passed: "
        f"path={binding.sbom_path} sha256={binding.sbom_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
