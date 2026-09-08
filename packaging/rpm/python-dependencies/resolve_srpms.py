#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


def resolve_srpms(
    distributions: list[str], metadata: str, srpm_dir: Path
) -> list[tuple[str, Path]]:
    by_name: dict[str, Path] = {}
    for line in metadata.splitlines():
        fields = line.split("|")
        if len(fields) != 5:
            raise RuntimeError(f"unexpected SRPM metadata: {line!r}")
        name, _version, _release, _header_arch, filename = fields
        if Path(filename).name != filename or not filename.endswith(".src.rpm"):
            raise RuntimeError(f"unsafe SRPM filename in metadata: {filename!r}")
        path = srpm_dir / filename
        if not path.is_file():
            raise RuntimeError(f"SRPM metadata has no matching file: {path.name}")
        if name in by_name:
            raise RuntimeError(f"duplicate SRPM name: {name}")
        by_name[name] = path

    resolved = []
    for distribution in distributions:
        expected_name = f"python-{distribution}"
        if expected_name not in by_name:
            raise RuntimeError(f"missing exact SRPM name: {expected_name}")
        resolved.append((distribution, by_name[expected_name]))
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-order", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--srpm-dir", type=Path, required=True)
    args = parser.parse_args()
    distributions = [line for line in args.build_order.read_text().splitlines() if line]
    for distribution, path in resolve_srpms(
        distributions, args.metadata.read_text(), args.srpm_dir
    ):
        print(f"{distribution}|{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
