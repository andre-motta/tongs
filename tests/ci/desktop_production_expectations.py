"""Derive and pass caller-owned expectations to the bespoke evidence adapters.

The issue #139 archive adapter and the issue #135 SBOM adapter both require the
caller to supply every expected identity, because neither may read policy from
the producer output it is checking.  Between them that is more than forty
values, and a workflow step cannot assemble them by hand without becoming
unreviewable.  This module assembles them and invokes the adapters, and it is
deliberately the only place that does so.

Which values come from where matters, so the module keeps them separate:

* Subject source identity (commit, tree, ``git archive`` digest, commit epoch)
  is supplied by the caller from the workflow's exact-SHA checkout job.  It is
  never read here from producer JSON.
* Subject archive identity (archive digest, artifact ID, release version, and
  the release, install and license manifest digests) is supplied by the caller
  from the producer job's declared outputs.  Passing the producer job's own
  declarations as the consumer's expectation is what binds "what the producer
  says it uploaded" to "what this job downloaded"; the adapters then reject any
  disagreement with the downloaded bytes.
* Reviewed configuration identity (Electron version, its configuration digest,
  and the upstream Electron archive digest) is read from the checked-in
  configuration in the subject checkout, which is the reviewed source of truth
  for those values rather than anything the producer emitted.
* Trusted tool identity is recomputed from the checked-in programs on every
  run, as the archive adapter's contract requires.  These are not independent
  judgement values: both sides must agree on the same file bytes, and pinning
  them in a long-lived file would silently rot.

Nothing here signs, publishes or relaxes an adapter check.  Every value is
handed to the reviewed adapter, which remains free to reject it.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_ADAPTER_PROGRAM = "tests/integration/desktop/archive_evidence.py"
SBOM_ADAPTER_PROGRAM = "tests/integration/desktop/sbom_evidence.py"
SBOM_GENERATOR_PROGRAM = "scripts/build_desktop_sbom.py"
TRANSFER_VALIDATOR_PROGRAM = "tests/integration/desktop/candidate_attestation.py"
RECEIPT_READER_PROGRAM = ".github/scripts/verify_desktop_production.py"
ARTIFACT_CONTRACT_PACKAGE = "src/tongs/desktop/artifact_contract"
SPDX_SCHEMA = "tests/packaging/desktop/sbom/schema/spdx-2.3.schema.json"
DESKTOP_PACKAGE = "desktop/package.json"
ELECTRON_CONFIGURATION_DIRECTORY = "packaging/desktop/archive"

#: Reviewed SPDX generator version recorded in the SBOM creator field.
SBOM_GENERATOR_VERSION = "1.0.0"

#: Reviewed release manifest artifact identifier for the per-user archive.  This
#: is the contract identifier the release manifest declares, not the numeric ID
#: GitHub assigns to an uploaded workflow artifact.  It is caller-owned policy
#: and must be changed deliberately alongside the reviewed producer.
USER_ARCHIVE_ARTIFACT_ID = "fedora-44-x86_64-user-archive"

#: Reviewed desktop release version.  The producer declares the same value, so
#: this constant is what makes the consumer expectation independent of it.
DESKTOP_RELEASE_VERSION = "0.5.0"

#: Reviewed half-open core compatibility interval for the desktop payload.  The
#: producer declares it, and packaging/rpm/desktop/manifest.json repeats it,
#: because package_contract.validate_accepted_payload compares the freshly built
#: archive against the bound contract field by field.  All three must agree.
#:
#: The upper bound admits the whole core 1.x series and refuses 2.0.0, which is
#: what makes a tagged core 1.0.0 installable.  The lower bound is deliberately
#: still the development bound the payload was built against.  Raising it to
#: 1.0.0 before the v1.0.0 tag exists would put this branch's own derived core,
#: 0.4.2.devN, below the interval and make package_contract.bind_manifest raise
#: on every push, so that raise belongs after the tag rather than here.  This is
#: option A of section 2.3 of docs/work/release-v1.0.0.md.
DESKTOP_CORE_MINIMUM = "0.4.2-dev.183"
DESKTOP_CORE_MAXIMUM_EXCLUSIVE = "2.0.0"

MAX_PROGRAM_BYTES = 8 * 1024 * 1024
MAX_CONFIGURATION_BYTES = 8 * 1024 * 1024
MAX_RECEIPT_BYTES = 256 * 1024


class ExpectationError(ValueError):
    """Raised when a caller-owned expectation cannot be derived honestly."""


def _fail(message: str) -> NoReturn:
    raise ExpectationError(message)


def _load_module(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        _fail(f"unable to load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


_ADAPTERS: dict[str, ModuleType] = {}


def _adapter(name: str, program: str) -> ModuleType:
    """Load one evidence adapter on first use and cache it.

    Loading is lazy and per subcommand on purpose.  The issue #135 SBOM adapter
    imports ``jsonschema`` and the deterministic SPDX generator; the issue #139
    archive adapter needs neither.  Importing both eagerly would make the
    archive receipt depend on the SBOM toolchain, which inverts the direction
    the #139 assignment fixed: the SBOM consumes the archive receipt, never the
    other way round.  It would also make the archive job fail on a dependency
    it has no reason to install.
    """

    module = _ADAPTERS.get(name)
    if module is None:
        module = _load_module(name, ROOT / program)
        _ADAPTERS[name] = module
    return module


def archive_adapter() -> ModuleType:
    """Return the issue #139 archive lifecycle adapter."""

    return _adapter("production_archive_evidence", ARCHIVE_ADAPTER_PROGRAM)


def sbom_adapter() -> ModuleType:
    """Return the issue #135 archive SBOM adapter."""

    return _adapter("production_sbom_evidence", SBOM_ADAPTER_PROGRAM)


def _read_regular_bytes(path: Path, maximum: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        _fail(f"unable to open {label} safely: {error}")
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            _fail(f"{label} must be a regular file")
        if details.st_size > maximum:
            _fail(f"{label} exceeds its bounded size")
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            payload = handle.read(maximum + 1)
    finally:
        os.close(descriptor)
    if len(payload) > maximum:
        _fail(f"{label} exceeds its bounded size")
    return payload


def _program_digest(source_root: Path, program: str) -> str:
    payload = _read_regular_bytes(source_root / program, MAX_PROGRAM_BYTES, program)
    return hashlib.sha256(payload).hexdigest()


def artifact_contract_digest(source_root: Path) -> str:
    """Recompute the artifact contract package digest the archive adapter uses.

    The adapter owns this algorithm.  Reimplementing it here would let the two
    drift apart silently, so the adapter's own helper is called instead: if the
    adapter renames or changes it, this fails loudly on the next run rather
    than producing a value the adapter will reject for the wrong reason.
    """

    helper = getattr(archive_adapter(), "_directory_digest", None)
    if helper is None:
        _fail(
            "the archive adapter no longer exposes its package directory digest; "
            "issue #53 must be updated with its replacement"
        )
    return helper(source_root / ARTIFACT_CONTRACT_PACKAGE)


def electron_identity(source_root: Path) -> tuple[str, str, str]:
    """Read the reviewed Electron version, configuration and archive digests."""

    package = json.loads(
        _read_regular_bytes(
            source_root / DESKTOP_PACKAGE, MAX_CONFIGURATION_BYTES, DESKTOP_PACKAGE
        ).decode("utf-8", errors="strict")
    )
    version = package.get("devDependencies", {}).get("electron")
    if not isinstance(version, str) or not version:
        _fail("the desktop package does not pin an Electron version")
    name = f"electron-runtime-{version}-linux-x64.json"
    relative = f"{ELECTRON_CONFIGURATION_DIRECTORY}/{name}"
    payload = _read_regular_bytes(
        source_root / relative, MAX_CONFIGURATION_BYTES, relative
    )
    configuration = json.loads(payload.decode("utf-8", errors="strict"))
    if configuration.get("electron_version") != version:
        _fail("the Electron configuration does not match the pinned version")
    upstream = configuration.get("upstream_archive", {})
    archive_digest = upstream.get("sha256")
    if not isinstance(archive_digest, str) or len(archive_digest) != 64:
        _fail("the Electron configuration does not declare an upstream digest")
    return version, hashlib.sha256(payload).hexdigest(), archive_digest


def _release_manifest_archive_name(transfer_root: Path) -> str:
    relative = "archive/desktop-manifest-v1.json"
    document = json.loads(
        _read_regular_bytes(
            transfer_root / relative, MAX_CONFIGURATION_BYTES, relative
        ).decode("utf-8", errors="strict")
    )
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        _fail("the downloaded release manifest does not declare one artifact")
    name = artifacts[0].get("name")
    if not isinstance(name, str) or not name:
        _fail("the downloaded release manifest does not name its archive")
    return name


def _transfer_flags(arguments: argparse.Namespace) -> list[str]:
    return [
        "--expected-transfer-repository",
        arguments.repository,
        "--expected-transfer-repository-id",
        _environment("GITHUB_REPOSITORY_ID"),
        "--expected-transfer-repository-owner-id",
        _environment("GITHUB_REPOSITORY_OWNER_ID"),
        "--expected-transfer-ref",
        _environment("GITHUB_REF"),
        "--expected-transfer-event",
        _environment("GITHUB_EVENT_NAME"),
        "--expected-transfer-run-id",
        arguments.run_id,
        "--expected-transfer-run-attempt",
        str(arguments.attempt),
    ]


def _policy_flags(arguments: argparse.Namespace) -> list[str]:
    return [
        "--repository",
        arguments.repository,
        "--run-id",
        arguments.run_id,
        "--attempt",
        str(arguments.attempt),
        "--environment",
        arguments.environment,
        "--provenance",
        arguments.provenance,
        "--check-id",
        arguments.check_id,
    ]


def _environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        _fail(f"the workflow context variable {name} is absent")
    return value


def archive_evidence_argv(arguments: argparse.Namespace) -> list[str]:
    """Build the complete issue #139 adapter argument vector."""

    source_root = Path(arguments.source_root)
    version, configuration_digest, upstream_digest = electron_identity(source_root)
    argv = [
        arguments.mode,
        "--source-root",
        str(source_root),
        "--transfer-root",
        str(arguments.transfer_root),
        "--expected-source-commit",
        arguments.commit,
        "--expected-source-tree",
        arguments.tree,
        "--expected-source-archive-sha256",
        arguments.source_archive_sha256,
        "--expected-source-date-epoch",
        str(arguments.source_date_epoch),
        "--expected-archive-name",
        _release_manifest_archive_name(Path(arguments.transfer_root)),
        "--expected-archive-artifact-id",
        USER_ARCHIVE_ARTIFACT_ID,
        "--expected-archive-sha256",
        arguments.archive_sha256,
        "--expected-release-version",
        DESKTOP_RELEASE_VERSION,
        "--expected-release-manifest-sha256",
        arguments.release_manifest_sha256,
        "--expected-install-manifest-sha256",
        arguments.install_manifest_sha256,
        "--expected-license-inventory-sha256",
        arguments.license_inventory_sha256,
        "--expected-electron-version",
        version,
        "--expected-electron-configuration-sha256",
        configuration_digest,
        "--expected-electron-archive-sha256",
        upstream_digest,
        "--expected-adapter-program-sha256",
        _program_digest(source_root, ARCHIVE_ADAPTER_PROGRAM),
        "--expected-transfer-validator-program-sha256",
        _program_digest(source_root, TRANSFER_VALIDATOR_PROGRAM),
        "--expected-receipt-reader-program-sha256",
        _program_digest(source_root, RECEIPT_READER_PROGRAM),
        "--expected-artifact-contract-sha256",
        artifact_contract_digest(source_root),
        *_transfer_flags(arguments),
        *_policy_flags(arguments),
    ]
    if arguments.mode == "produce":
        argv += ["--output-root", str(arguments.output_root)]
    else:
        argv += [
            "--evidence-root",
            str(arguments.evidence_root),
            "--receipt",
            str(Path(arguments.evidence_root) / "archive-receipt.json"),
        ]
    return argv


def sbom_evidence_argv(arguments: argparse.Namespace) -> list[str]:
    """Build the complete issue #135 adapter argument vector."""

    source_root = Path(arguments.source_root)
    _, configuration_digest, upstream_digest = electron_identity(source_root)
    receipt_digest = hashlib.sha256(
        _read_regular_bytes(
            Path(arguments.archive_receipt), MAX_RECEIPT_BYTES, "archive receipt"
        )
    ).hexdigest()
    argv = [
        arguments.mode,
        "--expected-source-commit",
        arguments.commit,
        "--expected-source-tree",
        arguments.tree,
        "--expected-source-archive-sha256",
        arguments.source_archive_sha256,
        "--expected-source-date-epoch",
        str(arguments.source_date_epoch),
        "--expected-archive-sha256",
        arguments.archive_sha256,
        "--expected-electron-archive-sha256",
        upstream_digest,
        "--expected-archive-receipt-sha256",
        receipt_digest,
        "--generator-version",
        SBOM_GENERATOR_VERSION,
        "--expected-tool-commit",
        arguments.commit,
        "--expected-tool-tree",
        arguments.tree,
        "--expected-adapter-program-sha256",
        _program_digest(source_root, SBOM_ADAPTER_PROGRAM),
        "--expected-generator-program-sha256",
        _program_digest(source_root, SBOM_GENERATOR_PROGRAM),
        "--expected-receipt-reader-program-sha256",
        _program_digest(source_root, RECEIPT_READER_PROGRAM),
        "--expected-schema-sha256",
        _program_digest(source_root, SPDX_SCHEMA),
        "--expected-electron-configuration-sha256",
        configuration_digest,
        *_transfer_flags(arguments),
        *_policy_flags(arguments),
        "--input-root",
        str(arguments.transfer_root),
    ]
    if arguments.mode == "produce":
        argv += [
            "--source-root",
            str(source_root),
            "--output-root",
            str(arguments.output_root),
            "--archive-receipt",
            str(arguments.archive_receipt),
        ]
    else:
        argv += [
            "--evidence-root",
            str(arguments.evidence_root),
            "--receipt",
            str(Path(arguments.evidence_root) / "sbom-receipt.json"),
        ]
    return argv


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", required=True, choices=("produce", "consume"))
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--transfer-root", required=True, type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--source-archive-sha256", required=True)
    parser.add_argument("--source-date-epoch", required=True, type=int)
    parser.add_argument("--archive-sha256", required=True)
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
    archive = commands.add_parser(
        "archive-evidence", help="run the issue #139 archive lifecycle adapter"
    )
    _add_common(archive)
    archive.add_argument("--release-manifest-sha256", required=True)
    archive.add_argument("--install-manifest-sha256", required=True)
    archive.add_argument("--license-inventory-sha256", required=True)
    sbom = commands.add_parser(
        "archive-sbom", help="run the issue #135 archive SBOM adapter"
    )
    _add_common(sbom)
    sbom.add_argument("--archive-receipt", required=True, type=Path)
    return parser


def _require_mode_paths(arguments: argparse.Namespace) -> None:
    if arguments.mode == "produce" and arguments.output_root is None:
        _fail("--output-root is required in produce mode")
    if arguments.mode == "consume" and arguments.evidence_root is None:
        _fail("--evidence-root is required in consume mode")


def main(argv: Sequence[str] | None = None) -> int:
    """Derive every caller-owned expectation and run the reviewed adapter."""

    arguments = _parser().parse_args(argv)
    try:
        _require_mode_paths(arguments)
        if arguments.command == "archive-evidence":
            adapter: Any = archive_adapter()
            adapter_argv = archive_evidence_argv(arguments)
        else:
            adapter = sbom_adapter()
            adapter_argv = sbom_evidence_argv(arguments)
    except (OSError, ExpectationError, ValueError, KeyError) as error:
        print(f"production expectations failed: {error}", file=sys.stderr)
        return 1
    return adapter.main(adapter_argv)


if __name__ == "__main__":
    raise SystemExit(main())
