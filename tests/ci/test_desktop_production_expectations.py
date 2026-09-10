"""Prove the derived expectations match what the reviewed adapters accept.

The archive and SBOM adapters each require more than twenty caller-owned
expectations.  A missing or misnamed flag would surface only as a hosted job
failure, so these cases parse the derived argument vectors with the adapters'
own parsers and compare the derived values with the checked-in source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ci.desktop_production_expectations import (
    ARCHIVE_ADAPTER_PROGRAM,
    DESKTOP_RELEASE_VERSION,
    RECEIPT_READER_PROGRAM,
    ROOT,
    SBOM_GENERATOR_VERSION,
    USER_ARCHIVE_ARTIFACT_ID,
    ExpectationError,
    archive_adapter,
    archive_evidence_argv,
    artifact_contract_digest,
    electron_identity,
    sbom_adapter,
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
    parsed = _parsed(archive_adapter(), argv)
    assert parsed.command == "produce"
    assert parsed.expected_source_commit == COMMIT
    assert parsed.expected_source_tree == TREE
    assert parsed.expected_archive_name == archive_adapter().ARCHIVE_NAME
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
    parsed = _parsed(archive_adapter(), argv)
    assert parsed.command == "consume"
    assert parsed.receipt == tmp_path / "gate" / archive_adapter().RECEIPT_PATH


def test_archive_arguments_build_a_valid_expectation_object(
    transfer_root: Path, tmp_path: Path
) -> None:
    """The adapter validates caller policy before reading producer evidence."""

    parsed = _parsed(
        archive_adapter(),
        archive_evidence_argv(_namespace(transfer_root, tmp_path)),
    )
    expectations = archive_adapter().ArchiveEvidenceExpectations(
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
    parsed = _parsed(sbom_adapter(), argv)
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
        sbom_adapter(),
        sbom_evidence_argv(
            _namespace(
                transfer_root,
                tmp_path,
                check_id="desktop-archive-sbom",
                archive_receipt=receipt,
            )
        ),
    )
    sbom_adapter()._expectations(parsed).validate()


def test_electron_identity_matches_the_checked_in_configuration() -> None:
    version, configuration_digest, archive_digest = electron_identity(ROOT)
    assert version == archive_adapter().ELECTRON_VERSION
    configuration = (
        ROOT
        / "packaging/desktop/archive"
        / archive_adapter().ELECTRON_CONFIGURATION_NAME
    )
    assert (
        hashlib.sha256(configuration.read_bytes()).hexdigest() == configuration_digest
    )
    assert len(archive_digest) == 64


def test_artifact_contract_digest_matches_the_adapter_helper() -> None:
    assert artifact_contract_digest(ROOT) == archive_adapter()._directory_digest(
        ROOT / "src/tongs/desktop/artifact_contract"
    )


def test_tool_digests_match_the_checked_in_programs(
    transfer_root: Path, tmp_path: Path
) -> None:
    parsed = _parsed(
        archive_adapter(),
        archive_evidence_argv(_namespace(transfer_root, tmp_path)),
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


def test_reviewed_constants_still_match_the_producer_literals() -> None:
    """Guard the caller-owned constants against a silent producer change.

    The producer declares the same release version and artifact identifier.
    Holding them here is what makes the consumer expectation independent, so
    this case exists to fail loudly when the producer moves and force a
    deliberate update rather than letting the two drift apart.
    """

    producer = (ROOT / "scripts/build_desktop_archive.py").read_text()
    assert f'"artifact_id": "{USER_ARCHIVE_ARTIFACT_ID}"' in producer
    assert f'"{USER_ARCHIVE_ARTIFACT_ID}"' in producer

    hosted = (ROOT / "packaging/desktop/archive/run_hosted.sh").read_text()
    assert (
        f"release_version=${{TONGS_RELEASE_VERSION:-{DESKTOP_RELEASE_VERSION}}}\n"
        in hosted
    )

    contract = json.loads((ROOT / "packaging/rpm/desktop/manifest.json").read_text())
    assert contract["accepted_desktop"]["release_version"] == DESKTOP_RELEASE_VERSION

    # The trusted signing workflow hardcodes the Electron version to locate the
    # reviewed runtime configuration.  Nothing else guards that copy, so a bump
    # of desktop/package.json without it would only fail at file-open time.
    version, _, _ = electron_identity(ROOT)
    release = (ROOT / ".github/workflows/release-desktop.yml").read_text()
    assert f'ELECTRON_VERSION: "{version}"' in release


#: Module names the SBOM chain registers.  ``sbom_evidence`` and the SPDX
#: generator are loaded by path under these names, so checking ``sys.modules``
#: for them is what detects an accidental archive-to-SBOM dependency.
SBOM_CHAIN_MODULES = (
    "production_sbom_evidence",
    "desktop_sbom_evidence_generator",
    "jsonschema",
)

_ISOLATION_PROBE = """
import sys, json, types

class _Blocked:
    def find_module(self, name, path=None):
        return None
    def find_spec(self, name, path=None, target=None):
        if name == "jsonschema" or name.startswith("jsonschema."):
            raise ImportError("jsonschema is deliberately absent")
        return None

sys.meta_path.insert(0, _Blocked())
sys.path.insert(0, {root!r})

from tests.ci.desktop_production_expectations import archive_evidence_argv

class N:
    pass

n = N()
for key, value in json.loads({payload!r}).items():
    setattr(n, key, value)
argv = archive_evidence_argv(n)
print(json.dumps({{
    "argv": argv,
    "loaded": [name for name in {chain!r} if name in sys.modules],
}}))
"""


def test_archive_expectations_never_import_the_sbom_chain(
    transfer_root: Path, tmp_path: Path
) -> None:
    """The archive receipt path must not depend on the SBOM toolchain.

    Issue #139 fixed the direction: the SBOM consumes the archive receipt, not
    the other way round.  Importing the adapters eagerly broke that and made
    the archive job die on ``jsonschema`` before doing any work.  This runs the
    archive expectations in a subprocess where importing ``jsonschema`` raises,
    which is exactly the hosted condition.
    """

    namespace = _namespace(transfer_root, tmp_path)
    payload = json.dumps(
        {
            key: (str(value) if isinstance(value, Path) else value)
            for key, value in vars(namespace).items()
        }
    )
    script = _ISOLATION_PROBE.format(
        root=str(ROOT), payload=payload, chain=SBOM_CHAIN_MODULES
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["loaded"] == []
    assert "--expected-archive-name" in result["argv"]


def test_the_sbom_command_still_loads_its_own_adapter() -> None:
    """The lazy loader must still return the reviewed #135 module."""

    module = sbom_adapter()
    assert module.__file__ is not None
    assert module.__file__.endswith("tests/integration/desktop/sbom_evidence.py")
    assert callable(module.produce_sbom_evidence)
    archive = archive_adapter()
    assert archive.__file__ is not None
    assert archive.__file__.endswith("tests/integration/desktop/archive_evidence.py")
