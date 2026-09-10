"""Assemble, verify and confirm the desktop GitHub Release for one version tag.

One ``vX.Y.Z`` tag publishes the core to PyPI and, through the trusted
``release-desktop.yml`` workflow, the desktop assets to the GitHub Release of
the same tag.  ``tongs --install-desktop`` then selects the release whose
version equals the running core, so the release must carry exactly what the
installer verifies: the release manifest, the Sigstore bundle that attests the
manifest and the per-user archive, and the archive itself.  The RPM set, the
archive SBOM and its bundle, and a checksum list ride along for people who
install by hand.

This module is the only thing between the signed producer output and
``gh release create``.  Its three commands run in order in the publish job:

``assemble``
    Copies the producer output and the RPMs into one flat asset directory,
    naming the bundle as the installer expects, and writes ``SHA256SUMS``.

``verify``
    Replays the installer's own verification against the assembled directory:
    the manifest parses, names the tag's version and the tag's commit, the
    archive is the artifact the manifest selects for Fedora 44 x86_64, the
    compatibility interval admits the tag's own core version, and the bundle
    verifies under ``_verify_production_attestation`` with the identity the
    installer derives from the tag.  Nothing is created until this passes.

``require-absent`` and ``verify-published``
    Refuse to overwrite a release that already exists, and after creation
    confirm the published release is immutable, stable, and carries every
    assembled asset with the exact size and digest.

Nothing here signs anything, and nothing here talks to GitHub except through
``gh api`` for the two release-state checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn

from sigstore.verify import Verifier

from tongs.desktop.artifact_contract import (
    ArtifactContractError,
    DesktopPlatform,
    PackageKind,
    TargetArchitecture,
    TargetOperatingSystem,
    parse_release_manifest,
    select_release_artifact,
)
from tongs.desktop.installer.metadata import (
    OFFICIAL_REPOSITORY,
    RELEASE_BUNDLE_NAME,
    RELEASE_MANIFEST_NAME,
    RELEASE_TAG_PREFIX,
    _build_identity,
    _validate_compatibility,
    _verify_production_attestation,
)
from tongs.desktop.installer.models import InstallerError, InstallerLimits
from tongs.desktop.protocol import PROTOCOL_MAJOR
from tongs.plugins.desktop import DESKTOP_PLUGIN_API_MAJOR

#: The producer's Sigstore bundle for the manifest and archive, as the
#: candidate attestation job retains it.
CANDIDATE_BUNDLE_NAME: Final = "candidate-attestation.sigstore.json"
#: The producer's archive SBOM and its bundle, as the same job retains them.
CANDIDATE_SBOM_NAME: Final = "tongs-desktop.spdx.json"
CANDIDATE_SBOM_BUNDLE_NAME: Final = "candidate-sbom.sigstore.json"
ATTESTATION_EVIDENCE_DIRECTORY: Final = "attestation-evidence"
CHECKSUM_FILE_NAME: Final = "SHA256SUMS"
#: Where the RPM producer leaves the packages a release publishes.
FINAL_RPM_DIRECTORY: Final = "rpms/final"
COMPANION_RPM_DIRECTORY: Final = "companion-consumer-rpms"

MAX_SMALL_ASSET_BYTES: Final = 8 * 1024 * 1024
MAX_RPM_BYTES: Final = 512 * 1024 * 1024
MAX_ASSET_COUNT: Final = 64
MAX_NOTES_BYTES: Final = 256 * 1024
HASH_CHUNK_BYTES: Final = 1024 * 1024

_RELEASE_TAG_RE: Final = re.compile(
    rf"^{re.escape(RELEASE_TAG_PREFIX)}(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
_SHA1_RE: Final = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_ASSET_NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]{0,127}$")
_CHECKSUM_LINE_RE: Final = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9._+~-]{1,128})$")
_RPM_NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]*\.rpm$")

SUPPORTED_PLATFORM: Final = DesktopPlatform(
    TargetOperatingSystem.LINUX,
    TargetArchitecture.X86_64,
    "fedora",
    "44",
    "gnu",
)

#: Runs ``gh`` with the given arguments and returns (exit status, stdout, stderr).
GhRunner = Callable[[Sequence[str]], tuple[int, str, str]]


class ReleasePublicationError(ValueError):
    """Raised when the release assets cannot be assembled, verified or confirmed."""


def _fail(message: str) -> NoReturn:
    raise ReleasePublicationError(message)


@dataclass(frozen=True, slots=True)
class ReleaseTag:
    """One stable release tag and the version it names."""

    tag: str
    version: str

    @classmethod
    def parse(cls, tag: str) -> ReleaseTag:
        match = _RELEASE_TAG_RE.fullmatch(tag)
        if match is None:
            _fail(f"release tag is not a stable {RELEASE_TAG_PREFIX}X.Y.Z tag: {tag!r}")
        return cls(tag=tag, version=".".join(match.groups()))


def archive_name_for(version: str) -> str:
    """Return the producer's per-user archive name for one release version."""

    return f"tongs-desktop-{version}-fedora44-x86_64.tar.gz"


def sbom_name_for(version: str) -> str:
    """Return the published SBOM asset name for one release version."""

    return f"tongs-desktop-{version}.spdx.json"


def sbom_bundle_name_for(version: str) -> str:
    """Return the published SBOM bundle asset name for one release version."""

    return f"tongs-desktop-{version}.spdx.sigstore.json"


def expected_fixed_assets(version: str) -> dict[str, tuple[str, int]]:
    """Map every non-RPM asset name to its producer path and size bound."""

    return {
        RELEASE_MANIFEST_NAME: (
            f"archive/{RELEASE_MANIFEST_NAME}",
            MAX_SMALL_ASSET_BYTES,
        ),
        RELEASE_BUNDLE_NAME: (
            f"{ATTESTATION_EVIDENCE_DIRECTORY}/{CANDIDATE_BUNDLE_NAME}",
            InstallerLimits().max_bundle_bytes,
        ),
        archive_name_for(version): (
            f"archive/{archive_name_for(version)}",
            InstallerLimits().max_archive_bytes,
        ),
        sbom_name_for(version): (
            f"{ATTESTATION_EVIDENCE_DIRECTORY}/{CANDIDATE_SBOM_NAME}",
            MAX_SMALL_ASSET_BYTES,
        ),
        sbom_bundle_name_for(version): (
            f"{ATTESTATION_EVIDENCE_DIRECTORY}/{CANDIDATE_SBOM_BUNDLE_NAME}",
            InstallerLimits().max_bundle_bytes,
        ),
    }


def _open_regular(path: Path, label: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        _fail(f"unable to open {label} safely: {error}")
    return descriptor


def _read_regular_bytes(path: Path, maximum: int, label: str) -> bytes:
    descriptor = _open_regular(path, label)
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
    if (first.st_size, first.st_mtime_ns) != (second.st_size, second.st_mtime_ns):
        _fail(f"{label} changed while it was read")
    return payload


def _file_identity(path: Path, maximum: int, label: str) -> tuple[int, str]:
    descriptor = _open_regular(path, label)
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
    if (first.st_size, first.st_mtime_ns) != (second.st_size, second.st_mtime_ns):
        _fail(f"{label} changed while it was read")
    if size <= 0:
        _fail(f"{label} is empty")
    return size, digest.hexdigest()


def _copy_bounded(source: Path, destination: Path, maximum: int, label: str) -> None:
    if destination.exists() or destination.is_symlink():
        _fail(f"asset {destination.name!r} would be written twice")
    _file_identity(source, maximum, label)
    with (
        open(source, "rb") as reader,
        open(destination, "xb") as writer,
    ):
        shutil.copyfileobj(reader, writer, HASH_CHUNK_BYTES)
    os.chmod(destination, 0o644)


def _select_rpms(rpm_root: Path) -> list[Path]:
    """Select the binary RPMs a release publishes, in a fixed order.

    Source RPMs and debug packages stay in the workflow artifact: the source
    is the tag itself, and nothing installs debug packages from a release.
    """

    selected: list[Path] = []
    for directory in (FINAL_RPM_DIRECTORY, COMPANION_RPM_DIRECTORY):
        root = rpm_root / directory
        if not root.is_dir():
            _fail(f"RPM directory {directory!r} is missing from the producer output")
        names = sorted(
            entry.name
            for entry in root.iterdir()
            if entry.is_file()
            and not entry.is_symlink()
            and entry.name.endswith(".rpm")
        )
        for name in names:
            if _RPM_NAME_RE.fullmatch(name) is None:
                _fail(f"RPM name is not publishable: {name!r}")
            if name.endswith(".src.rpm"):
                continue
            if "-debuginfo-" in name or "-debugsource-" in name:
                continue
            selected.append(root / name)
    if not selected:
        _fail("no binary RPM was produced for the release")
    return selected


def assemble_assets(
    *,
    tag: ReleaseTag,
    transfer_root: Path,
    rpm_root: Path,
    output_dir: Path,
) -> list[str]:
    """Copy the release assets into ``output_dir`` and write the checksum list."""

    if output_dir.exists() or output_dir.is_symlink():
        _fail("asset output directory must not already exist")
    fixed = expected_fixed_assets(tag.version)
    rpms = _select_rpms(rpm_root)
    names = [*fixed, *(path.name for path in rpms), CHECKSUM_FILE_NAME]
    if len(names) > MAX_ASSET_COUNT:
        _fail("the release would carry too many assets")
    if len({name.casefold() for name in names}) != len(names):
        _fail("release asset names collide")
    for name in names:
        if _ASSET_NAME_RE.fullmatch(name) is None:
            _fail(f"asset name is not publishable: {name!r}")

    output_dir.mkdir(mode=0o755, parents=True, exist_ok=False)
    for name, (relative, maximum) in fixed.items():
        _copy_bounded(
            transfer_root / relative, output_dir / name, maximum, f"asset {name}"
        )
    for path in rpms:
        _copy_bounded(path, output_dir / path.name, MAX_RPM_BYTES, f"asset {path.name}")

    records = {
        name: _file_identity(
            output_dir / name,
            max(MAX_RPM_BYTES, InstallerLimits().max_archive_bytes),
            name,
        )[1]
        for name in names
        if name != CHECKSUM_FILE_NAME
    }
    checksum_lines = "".join(
        f"{digest}  {name}\n" for name, digest in sorted(records.items())
    )
    with open(output_dir / CHECKSUM_FILE_NAME, "xb") as handle:
        handle.write(checksum_lines.encode("ascii"))
    os.chmod(output_dir / CHECKSUM_FILE_NAME, 0o644)
    return sorted(names)


def parse_checksums(payload: bytes) -> dict[str, str]:
    """Parse ``SHA256SUMS`` strictly: one record per line, unique names."""

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


def observe_assets(assets_dir: Path) -> dict[str, tuple[int, str]]:
    """Return the size and digest of every asset, corroborated by ``SHA256SUMS``."""

    if not assets_dir.is_dir() or assets_dir.is_symlink():
        _fail("asset directory is missing")
    entries = sorted(assets_dir.iterdir())
    if len(entries) > MAX_ASSET_COUNT:
        _fail("the asset directory holds too many entries")
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            _fail(f"asset directory entry is not a regular file: {entry.name!r}")
        if _ASSET_NAME_RE.fullmatch(entry.name) is None:
            _fail(f"asset name is not publishable: {entry.name!r}")
    checksums = parse_checksums(
        _read_regular_bytes(
            assets_dir / CHECKSUM_FILE_NAME, MAX_SMALL_ASSET_BYTES, CHECKSUM_FILE_NAME
        )
    )
    names = {entry.name for entry in entries} - {CHECKSUM_FILE_NAME}
    if set(checksums) != names:
        _fail(f"{CHECKSUM_FILE_NAME} does not cover exactly the asset set")
    maximum = max(MAX_RPM_BYTES, InstallerLimits().max_archive_bytes)
    observed: dict[str, tuple[int, str]] = {}
    for name in sorted(names):
        size, digest = _file_identity(assets_dir / name, maximum, f"asset {name}")
        if digest != checksums[name]:
            _fail(f"asset {name!r} disagrees with {CHECKSUM_FILE_NAME}")
        observed[name] = (size, digest)
    checksum_size, checksum_digest = _file_identity(
        assets_dir / CHECKSUM_FILE_NAME, MAX_SMALL_ASSET_BYTES, CHECKSUM_FILE_NAME
    )
    observed[CHECKSUM_FILE_NAME] = (checksum_size, checksum_digest)
    return observed


def verify_assets(
    *,
    tag: ReleaseTag,
    source_commit: str,
    assets_dir: Path,
    notes_path: Path,
    verifier: Any | None = None,
) -> dict[str, Any]:
    """Replay the installer's release verification against the assembled assets."""

    if _SHA1_RE.fullmatch(source_commit) is None:
        _fail("source commit is invalid")
    observed = observe_assets(assets_dir)
    expected_names = set(expected_fixed_assets(tag.version)) | {CHECKSUM_FILE_NAME}
    missing = sorted(expected_names - set(observed))
    if missing:
        _fail(f"release assets are missing: {missing}")
    rpm_names = sorted(name for name in observed if name.endswith(".rpm"))
    if not rpm_names:
        _fail("release carries no RPM")
    for required in (f"tongs-desktop-{tag.version}-", f"python3-tongs-{tag.version}-"):
        if not any(name.startswith(required) for name in rpm_names):
            _fail(f"release carries no RPM named {required}*")
    unexpected = sorted(set(observed) - expected_names - set(rpm_names))
    if unexpected:
        _fail(f"release carries unexpected assets: {unexpected}")

    notes = _read_regular_bytes(notes_path, MAX_NOTES_BYTES, "release notes")
    if not notes.strip():
        _fail("release notes are empty")

    manifest_document = _read_regular_bytes(
        assets_dir / RELEASE_MANIFEST_NAME, MAX_SMALL_ASSET_BYTES, RELEASE_MANIFEST_NAME
    )
    try:
        manifest = parse_release_manifest(manifest_document)
        artifact = select_release_artifact(
            manifest, SUPPORTED_PLATFORM, PackageKind.USER_ARCHIVE
        )
    except ArtifactContractError as error:
        _fail(
            f"release manifest is invalid or has no Fedora 44 x86_64 archive: {error}"
        )
    if manifest.release_version != tag.version:
        _fail("release manifest version does not equal the tag's version")
    if manifest.source_commit != source_commit:
        _fail("release manifest source commit does not equal the tag's commit")
    if artifact.name != archive_name_for(tag.version):
        _fail("release manifest selects an archive with an unexpected name")
    archive_size, archive_digest = observed[artifact.name]
    if (archive_size, archive_digest) != (artifact.byte_count, artifact.sha256):
        _fail("archive asset disagrees with the release manifest")
    try:
        _validate_compatibility(
            manifest.compatibility.core_minimum,
            manifest.compatibility.core_maximum_exclusive,
            manifest.compatibility.rpc_api_major,
            manifest.compatibility.plugin_api_major,
            tag.version,
            PROTOCOL_MAJOR,
            DESKTOP_PLUGIN_API_MAJOR,
        )
    except InstallerError as error:
        _fail(
            f"the installer would refuse this release for core {tag.version}: {error}"
        )

    bundle_document = _read_regular_bytes(
        assets_dir / RELEASE_BUNDLE_NAME,
        InstallerLimits().max_bundle_bytes,
        RELEASE_BUNDLE_NAME,
    )
    identity = _build_identity(tag.tag, source_commit)
    subjects = {
        RELEASE_MANIFEST_NAME: hashlib.sha256(manifest_document).hexdigest(),
        artifact.name: artifact.sha256,
    }
    if verifier is None:
        try:
            verifier = Verifier.production()
        except Exception as error:
            raise ReleasePublicationError(
                "Sigstore production trust root initialization failed"
            ) from error
    try:
        _verify_production_attestation(
            verifier, bundle_document, identity, subjects, InstallerLimits()
        )
    except InstallerError as error:
        _fail(f"the installer's release policy rejected the bundle: {error}")

    return {
        "schema_version": 1,
        "result": "pass",
        "tag": tag.tag,
        "release_version": tag.version,
        "source_commit": source_commit,
        "builder_id": identity.builder_id,
        "subjects": subjects,
        "assets": {
            name: {"bytes": size, "sha256": digest}
            for name, (size, digest) in sorted(observed.items())
        },
        "rpms": rpm_names,
    }


def _run_gh(arguments: Sequence[str]) -> tuple[int, str, str]:
    completed = subprocess.run(
        ["gh", *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _release_endpoint(tag: ReleaseTag) -> str:
    return f"repos/{OFFICIAL_REPOSITORY}/releases/tags/{tag.tag}"


def require_release_absent(tag: ReleaseTag, gh: GhRunner = _run_gh) -> None:
    """Fail unless GitHub reports no release for the tag at all.

    A published release is immutable and must never be replaced, and a draft
    left by an earlier attempt must be inspected by a person, not overwritten.
    """

    status, stdout, stderr = gh(["api", _release_endpoint(tag)])
    if status == 0:
        _fail(f"a release for {tag.tag} already exists")
    if "HTTP 404" not in stderr:
        _fail(f"release lookup for {tag.tag} failed: {(stderr or stdout).strip()}")


def _decode_release(document: str) -> dict[str, Any]:
    try:
        value = json.loads(document)
    except json.JSONDecodeError as error:
        _fail(f"release metadata is not JSON: {error}")
    if not isinstance(value, dict):
        _fail("release metadata is not an object")
    return value


def verify_published_release(
    tag: ReleaseTag,
    assets_dir: Path,
    *,
    expect_draft: bool,
    gh: GhRunner = _run_gh,
) -> dict[str, Any]:
    """Confirm the GitHub release carries the assembled assets exactly.

    With ``expect_draft`` the release must still be a draft, which is the
    state between ``gh release create --draft`` and publication; without it
    the release must be published, stable and immutable, which is what the
    installer requires.
    """

    observed = observe_assets(assets_dir)
    status, stdout, stderr = gh(["api", _release_endpoint(tag)])
    if status != 0:
        _fail(f"release lookup for {tag.tag} failed: {(stderr or stdout).strip()}")
    release = _decode_release(stdout)
    if release.get("tag_name") != tag.tag:
        _fail("published release tag does not equal the requested tag")
    if release.get("draft") is not expect_draft:
        state = "a draft" if expect_draft else "published"
        _fail(f"release {tag.tag} is not {state}")
    if release.get("prerelease") is not False:
        _fail(f"release {tag.tag} is marked as a prerelease")
    if not expect_draft and release.get("immutable") is not True:
        _fail(
            f"release {tag.tag} is not immutable; the installer refuses mutable "
            "releases, so immutable releases must be enabled for the repository"
        )
    raw_assets = release.get("assets")
    if not isinstance(raw_assets, list):
        _fail("published release assets are missing")
    published: dict[str, tuple[int, str, str]] = {}
    for item in raw_assets:
        if not isinstance(item, Mapping):
            _fail("published release asset is invalid")
        name = item.get("name")
        size = item.get("size")
        digest = item.get("digest")
        state = item.get("state")
        if not isinstance(name, str) or name in published:
            _fail("published release asset names are ambiguous")
        if (
            type(size) is not int
            or not isinstance(digest, str)
            or not isinstance(state, str)
        ):
            _fail(f"published asset {name!r} lacks a size, digest or state")
        published[name] = (size, digest, state)
    if set(published) != set(observed):
        _fail(
            "published asset set differs from the assembled set: "
            f"missing={sorted(set(observed) - set(published))}, "
            f"extra={sorted(set(published) - set(observed))}"
        )
    for name, (size, digest) in observed.items():
        published_size, published_digest, state = published[name]
        if state != "uploaded":
            _fail(f"published asset {name!r} is not fully uploaded")
        if published_size != size or published_digest != f"sha256:{digest}":
            _fail(f"published asset {name!r} differs from the assembled asset")
    return {
        "schema_version": 1,
        "result": "pass",
        "tag": tag.tag,
        "draft": expect_draft,
        "immutable": release.get("immutable"),
        "release_id": release.get("id"),
        "assets": sorted(observed),
    }


def _write_report(path: Path | None, report: Mapping[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with open(path, "xb") as handle:
        handle.write((json.dumps(report, indent=2, sort_keys=True) + "\n").encode())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    assemble = commands.add_parser("assemble")
    assemble.add_argument("--tag", required=True)
    assemble.add_argument("--transfer-root", required=True, type=Path)
    assemble.add_argument("--rpm-root", required=True, type=Path)
    assemble.add_argument("--output-dir", required=True, type=Path)

    verify = commands.add_parser("verify")
    verify.add_argument("--tag", required=True)
    verify.add_argument("--source-commit", required=True)
    verify.add_argument("--assets-dir", required=True, type=Path)
    verify.add_argument("--notes", required=True, type=Path)
    verify.add_argument("--report", type=Path, default=None)

    absent = commands.add_parser("require-absent")
    absent.add_argument("--tag", required=True)

    published = commands.add_parser("verify-published")
    published.add_argument("--tag", required=True)
    published.add_argument("--assets-dir", required=True, type=Path)
    published.add_argument("--expect-draft", action="store_true")
    published.add_argument("--report", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one fail-closed release publication command."""

    arguments = _parser().parse_args(argv)
    try:
        tag = ReleaseTag.parse(arguments.tag)
        if arguments.command == "assemble":
            names = assemble_assets(
                tag=tag,
                transfer_root=arguments.transfer_root.resolve(strict=True),
                rpm_root=arguments.rpm_root.resolve(strict=True),
                output_dir=arguments.output_dir,
            )
            print(f"assembled {len(names)} release assets for {tag.tag}")
        elif arguments.command == "verify":
            report = verify_assets(
                tag=tag,
                source_commit=arguments.source_commit,
                assets_dir=arguments.assets_dir.resolve(strict=True),
                notes_path=arguments.notes,
            )
            _write_report(arguments.report, report)
            print(f"release assets for {tag.tag} verified under the installer's policy")
        elif arguments.command == "require-absent":
            require_release_absent(tag)
            print(f"no release exists for {tag.tag}")
        else:
            report = verify_published_release(
                tag,
                arguments.assets_dir.resolve(strict=True),
                expect_draft=arguments.expect_draft,
            )
            _write_report(arguments.report, report)
            state = "draft" if arguments.expect_draft else "published immutable"
            print(f"{state} release {tag.tag} carries the assembled assets")
    except (ReleasePublicationError, OSError, subprocess.SubprocessError) as error:
        print(f"release publication failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
