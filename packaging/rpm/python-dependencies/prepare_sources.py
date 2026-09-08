#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(source: dict[str, Any], output: Path) -> None:
    request = urllib.request.Request(
        source["url"], headers={"User-Agent": "tongs-issue-85-source-preparer/1"}
    )
    expected_bytes = source["bytes"]
    read_bytes = 0
    with (
        urllib.request.urlopen(request, timeout=60) as response,
        output.open("wb") as dest,
    ):
        expected_host = urlparse(source["url"]).hostname
        if urlparse(response.geturl()).hostname != expected_host:
            raise RuntimeError(
                f"source redirected outside {expected_host}: {response.geturl()}"
            )
        while block := response.read(1024 * 1024):
            read_bytes += len(block)
            if read_bytes > expected_bytes:
                raise RuntimeError(
                    f"source exceeds declared size: {source['filename']}"
                )
            dest.write(block)
    if read_bytes != expected_bytes:
        raise RuntimeError(
            f"source size mismatch for {source['filename']}: {read_bytes} != {expected_bytes}"
        )
    actual = _sha256(output)
    if actual != source["sha256"]:
        raise RuntimeError(
            f"source hash mismatch for {source['filename']}: {actual} != {source['sha256']}"
        )


def _safe_extract(source: Path, destination: Path) -> None:
    with tarfile.open(source, "r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"unsafe source member: {member.name}")
            if not (member.isdir() or member.isreg()):
                raise RuntimeError(f"unsupported source member type: {member.name}")
        archive.extractall(destination, filter="data")


def _relativize_vendor_config(config: str, vendor: Path) -> str:
    absolute = str(vendor)
    relative = config.replace(f'directory = "{absolute}"', 'directory = "vendor"')
    if absolute in relative or 'directory = "vendor"' not in relative:
        raise RuntimeError("cargo vendor config did not contain the expected directory")
    return relative


def _use_system_openssl(source_root: Path) -> None:
    manifest = source_root / "rust" / "Cargo.toml"
    text = manifest.read_text()
    old = 'openssl = { version = "0.10.80", features = ["vendored"] }'
    new = 'openssl = "0.10.80"'
    if text.count(old) != 1:
        raise RuntimeError("unexpected rfc3161-client OpenSSL dependency declaration")
    manifest.write_text(text.replace(old, new))


def _license_candidates(package_dir: Path) -> list[Path]:
    prefixes = ("LICENSE", "COPYING", "NOTICE", "COPYRIGHT")
    return sorted(
        path
        for path in package_dir.iterdir()
        if path.is_file() and path.name.upper().startswith(prefixes)
    )


def _prepare_cargo(
    companion: dict[str, Any], source_archive: Path, output: Path
) -> dict[str, Any]:
    external_licenses = []
    for source in companion["cargo"]["license_sources"]:
        destination = output / source["filename"]
        _download(source, destination)
        external_licenses.append(destination)
    with tempfile.TemporaryDirectory(prefix="tongs-rfc3161-") as temp_name:
        temp = Path(temp_name)
        _safe_extract(source_archive, temp)
        source_root = temp / f"rfc3161_client-{companion['version']}"
        lock_path = source_root / "Cargo.lock"
        lock_hash = _sha256(lock_path)
        if lock_hash != companion["cargo"]["lock_sha256"]:
            raise RuntimeError(f"Cargo.lock hash mismatch: {lock_hash}")
        _use_system_openssl(source_root)
        vendor = source_root / "vendor"
        cargo_env = os.environ.copy()
        cargo_env["CARGO_HOME"] = str(temp / "cargo-home")
        result = subprocess.run(
            ["cargo", "vendor", "--locked", str(vendor)],
            cwd=source_root,
            check=False,
            capture_output=True,
            env=cargo_env,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"cargo vendor failed:\n{result.stderr.strip()}")
        cargo_dir = source_root / ".cargo"
        cargo_dir.mkdir()
        (cargo_dir / "config.toml").write_text(
            _relativize_vendor_config(result.stdout, vendor)
        )
        metadata = subprocess.run(
            [
                "cargo",
                "metadata",
                "--locked",
                "--offline",
                "--filter-platform",
                "x86_64-unknown-linux-gnu",
                "--format-version",
                "1",
            ],
            cwd=source_root,
            check=True,
            capture_output=True,
            env=cargo_env,
            text=True,
        )
        metadata_report = json.loads(metadata.stdout)
        resolved_ids = {node["id"] for node in metadata_report["resolve"]["nodes"]}
        packages = []
        license_root = source_root / "cargo-licenses"
        license_root.mkdir()
        for package in metadata_report["packages"]:
            license_value = package.get("license")
            license_file = package.get("license_file")
            if not license_value and package["name"] in {"rfc3161-client", "tsp-asn1"}:
                license_value = "Apache-2.0"
                license_file = "LICENSE (workspace root)"
            if not license_value and not license_file:
                raise RuntimeError(
                    f"Cargo dependency lacks license metadata: {package['name']} {package['version']}"
                )
            resolved = package["id"] in resolved_ids
            package_license_files = []
            if resolved:
                package_dir = Path(package["manifest_path"]).parent
                candidates = _license_candidates(package_dir)
                if package["name"] in {"rfc3161-client", "tsp-asn1"}:
                    candidates = [source_root / "LICENSE"]
                elif package["name"] == "cryptography-x509":
                    candidates = external_licenses
                if not candidates:
                    raise RuntimeError(
                        "resolved Cargo package lacks required license text: "
                        f"{package['name']} {package['version']}"
                    )
                package_license_dir = (
                    license_root / f"{package['name']}-{package['version']}"
                )
                package_license_dir.mkdir()
                for candidate in candidates:
                    destination = package_license_dir / candidate.name
                    shutil.copyfile(candidate, destination)
                    package_license_files.append(
                        {
                            "path": str(destination.relative_to(license_root)),
                            "sha256": _sha256(destination),
                        }
                    )
            packages.append(
                {
                    "name": package["name"],
                    "version": package["version"],
                    "source": package.get("source"),
                    "license": license_value,
                    "license_file": license_file,
                    "resolved_for_fedora_x86_64": resolved,
                    "disposition": (
                        "binary-license-bundle"
                        if resolved
                        else "retained-vendor-source"
                    ),
                    "bundled_license_files": package_license_files,
                }
            )
        packages.sort(
            key=lambda item: (item["name"], item["version"], item["source"] or "")
        )
        inventory_path = output / "rfc3161-cargo-inventory.json"
        inventory_path.write_text(
            json.dumps(
                {"schema_version": 1, "packages": packages}, indent=2, sort_keys=True
            )
            + "\n"
        )
        shutil.copyfile(inventory_path, license_root / "cargo-inventory.json")
        archive_path = output / "rfc3161-client-1.0.8-cargo-vendor.tar.gz"
        license_archive_path = output / "rfc3161-client-1.0.8-cargo-licenses.tar.gz"
        env = os.environ.copy()
        env["GZIP"] = "-n"
        subprocess.run(
            [
                "tar",
                "--sort=name",
                "--mtime=@0",
                "--owner=0",
                "--group=0",
                "--numeric-owner",
                "-czf",
                str(archive_path),
                "vendor",
                ".cargo/config.toml",
            ],
            cwd=source_root,
            env=env,
            check=True,
        )
        subprocess.run(
            [
                "tar",
                "--sort=name",
                "--mtime=@0",
                "--owner=0",
                "--group=0",
                "--numeric-owner",
                "-czf",
                str(license_archive_path),
                "cargo-licenses",
            ],
            cwd=source_root,
            env=env,
            check=True,
        )
        if any(
            package["name"] == "openssl-src" and package["resolved_for_fedora_x86_64"]
            for package in packages
        ):
            raise RuntimeError("vendored OpenSSL remains in the Fedora resolved graph")
        return {
            "cargo_lock_sha256": lock_hash,
            "cargo_packages": len(packages),
            "vendor_archive": archive_path.name,
            "vendor_archive_sha256": _sha256(archive_path),
            "inventory": inventory_path.name,
            "inventory_sha256": _sha256(inventory_path),
            "resolved_packages": sum(
                package["resolved_for_fedora_x86_64"] for package in packages
            ),
            "license_archive": license_archive_path.name,
            "license_archive_sha256": _sha256(license_archive_path),
            "license_sources": [
                {
                    "filename": path.name,
                    "sha256": _sha256(path),
                    "bytes": path.stat().st_size,
                }
                for path in external_licenses
            ],
        }


def prepare(manifest: dict[str, Any], output: Path) -> dict[str, Any]:
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    records = []
    cargo_record = None
    for companion in manifest["companions"]:
        source = companion["source"]
        destination = output / source["filename"]
        _download(source, destination)
        records.append(
            {
                "distribution": companion["distribution"],
                "version": companion["version"],
                "filename": destination.name,
                "bytes": destination.stat().st_size,
                "sha256": _sha256(destination),
                "license": companion["license"],
            }
        )
        if companion["distribution"] == "rfc3161-client":
            cargo_record = _prepare_cargo(companion, destination, output)
    report = {"schema_version": 1, "sources": records, "cargo": cargo_record}
    (output / "prepared-sources.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    prepare(manifest, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
