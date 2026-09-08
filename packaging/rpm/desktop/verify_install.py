#!/usr/bin/env python3
"""Verify an installed issue 52 core and desktop package pair."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(
    install_manifest: Path,
    libexec_dir: Path,
    expected_version: str,
    expected_launcher: Path,
    expected_desktop: Path,
    output: Path,
) -> None:
    manifest = json.loads(install_manifest.read_text())
    failures = []
    for record in manifest["files"]:
        relative = Path(record["path"])
        if relative.parts[0] != "runtime":
            failures.append(f"unexpected payload path {relative}")
            continue
        installed = libexec_dir.joinpath(*relative.parts[1:])
        expected_mode = 0o755 if record["executable"] else 0o644
        if not installed.is_file():
            failures.append(f"missing {installed}")
            continue
        actual_mode = stat.S_IMODE(installed.stat().st_mode)
        if actual_mode != expected_mode:
            failures.append(f"mode {actual_mode:o} for {installed}")
        if installed.stat().st_size != record["byte_count"]:
            failures.append(f"size mismatch for {installed}")
        if _hash(installed) != record["sha256"]:
            failures.append(f"hash mismatch for {installed}")
        if installed.stat().st_mode & 0o7000:
            failures.append(f"special mode for {installed}")

    actual_paths = {
        path.relative_to(libexec_dir).as_posix()
        for path in libexec_dir.rglob("*")
        if path.is_file()
    }
    expected_paths = {
        Path(item["path"]).relative_to("runtime").as_posix()
        for item in manifest["files"]
    }
    if actual_paths != expected_paths:
        failures.append("installed runtime path set differs from accepted payload")
    if importlib.metadata.version("tongs") != expected_version:
        failures.append("installed Python distribution version mismatch")
    if sys.version_info < (3, 12):
        failures.append("system Python is below 3.12")
    if sys.executable != "/usr/bin/python3":
        failures.append(f"verification did not use /usr/bin/python3: {sys.executable}")
    distribution = importlib.metadata.distribution("tongs")
    module_paths = []
    for module_name in ("tongs", "tongs.app", "tongs.desktop.sidecar"):
        module = importlib.util.find_spec(module_name)
        if module is None or module.origin is None:
            failures.append(f"installed module is unavailable: {module_name}")
            continue
        module_paths.append(module.origin)
    module_paths.append(os.fspath(distribution.locate_file("")))
    forbidden = ("/checkout", "/.venv", "/root/.local", "/home/")
    for path in module_paths:
        if any(marker in path for marker in forbidden):
            failures.append(f"module resolved outside the system installation: {path}")
    if Path("/usr/bin/tongs-desktop").read_bytes() != expected_launcher.read_bytes():
        failures.append("system launcher differs from prepared source")
    installed_desktop = Path("/usr/share/applications/tongs.desktop")
    if installed_desktop.read_bytes() != expected_desktop.read_bytes():
        failures.append("system desktop entry differs from prepared source")
    if Path("/usr/share/pixmaps/tongs.png").read_bytes() != (
        libexec_dir / "share/pixmaps/tongs.png"
    ).read_bytes():
        failures.append("system icon differs from accepted runtime icon")
    if failures:
        raise RuntimeError("; ".join(failures))
    output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "core_version": expected_version,
                "executable": sys.executable,
                "file_count": len(expected_paths),
                "libexec": os.fspath(libexec_dir),
                "module_paths": module_paths,
                "python": sys.version.split()[0],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-manifest", required=True, type=Path)
    parser.add_argument("--libexec-dir", required=True, type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-launcher", required=True, type=Path)
    parser.add_argument("--expected-desktop", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    verify(
        args.install_manifest,
        args.libexec_dir,
        args.expected_version,
        args.expected_launcher,
        args.expected_desktop,
        args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
