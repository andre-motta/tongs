#!/usr/bin/env python3
"""Select the exact manifest-declared companion RPMs for consumer repositories."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

_QUERY_FORMAT = "%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_identities(manifest: dict[str, Any]) -> list[str]:
    identities = []
    for companion in manifest["companions"]:
        binary = companion["binary_rpm"]
        identities.append(
            "|".join(
                (
                    binary["name"],
                    str(binary["epoch"]),
                    companion["version"],
                    binary["release"],
                    binary["architecture"],
                )
            )
        )
    if len(identities) != 7 or len(set(identities)) != 7:
        raise ValueError("companion manifest must declare seven unique binary RPMs")
    return identities


def _rpm_identity(path: Path) -> str:
    return subprocess.run(
        ["rpm", "-qp", "--queryformat", _QUERY_FORMAT, str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def select(
    manifest_path: Path, rpm_dir: Path, output_dir: Path, report_path: Path
) -> None:
    if any(output_dir.iterdir()):
        raise ValueError(f"companion output directory is not empty: {output_dir}")
    manifest_bytes = manifest_path.read_bytes()
    expected = expected_identities(json.loads(manifest_bytes))
    expected_set = set(expected)
    selected: dict[str, dict[str, object]] = {}
    excluded = []
    unexpected = []
    for path in sorted(rpm_dir.glob("*.rpm")):
        identity = _rpm_identity(path)
        record = {
            "filename": path.name,
            "identity": identity,
            "sha256": _sha256(path),
        }
        if identity in expected_set:
            if identity in selected:
                raise ValueError(f"duplicate companion RPM identity: {identity}")
            selected[identity] = record
        elif identity.split("|", 1)[0].endswith(("-debuginfo", "-debugsource")):
            excluded.append(record)
        else:
            unexpected.append(record)
    missing = sorted(expected_set - selected.keys())
    if missing or unexpected:
        raise ValueError(
            f"companion RPM selection mismatch: missing={missing}, "
            f"unexpected={unexpected}"
        )
    for identity in expected:
        filename = str(selected[identity]["filename"])
        shutil.copy2(rpm_dir / filename, output_dir / filename)
    contract = output_dir / "expected-packages.tsv"
    contract.write_text("\n".join(expected) + "\n")
    report = {
        "schema_version": 1,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "expected": expected,
        "selected": [selected[identity] for identity in expected],
        "excluded_debug_packages": excluded,
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--rpm-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    select(args.manifest, args.rpm_dir, args.output_dir, args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
