#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


def _query(requirement: str) -> list[dict[str, str]]:
    command = [
        "dnf",
        "--quiet",
        "repoquery",
        "--available",
        "--latest-limit=1",
        "--queryformat",
        "%{name}|%{epoch}|%{version}|%{release}|%{arch}|%{repoid}\n",
        "--whatprovides",
        requirement,
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"repoquery failed for {requirement!r}: {result.stderr.strip()}"
        )
    providers = []
    for line in sorted(set(result.stdout.splitlines())):
        if not line:
            continue
        fields = line.split("|")
        if len(fields) != 6:
            raise RuntimeError(f"unexpected repoquery output: {line!r}")
        providers.append(
            dict(zip(("name", "epoch", "version", "release", "arch", "repo"), fields))
        )
    return providers


def audit(manifest: dict[str, Any]) -> dict[str, Any]:
    system = []
    missing = []
    for item in manifest["system_requirements"]:
        providers = _query(item["requirement"])
        system.append({"requirement": item["requirement"], "providers": providers})
        if not providers:
            missing.append(item["requirement"])

    companions = []
    unexpected = []
    for item in manifest["companions"]:
        requirement = f"python3dist({item['distribution']}) = {item['version']}"
        providers = _query(requirement)
        companions.append({"requirement": requirement, "providers": providers})
        if providers:
            unexpected.append(requirement)

    if missing:
        raise RuntimeError(f"missing Fedora system providers: {', '.join(missing)}")
    if unexpected:
        raise RuntimeError(
            "compatible Fedora providers now exist; reuse them and update the companion "
            f"frontier: {', '.join(unexpected)}"
        )
    return {"schema_version": 1, "system": system, "companions": companions}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    report = audit(manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
