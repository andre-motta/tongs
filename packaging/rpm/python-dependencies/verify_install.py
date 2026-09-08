#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

from packaging.version import Version
from sigstore.models import Bundle, ClientTrustConfig
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

FIXTURE_SHA256 = "6b034d6a6046beffec171b4b087001e97029b0bf97b42feea9e3a3deb3fdcffe"
TRUST_SHA256 = "53553bc92bfb7e0d408c01c84a03df573b33b9900b82c9e3062e186f7094c728"
SOURCE_SHA = "181074f4dc11b7e85ef44556e25248ef14fcb554"
REPOSITORY_URL = "https://github.com/sigstore/sigstore-python"
REPOSITORY_ID = "447691086"
REPOSITORY_OWNER_ID = "71096353"
REF = "refs/tags/v4.5.0"
BUILDER = f"{REPOSITORY_URL}/.github/workflows/release.yml@{REF}"
ISSUER = "https://token.actions.githubusercontent.com"
CARGO_LICENSE_ROOT = Path("/usr/share/licenses/python3-rfc3161-client")


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(manifest: dict[str, Any], fixture_dir: Path) -> dict[str, Any]:
    if sys.version_info < (3, 12):  # noqa: UP036 - Verify installed RPM runtime.
        raise RuntimeError(f"Python 3.12 or newer required, got {sys.version}")
    versions = {}
    imports = []
    for item in manifest["system_requirements"]:
        module = item["import"]
        if module:
            importlib.import_module(module)
            imports.append(module)
    for item in manifest["companions"]:
        name = item["distribution"]
        expected = item["version"]
        actual = importlib.metadata.version(name)
        if actual != expected:
            raise RuntimeError(f"{name} version mismatch: {actual} != {expected}")
        importlib.import_module(item["module"])
        versions[name] = actual
    sigstore_version = Version(versions["sigstore"])
    if not Version("4.5") <= sigstore_version < Version("5"):
        raise RuntimeError(f"unsupported Sigstore version: {sigstore_version}")

    cargo_inventory = json.loads(
        (CARGO_LICENSE_ROOT / "cargo-inventory.json").read_text()
    )
    rust_manifest = next(
        item
        for item in manifest["companions"]
        if item["distribution"] == "rfc3161-client"
    )
    if (
        cargo_inventory["upstream_cargo_lock_sha256"]
        != rust_manifest["cargo"]["lock_sha256"]
    ):
        raise RuntimeError("installed Cargo inventory lock hash mismatch")
    resolved_cargo = [
        package
        for package in cargo_inventory["packages"]
        if package["resolved_for_fedora_x86_64"]
    ]
    for package in resolved_cargo:
        if not package["bundled_license_files"]:
            raise RuntimeError(
                f"resolved Cargo package lacks bundled license: {package['name']}"
            )
        for license_file in package["bundled_license_files"]:
            path = CARGO_LICENSE_ROOT / "cargo" / license_file["path"]
            if not path.is_file() or _hash(path) != license_file["sha256"]:
                raise RuntimeError(f"Cargo license file mismatch: {path}")
    if any(package["name"] == "openssl-src" for package in resolved_cargo):
        raise RuntimeError("vendored OpenSSL appears in installed Cargo inventory")

    fixture = fixture_dir / "sigstore-python-4.5.0.intoto.sigstore.json"
    trust = fixture_dir / "sigstore-production-client-trust-config.json"
    if _hash(fixture) != FIXTURE_SHA256 or _hash(trust) != TRUST_SHA256:
        raise RuntimeError("retained Sigstore fixture hash mismatch")
    config = ClientTrustConfig.from_json(trust.read_text())
    verifier = Verifier(trusted_root=config.trusted_root)
    bundle = Bundle.from_json(fixture.read_bytes())
    policy = AllOf(
        [
            Identity(identity=BUILDER, issuer=ISSUER),
            OIDCIssuerV2(ISSUER),
            OIDCRunnerEnvironment("github-hosted"),
            OIDCSourceRepositoryURI(REPOSITORY_URL),
            OIDCSourceRepositoryIdentifier(REPOSITORY_ID),
            OIDCSourceRepositoryOwnerIdentifier(REPOSITORY_OWNER_ID),
            OIDCSourceRepositoryDigest(SOURCE_SHA),
            OIDCSourceRepositoryRef(REF),
            OIDCBuildSignerURI(BUILDER),
            OIDCBuildConfigURI(BUILDER),
            OIDCBuildConfigDigest(SOURCE_SHA),
            OIDCBuildTrigger("release"),
        ]
    )
    payload_type, payload = verifier.verify_dsse(bundle, policy)
    if payload_type != "application/vnd.in-toto+json":
        raise RuntimeError(f"unexpected DSSE payload type: {payload_type}")
    statement = json.loads(payload)
    expected_subject = {
        "name": "sigstore-4.5.0-py3-none-any.whl",
        "digest": {
            "sha256": "f045b207f2e12605cf775ec38e89c5eda625d71ffa7830477db65e47ec2bc8b2"
        },
    }
    if statement["subject"][0] != expected_subject:
        raise RuntimeError("verified statement subject mismatch")
    rust = importlib.import_module("rfc3161_client._rust")
    return {
        "schema_version": 1,
        "python": sys.version,
        "imports": sorted(imports),
        "companion_versions": versions,
        "rfc3161_extension": rust.__file__,
        "resolved_cargo_packages": len(resolved_cargo),
        "verified_payload_type": payload_type,
        "verified_subject": expected_subject,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify(json.loads(args.manifest.read_text()), args.fixture_dir)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
