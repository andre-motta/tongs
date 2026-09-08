"""Failure-oriented checks for the unpublished candidate attestation harness."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from sigstore.errors import VerificationError
from sigstore.models import Bundle

from scripts.build_desktop_archive import (
    BuildParameters,
    build_contract_documents,
    canonical_json,
)
from tests.integration.desktop import candidate_attestation as candidate
from tongs.desktop.installer.models import InstallerLimits

SOURCE_COMMIT = "a" * 40
SOURCE_TREE = "b" * 40
REF = "refs/heads/feat/desktop-120-candidate-attestation"


def _identity() -> candidate.CandidateIdentity:
    return candidate.CandidateIdentity.official(
        repository="andre-motta/tongs",
        repository_id="1305350434",
        repository_owner_id="30708955",
        ref=REF,
        source_commit=SOURCE_COMMIT,
        source_tree=SOURCE_TREE,
        event="push",
        run_id="123456",
        run_attempt=1,
    )


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_output(root: Path) -> Path:
    archive_root = root / "archive"
    evidence_root = root / "evidence"
    archive_root.mkdir(parents=True)
    evidence_root.mkdir()
    license_inventory = canonical_json(
        {
            "schema_version": 1,
            "components": [
                {
                    "name": "fixture-runtime",
                    "version": "1.0.0",
                    "license": "MIT",
                    "license_paths": ["runtime/licenses/fixture/LICENSE"],
                }
            ],
        }
    )
    payload = {
        "runtime/tongs-desktop": (b"launcher", 0o755),
        "runtime/resources/app.asar": (b"asar", 0o644),
        "runtime/LICENSES.json": (license_inventory, 0o644),
        "runtime/licenses/fixture/LICENSE": (b"fixture license\n", 0o644),
    }
    contract = json.loads(
        (
            Path(__file__).parents[3] / "packaging/desktop/archive/contract.json"
        ).read_text()
    )
    built = build_contract_documents(
        payload,
        BuildParameters(
            release_version="0.5.0",
            core_minimum="0.4.2-dev.183",
            core_maximum_exclusive="0.5.0",
            source_commit=SOURCE_COMMIT,
            source_date_epoch=1_700_000_000,
        ),
        contract,
    )
    output_documents = {
        built.archive_name: built.archive,
        "desktop-install.json": built.install_manifest,
        "desktop-manifest-v1.json": built.release_manifest,
        "app-asar-inventory.json": canonical_json(
            {"schema_version": 1, "files": [{"path": "dist/app.js"}]}
        ),
        "runtime-inventory.json": canonical_json(
            {
                "schema_version": 1,
                "files": [
                    {"path": name, "mode": mode} for name, (_, mode) in payload.items()
                ],
            }
        ),
        "prepared-source-inventory.json": canonical_json(
            {
                "schema_version": 1,
                "source_commit": SOURCE_COMMIT,
                "files": [{"path": "desktop/package.json"}],
                "npm_packages": [{"path": "node_modules/react"}],
            }
        ),
        "license-inventory.json": license_inventory,
        "build-provenance.json": canonical_json(
            {
                "schema_version": 1,
                "candidate": "UNPUBLISHED",
                "source_commit": SOURCE_COMMIT,
                "outputs": {"archive": {"name": built.archive_name}},
            }
        ),
    }
    for name, document in output_documents.items():
        (archive_root / name).write_bytes(document)
    checksums = "".join(
        f"{_digest(document)}  {name}\n"
        for name, document in sorted(output_documents.items())
    )
    (archive_root / "SHA256SUMS").write_text(checksums, encoding="ascii")

    evidence = {
        "builder-image.json": b"{}\n",
        "build-a.sha256": b"identical build\n",
        "build-b.sha256": b"identical build\n",
        "electron-v44.2.0-linux-x64.zip": b"electron fixture",
        "inputs.env": f"TONGS_HEAD_SHA={SOURCE_COMMIT}\n".encode(),
        "rpm-nevra.txt": b"fixture-1.0-1.x86_64\n",
        "rpm-sha256-check.txt": b"fixture OK\n",
        "rpm-signatures.txt": b"fixture signature\n",
        "source.tar": b"exact source archive",
        "toolchain.txt": b"fixture toolchain\n",
    }
    for name, document in evidence.items():
        (evidence_root / name).write_bytes(document)
    expected_source = root.parent / "expected-source.tar"
    expected_source.write_bytes(evidence["source.tar"])
    return expected_source


def _statement(
    identity: candidate.CandidateIdentity, subjects: dict[str, str]
) -> bytes:
    return canonical_json(
        {
            "_type": "https://in-toto.io/Statement/v1",
            "subject": [
                {"name": name, "digest": {"sha256": digest}}
                for name, digest in subjects.items()
            ],
            "predicateType": "https://slsa.dev/provenance/v1",
            "predicate": {
                "buildDefinition": {
                    "buildType": "https://actions.github.io/buildtypes/workflow/v1",
                    "externalParameters": {
                        "workflow": {
                            "ref": identity.ref,
                            "repository": identity.repository_url,
                            "path": identity.workflow_path,
                        }
                    },
                    "internalParameters": {
                        "github": {
                            "event_name": identity.event,
                            "repository_id": identity.repository_id,
                            "repository_owner_id": identity.repository_owner_id,
                            "runner_environment": identity.runner_environment,
                        }
                    },
                    "resolvedDependencies": [
                        {
                            "uri": f"git+{identity.repository_url}@{identity.ref}",
                            "digest": {"gitCommit": identity.source_commit},
                        }
                    ],
                },
                "runDetails": {
                    "builder": {"id": identity.builder_id},
                    "metadata": {
                        "invocationId": (
                            f"{identity.repository_url}/actions/runs/"
                            f"{identity.run_id}/attempts/{identity.run_attempt}"
                        )
                    },
                },
            },
        }
    )


class _PolicyAwareVerifier:
    def __init__(self, identity: candidate.CandidateIdentity, payload: bytes) -> None:
        self.identity = identity
        self.payload = payload
        self.positive_calls = 0
        self.rejected_calls = 0

    def verify_dsse(self, bundle: Bundle, policy: object) -> tuple[str, bytes]:
        del bundle
        values = {
            (type(child).__name__, getattr(child, "_value", None))
            for child in policy._children
        }
        expected = {
            ("OIDCIssuerV2", self.identity.issuer),
            ("OIDCRunnerEnvironment", self.identity.runner_environment),
            ("OIDCSourceRepositoryURI", self.identity.repository_url),
            ("OIDCSourceRepositoryIdentifier", self.identity.repository_id),
            (
                "OIDCSourceRepositoryOwnerIdentifier",
                self.identity.repository_owner_id,
            ),
            ("OIDCSourceRepositoryDigest", self.identity.source_commit),
            ("OIDCSourceRepositoryRef", self.identity.ref),
            ("OIDCBuildSignerURI", self.identity.builder_id),
            ("OIDCBuildConfigURI", self.identity.builder_id),
            ("OIDCBuildConfigDigest", self.identity.source_commit),
            ("OIDCBuildTrigger", self.identity.event),
        }
        if expected <= values:
            self.positive_calls += 1
            return "application/vnd.in-toto+json", self.payload
        self.rejected_calls += 1
        raise VerificationError("controlled identity mismatch")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "someone/tongs"),
        ("repository_id", "1"),
        ("repository_owner_id", "1"),
        ("ref", "refs/heads/untrusted"),
        ("event", "pull_request"),
        ("source_commit", "short"),
        ("source_tree", "short"),
        ("run_id", "0"),
        ("run_attempt", 0),
    ],
)
def test_official_identity_rejects_every_untrusted_boundary(
    field: str, value: str | int
) -> None:
    values = {
        "repository": "andre-motta/tongs",
        "repository_id": "1305350434",
        "repository_owner_id": "30708955",
        "ref": REF,
        "source_commit": SOURCE_COMMIT,
        "source_tree": SOURCE_TREE,
        "event": "push",
        "run_id": "123456",
        "run_attempt": 1,
    }
    values[field] = value

    with pytest.raises(candidate.CandidateAttestationError):
        candidate.CandidateIdentity.official(**values)  # type: ignore[arg-type]


def test_transfer_manifest_binds_exact_paths_source_and_subjects(
    tmp_path: Path,
) -> None:
    root = tmp_path / "candidate"
    source = _write_output(root)
    identity = _identity()

    prepared = candidate.prepare_transfer_manifest(
        root, root / candidate.TRANSFER_MANIFEST_NAME, identity, source
    )
    validated = candidate.validate_transfer_manifest(
        root, root / candidate.TRANSFER_MANIFEST_NAME, identity, source
    )

    assert validated == prepared
    assert {item["name"] for item in validated["subjects"]} == {
        "desktop-manifest-v1.json",
        candidate.CANDIDATE_ARCHIVE_NAME,
    }
    assert len(validated["files"]) == len(candidate._EXPECTED_TRANSFER_FILES)


@pytest.mark.parametrize("mutation", ["extra", "missing", "bytes", "source"])
def test_transfer_rejects_path_byte_and_source_mutation(
    tmp_path: Path, mutation: str
) -> None:
    root = tmp_path / "candidate"
    source = _write_output(root)
    identity = _identity()
    manifest = root / candidate.TRANSFER_MANIFEST_NAME
    candidate.prepare_transfer_manifest(root, manifest, identity, source)
    if mutation == "extra":
        (root / "unexpected.txt").write_text("unexpected")
    elif mutation == "missing":
        (root / "evidence/toolchain.txt").unlink()
    elif mutation == "bytes":
        (root / "evidence/toolchain.txt").write_text("changed")
    else:
        source.write_text("wrong source")

    with pytest.raises(candidate.CandidateAttestationError):
        candidate.validate_transfer_manifest(root, manifest, identity, source)


def test_transfer_rejects_stale_identity_and_symlink(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    source = _write_output(root)
    identity = _identity()
    manifest = root / candidate.TRANSFER_MANIFEST_NAME
    candidate.prepare_transfer_manifest(root, manifest, identity, source)

    with pytest.raises(candidate.CandidateAttestationError, match="stale"):
        candidate.validate_transfer_manifest(
            root,
            manifest,
            replace(identity, source_tree="c" * 40),
            source,
        )
    toolchain = root / "evidence/toolchain.txt"
    toolchain.unlink()
    toolchain.symlink_to(root / "evidence/inputs.env")
    with pytest.raises(candidate.CandidateAttestationError, match="non-regular"):
        candidate.validate_transfer_manifest(root, manifest, identity, source)


def test_corrupt_subject_bytes_are_actually_rejected() -> None:
    original = b"actual subject"
    subjects = {"artifact.tar.gz": _digest(original)}

    candidate._validate_subject_bytes("artifact.tar.gz", original, subjects)
    with pytest.raises(candidate.CandidateAttestationError, match="signed digest"):
        candidate._validate_subject_bytes(
            "artifact.tar.gz", original + b"corruption", subjects
        )


def test_archive_reader_rejects_oversized_subject_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "oversized.tar.gz"
    archive.write_bytes(b"small")
    original_identity = candidate._file_identity

    def oversized_identity(path: Path) -> dict[str, object]:
        identity = original_identity(path)
        return identity | {"size": candidate._MAX_ARCHIVE_BYTES + 1}

    monkeypatch.setattr(candidate, "_file_identity", oversized_identity)
    with pytest.raises(candidate.CandidateAttestationError, match="size bound"):
        candidate._read_regular_bytes(archive, candidate._MAX_ARCHIVE_BYTES)


def test_bundle_and_archive_use_unchanged_installer_bounds() -> None:
    limits = InstallerLimits()
    assert candidate._MAX_BUNDLE_BYTES == limits.max_bundle_bytes
    assert candidate._MAX_ARCHIVE_BYTES == limits.max_archive_bytes


def test_full_harness_requires_positive_baselines_and_records_each_stage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "candidate"
    source = _write_output(root)
    identity = _identity()
    candidate.prepare_transfer_manifest(
        root, root / candidate.TRANSFER_MANIFEST_NAME, identity, source
    )
    subjects = {
        "desktop-manifest-v1.json": _digest(
            (root / "archive/desktop-manifest-v1.json").read_bytes()
        ),
        candidate.CANDIDATE_ARCHIVE_NAME: _digest(
            (root / "archive" / candidate.CANDIDATE_ARCHIVE_NAME).read_bytes()
        ),
    }
    verifier = _PolicyAwareVerifier(identity, _statement(identity, subjects))
    bundle = Path(__file__).parents[2] / (
        "desktop/installer/fixtures/sigstore-python-4.5.0.intoto.sigstore.json"
    )
    report_root = root / "attestation-evidence"

    report = candidate.verify_candidate(
        root,
        bundle,
        report_root,
        identity,
        verifier=verifier,  # type: ignore[arg-type]
    )

    cases = {item["case"]: item["rejected_stage"] for item in report["negatives"]}
    assert report["result"] == "pass"
    assert report["production_policy"]["result"] == "rejected"
    assert cases["unchanged_production_release_identity"] == (
        "certificate_identity_policy"
    )
    assert cases["wrong_repository"] == "certificate_identity_policy"
    assert cases["wrong_workflow"] == "certificate_identity_policy"
    assert cases["wrong_ref"] == "certificate_identity_policy"
    assert cases["wrong_source_sha"] == "certificate_identity_policy"
    assert cases["wrong_event"] == "certificate_identity_policy"
    assert cases["wrong_runner_environment"] == "certificate_identity_policy"
    assert cases["missing_subject"] == "statement_semantics"
    assert cases["duplicate_subject"] == "statement_semantics"
    assert cases["wrong_subject"] == "statement_semantics"
    assert cases["wrong_run_attempt"] == "statement_semantics"
    assert cases["corrupt_archive_bytes"] == "subject_bytes_binding"
    assert cases["corrupt_manifest_bytes"] == "subject_bytes_binding"
    assert verifier.positive_calls == 16
    assert verifier.rejected_calls == 9
    assert (
        (report_root / "certificate.pem")
        .read_bytes()
        .startswith(b"-----BEGIN CERTIFICATE-----")
    )
    assert (report_root / "verified-dsse-payload.json").read_bytes() == (
        verifier.payload
    )


def test_workflow_separates_unprivileged_build_from_candidate_signing() -> None:
    workflow = (
        Path(__file__).parents[3] / ".github/workflows/release-desktop.yml"
    ).read_text(encoding="utf-8")
    workflow_env = workflow.split("env:\n", 1)[1].split("\njobs:\n", 1)[0]

    assert "pull_request_target" not in workflow
    assert "workflow_dispatch" not in workflow
    assert "runner.temp" not in workflow_env
    assert "CANDIDATE_ROOT=%s/tongs-desktop-candidate" in workflow
    assert '>> "$GITHUB_ENV"' in workflow
    assert workflow.count('-I -m venv "$RUNNER_TEMP/tongs-candidate-venv"') == 3
    assert "CANDIDATE_PYTHON=%s/tongs-candidate-venv/bin/python" in workflow
    assert workflow.count('"$CANDIDATE_PYTHON" -I -m pip install') == 3
    assert '"$CANDIDATE_PYTHON" -I -m pytest' in workflow
    assert workflow.count('"$CANDIDATE_PYTHON" -I\n') == 3
    assert workflow.count("--isolated") == 3
    assert workflow.count("PYTHONNOUSERSITE=1") == 3
    assert workflow.count("site.ENABLE_USER_SITE is False") == 3
    assert workflow.count("tongs_path.is_relative_to(workspace)") == 3
    assert "${{ runner.temp }}/tongs-desktop-candidate" in workflow
    assert "push-to-registry: false" in workflow
    assert "create-storage-record: false" in workflow
    assert "persist-credentials: false" in workflow
    assert "retention-days: 14" in workflow
    assert "packaging/desktop/archive/run_hosted.sh" in workflow
    assert "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6" in workflow
    assert "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803" in workflow
    assert (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in workflow
    )
    assert (
        "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c" in workflow
    )
    archive_job = workflow.split("  candidate-archive:", 1)[1].split(
        "  candidate-attestation:", 1
    )[0]
    signing_job = workflow.split("  candidate-attestation:", 1)[1]
    assert "id-token: write" not in archive_job
    assert "attestations: write" not in archive_job
    assert "id-token: write" in signing_job
    assert "attestations: write" in signing_job
    assert "packages: write" not in signing_job
    assert "contents: write" not in signing_job
    assert "artifact-metadata: write" not in signing_job


def test_verify_cli_retains_a_fail_closed_result(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    root.mkdir()
    report = tmp_path / "report"

    result = candidate.main(
        [
            "verify",
            "--root",
            str(root),
            "--bundle",
            str(tmp_path / "missing.sigstore.json"),
            "--report-root",
            str(report),
            "--repository",
            "andre-motta/tongs",
            "--repository-id",
            "1305350434",
            "--repository-owner-id",
            "30708955",
            "--ref",
            REF,
            "--source-commit",
            SOURCE_COMMIT,
            "--source-tree",
            SOURCE_TREE,
            "--event",
            "push",
            "--run-id",
            "123456",
            "--run-attempt",
            "1",
        ]
    )

    assert result == 1
    failure = json.loads(
        (report / "candidate-attestation-results.json").read_text(encoding="utf-8")
    )
    assert failure["result"] == "fail"
    assert failure["candidate"] == "UNPUBLISHED"
