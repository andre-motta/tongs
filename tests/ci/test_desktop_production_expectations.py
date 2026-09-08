"""Prove the derived expectations match what the reviewed adapters accept.

The archive and SBOM adapters each require more than twenty caller-owned
expectations.  A missing or misnamed flag would surface only as a hosted job
failure, so these cases parse the derived argument vectors with the adapters'
own parsers and compare the derived values with the checked-in source.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import pytest

from tests.ci.desktop_production_expectations import (
    ARCHIVE,
    ARCHIVE_ADAPTER_PROGRAM,
    DESKTOP_RELEASE_VERSION,
    RECEIPT_READER_PROGRAM,
    ROOT,
    SBOM,
    SBOM_GENERATOR_VERSION,
    USER_ARCHIVE_ARTIFACT_ID,
    ExpectationError,
    archive_evidence_argv,
    artifact_contract_digest,
    electron_identity,
    sbom_evidence_argv,
)

COMMIT = "1" * 40
TREE = "2" * 40
SOURCE_ARCHIVE = "3" * 64
ARCHIVE_DIGEST = "4" * 64
RELEASE_MANIFEST = "5" * 64
INSTALL_MANIFEST = "6" * 64
LICENSE_INVENTORY = "7" * 64

WORKFLOW_ENVIRONMENT = {
    "GITHUB_REPOSITORY_ID": "1305350434",
    "GITHUB_REPOSITORY_OWNER_ID": "30708955",
    "GITHUB_REF": "refs/pull/149/merge",
    "GITHUB_EVENT_NAME": "pull_request",
}


@pytest.fixture(autouse=True)
def workflow_context(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in WORKFLOW_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


@pytest.fixture()
def transfer_root(tmp_path: Path) -> Path:
    """Create the one release manifest the argument builder reads."""

    root = tmp_path / "transfer"
    (root / "archive").mkdir(parents=True)
    (root / "archive/desktop-manifest-v1.json").write_text(
        '{"artifacts": [{"name": "tongs-desktop-0.5.0-fedora44-x86_64.tar.gz"}]}'
    )
    return root


def _namespace(transfer_root: Path, tmp_path: Path, **overrides: object):
    values: dict[str, object] = {
        "mode": "produce",
        "source_root": ROOT,
        "transfer_root": transfer_root,
        "output_root": tmp_path / "gate",
        "evidence_root": None,
        "commit": COMMIT,
        "tree": TREE,
        "source_archive_sha256": SOURCE_ARCHIVE,
        "source_date_epoch": 1757000000,
        "archive_sha256": ARCHIVE_DIGEST,
        "repository": "andre-motta/tongs",
        "run_id": "34274245440",
        "attempt": 1,
        "environment": "github-hosted-ubuntu-24.04",
        "provenance": "hosted",
        "check_id": "desktop-archive-lifecycle",
        "release_manifest_sha256": RELEASE_MANIFEST,
        "install_manifest_sha256": INSTALL_MANIFEST,
        "license_inventory_sha256": LICENSE_INVENTORY,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _parsed(module, argv: list[str]) -> argparse.Namespace:
    return module._parser().parse_args(argv)


def test_archive_arguments_parse_with_the_adapter_parser(
    transfer_root: Path, tmp_path: Path
) -> None:
    argv = archive_evidence_argv(_namespace(transfer_root, tmp_path))
    parsed = _parsed(ARCHIVE, argv)
    assert parsed.command == "produce"
    assert parsed.expected_source_commit == COMMIT
    assert parsed.expected_source_tree == TREE
    assert parsed.expected_archive_name == ARCHIVE.ARCHIVE_NAME
    assert parsed.expected_archive_artifact_id == USER_ARCHIVE_ARTIFACT_ID
    assert parsed.expected_release_version == DESKTOP_RELEASE_VERSION
    assert parsed.expected_transfer_event == "pull_request"
    assert parsed.expected_transfer_ref == "refs/pull/149/merge"
    assert parsed.check_id == "desktop-archive-lifecycle"
    assert parsed.output_root == tmp_path / "gate"


def test_archive_consume_arguments_name_the_published_receipt(
    transfer_root: Path, tmp_path: Path
) -> None:
    argv = archive_evidence_argv(
        _namespace(
            transfer_root,
            tmp_path,
            mode="consume",
            evidence_root=tmp_path / "gate",
            output_root=None,
        )
    )
    parsed = _parsed(ARCHIVE, argv)
    assert parsed.command == "consume"
    assert parsed.receipt == tmp_path / "gate" / ARCHIVE.RECEIPT_PATH


def test_archive_arguments_build_a_valid_expectation_object(
    transfer_root: Path, tmp_path: Path
) -> None:
    """The adapter validates caller policy before reading producer evidence."""

    parsed = _parsed(
        ARCHIVE, archive_evidence_argv(_namespace(transfer_root, tmp_path))
    )
    expectations = ARCHIVE.ArchiveEvidenceExpectations(
        source_commit=parsed.expected_source_commit,
        source_tree=parsed.expected_source_tree,
        source_archive_sha256=parsed.expected_source_archive_sha256,
        source_date_epoch=parsed.expected_source_date_epoch,
        archive_name=parsed.expected_archive_name,
        archive_artifact_id=parsed.expected_archive_artifact_id,
        archive_sha256=parsed.expected_archive_sha256,
        release_version=parsed.expected_release_version,
        release_manifest_sha256=parsed.expected_release_manifest_sha256,
        install_manifest_sha256=parsed.expected_install_manifest_sha256,
        license_inventory_sha256=parsed.expected_license_inventory_sha256,
        electron_version=parsed.expected_electron_version,
        electron_configuration_sha256=parsed.expected_electron_configuration_sha256,
        electron_archive_sha256=parsed.expected_electron_archive_sha256,
        adapter_program_sha256=parsed.expected_adapter_program_sha256,
        transfer_validator_program_sha256=(
            parsed.expected_transfer_validator_program_sha256
        ),
        receipt_reader_program_sha256=parsed.expected_receipt_reader_program_sha256,
        artifact_contract_sha256=parsed.expected_artifact_contract_sha256,
        transfer_repository=parsed.expected_transfer_repository,
        transfer_repository_id=parsed.expected_transfer_repository_id,
        transfer_repository_owner_id=parsed.expected_transfer_repository_owner_id,
        transfer_ref=parsed.expected_transfer_ref,
        transfer_event=parsed.expected_transfer_event,
        transfer_run_id=parsed.expected_transfer_run_id,
        transfer_run_attempt=parsed.expected_transfer_run_attempt,
    )
    expectations.validate()


def test_sbom_arguments_parse_with_the_adapter_parser(
    transfer_root: Path, tmp_path: Path
) -> None:
    receipt = tmp_path / "archive-receipt.json"
    receipt.write_bytes(b'{"schema_version": 1}\n')
    argv = sbom_evidence_argv(
        _namespace(
            transfer_root,
            tmp_path,
            check_id="desktop-archive-sbom",
            archive_receipt=receipt,
        )
    )
    parsed = _parsed(SBOM, argv)
    assert parsed.command == "produce"
    assert parsed.generator_version == SBOM_GENERATOR_VERSION
    assert parsed.expected_tool_commit == COMMIT
    assert parsed.expected_tool_tree == TREE
    assert parsed.input_root == transfer_root
    assert parsed.expected_archive_receipt_sha256 == (
        hashlib.sha256(receipt.read_bytes()).hexdigest()
    )


def test_sbom_arguments_build_a_valid_expectation_object(
    transfer_root: Path, tmp_path: Path
) -> None:
    receipt = tmp_path / "archive-receipt.json"
    receipt.write_bytes(b"{}\n")
    parsed = _parsed(
        SBOM,
        sbom_evidence_argv(
            _namespace(
                transfer_root,
                tmp_path,
                check_id="desktop-archive-sbom",
                archive_receipt=receipt,
            )
        ),
    )
    SBOM._expectations(parsed).validate()


def test_electron_identity_matches_the_checked_in_configuration() -> None:
    version, configuration_digest, archive_digest = electron_identity(ROOT)
    assert version == ARCHIVE.ELECTRON_VERSION
    configuration = (
        ROOT / "packaging/desktop/archive" / ARCHIVE.ELECTRON_CONFIGURATION_NAME
    )
    assert (
        hashlib.sha256(configuration.read_bytes()).hexdigest() == configuration_digest
    )
    assert len(archive_digest) == 64


def test_artifact_contract_digest_matches_the_adapter_helper() -> None:
    assert artifact_contract_digest(ROOT) == ARCHIVE._directory_digest(
        ROOT / "src/tongs/desktop/artifact_contract"
    )


def test_tool_digests_match_the_checked_in_programs(
    transfer_root: Path, tmp_path: Path
) -> None:
    parsed = _parsed(
        ARCHIVE, archive_evidence_argv(_namespace(transfer_root, tmp_path))
    )
    for program, derived in (
        (ARCHIVE_ADAPTER_PROGRAM, parsed.expected_adapter_program_sha256),
        (RECEIPT_READER_PROGRAM, parsed.expected_receipt_reader_program_sha256),
    ):
        assert hashlib.sha256((ROOT / program).read_bytes()).hexdigest() == derived


def test_rejects_a_transfer_without_a_usable_release_manifest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "transfer"
    (root / "archive").mkdir(parents=True)
    (root / "archive/desktop-manifest-v1.json").write_text('{"artifacts": []}')
    with pytest.raises(ExpectationError, match="one artifact"):
        archive_evidence_argv(_namespace(root, tmp_path))


def test_rejects_a_missing_workflow_context_variable(
    transfer_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_REF")
    with pytest.raises(ExpectationError, match="GITHUB_REF"):
        archive_evidence_argv(_namespace(transfer_root, tmp_path))
