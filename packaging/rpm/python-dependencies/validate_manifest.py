#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported manifest schema")
    target = manifest["target"]
    if target != {
        "architecture": "x86_64",
        "distribution": "fedora",
        "release": "44",
        "python_minimum": "3.12",
    }:
        raise ValueError("unexpected target")
    companions = manifest["companions"]
    names = [item["distribution"] for item in companions]
    if names != list(dict.fromkeys(names)):
        raise ValueError("duplicate companion distribution")
    if manifest["build_order"] != names:
        raise ValueError("companions must be listed in build order")
    for item in companions:
        source = item["source"]
        parsed = urlparse(source["url"])
        if parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org":
            raise ValueError(f"unapproved source host for {item['distribution']}")
        if Path(parsed.path).name != source["filename"]:
            raise ValueError(f"source filename mismatch for {item['distribution']}")
        if not _SHA256.fullmatch(source["sha256"]):
            raise ValueError(f"invalid source hash for {item['distribution']}")
        if not isinstance(source["bytes"], int) or source["bytes"] <= 0:
            raise ValueError(f"invalid source size for {item['distribution']}")
        if not item["license"]:
            raise ValueError(f"missing license for {item['distribution']}")
    rust = next(item for item in companions if item["distribution"] == "rfc3161-client")
    if rust["build_backend"] != "maturin" or not rust["cargo"]["system_openssl_patch"]:
        raise ValueError(
            "rfc3161-client must use bounded Fedora Rust tooling and system OpenSSL"
        )
    if not _SHA256.fullmatch(rust["cargo"]["lock_sha256"]):
        raise ValueError("invalid Cargo.lock hash")
    commit = rust["cargo"]["git_dependency"].rsplit("#", 1)[1]
    license_prefix = (
        f"https://raw.githubusercontent.com/pyca/cryptography/{commit}/LICENSE"
    )
    for source in rust["cargo"]["license_sources"]:
        if not source["url"].startswith(license_prefix):
            raise ValueError("unapproved Cargo license source")
        if Path(urlparse(source["url"]).path).name != source["filename"].removeprefix(
            "cryptography-"
        ):
            raise ValueError("Cargo license source filename mismatch")
        if not _SHA256.fullmatch(source["sha256"]):
            raise ValueError("invalid Cargo license source hash")
        if not isinstance(source["bytes"], int) or source["bytes"] <= 0:
            raise ValueError("invalid Cargo license source size")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    validate(json.loads(args.manifest.read_text()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
