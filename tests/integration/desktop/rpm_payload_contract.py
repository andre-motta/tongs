"""Materialize the exact-mode RPM payload contract from a fresh archive.

The issue #52 RPM harness accepts ``--accepted-dir`` and ``--payload-manifest``.
Without them it downloads a historical issue #51 artifact and enables
``--allow-reviewed-fixture``, which cannot satisfy the final issue #53 gate.
The checked-in ``packaging/rpm/desktop/manifest.json`` still names that
historical fixture, so the final gate needs one contract bound to the archive
this run actually produced.

This module writes that contract and nothing else.  It changes no issue #52
producer semantics and adds no schema: the reviewed target, compatibility,
Electron, companion and runtime-requirement policy is copied byte for byte from
the checked-in base manifest, and only the accepted payload identity is
replaced.  The replacement values are observed from the archive directory
itself, and each one is corroborated against the producer's own ``SHA256SUMS``
before the contract is written, so a contract cannot name a digest that the
producer's checksum list contradicts.

A release tag run passes ``--release-version``.  The checked-in base manifest
names the candidate release version, so for a tagged release the accepted
``release_version`` and the accepted archive file name are derived from the tag
instead, and the fresh archive's own release manifest and install manifest must
carry that same version before the contract is written.  Nothing else in the
reviewed policy changes: the compatibility interval, Electron version and
runtime requirements are still copied from the base manifest.

The ``reviewed_fixture`` block is removed rather than updated.  Its presence is
what lets ``package_contract.bind_manifest`` fall back to
``reviewed-fixture`` pairing; a contract produced here must only ever bind as
``exact``, and it does so because the accepted source commit is the same commit
the core is built from.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[3]
PACKAGE_CONTRACT_PROGRAM = "packaging/rpm/desktop/package_contract.py"
BASE_MANIFEST = "packaging/rpm/desktop/manifest.json"
CHECKSUM_FILE_NAME = "SHA256SUMS"

MAX_MANIFEST_BYTES = 256 * 1024
MAX_CHECKSUM_BYTES = 256 * 1024
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024

_SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DECIMAL_RE = re.compile(r"[0-9]{1,19}\Z")
_CHECKSUM_LINE_RE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]{1,128})\Z")
_RELEASE_VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")

RELEASE_MANIFEST_NAME = "desktop-manifest-v1.json"
INSTALL_MANIFEST_NAME = "desktop-install.json"

#: Keys copied unchanged from the reviewed base manifest.
PRESERVED_TOP_LEVEL_KEYS = (
    "schema_version",
    "target",
    "core_runtime_requirements",
    "mcp_requirement",
    "desktop_runtime_requirements",
    "companion_binary_count",
)
#: Keys of ``accepted_desktop`` copied unchanged from the base manifest.  The
#: release version is copied too unless a release tag overrides it.
PRESERVED_ACCEPTED_KEYS = ("electron_version", "release_version", "compatibility")


def archive_filename_for(release_version: str) -> str:
    """Return the producer's archive file name for one release version."""

    return f"tongs-desktop-{release_version}-fedora44-x86_64.tar.gz"


class PayloadContractError(ValueError):
    """Raised when a payload contract cannot be bound to the fresh archive."""


def _fail(message: str) -> NoReturn:
    raise PayloadContractError(message)


def _load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        _fail(f"unable to load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


CONTRACT = _load_module("rpm_payload_package_contract", ROOT / PACKAGE_CONTRACT_PROGRAM)


@dataclass(frozen=True, slots=True)
class PayloadContractBinding:
    """Observed identity of one materialized exact-mode payload contract."""

    source_commit: str
    artifact_id: str
    artifact_name: str
    run_id: str
    archive_filename: str
    archive_bytes: int
    archive_sha256: str
    evidence_count: int
    contract_sha256: str


def _read_regular_bytes(path: Path, maximum: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        _fail(f"unable to open {label} safely: {error}")
    try:
        first = os.fstat(descriptor)
        if not stat.S_ISREG(first.st_mode):
            _fail(f"{label} must be a regular file")
        if first.st_size > maximum:
            _fail(f"{label} exceeds its bounded size")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            payload = handle.read(maximum + 1)
        second = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(payload) > maximum:
        _fail(f"{label} exceeds its bounded size")
    if (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns) != (
        second.st_dev,
        second.st_ino,
        second.st_size,
        second.st_mtime_ns,
    ):
        _fail(f"{label} changed while it was read")
    return payload


def _stream_identity(path: Path, maximum: int, label: str) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        _fail(f"unable to open {label} safely: {error}")
    digest = hashlib.sha256()
    size = 0
    try:
        first = os.fstat(descriptor)
        if not stat.S_ISREG(first.st_mode):
            _fail(f"{label} must be a regular file")
        if first.st_size > maximum:
            _fail(f"{label} exceeds its bounded size")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            while chunk := handle.read(HASH_CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
        second = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns) != (
        second.st_dev,
        second.st_ino,
        second.st_size,
        second.st_mtime_ns,
    ):
        _fail(f"{label} changed while it was read")
    return size, digest.hexdigest()


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _decode_object(payload: bytes, label: str) -> dict[str, Any]:
    def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            _fail(f"{label} contains a duplicate key")
        return dict(pairs)

    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail(f"{label} is not decodable JSON: {error}")
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    return value


def parse_checksums(payload: bytes) -> dict[str, str]:
    """Parse the producer checksum list, rejecting malformed or repeated names."""

    try:
        text = payload.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        _fail(f"{CHECKSUM_FILE_NAME} must be ASCII")
    if not text.endswith("\n"):
        _fail(f"{CHECKSUM_FILE_NAME} is truncated without a final newline")
    records: dict[str, str] = {}
    for line in text[:-1].split("\n"):
        match = _CHECKSUM_LINE_RE.fullmatch(line)
        if match is None:
            _fail(f"{CHECKSUM_FILE_NAME} contains a malformed record")
        digest, name = match.group(1), match.group(2)
        if name in records:
            _fail(f"{CHECKSUM_FILE_NAME} repeats the name {name!r}")
        records[name] = digest
    if not records:
        _fail(f"{CHECKSUM_FILE_NAME} is empty")
    return records


def _observe_archive_payload(
    archive_dir: Path, base_accepted: Mapping[str, Any], archive_filename: str
) -> tuple[dict[str, Any], dict[str, str], int, str]:
    """Observe the fresh archive's identity and corroborate it with SHA256SUMS."""

    checksum_bytes = _read_regular_bytes(
        archive_dir / CHECKSUM_FILE_NAME,
        MAX_CHECKSUM_BYTES,
        f"archive {CHECKSUM_FILE_NAME}",
    )
    checksums = parse_checksums(checksum_bytes)
    expected_evidence_names = sorted(base_accepted["evidence"])
    if CHECKSUM_FILE_NAME not in expected_evidence_names:
        _fail("the reviewed evidence set is expected to include the checksum list")
    # The producer deliberately omits the checksum list from its own records,
    # so the corroborated set is every other accepted file plus the archive.
    corroborated = {
        *(name for name in expected_evidence_names if name != CHECKSUM_FILE_NAME),
        archive_filename,
    }
    if set(checksums) != corroborated:
        missing = sorted(corroborated - set(checksums))
        unexpected = sorted(set(checksums) - corroborated)
        _fail(
            "producer checksum list does not cover the reviewed accepted file set: "
            f"missing={missing}, unexpected={unexpected}"
        )

    evidence: dict[str, str] = {CHECKSUM_FILE_NAME: _digest(checksum_bytes)}
    for name in expected_evidence_names:
        if name == CHECKSUM_FILE_NAME:
            continue
        payload = _read_regular_bytes(
            archive_dir / name, MAX_EVIDENCE_BYTES, f"archive evidence {name}"
        )
        digest = _digest(payload)
        if digest != checksums[name]:
            _fail(f"archive evidence {name!r} disagrees with the producer checksum")
        evidence[name] = digest

    archive_bytes, archive_sha256 = _stream_identity(
        archive_dir / archive_filename, MAX_ARCHIVE_BYTES, "desktop archive"
    )
    if archive_sha256 != checksums[archive_filename]:
        _fail("desktop archive disagrees with the producer checksum")
    if archive_bytes <= 0:
        _fail("desktop archive is empty")

    archive = {
        "filename": archive_filename,
        "bytes": archive_bytes,
        "sha256": archive_sha256,
    }
    return archive, evidence, archive_bytes, archive_sha256


def _require_identity(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _fail(f"{label} is invalid")
    return value


def _require_archive_release_version(archive_dir: Path, release_version: str) -> None:
    """Require the fresh archive's own manifests to carry the accepted version.

    ``package_contract.validate_accepted_payload`` makes the same comparison
    later, inside the producer container.  Making it here, before the contract
    exists, is what turns a tag that disagrees with what the producer built
    into one clear failure instead of a contract that binds and then fails.
    """

    for name in (RELEASE_MANIFEST_NAME, INSTALL_MANIFEST_NAME):
        document = _decode_object(
            _read_regular_bytes(
                archive_dir / name, MAX_EVIDENCE_BYTES, f"archive evidence {name}"
            ),
            f"archive evidence {name}",
        )
        if document.get("release_version") != release_version:
            _fail(
                f"archive evidence {name!r} does not carry release version "
                f"{release_version}"
            )


def materialize_payload_contract(
    *,
    base_manifest_path: Path,
    archive_dir: Path,
    output_path: Path,
    source_commit: str,
    artifact_id: str,
    artifact_name: str,
    run_id: str,
    release_version: str | None = None,
) -> PayloadContractBinding:
    """Write one exact-mode payload contract bound to this run's archive.

    ``release_version`` is the version a release tag names.  When given, it
    replaces the base manifest's candidate release version and archive file
    name; the fresh archive must have been produced for that same version.
    """

    _require_identity(source_commit, _SHA1_RE, "accepted source commit")
    if release_version is not None:
        _require_identity(release_version, _RELEASE_VERSION_RE, "release version")
    _require_identity(artifact_id, _DECIMAL_RE, "accepted artifact ID")
    _require_identity(run_id, _DECIMAL_RE, "accepted run ID")
    if not isinstance(artifact_name, str) or not 1 <= len(artifact_name) <= 256:
        _fail("accepted artifact name is invalid")

    destination = Path(output_path)
    if destination.exists() or destination.is_symlink():
        _fail("payload contract output must not already exist")

    base = _decode_object(
        _read_regular_bytes(
            Path(base_manifest_path), MAX_MANIFEST_BYTES, "base payload manifest"
        ),
        "base payload manifest",
    )
    if "rpm_pairing" in base:
        _fail("the base manifest must not already be bound to a core identity")
    CONTRACT.validate_manifest(base)
    base_accepted = base["accepted_desktop"]
    if "reviewed_fixture" not in base_accepted:
        _fail(
            "the checked-in base manifest is expected to carry the reviewed fixture "
            "block that this materializer removes"
        )

    accepted_release_version = base_accepted["release_version"]
    archive_filename = base_accepted["archive"]["filename"]
    if release_version is not None:
        accepted_release_version = release_version
        archive_filename = archive_filename_for(release_version)
    if not isinstance(accepted_release_version, str) or (
        _RELEASE_VERSION_RE.fullmatch(accepted_release_version) is None
    ):
        _fail("accepted release version is invalid")

    archive, evidence, archive_bytes, archive_sha256 = _observe_archive_payload(
        Path(archive_dir), base_accepted, archive_filename
    )
    _require_archive_release_version(Path(archive_dir), accepted_release_version)

    contract: dict[str, Any] = {
        key: copy.deepcopy(base[key]) for key in PRESERVED_TOP_LEVEL_KEYS
    }
    accepted: dict[str, Any] = {
        "artifact_id": int(artifact_id),
        "artifact_name": artifact_name,
        "run_id": int(run_id),
        "source_commit": source_commit,
        "archive": archive,
        "evidence": evidence,
    }
    for key in PRESERVED_ACCEPTED_KEYS:
        accepted[key] = copy.deepcopy(base_accepted[key])
    accepted["release_version"] = accepted_release_version
    contract["accepted_desktop"] = accepted

    CONTRACT.validate_manifest(contract)
    if "reviewed_fixture" in contract["accepted_desktop"]:
        _fail("an exact payload contract must not retain a reviewed fixture block")

    payload = (json.dumps(contract, indent=2, sort_keys=True) + "\n").encode()
    if len(payload) > MAX_MANIFEST_BYTES:
        _fail("materialized payload contract exceeds its bounded size")
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)

    return PayloadContractBinding(
        source_commit=source_commit,
        artifact_id=artifact_id,
        artifact_name=artifact_name,
        run_id=run_id,
        archive_filename=archive["filename"],
        archive_bytes=archive_bytes,
        archive_sha256=archive_sha256,
        evidence_count=len(evidence),
        contract_sha256=hashlib.sha256(payload).hexdigest(),
    )


def require_exact_pairing(
    contract: Mapping[str, Any], core_commit: str, core_pep440_version: str
) -> None:
    """Require that binding this contract to the core yields exact pairing.

    ``package_contract.bind_manifest`` selects ``reviewed-fixture`` pairing only
    when the payload and core commits differ and a reviewed fixture block is
    present.  Calling it here with the fixture allowance disabled proves the
    contract can bind only as ``exact`` against this exact core identity.
    """

    identity = CONTRACT.SourceIdentity(
        commit=core_commit,
        pep440_version=core_pep440_version,
        rpm_version=core_pep440_version.replace("+", "^").replace(".dev", "~dev"),
        rpm_release="1",
        source_date_epoch=0,
    )
    bound = CONTRACT.bind_manifest(dict(contract), identity, False)
    if bound["rpm_pairing"]["mode"] != "exact":
        _fail("materialized payload contract did not bind as exact pairing")
    if bound["rpm_pairing"]["payload_source_commit"] != core_commit:
        _fail("materialized payload contract bound to another core commit")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-manifest", type=Path, default=ROOT / BASE_MANIFEST)
    parser.add_argument("--archive-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--release-version",
        default=None,
        help=(
            "stable X.Y.Z version named by a release tag; replaces the base "
            "manifest's candidate release version and archive file name"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Materialize one exact-mode payload contract, failing closed."""

    arguments = _parser().parse_args(argv)
    try:
        binding = materialize_payload_contract(
            base_manifest_path=arguments.base_manifest,
            archive_dir=arguments.archive_dir,
            output_path=arguments.output,
            source_commit=arguments.source_commit,
            artifact_id=arguments.artifact_id,
            artifact_name=arguments.artifact_name,
            run_id=arguments.run_id,
            release_version=arguments.release_version,
        )
    except (OSError, PayloadContractError, ValueError) as error:
        print(f"payload contract materialization failed: {error}", file=sys.stderr)
        return 1
    print(
        "exact payload contract bound "
        f"{binding.archive_filename} ({binding.archive_bytes} bytes, "
        f"sha256={binding.archive_sha256}) with {binding.evidence_count} "
        f"evidence records: sha256={binding.contract_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
