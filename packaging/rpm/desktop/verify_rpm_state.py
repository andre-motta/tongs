#!/usr/bin/env python3
"""Capture and verify installed Fedora RPM state for issue 52."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
from pathlib import Path
from typing import Any


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, capture_output=True, text=True)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(packages: list[str], sentinels: list[Path]) -> dict[str, Any]:
    result: dict[str, Any] = {"packages": {}, "sentinels": {}}
    for name in packages:
        query = _run(
            [
                "rpm",
                "-q",
                name,
                "--queryformat",
                "%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n",
            ],
            check=False,
        )
        if query.returncode != 0:
            result["packages"][name] = None
            continue
        files = []
        for raw in _run(["rpm", "-ql", name]).stdout.splitlines():
            path = Path(raw)
            if path.is_symlink():
                files.append({"path": raw, "link": os.readlink(path)})
            elif path.is_file():
                files.append(
                    {
                        "path": raw,
                        "bytes": path.stat().st_size,
                        "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}",
                        "sha256": _hash(path),
                    }
                )
        result["packages"][name] = {
            "nevra": query.stdout.strip(),
            "files": files,
        }
    for path in sentinels:
        result["sentinels"][os.fspath(path)] = {
            "bytes": path.stat().st_size,
            "sha256": _hash(path),
        }
    return result


def verify_metadata(packages: list[str], output: Path) -> None:
    failures = []
    package_reports = []
    license_files = []
    for name in packages:
        lines = _run(
            [
                "rpm",
                "-q",
                name,
                "--queryformat",
                "[%{FILENAMES}|%{FILEMODES:perms}|%{FILEUSERNAME}|"
                "%{FILEGROUPNAME}|%{FILEFLAGS:fflags}\\n]",
            ]
        ).stdout.splitlines()
        records = []
        for line in lines:
            path, mode, user, group, flags = line.split("|", 4)
            record = {
                "path": path,
                "mode": mode,
                "user": user,
                "group": group,
                "flags": flags,
            }
            records.append(record)
            if user != "root" or group != "root":
                failures.append(f"non-root ownership: {path}")
            if mode[0] != "l" and mode[5] == "w":
                failures.append(f"group-writable package file: {path}")
            if mode[0] != "l" and mode[8] == "w":
                failures.append(f"world-writable package file: {path}")
            if "s" in mode or "t" in mode:
                failures.append(f"special package mode: {path}")
            if "l" in flags:
                license_files.append(path)
        package_reports.append({"name": name, "files": records})

    required_license_fragments = (
        "/usr/share/licenses/python3-tongs/LICENSE",
        "/usr/share/licenses/tongs-desktop/Electron-LICENSE",
        "/usr/share/licenses/tongs-desktop/LICENSES.chromium.html",
        "/usr/share/licenses/tongs-desktop/LICENSES.json",
    )
    for fragment in required_license_fragments:
        if not any(fragment in path for path in license_files):
            failures.append(f"missing RPM license flag: {fragment}")

    icon = Path("/usr/share/pixmaps/tongs.png").read_bytes()
    if icon[:8] != b"\x89PNG\r\n\x1a\n" or icon[12:16] != b"IHDR":
        failures.append("installed system icon is not a PNG")
        dimensions = None
    else:
        dimensions = struct.unpack(">II", icon[16:24])
        if dimensions != (2048, 2048):
            failures.append(f"unexpected icon dimensions: {dimensions}")

    capabilities = _run(
        ["getcap", "-r", "/usr/libexec/tongs-desktop"], check=False
    ).stdout.strip()
    if capabilities:
        failures.append("desktop runtime contains file capabilities")
    if failures:
        raise RuntimeError("; ".join(failures))
    output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "icon_dimensions": dimensions,
                "license_files": sorted(license_files),
                "packages": package_reports,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def verify_elf(package: str, output: Path) -> None:
    requirements = [
        line
        for line in _run(["rpm", "-q", "--requires", package]).stdout.splitlines()
        if re.search(r"(?:ld-linux|\.so(?:\.|\())", line)
    ]
    records = []
    failures = []
    for requirement in sorted(requirements):
        providers = _run(
            ["rpm", "-q", "--whatprovides", requirement], check=False
        )
        if providers.returncode != 0:
            failures.append(f"missing installed ELF provider: {requirement}")
            continue
        provider_records = []
        for provider in providers.stdout.splitlines():
            name = _run(
                ["rpm", "-q", "--queryformat", "%{NAME}", provider]
            ).stdout
            origin = _run(
                [
                    "dnf",
                    "repoquery",
                    "--installed",
                    "--queryformat",
                    "%{name}|%{epoch}|%{version}|%{release}|%{arch}|%{from_repo}\\n",
                    name,
                ]
            ).stdout.splitlines()
            provider_records.append(
                {"installed": provider, "name": name, "repository_records": origin}
            )
        records.append(
            {"requirement": requirement, "providers": provider_records}
        )
    ldd = _run(["ldd", "/usr/libexec/tongs-desktop/tongs-desktop"], check=False)
    if ldd.returncode != 0 or "not found" in ldd.stdout + ldd.stderr:
        failures.append("installed Electron ldd resolution failed")
    rust = _run(
        [
            "/usr/bin/python3",
            "-E",
            "-P",
            "-c",
            "import rfc3161_client._rust as r; print(r.__file__)",
        ]
    ).stdout.strip()
    rust_ldd = _run(["ldd", rust], check=False)
    crypto = next(
        (
            match.group(1)
            for line in rust_ldd.stdout.splitlines()
            if (match := re.search(r"libcrypto\.so\.[^ ]+ => (/[^ ]+)", line))
        ),
        None,
    )
    if crypto is None:
        failures.append("rfc3161-client does not resolve system libcrypto")
        crypto_owner = None
    else:
        crypto_owner = _run(["rpm", "-qf", crypto], check=False).stdout.strip()
        if not crypto_owner or "not owned" in crypto_owner:
            failures.append("resolved libcrypto lacks an installed RPM owner")
    if failures:
        raise RuntimeError("; ".join(failures))
    output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "electron_ldd": ldd.stdout,
                "requirements": records,
                "rfc3161_extension": rust,
                "rfc3161_ldd": rust_ldd.stdout,
                "system_libcrypto": crypto,
                "system_libcrypto_owner": crypto_owner,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--package", action="append", default=[])
    snapshot_parser.add_argument("--sentinel", action="append", type=Path, default=[])
    snapshot_parser.add_argument("--output", type=Path, required=True)
    metadata_parser = subparsers.add_parser("metadata")
    metadata_parser.add_argument("--package", action="append", required=True)
    metadata_parser.add_argument("--output", type=Path, required=True)
    elf_parser = subparsers.add_parser("elf")
    elf_parser.add_argument("--package", required=True)
    elf_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "snapshot":
        report = snapshot(args.package, args.sentinel)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    elif args.command == "metadata":
        verify_metadata(args.package, args.output)
    else:
        verify_elf(args.package, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
