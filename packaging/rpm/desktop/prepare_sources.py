#!/usr/bin/env python3
"""Prepare immutable source inputs and materialized specs for issue 52."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from package_contract import (
    bind_manifest,
    sha256,
    source_identity,
    validate_accepted_payload,
)


def _render(source: Path, destination: Path, values: dict[str, str]) -> None:
    contents = source.read_text()
    for key, value in values.items():
        marker = f"@{key}@"
        if marker not in contents:
            raise RuntimeError(f"template {source.name} lacks {marker}")
        contents = contents.replace(marker, value)
    if "@" in contents:
        raise RuntimeError(f"unexpanded marker in {source.name}")
    destination.write_text(contents)


def prepare(
    checkout: Path,
    accepted_dir: Path,
    output_dir: Path,
    manifest_path: Path,
    allow_reviewed_fixture: bool = False,
) -> dict[str, object]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    checkout = checkout.resolve()
    if subprocess.run(
        [
            "git",
            "-C",
            os.fspath(checkout),
            "status",
            "--porcelain",
            "--untracked-files=no",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout:
        raise RuntimeError("tracked checkout must be clean")

    manifest = json.loads(manifest_path.read_text())
    install = validate_accepted_payload(manifest, accepted_dir)
    identity = source_identity(checkout)
    manifest = bind_manifest(manifest, identity, allow_reviewed_fixture)
    sources = output_dir / "SOURCES"
    specs = output_dir / "SPECS"
    sources.mkdir()
    specs.mkdir()

    prefix = f"tongs-{identity.pep440_version}/"
    source_name = f"tongs-{identity.commit}.tar"
    source_path = sources / source_name
    subprocess.run(
        [
            "git",
            "-C",
            os.fspath(checkout),
            "archive",
            "--format=tar",
            f"--prefix={prefix}",
            "-o",
            os.fspath(source_path),
            "HEAD",
        ],
        check=True,
    )

    accepted = manifest["accepted_desktop"]
    accepted_names = [accepted["archive"]["filename"], *accepted["evidence"]]
    for filename in accepted_names:
        shutil.copyfile(accepted_dir / filename, sources / filename)
    shutil.copyfile(
        Path(__file__).parent / "package_contract.py", sources / "package_contract.py"
    )
    (sources / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )

    common = checkout / "packaging" / "desktop" / "common"
    icon = common / "tongs.png"
    runtime_icon_hash = next(
        item["sha256"]
        for item in install["files"]
        if item["path"] == "runtime/share/pixmaps/tongs.png"
    )
    if sha256(icon) != runtime_icon_hash:
        raise RuntimeError("shared icon differs from accepted runtime icon")
    shutil.copyfile(icon, sources / "tongs.png")

    values = {
        "CORE_COMMIT": identity.commit,
        "CORE_PEP440_VERSION": identity.pep440_version,
        "CORE_RPM_VERSION": identity.rpm_version,
        "RPM_RELEASE": identity.rpm_release,
        "SOURCE_ARCHIVE": source_name,
        "DESKTOP_ARCHIVE": accepted["archive"]["filename"],
        "DESKTOP_VERSION": accepted["release_version"],
    }
    templates = Path(__file__).parent / "templates"
    for template in sorted(templates.iterdir()):
        if not template.is_file():
            continue
        destination_name = template.name.removesuffix(".in")
        destination_dir = specs if destination_name.endswith(".spec") else sources
        _render(template, destination_dir / destination_name, values)

    result: dict[str, object] = {
        "schema_version": 1,
        "source": {
            "commit": identity.commit,
            "date_epoch": identity.source_date_epoch,
            "pep440_version": identity.pep440_version,
            "rpm_version": identity.rpm_version,
            "rpm_release_without_dist": identity.rpm_release,
            "archive": source_name,
            "archive_bytes": source_path.stat().st_size,
            "archive_sha256": sha256(source_path),
        },
        "desktop": {
            "accepted_source_commit": accepted["source_commit"],
            "archive": accepted["archive"],
            "file_count": len(install["files"]),
            "pairing_mode": manifest["rpm_pairing"]["mode"],
        },
    }
    (output_dir / "prepared-inputs.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    shutil.copyfile(
        output_dir / "prepared-inputs.json", sources / "prepared-inputs.json"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--accepted-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--allow-reviewed-fixture", action="store_true")
    args = parser.parse_args()
    prepare(
        args.checkout,
        args.accepted_dir,
        args.output_dir,
        args.manifest,
        args.allow_reviewed_fixture,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
