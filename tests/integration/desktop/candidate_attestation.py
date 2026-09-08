"""Verify one real unpublished desktop archive candidate and its attestation.

This is a test-only consumer for the trusted candidate branches. Production
installation continues to use the release-tag identity policy in
``tongs.desktop.installer.metadata``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import io
import json
import os
import re
import stat
import sys
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Final, NoReturn

from cryptography.hazmat.primitives import serialization
from sigstore.errors import VerificationError
from sigstore.models import Bundle, InvalidBundle
from sigstore.verify import Verifier
from sigstore.verify.policy import (
    AllOf,
    Identity,
    OIDCBuildConfigDigest,
    OIDCBuildConfigURI,
    OIDCBuildSignerURI,
    OIDCBuildTrigger,
    OIDCIssuerV2,
    OIDCRunnerEnvironment,
    OIDCSourceRepositoryDigest,
    OIDCSourceRepositoryIdentifier,
    OIDCSourceRepositoryOwnerIdentifier,
    OIDCSourceRepositoryRef,
    OIDCSourceRepositoryURI,
)

from tongs.desktop.artifact_contract import (
    ArtifactContractError,
    parse_release_manifest,
    validate_artifact_archive,
)
from tongs.desktop.installer.metadata import (
    GITHUB_OIDC_ISSUER,
    INTOTO_PAYLOAD_TYPE,
    OFFICIAL_REPOSITORY,
    OFFICIAL_REPOSITORY_ID,
    OFFICIAL_REPOSITORY_OWNER_ID,
    OFFICIAL_REPOSITORY_URL,
    OFFICIAL_WORKFLOW_PATH,
    RELEASE_MANIFEST_NAME,
    _build_identity,
    _validate_statement,
    production_verification_policy,
)
from tongs.desktop.installer.models import (
    BuildIdentity,
    InstallerError,
    InstallerLimits,
)

SIGSTORE_VERSION: Final = "4.5.0"
CANDIDATE_ARCHIVE_NAME: Final = "tongs-desktop-0.5.0-fedora44-x86_64.tar.gz"
TRANSFER_MANIFEST_NAME: Final = "candidate-attestation-transfer-v1.json"
ALLOWED_REFS: Final = frozenset(
    {
        "refs/heads/feat/desktop-120-candidate-attestation",
        "refs/heads/feat/desktop-app",
    }
)
_ARCHIVE_FILES: Final = frozenset(
    {
        "archive/SHA256SUMS",
        "archive/app-asar-inventory.json",
        "archive/build-provenance.json",
        "archive/desktop-install.json",
        f"archive/{CANDIDATE_ARCHIVE_NAME}",
        f"archive/{RELEASE_MANIFEST_NAME}",
        "archive/license-inventory.json",
        "archive/prepared-source-inventory.json",
        "archive/runtime-inventory.json",
    }
)
_EVIDENCE_FILES: Final = frozenset(
    {
        "evidence/builder-image.json",
        "evidence/build-a.sha256",
        "evidence/build-b.sha256",
        "evidence/electron-v44.2.0-linux-x64.zip",
        "evidence/inputs.env",
        "evidence/rpm-nevra.txt",
        "evidence/rpm-sha256-check.txt",
        "evidence/rpm-signatures.txt",
        "evidence/source.tar",
        "evidence/toolchain.txt",
    }
)
_EXPECTED_TRANSFER_FILES: Final = _ARCHIVE_FILES | _EVIDENCE_FILES
_SHA1_RE: Final = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE: Final = re.compile(r"^[1-9][0-9]*$")
_MAX_JSON_BYTES: Final = 4 * 1024 * 1024
_MAX_BUNDLE_BYTES: Final = InstallerLimits().max_bundle_bytes
_MAX_ARCHIVE_BYTES: Final = InstallerLimits().max_archive_bytes
_HASH_CHUNK_BYTES: Final = 1024 * 1024


class CandidateAttestationError(ValueError):
    """Reject an incomplete, stale, or semantically invalid candidate."""


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    """Consumer-owned expected identity for one official candidate run."""

    issuer: str
    repository: str
    repository_url: str
    repository_id: str
    repository_owner_id: str
    workflow_path: str
    ref: str
    source_commit: str
    source_tree: str
    event: str
    runner_environment: str
    run_id: str
    run_attempt: int
    builder_id: str

    @classmethod
    def official(
        cls,
        *,
        repository: str,
        repository_id: str,
        repository_owner_id: str,
        ref: str,
        source_commit: str,
        source_tree: str,
        event: str,
        run_id: str,
        run_attempt: int,
    ) -> CandidateIdentity:
        """Create and strictly gate one trusted official-repository identity."""
        if repository != OFFICIAL_REPOSITORY:
            _fail("candidate repository is not the official repository")
        if repository_id != OFFICIAL_REPOSITORY_ID:
            _fail("candidate repository ID is not the official repository ID")
        if repository_owner_id != OFFICIAL_REPOSITORY_OWNER_ID:
            _fail("candidate owner ID is not the official owner ID")
        if ref not in ALLOWED_REFS:
            _fail("candidate ref is not an explicitly allowed branch")
        if event != "push":
            _fail("candidate event must be push")
        if _SHA1_RE.fullmatch(source_commit) is None:
            _fail("candidate source commit is invalid")
        if _SHA1_RE.fullmatch(source_tree) is None:
            _fail("candidate source tree is invalid")
        if _DECIMAL_RE.fullmatch(run_id) is None:
            _fail("candidate run ID is invalid")
        if type(run_attempt) is not int or run_attempt < 1:
            _fail("candidate run attempt is invalid")
        builder_id = f"{OFFICIAL_REPOSITORY_URL}/{OFFICIAL_WORKFLOW_PATH}@{ref}"
        return cls(
            issuer=GITHUB_OIDC_ISSUER,
            repository=repository,
            repository_url=OFFICIAL_REPOSITORY_URL,
            repository_id=repository_id,
            repository_owner_id=repository_owner_id,
            workflow_path=OFFICIAL_WORKFLOW_PATH,
            ref=ref,
            source_commit=source_commit,
            source_tree=source_tree,
            event=event,
            runner_environment="github-hosted",
            run_id=run_id,
            run_attempt=run_attempt,
            builder_id=builder_id,
        )

    def build_identity(self) -> BuildIdentity:
        """Return the production verifier's shared statement identity shape."""
        return BuildIdentity(
            issuer=self.issuer,
            repository=self.repository,
            workflow_path=self.workflow_path,
            ref=self.ref,
            source_commit=self.source_commit,
            event=self.event,
            builder_id=self.builder_id,
        )


def candidate_verification_policy(identity: CandidateIdentity) -> AllOf:
    """Build the test-only certificate policy for one candidate invocation."""
    return AllOf(
        [
            Identity(identity=identity.builder_id, issuer=identity.issuer),
            OIDCIssuerV2(identity.issuer),
            OIDCRunnerEnvironment(identity.runner_environment),
            OIDCSourceRepositoryURI(identity.repository_url),
            OIDCSourceRepositoryIdentifier(identity.repository_id),
            OIDCSourceRepositoryOwnerIdentifier(identity.repository_owner_id),
            OIDCSourceRepositoryDigest(identity.source_commit),
            OIDCSourceRepositoryRef(identity.ref),
            OIDCBuildSignerURI(identity.builder_id),
            OIDCBuildConfigURI(identity.builder_id),
            OIDCBuildConfigDigest(identity.source_commit),
            OIDCBuildTrigger(identity.event),
        ]
    )


def prepare_transfer_manifest(
    root: Path,
    destination: Path,
    identity: CandidateIdentity,
    source_archive: Path,
) -> dict[str, Any]:
    """Bind the complete producer output before the immutable artifact upload."""
    if destination.parent != root or destination.name != TRANSFER_MANIFEST_NAME:
        _fail("transfer manifest must use its fixed name at the transfer root")
    files = _validate_transfer_files(root, source_archive)
    archive = _validate_producer_output(root, identity.source_commit)
    subjects = _subject_records(root, archive)
    document = {
        "schema_version": 1,
        "candidate": "UNPUBLISHED",
        "source": {
            "commit": identity.source_commit,
            "tree": identity.source_tree,
        },
        "execution": {
            "repository": identity.repository,
            "repository_id": identity.repository_id,
            "repository_owner_id": identity.repository_owner_id,
            "ref": identity.ref,
            "event": identity.event,
            "run_id": identity.run_id,
            "run_attempt": identity.run_attempt,
        },
        "files": files,
        "subjects": subjects,
    }
    destination.write_bytes(_canonical_json(document))
    return document


def validate_transfer_manifest(
    root: Path,
    manifest_path: Path,
    identity: CandidateIdentity,
    source_archive: Path,
) -> dict[str, Any]:
    """Rebind the downloaded producer output to consumer-owned run identity."""
    document = _load_json(manifest_path)
    expected_fields = {
        "schema_version",
        "candidate",
        "source",
        "execution",
        "files",
        "subjects",
    }
    if set(document) != expected_fields:
        _fail("transfer manifest fields are invalid")
    if document["schema_version"] != 1 or document["candidate"] != "UNPUBLISHED":
        _fail("transfer manifest version or candidate marker is invalid")
    if document["source"] != {
        "commit": identity.source_commit,
        "tree": identity.source_tree,
    }:
        _fail("transfer manifest source identity is stale")
    if document["execution"] != {
        "repository": identity.repository,
        "repository_id": identity.repository_id,
        "repository_owner_id": identity.repository_owner_id,
        "ref": identity.ref,
        "event": identity.event,
        "run_id": identity.run_id,
        "run_attempt": identity.run_attempt,
    }:
        _fail("transfer manifest execution identity is stale")
    observed_files = _validate_transfer_files(root, source_archive)
    if document["files"] != observed_files:
        _fail("downloaded transfer files do not match the producer manifest")
    archive = _validate_producer_output(root, identity.source_commit)
    if document["subjects"] != _subject_records(root, archive):
        _fail("transfer manifest subjects do not match the producer output")
    return document


def verify_candidate(
    root: Path,
    bundle_path: Path,
    report_root: Path,
    identity: CandidateIdentity,
    *,
    verifier: Verifier | None = None,
) -> dict[str, Any]:
    """Verify a real bundle, then exercise failure-oriented candidate checks."""
    if importlib.metadata.version("sigstore") != SIGSTORE_VERSION:
        _fail(f"candidate verifier requires sigstore {SIGSTORE_VERSION}")
    archive = _validate_producer_output(root, identity.source_commit)
    subjects = _subject_digests(root, archive)
    bundle_document = _read_regular_bytes(bundle_path, _MAX_BUNDLE_BYTES)
    try:
        bundle = Bundle.from_json(bundle_document)
    except (InvalidBundle, ValueError, TypeError) as error:
        raise CandidateAttestationError("Sigstore bundle is invalid") from error
    if verifier is None:
        try:
            verifier = Verifier.production()
        except Exception as error:
            raise CandidateAttestationError(
                "Sigstore production trust root initialization failed"
            ) from error
    payload_type, payload = _cryptographic_baseline(
        verifier, bundle, identity, subjects
    )
    statement = _decode_json(payload, "verified DSSE payload")
    negatives: list[dict[str, str]] = []

    _verify_production_policy_rejection(verifier, bundle, identity, subjects, negatives)
    _verify_identity_negatives(verifier, bundle, identity, subjects, negatives)
    _verify_subject_negatives(
        verifier, bundle, identity, subjects, statement, negatives
    )
    _verify_byte_negatives(
        verifier, bundle, identity, subjects, root, archive, negatives
    )

    report_root.mkdir(mode=0o755, parents=True, exist_ok=False)
    certificate = bundle.signing_certificate
    (report_root / "certificate.pem").write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
    )
    (report_root / "verified-dsse-payload.json").write_bytes(payload)
    extensions = []
    for extension in certificate.extensions:
        raw_value = getattr(extension.value, "value", None)
        if isinstance(raw_value, bytes):
            value = raw_value.decode("utf-8", errors="backslashreplace")
        else:
            value = str(extension.value)
        extensions.append(
            {
                "oid": extension.oid.dotted_string,
                "critical": extension.critical,
                "value": value,
            }
        )
    (report_root / "certificate-extensions.json").write_bytes(
        _canonical_json(extensions)
    )
    policy = {
        "kind": "test-only-unpublished-candidate",
        "sigstore_version": SIGSTORE_VERSION,
        "identity": asdict(identity),
        "certificate_policy_checks": [
            "Identity",
            "OIDCIssuerV2",
            "OIDCRunnerEnvironment",
            "OIDCSourceRepositoryURI",
            "OIDCSourceRepositoryIdentifier",
            "OIDCSourceRepositoryOwnerIdentifier",
            "OIDCSourceRepositoryDigest",
            "OIDCSourceRepositoryRef",
            "OIDCBuildSignerURI",
            "OIDCBuildConfigURI",
            "OIDCBuildConfigDigest",
            "OIDCBuildTrigger",
        ],
        "production_policy": "unchanged release-tag policy; candidate rejected",
    }
    (report_root / "candidate-policy.json").write_bytes(_canonical_json(policy))
    subject_lines = [f"{digest}  {name}\n" for name, digest in subjects.items()]
    (report_root / "subjects.sha256").write_text(
        "".join(subject_lines), encoding="ascii"
    )
    report = {
        "schema_version": 1,
        "result": "pass",
        "candidate": "UNPUBLISHED",
        "positive": {
            "stage": "cryptographic_identity_and_statement",
            "payload_type": payload_type,
            "subjects": subjects,
            "archive_contract": "valid",
            "license_closure": "license inventory equals runtime/LICENSES.json",
        },
        "production_policy": {
            "result": "rejected",
            "stage": "certificate_identity_policy",
        },
        "negatives": negatives,
        "limitations": [
            "archive and release manifest subjects only",
            "RPM, SBOM subject, release publication, and installed native GPU proof remain parent gates",
        ],
    }
    (report_root / "candidate-attestation-results.json").write_bytes(
        _canonical_json(report)
    )
    return report


def _cryptographic_baseline(
    verifier: Verifier,
    bundle: Bundle,
    identity: CandidateIdentity,
    subjects: dict[str, str],
) -> tuple[str, bytes]:
    try:
        payload_type, payload = verifier.verify_dsse(
            bundle, candidate_verification_policy(identity)
        )
    except VerificationError as error:
        raise CandidateAttestationError(
            "candidate cryptographic identity verification failed"
        ) from error
    if payload_type != INTOTO_PAYLOAD_TYPE:
        _fail("candidate DSSE payload type is invalid")
    statement = _decode_json(payload, "verified DSSE payload")
    try:
        _validate_candidate_statement(statement, identity, subjects)
    except InstallerError as error:
        raise CandidateAttestationError(
            "candidate SLSA statement verification failed"
        ) from error
    return payload_type, payload


def _verify_production_policy_rejection(
    verifier: Verifier,
    bundle: Bundle,
    identity: CandidateIdentity,
    subjects: dict[str, str],
    negatives: list[dict[str, str]],
) -> None:
    _cryptographic_baseline(verifier, bundle, identity, subjects)
    release_version = "0.5.0"
    production_identity = _build_identity(
        f"desktop-v{release_version}", identity.source_commit
    )
    try:
        verifier.verify_dsse(
            bundle, production_verification_policy(production_identity)
        )
    except VerificationError:
        negatives.append(
            {
                "case": "unchanged_production_release_identity",
                "baseline": "cryptographic candidate baseline passed",
                "rejected_stage": "certificate_identity_policy",
            }
        )
        return
    _fail("unchanged production release policy accepted a branch candidate")


def _verify_identity_negatives(
    verifier: Verifier,
    bundle: Bundle,
    identity: CandidateIdentity,
    subjects: dict[str, str],
    negatives: list[dict[str, str]],
) -> None:
    wrong_workflow = ".github/workflows/not-release-desktop.yml"
    wrong_ref = "refs/heads/feat/not-the-candidate"
    mutations = {
        "wrong_repository": replace(
            identity,
            repository_url="https://github.com/andre-motta/not-tongs",
        ),
        "wrong_repository_id": replace(identity, repository_id="1"),
        "wrong_repository_owner_id": replace(identity, repository_owner_id="1"),
        "wrong_workflow": replace(
            identity,
            workflow_path=wrong_workflow,
            builder_id=f"{OFFICIAL_REPOSITORY_URL}/{wrong_workflow}@{identity.ref}",
        ),
        "wrong_ref": replace(
            identity,
            ref=wrong_ref,
            builder_id=(
                f"{OFFICIAL_REPOSITORY_URL}/{identity.workflow_path}@{wrong_ref}"
            ),
        ),
        "wrong_source_sha": replace(identity, source_commit="0" * 40),
        "wrong_event": replace(identity, event="workflow_dispatch"),
        "wrong_runner_environment": replace(identity, runner_environment="self-hosted"),
    }
    for name, wrong_identity in mutations.items():
        _cryptographic_baseline(verifier, bundle, identity, subjects)
        try:
            verifier.verify_dsse(bundle, candidate_verification_policy(wrong_identity))
        except VerificationError:
            negatives.append(
                {
                    "case": name,
                    "baseline": "cryptographic and semantic baseline passed",
                    "rejected_stage": "certificate_identity_policy",
                }
            )
            continue
        _fail(f"candidate identity negative unexpectedly passed: {name}")


def _verify_subject_negatives(
    verifier: Verifier,
    bundle: Bundle,
    identity: CandidateIdentity,
    subjects: dict[str, str],
    statement: dict[str, Any],
    negatives: list[dict[str, str]],
) -> None:
    names = list(subjects)
    mutations: dict[str, dict[str, Any]] = {}
    missing = copy.deepcopy(statement)
    missing["subject"] = missing["subject"][:-1]
    mutations["missing_subject"] = missing
    duplicate = copy.deepcopy(statement)
    duplicate["subject"] = [duplicate["subject"][0], duplicate["subject"][0]]
    mutations["duplicate_subject"] = duplicate
    wrong = copy.deepcopy(statement)
    wrong["subject"][0]["name"] = f"wrong-{names[0]}"
    mutations["wrong_subject"] = wrong
    wrong_invocation = copy.deepcopy(statement)
    wrong_invocation["predicate"]["runDetails"]["metadata"]["invocationId"] = (
        f"{identity.repository_url}/actions/runs/{identity.run_id}/attempts/999"
    )
    mutations["wrong_run_attempt"] = wrong_invocation
    for name, mutated in mutations.items():
        _cryptographic_baseline(verifier, bundle, identity, subjects)
        try:
            _validate_candidate_statement(mutated, identity, subjects)
        except (CandidateAttestationError, InstallerError):
            negatives.append(
                {
                    "case": name,
                    "baseline": "cryptographic and semantic baseline passed",
                    "rejected_stage": "statement_semantics",
                }
            )
            continue
        _fail(f"candidate subject negative unexpectedly passed: {name}")


def _verify_byte_negatives(
    verifier: Verifier,
    bundle: Bundle,
    identity: CandidateIdentity,
    subjects: dict[str, str],
    root: Path,
    archive: str,
    negatives: list[dict[str, str]],
) -> None:
    paths = {
        "corrupt_archive_bytes": (
            root / "archive" / archive,
            _MAX_ARCHIVE_BYTES,
        ),
        "corrupt_manifest_bytes": (
            root / "archive" / RELEASE_MANIFEST_NAME,
            _MAX_JSON_BYTES,
        ),
    }
    for name, (path, maximum) in paths.items():
        _cryptographic_baseline(verifier, bundle, identity, subjects)
        original = _read_regular_bytes(path, maximum)
        _validate_subject_bytes(path.name, original, subjects)
        corrupted = original[:-1] + bytes([original[-1] ^ 1])
        try:
            _validate_subject_bytes(path.name, corrupted, subjects)
        except CandidateAttestationError:
            negatives.append(
                {
                    "case": name,
                    "baseline": "cryptographic, semantic, and byte baseline passed",
                    "rejected_stage": "subject_bytes_binding",
                }
            )
            continue
        _fail(f"candidate byte corruption unexpectedly passed: {name}")


def _validate_subject_bytes(
    name: str, document: bytes, subjects: Mapping[str, str]
) -> None:
    expected = subjects.get(name)
    if expected is None or hashlib.sha256(document).hexdigest() != expected:
        _fail("candidate subject bytes do not match the signed digest")


def _validate_candidate_statement(
    statement: dict[str, Any],
    identity: CandidateIdentity,
    subjects: dict[str, str],
) -> None:
    """Apply shared strict SLSA checks plus exact candidate invocation binding."""
    _validate_statement(statement, identity.build_identity(), subjects)
    invocation = statement["predicate"]["runDetails"]["metadata"]["invocationId"]
    expected = (
        f"{identity.repository_url}/actions/runs/"
        f"{identity.run_id}/attempts/{identity.run_attempt}"
    )
    if invocation != expected:
        _fail("candidate workflow invocation does not match this exact run")


def _validate_transfer_files(root: Path, source_archive: Path) -> list[dict[str, Any]]:
    observed = _scan_regular_files(root, exclude={TRANSFER_MANIFEST_NAME})
    if set(observed) != _EXPECTED_TRANSFER_FILES:
        _fail("candidate transfer path set is incomplete or unexpected")
    transferred_source = root / "evidence/source.tar"
    if _file_identity(transferred_source) != _file_identity(source_archive):
        _fail("candidate source archive does not match the exact checkout")
    return [{"path": path, **_file_identity(root / path)} for path in sorted(observed)]


def _validate_producer_output(root: Path, source_commit: str) -> str:
    archive_root = root / "archive"
    release_document = _read_regular_bytes(
        archive_root / RELEASE_MANIFEST_NAME, _MAX_JSON_BYTES
    )
    try:
        release = parse_release_manifest(release_document)
    except ArtifactContractError as error:
        raise CandidateAttestationError("release manifest is invalid") from error
    if release.source_commit != source_commit or len(release.artifacts) != 1:
        _fail("release manifest is not bound to the candidate source and archive")
    artifact = release.artifacts[0]
    if artifact.name != CANDIDATE_ARCHIVE_NAME:
        _fail("release manifest archive name is unexpected")
    archive_path = archive_root / artifact.name
    archive_bytes = _read_regular_bytes(archive_path, _MAX_ARCHIVE_BYTES)
    try:
        validate_artifact_archive(
            archive_bytes,
            artifact.name,
            release,
            artifact.artifact_id,
        )
    except ArtifactContractError as error:
        raise CandidateAttestationError(
            "candidate archive contract is invalid"
        ) from error
    _validate_producer_metadata(root, source_commit, artifact.name)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as candidate:
        member = candidate.getmember("runtime/LICENSES.json")
        extracted = candidate.extractfile(member)
        if extracted is None:
            _fail("candidate license inventory is absent from the archive")
        archived_licenses = extracted.read()
    retained_licenses = _read_regular_bytes(
        archive_root / "license-inventory.json", _MAX_JSON_BYTES
    )
    if archived_licenses != retained_licenses:
        _fail("candidate license inventory is not closed over the archive")
    return artifact.name


def _validate_producer_metadata(root: Path, source_commit: str, archive: str) -> None:
    archive_root = root / "archive"
    _validate_checksums(archive_root)
    if _read_regular_bytes(root / "evidence/build-a.sha256", _MAX_JSON_BYTES) != (
        _read_regular_bytes(root / "evidence/build-b.sha256", _MAX_JSON_BYTES)
    ):
        _fail("producer reproducibility checksum sets disagree")
    provenance = _load_json(archive_root / "build-provenance.json")
    prepared = _load_json(archive_root / "prepared-source-inventory.json")
    runtime = _load_json(archive_root / "runtime-inventory.json")
    licenses = _load_json(archive_root / "license-inventory.json")
    if (
        provenance.get("candidate") != "UNPUBLISHED"
        or provenance.get("source_commit") != source_commit
        or prepared.get("source_commit") != source_commit
    ):
        _fail("producer metadata is not bound to the candidate source")
    outputs = provenance.get("outputs")
    if not isinstance(outputs, dict) or not isinstance(outputs.get("archive"), dict):
        _fail("producer provenance outputs are invalid")
    if outputs["archive"].get("name") != archive:
        _fail("producer provenance archive name is invalid")
    if not prepared.get("files") or not prepared.get("npm_packages"):
        _fail("producer prepared source inventory is incomplete")
    runtime_files = runtime.get("files")
    components = licenses.get("components")
    if not isinstance(runtime_files, list) or not isinstance(components, list):
        _fail("producer runtime or license inventory is invalid")
    runtime_paths = {
        item.get("path") for item in runtime_files if isinstance(item, dict)
    }
    if "runtime/LICENSES.json" not in runtime_paths or not components:
        _fail("producer license closure evidence is incomplete")
    inputs = _parse_inputs_env(root / "evidence/inputs.env")
    if inputs.get("TONGS_HEAD_SHA") != source_commit:
        _fail("producer inputs do not identify the candidate source")


def _subject_records(root: Path, archive: str) -> list[dict[str, Any]]:
    subjects = _subject_digests(root, archive)
    return [
        {
            "name": name,
            "path": f"archive/{name}",
            "size": (root / "archive" / name).stat().st_size,
            "sha256": digest,
        }
        for name, digest in subjects.items()
    ]


def _subject_digests(root: Path, archive: str) -> dict[str, str]:
    return {
        RELEASE_MANIFEST_NAME: _file_identity(root / "archive" / RELEASE_MANIFEST_NAME)[
            "sha256"
        ],
        archive: _file_identity(root / "archive" / archive)["sha256"],
    }


def _validate_checksums(root: Path) -> None:
    lines = _read_regular_bytes(root / "SHA256SUMS", _MAX_JSON_BYTES).decode("ascii")
    expected: dict[str, str] = {}
    for line in lines.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/]+)", line)
        if match is None or match.group(2) in expected:
            _fail("producer SHA256SUMS is invalid")
        expected[match.group(2)] = match.group(1)
    names = {path.name for path in root.iterdir() if path.name != "SHA256SUMS"}
    if set(expected) != names:
        _fail("producer SHA256SUMS path set is incomplete")
    for name, digest in expected.items():
        if _file_identity(root / name)["sha256"] != digest:
            _fail("producer SHA256SUMS digest does not match an output")


def _scan_regular_files(root: Path, *, exclude: set[str]) -> dict[str, os.stat_result]:
    try:
        root_stat = root.lstat()
    except OSError as error:
        raise CandidateAttestationError("candidate transfer root is missing") from error
    if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink():
        _fail("candidate transfer root must be a real directory")
    result: dict[str, os.stat_result] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            if path.is_symlink():
                _fail("candidate transfer contains a symlinked directory")
            continue
        if relative in exclude:
            continue
        if not stat.S_ISREG(details.st_mode) or path.is_symlink():
            _fail("candidate transfer contains a non-regular file")
        result[relative] = details
    return result


def _file_identity(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    try:
        details_before = path.lstat()
        if not stat.S_ISREG(details_before.st_mode) or path.is_symlink():
            _fail("candidate evidence path is not a regular file")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(_HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
        details_after = path.lstat()
    except OSError as error:
        raise CandidateAttestationError(
            "candidate evidence file cannot be read"
        ) from error
    if (
        details_before.st_dev,
        details_before.st_ino,
        details_before.st_size,
        details_before.st_mtime_ns,
    ) != (
        details_after.st_dev,
        details_after.st_ino,
        details_after.st_size,
        details_after.st_mtime_ns,
    ):
        _fail("candidate evidence file changed while it was read")
    return {"size": details_after.st_size, "sha256": digest.hexdigest()}


def _read_regular_bytes(path: Path, maximum: int | None) -> bytes:
    identity = _file_identity(path)
    if maximum is not None and identity["size"] > maximum:
        _fail("candidate evidence file exceeds its size bound")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != identity["sha256"]:
        _fail("candidate evidence file changed between reads")
    return data


def _load_json(path: Path) -> dict[str, Any]:
    return _decode_json(_read_regular_bytes(path, _MAX_JSON_BYTES), path.name)


def _decode_json(document: bytes, label: str) -> dict[str, Any]:
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
            parse_constant=lambda _value: _fail(
                f"{label} contains a non-finite number"
            ),
        )
    except CandidateAttestationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CandidateAttestationError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        _fail(f"{label} root must be an object")
    return value


def _parse_inputs_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = _read_regular_bytes(path, _MAX_JSON_BYTES).decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise CandidateAttestationError("producer inputs.env is invalid") from error
    for line in lines:
        if not line or "=" not in line:
            _fail("producer inputs.env is invalid")
        key, value = line.split("=", 1)
        if not key or key in result:
            _fail("producer inputs.env is ambiguous")
        result[key] = value
    return result


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _fail(message: str) -> NoReturn:
    raise CandidateAttestationError(message)


def _identity_from_arguments(arguments: argparse.Namespace) -> CandidateIdentity:
    return CandidateIdentity.official(
        repository=arguments.repository,
        repository_id=arguments.repository_id,
        repository_owner_id=arguments.repository_owner_id,
        ref=arguments.ref,
        source_commit=arguments.source_commit,
        source_tree=arguments.source_tree,
        event=arguments.event,
        run_id=arguments.run_id,
        run_attempt=arguments.run_attempt,
    )


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--repository-id", required=True)
    parser.add_argument("--repository-owner-id", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True, type=int)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-transfer")
    prepare.add_argument("--root", type=Path, required=True)
    prepare.add_argument("--source-archive", type=Path, required=True)
    _add_identity_arguments(prepare)
    validate = commands.add_parser("validate-transfer")
    validate.add_argument("--root", type=Path, required=True)
    validate.add_argument("--source-archive", type=Path, required=True)
    _add_identity_arguments(validate)
    verify = commands.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--report-root", type=Path, required=True)
    _add_identity_arguments(verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fail-closed candidate transfer or verification command."""
    arguments = _parser().parse_args(argv)
    try:
        identity = _identity_from_arguments(arguments)
        root = arguments.root.resolve(strict=True)
        manifest_path = root / TRANSFER_MANIFEST_NAME
        if arguments.command == "prepare-transfer":
            prepare_transfer_manifest(
                root, manifest_path, identity, arguments.source_archive
            )
            print("candidate transfer manifest prepared")
        elif arguments.command == "validate-transfer":
            validate_transfer_manifest(
                root, manifest_path, identity, arguments.source_archive
            )
            print("candidate transfer identity and files verified")
        else:
            verify_candidate(
                root,
                arguments.bundle,
                arguments.report_root,
                identity,
            )
            print("real unpublished candidate attestation verified")
    except (CandidateAttestationError, OSError) as error:
        if arguments.command == "verify":
            arguments.report_root.mkdir(mode=0o755, parents=True, exist_ok=True)
            failure = {
                "schema_version": 1,
                "result": "fail",
                "candidate": "UNPUBLISHED",
                "error": str(error),
            }
            (arguments.report_root / "candidate-attestation-results.json").write_bytes(
                _canonical_json(failure)
            )
        print(f"candidate attestation verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
