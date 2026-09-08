#!/usr/bin/env python3
"""Source identities and accepted desktop payload validation for Fedora RPMs."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.version import Version

_DESCRIBE_RE = re.compile(
    r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)-"
    r"(?P<distance>\d+)-g(?P<short>[0-9a-f]+)$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RESERVED_SOURCE_NAMES = {
    "io.github.andre_motta.tongs.metainfo.xml",
    "manifest.json",
    "package_contract.py",
    "prepared-inputs.json",
    "tongs-desktop",
    "tongs-desktop.1",
    "tongs.desktop",
    "tongs.png",
}


@dataclass(frozen=True)
class SourceIdentity:
    commit: str
    pep440_version: str
    rpm_version: str
    rpm_release: str
    source_date_epoch: int


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_identity(checkout: Path) -> SourceIdentity:
    commit = _git(checkout, "rev-parse", "HEAD")
    describe = _git(checkout, "describe", "--tags", "--long", "--match", "v[0-9]*")
    match = _DESCRIBE_RE.fullmatch(describe)
    if match is None:
        raise RuntimeError(f"unsupported git description: {describe!r}")
    distance = int(match.group("distance"))
    if distance == 0:
        pep440 = ".".join(match.group(name) for name in ("major", "minor", "patch"))
        rpm_version = pep440
    else:
        next_patch = int(match.group("patch")) + 1
        public = f"{match.group('major')}.{match.group('minor')}.{next_patch}"
        short = commit[:10]
        pep440 = f"{public}.dev{distance}+g{short}"
        rpm_version = f"{public}~dev{distance}"
    epoch = int(_git(checkout, "show", "-s", "--format=%ct", "HEAD"))
    date = _git(checkout, "show", "-s", "--format=%cd", "--date=format:%Y%m%d", "HEAD")
    release = f"0.1.{date}git{commit[:7]}"
    return SourceIdentity(commit, pep440, rpm_version, release, epoch)


def _git(checkout: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", os.fspath(checkout), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported manifest schema")
    target = manifest.get("target", {})
    if target != {
        "architecture": "x86_64",
        "distribution": "fedora",
        "release": "44",
        "python_minimum": "3.12",
    }:
        raise ValueError("unexpected Fedora RPM target")
    accepted = manifest.get("accepted_desktop", {})
    if not re.fullmatch(r"[0-9a-f]{40}", accepted.get("source_commit", "")):
        raise ValueError("invalid accepted source commit")
    archive = accepted.get("archive", {})
    archive_filename = archive.get("filename")
    _validate_source_filename(archive_filename)
    if archive.get("bytes", 0) <= 0 or not _SHA256_RE.fullmatch(
        archive.get("sha256", "")
    ):
        raise ValueError("invalid accepted archive identity")
    evidence = accepted.get("evidence", {})
    if not evidence or any(
        not _SHA256_RE.fullmatch(value) for value in evidence.values()
    ):
        raise ValueError("invalid accepted evidence identity")
    source_names = [archive_filename, *evidence]
    if len(source_names) != len(set(source_names)):
        raise ValueError("accepted source filenames must be unique")
    for filename in evidence:
        _validate_source_filename(filename)
    if _RESERVED_SOURCE_NAMES.intersection(source_names):
        raise ValueError("accepted source filename collides with generated RPM input")
    pairing = manifest.get("rpm_pairing")
    if pairing is not None:
        mode = pairing.get("mode")
        core_commit = pairing.get("core_source_commit", "")
        if not re.fullmatch(r"[0-9a-f]{40}", core_commit):
            raise ValueError("invalid bound core source commit")
        if mode == "exact" and core_commit != accepted["source_commit"]:
            raise ValueError("exact RPM source pairing mismatch")
        if mode == "reviewed-fixture":
            fixture = accepted.get("reviewed_fixture", {})
            if not _SHA256_RE.fullmatch(fixture.get("review_report_sha256", "")):
                raise ValueError("reviewed fixture lacks immutable review evidence")
        elif mode != "exact":
            raise ValueError("unsupported RPM source pairing mode")


def _validate_source_filename(filename: object) -> None:
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or PurePosixPath(filename).name != filename
    ):
        raise ValueError("accepted source filename must be a basename")


def bind_manifest(
    manifest: dict[str, Any], identity: SourceIdentity, allow_reviewed_fixture: bool
) -> dict[str, Any]:
    """Bind a payload contract to one exact core source identity."""
    validate_manifest(manifest)
    accepted = manifest["accepted_desktop"]
    payload_commit = accepted["source_commit"]
    if payload_commit == identity.commit:
        mode = "exact"
    elif allow_reviewed_fixture and accepted.get("reviewed_fixture"):
        mode = "reviewed-fixture"
    else:
        raise RuntimeError(
            "desktop payload and core must use the same source commit; "
            "only the explicitly reviewed initial fixture may differ"
        )
    compatibility = accepted["compatibility"]
    minimum = Version(compatibility["core_minimum"].replace("-dev.", ".dev"))
    maximum = Version(compatibility["core_maximum_exclusive"])
    core_version = Version(identity.pep440_version)
    if not minimum <= core_version < maximum:
        raise RuntimeError("core version is outside the desktop compatibility interval")
    bound = copy.deepcopy(manifest)
    bound["rpm_pairing"] = {
        "core_pep440_version": identity.pep440_version,
        "core_source_commit": identity.commit,
        "mode": mode,
        "payload_source_commit": payload_commit,
    }
    validate_manifest(bound)
    return bound


def validate_accepted_payload(
    manifest: dict[str, Any], archive_dir: Path
) -> dict[str, Any]:
    """Verify the accepted #51 archive and return its install manifest."""
    validate_manifest(manifest)
    accepted = manifest["accepted_desktop"]
    archive_record = accepted["archive"]
    archive_path = archive_dir / archive_record["filename"]
    _verify_file(archive_path, archive_record["bytes"], archive_record["sha256"])
    for filename, expected_hash in accepted["evidence"].items():
        path = archive_dir / filename
        if not path.is_file() or sha256(path) != expected_hash:
            raise RuntimeError(f"accepted evidence mismatch: {filename}")

    install = json.loads((archive_dir / "desktop-install.json").read_text())
    desktop_manifest = json.loads(
        (archive_dir / "desktop-manifest-v1.json").read_text()
    )
    runtime_inventory = json.loads((archive_dir / "runtime-inventory.json").read_text())
    provenance = json.loads((archive_dir / "build-provenance.json").read_text())
    if desktop_manifest.get("source_commit") != accepted["source_commit"]:
        raise RuntimeError("accepted source commit mismatch")
    for field in ("release_version", "compatibility"):
        if desktop_manifest.get(field) != accepted[field]:
            raise RuntimeError(f"accepted desktop {field} mismatch")
    expected_platform = {
        "abi": "gnu",
        "architecture": "x86_64",
        "distribution": "fedora",
        "distribution_version": "44",
        "operating_system": "linux",
    }
    if install.get("platform") != expected_platform:
        raise RuntimeError("accepted desktop platform mismatch")
    if install.get("release_version") != accepted["release_version"]:
        raise RuntimeError("accepted desktop release mismatch")
    if install.get("electron_version") != accepted["electron_version"]:
        raise RuntimeError("accepted Electron version mismatch")
    if install.get("compatibility") != accepted["compatibility"]:
        raise RuntimeError("accepted compatibility mismatch")
    install_records = {
        item["path"]: (
            item["byte_count"],
            "0755" if item["executable"] else "0644",
            item["sha256"],
        )
        for item in install["files"]
    }
    inventory_records = {
        item["path"]: (item["byte_count"], item["mode"], item["sha256"])
        for item in runtime_inventory.get("files", [])
    }
    if install_records != inventory_records:
        raise RuntimeError("accepted install and runtime inventories disagree")
    _validate_archive_members(
        archive_path, install, inventory_records, provenance["source_date_epoch"]
    )
    return install


def _verify_file(path: Path, expected_bytes: int, expected_hash: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"missing accepted input: {path.name}")
    if path.stat().st_size != expected_bytes or sha256(path) != expected_hash:
        raise RuntimeError(f"accepted input mismatch: {path.name}")


def _validate_archive_members(
    archive_path: Path,
    install: dict[str, Any],
    inventory_records: dict[str, tuple[int, str, str]] | None = None,
    source_date_epoch: int | None = None,
) -> None:
    records = {item["path"]: item for item in install.get("files", [])}
    if len(records) != len(install.get("files", [])):
        raise RuntimeError("duplicate install manifest path")
    limits = install.get("extraction_limits", {})
    seen: set[str] = set()
    seen_files: set[str] = set()
    total = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > limits.get("max_entries", 0):
            raise RuntimeError("accepted archive entry limit exceeded")
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not member.name:
                raise RuntimeError(f"unsafe accepted archive member: {member.name!r}")
            if len(member.name.encode()) > limits.get("max_path_bytes", 0):
                raise RuntimeError("accepted archive path limit exceeded")
            if member.issym() or member.islnk() or not (
                member.isfile() or member.isdir()
            ):
                raise RuntimeError(
                    f"unsupported accepted archive member: {member.name}"
                )
            owner = (member.uid, member.gid, member.uname, member.gname)
            if owner != (0, 0, "root", "root"):
                raise RuntimeError(f"non-root accepted archive owner: {member.name}")
            if source_date_epoch is not None and member.mtime != source_date_epoch:
                raise RuntimeError(
                    f"accepted archive timestamp mismatch: {member.name}"
                )
            if member.name in seen:
                raise RuntimeError(f"duplicate accepted archive member: {member.name}")
            seen.add(member.name)
            if member.isdir():
                if member.mode != 0o755:
                    raise RuntimeError(
                        f"accepted archive directory mode mismatch: {member.name}"
                    )
                continue
            seen_files.add(member.name)
            total += member.size
            if member.size > limits.get("max_file_bytes", 0):
                raise RuntimeError("accepted archive file limit exceeded")
            if member.name == "desktop-install.json":
                if member.mode != 0o644:
                    raise RuntimeError("accepted install manifest mode mismatch")
                continue
            record = records.get(member.name)
            if record is None:
                raise RuntimeError(f"undeclared accepted archive file: {member.name}")
            stream = archive.extractfile(member)
            if stream is None:
                raise RuntimeError(f"cannot read accepted archive file: {member.name}")
            digest = hashlib.sha256(stream.read()).hexdigest()
            executable = bool(member.mode & stat.S_IXUSR)
            default_mode = "0755" if record["executable"] else "0644"
            expected = (inventory_records or {}).get(
                member.name, (0, default_mode, "")
            )
            expected_mode = int(expected[1], 8)
            if member.mode & 0o7000:
                raise RuntimeError(f"special mode in accepted archive: {member.name}")
            if (
                member.size != record["byte_count"]
                or digest != record["sha256"]
                or executable != record["executable"]
                or member.mode != expected_mode
            ):
                raise RuntimeError(f"accepted archive file mismatch: {member.name}")
        if total > limits.get("max_total_bytes", 0):
            raise RuntimeError("accepted archive total limit exceeded")
    archive_files = seen_files - {"desktop-install.json"}
    if archive_files != set(records):
        missing = sorted(set(records) - archive_files)
        raise RuntimeError(f"accepted archive lacks declared files: {missing}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--archive-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    install = validate_accepted_payload(manifest, args.archive_dir)
    if args.output is not None:
        args.output.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "archive": manifest["accepted_desktop"]["archive"],
                    "file_count": len(install["files"]),
                    "source_commit": manifest["accepted_desktop"]["source_commit"],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
