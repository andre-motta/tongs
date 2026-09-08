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
import sys
import sysconfig
from pathlib import Path
from typing import Any

_RPM_PURELIB_SCHEME = "rpm_prefix"


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, capture_output=True, text=True)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(output: Path, value: object) -> None:
    output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _rpm_identity(arguments: list[str]) -> str:
    return _run(
        [
            "rpm",
            *arguments,
            "--queryformat",
            "%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n",
        ]
    ).stdout.strip()


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
                metadata = path.stat()
                files.append(
                    {
                        "path": raw,
                        "bytes": metadata.st_size,
                        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
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
            "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}",
            "sha256": _hash(path),
        }
    return result


def assert_installed(
    expected_rpms: list[Path], absent: list[str], output: Path
) -> None:
    reports = []
    failures = []
    for rpm_path in expected_rpms:
        expected = _rpm_identity(["-qp", os.fspath(rpm_path)])
        name = expected.split("|", 1)[0]
        installed = _rpm_identity(["-q", name])
        verification = _run(["rpm", "-V", name], check=False)
        if installed != expected:
            failures.append(f"installed candidate mismatch for {name}")
        if verification.returncode != 0 or verification.stdout.strip():
            failures.append(f"rpm -V detected installed file drift for {name}")
        reports.append(
            {
                "expected_rpm": os.fspath(rpm_path),
                "expected": expected,
                "installed": installed,
                "verification": {
                    "returncode": verification.returncode,
                    "stderr": verification.stderr,
                    "stdout": verification.stdout,
                },
            }
        )
    absent_reports = []
    for name in absent:
        query = _run(["rpm", "-q", name], check=False)
        if query.returncode == 0:
            failures.append(f"unexpected installed package: {name}")
        absent_reports.append(
            {"name": name, "returncode": query.returncode, "output": query.stdout}
        )
    if failures:
        raise RuntimeError("; ".join(failures))
    _write(
        output,
        {
            "schema_version": 1,
            "absent": absent_reports,
            "expected": reports,
        },
    )


def _package_file_records(name: str) -> list[dict[str, str]]:
    lines = _run(
        [
            "rpm",
            "-q",
            name,
            "--queryformat",
            (
                "[%{FILENAMES}|%{FILEMODES:perms}|%{FILEUSERNAME}|"
                "%{FILEGROUPNAME}|%{FILEFLAGS:fflags}\\n]"
            ),
        ]
    ).stdout.splitlines()
    records = []
    for line in lines:
        path, mode, user, group, flags = line.split("|", 4)
        records.append(
            {
                "path": path,
                "mode": mode,
                "user": user,
                "group": group,
                "flags": flags,
            }
        )
    return records


def _rpm_purelib() -> str:
    if _RPM_PURELIB_SCHEME not in sysconfig.get_scheme_names():
        raise RuntimeError("system Python does not provide the Fedora RPM path scheme")
    value = sysconfig.get_path("purelib", scheme=_RPM_PURELIB_SCHEME)
    if not isinstance(value, str):
        raise TypeError("Fedora RPM purelib path is unavailable")
    path = Path(value)
    expected_abi = f"python{sys.version_info.major}.{sys.version_info.minor}"
    if (
        not path.is_absolute()
        or path.parts[:2] != ("/", "usr")
        or "local" in path.parts[2:]
        or path.parent.name != expected_abi
        or path.name != "site-packages"
    ):
        raise RuntimeError(f"invalid Fedora RPM purelib path: {path}")
    return os.fspath(path)


def _allowed_path(package: str, path: str, purelib: str) -> bool:
    if package == "python3-tongs":
        return (
            path in {"/usr/bin/tongs", f"{purelib}/tongs"}
            or path.startswith(
                (f"{purelib}/tongs/", "/usr/share/licenses/python3-tongs/")
            )
            or (path.startswith(f"{purelib}/tongs-") and ".dist-info" in path)
            or path == "/usr/share/licenses/python3-tongs"
        )
    if package == "python3-tongs+mcp":
        return path == "/usr/bin/tongs-mcp"
    if package == "tongs-desktop":
        return path in {
            "/usr/bin/tongs-desktop",
            "/usr/libexec/tongs-desktop",
            "/usr/share/applications/tongs.desktop",
            "/usr/share/doc/tongs-desktop",
            "/usr/share/licenses/tongs-desktop",
            "/usr/share/metainfo/io.github.andre_motta.tongs.metainfo.xml",
            "/usr/share/pixmaps/tongs.png",
        } or path.startswith(
            (
                "/usr/libexec/tongs-desktop/",
                "/usr/share/doc/tongs-desktop/",
                "/usr/share/licenses/tongs-desktop/",
                "/usr/share/man/man1/tongs-desktop.1",
            )
        )
    if package == "tongs-desktop-test-plugin":
        return (
            path == "/usr/share/licenses/tongs-desktop-test-plugin"
            or path.startswith(
                (
                    f"{purelib}/tongs_desktop_test_plugin-",
                    f"{purelib}/tongs_rpm_test_plugin",
                    "/usr/share/licenses/tongs-desktop-test-plugin/",
                )
            )
        )
    return False


def _expected_mode(path: str, file_type: str, runtime_modes: dict[str, int]) -> int:
    if file_type == "d":
        return 0o755
    if path in runtime_modes:
        return runtime_modes[path]
    if path.startswith("/usr/bin/"):
        return 0o755
    return 0o644


def _assert_tree_equal(source: Path, installed: Path, failures: list[str]) -> None:
    source_files = {
        path.relative_to(source).as_posix(): path
        for path in source.rglob("*")
        if path.is_file()
    }
    installed_files = {
        path.relative_to(installed).as_posix(): path
        for path in installed.rglob("*")
        if path.is_file()
    }
    if source_files.keys() != installed_files.keys():
        failures.append(f"license tree path mismatch: {installed}")
        return
    for relative, source_path in source_files.items():
        if _hash(source_path) != _hash(installed_files[relative]):
            failures.append(f"license copy differs: {installed_files[relative]}")


def verify_metadata(
    packages: list[str],
    install_manifest: Path,
    libexec_dir: Path,
    checkout_license: Path,
    test_plugin_module: Path,
    output: Path,
) -> None:
    failures = []
    package_reports = []
    license_files = []
    owned_paths: dict[str, str] = {}
    purelib = _rpm_purelib()
    manifest = json.loads(install_manifest.read_text())
    runtime_modes = {
        f"{libexec_dir}/{Path(item['path']).relative_to('runtime').as_posix()}": (
            0o755 if item["executable"] else 0o644
        )
        for item in manifest["files"]
    }
    capability_paths = []
    for name in packages:
        records = _package_file_records(name)
        for record in records:
            path = record["path"]
            mode = record["mode"]
            if path in owned_paths and owned_paths[path] != name:
                failures.append(
                    f"path owned by both {owned_paths[path]} and {name}: {path}"
                )
            owned_paths[path] = name
            if not _allowed_path(name, path, purelib):
                failures.append(f"path outside {name} ownership boundary: {path}")
            if record["user"] != "root" or record["group"] != "root":
                failures.append(f"non-root ownership: {path}")
            if len(mode) != 10:
                failures.append(f"unparseable RPM mode for {path}: {mode}")
                continue
            file_type = mode[0]
            if file_type not in {"-", "d"}:
                failures.append(f"unexpected packaged file type {file_type}: {path}")
                continue
            expected_mode = _expected_mode(path, file_type, runtime_modes)
            actual_mode = stat.S_IMODE(Path(path).lstat().st_mode)
            if actual_mode != expected_mode:
                failures.append(
                    f"mode {actual_mode:04o} != {expected_mode:04o}: {path}"
                )
            if actual_mode & 0o7000:
                failures.append(f"special package mode: {path}")
            if file_type == "-":
                capability_paths.append(path)
            if file_type == "-" and "l" in record["flags"]:
                license_files.append(path)
        package_reports.append({"name": name, "files": records})

    expected_license_files = {
        "/usr/share/licenses/python3-tongs/LICENSE",
        "/usr/share/licenses/tongs-desktop/Electron-LICENSE",
        "/usr/share/licenses/tongs-desktop/LICENSES.chromium.html",
        "/usr/share/licenses/tongs-desktop/LICENSES.json",
        "/usr/share/licenses/tongs-desktop-test-plugin/LICENSE",
        *{
            f"/usr/share/licenses/tongs-desktop/runtime-licenses/{path.relative_to(libexec_dir / 'licenses').as_posix()}"
            for path in (libexec_dir / "licenses").rglob("*")
            if path.is_file()
        },
    }
    wheel_license_files = {
        path
        for path in license_files
        if path.startswith(f"{purelib}/tongs-") and ".dist-info/" in path
    }
    if not wheel_license_files:
        failures.append("core wheel metadata contains no RPM-flagged license copy")
    for path in wheel_license_files:
        if _hash(Path(path)) != _hash(checkout_license):
            failures.append(f"core wheel license copy differs from checkout: {path}")
    expected_license_files.update(wheel_license_files)
    if set(license_files) != expected_license_files:
        failures.append(
            "complete RPM license flag inventory differs from expected tree"
        )
    if _hash(Path("/usr/share/licenses/python3-tongs/LICENSE")) != _hash(
        checkout_license
    ):
        failures.append("core license copy differs from checkout LICENSE")
    if _hash(Path("/usr/share/licenses/tongs-desktop-test-plugin/LICENSE")) != _hash(
        checkout_license
    ):
        failures.append("test plugin license copy differs from checkout LICENSE")
    for source_name, installed_name in (
        ("LICENSE", "Electron-LICENSE"),
        ("LICENSES.chromium.html", "LICENSES.chromium.html"),
        ("LICENSES.json", "LICENSES.json"),
    ):
        if _hash(libexec_dir / source_name) != _hash(
            Path("/usr/share/licenses/tongs-desktop") / installed_name
        ):
            failures.append(f"desktop license copy differs: {installed_name}")
    _assert_tree_equal(
        libexec_dir / "licenses",
        Path("/usr/share/licenses/tongs-desktop/runtime-licenses"),
        failures,
    )
    expected_module = Path(f"{purelib}/tongs_rpm_test_plugin_assets/assets/module.mjs")
    if expected_module.read_bytes() != test_plugin_module.read_bytes():
        failures.append("installed test plugin module differs from source")

    capability_output = []
    for offset in range(0, len(capability_paths), 200):
        capabilities = _run(
            ["getcap", *capability_paths[offset : offset + 200]], check=False
        )
        capability_output.append(
            {
                "returncode": capabilities.returncode,
                "stderr": capabilities.stderr,
                "stdout": capabilities.stdout,
            }
        )
        if capabilities.returncode != 0 or capabilities.stdout.strip():
            failures.append("package-owned regular file contains capabilities")

    icon = Path("/usr/share/pixmaps/tongs.png").read_bytes()
    if icon[:8] != b"\x89PNG\r\n\x1a\n" or icon[12:16] != b"IHDR":
        failures.append("installed system icon is not a PNG")
        dimensions = None
    else:
        dimensions = struct.unpack(">II", icon[16:24])
        if dimensions != (2048, 2048):
            failures.append(f"unexpected icon dimensions: {dimensions}")
    if failures:
        raise RuntimeError("; ".join(failures))
    _write(
        output,
        {
            "schema_version": 2,
            "capability_output": capability_output,
            "icon_dimensions": dimensions,
            "license_files": sorted(license_files),
            "packages": package_reports,
            "purelib": purelib,
            "purelib_scheme": _RPM_PURELIB_SCHEME,
        },
    )


def _package_specific_directory(path: str, purelib: str) -> bool:
    return path in {
        "/usr/libexec/tongs-desktop",
        f"{purelib}/tongs",
    } or path.startswith(
        (
            "/usr/libexec/tongs-desktop/",
            "/usr/share/doc/tongs-desktop",
            "/usr/share/licenses/python3-tongs",
            "/usr/share/licenses/tongs-desktop",
            "/usr/share/licenses/tongs-desktop-test-plugin",
            f"{purelib}/tongs/",
            f"{purelib}/tongs-",
            f"{purelib}/tongs_desktop_test_plugin-",
            f"{purelib}/tongs_rpm_test_plugin_assets",
        )
    )


def write_inventory(packages: list[str], output: Path) -> None:
    purelib = _rpm_purelib()
    paths = []
    for name in packages:
        for record in _package_file_records(name):
            path = record["path"]
            if record["mode"].startswith("d"):
                if _package_specific_directory(path, purelib):
                    paths.append({"package": name, "path": path, "type": "directory"})
            else:
                paths.append({"package": name, "path": path, "type": "file"})
    _write(
        output,
        {
            "schema_version": 1,
            "paths": paths,
            "purelib": purelib,
            "purelib_scheme": _RPM_PURELIB_SCHEME,
        },
    )


def assert_absent(inventory: Path, packages: list[str], output: Path) -> None:
    report = json.loads(inventory.read_text())
    remaining = [
        item
        for item in report["paths"]
        if Path(item["path"]).exists() or Path(item["path"]).is_symlink()
    ]
    installed = [
        name
        for name in packages
        if _run(["rpm", "-q", name], check=False).returncode == 0
    ]
    if remaining or installed:
        raise RuntimeError(
            f"owned paths or packages remain after uninstall: {remaining}, {installed}"
        )
    _write(output, {"schema_version": 1, "packages": packages, "remaining": []})


def verify_elf(package: str, output: Path) -> None:
    requirements = [
        line
        for line in _run(["rpm", "-q", "--requires", package]).stdout.splitlines()
        if re.search(r"(?:ld-linux|\.so(?:\.|\())", line)
    ]
    records = []
    failures = []
    for requirement in sorted(requirements):
        providers = _run(["rpm", "-q", "--whatprovides", requirement], check=False)
        if providers.returncode != 0:
            failures.append(f"missing installed ELF provider: {requirement}")
            continue
        provider_records = []
        for provider in providers.stdout.splitlines():
            name = _run(["rpm", "-q", "--queryformat", "%{NAME}", provider]).stdout
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
        records.append({"requirement": requirement, "providers": provider_records})
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
    _write(
        output,
        {
            "schema_version": 1,
            "electron_ldd": ldd.stdout,
            "requirements": records,
            "rfc3161_extension": rust,
            "rfc3161_ldd": rust_ldd.stdout,
            "system_libcrypto": crypto,
            "system_libcrypto_owner": crypto_owner,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--package", action="append", default=[])
    snapshot_parser.add_argument("--sentinel", action="append", type=Path, default=[])
    snapshot_parser.add_argument("--output", type=Path, required=True)
    installed_parser = subparsers.add_parser("assert-installed")
    installed_parser.add_argument(
        "--expected-rpm", action="append", type=Path, default=[]
    )
    installed_parser.add_argument("--absent", action="append", default=[])
    installed_parser.add_argument("--output", type=Path, required=True)
    metadata_parser = subparsers.add_parser("metadata")
    metadata_parser.add_argument("--package", action="append", required=True)
    metadata_parser.add_argument("--install-manifest", type=Path, required=True)
    metadata_parser.add_argument("--libexec-dir", type=Path, required=True)
    metadata_parser.add_argument("--checkout-license", type=Path, required=True)
    metadata_parser.add_argument("--test-plugin-module", type=Path, required=True)
    metadata_parser.add_argument("--output", type=Path, required=True)
    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("--package", action="append", required=True)
    inventory_parser.add_argument("--output", type=Path, required=True)
    absent_parser = subparsers.add_parser("assert-absent")
    absent_parser.add_argument("--inventory", type=Path, required=True)
    absent_parser.add_argument("--package", action="append", required=True)
    absent_parser.add_argument("--output", type=Path, required=True)
    elf_parser = subparsers.add_parser("elf")
    elf_parser.add_argument("--package", required=True)
    elf_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "snapshot":
        _write(args.output, snapshot(args.package, args.sentinel))
    elif args.command == "assert-installed":
        assert_installed(args.expected_rpm, args.absent, args.output)
    elif args.command == "metadata":
        verify_metadata(
            args.package,
            args.install_manifest,
            args.libexec_dir,
            args.checkout_license,
            args.test_plugin_module,
            args.output,
        )
    elif args.command == "inventory":
        write_inventory(args.package, args.output)
    elif args.command == "assert-absent":
        assert_absent(args.inventory, args.package, args.output)
    else:
        verify_elf(args.package, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
