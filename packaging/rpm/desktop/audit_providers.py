#!/usr/bin/env python3
"""Record Fedora providers for issue 52 build and runtime requirements."""

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
        detail = result.stderr.strip()
        raise RuntimeError(f"repoquery failed for {requirement}: {detail}")
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
    requirements = [
        *manifest["core_runtime_requirements"],
        manifest["mcp_requirement"],
        *manifest["desktop_runtime_requirements"],
        "pyproject-rpm-macros",
        "python3-devel >= 3.12",
        "python3-pip",
        "python3dist(hatchling)",
        "python3dist(hatch-vcs)",
        "/usr/bin/appstreamcli",
        "desktop-file-utils",
    ]
    records = []
    missing = []
    for requirement in requirements:
        query = requirement
        if requirement == manifest["mcp_requirement"]:
            query = "python3dist(mcp[cli])"
        providers = _query(query)
        records.append(
            {"provider_query": query, "requirement": requirement, "providers": providers}
        )
        if not providers and "sigstore" not in requirement:
            missing.append(requirement)
    if missing:
        raise RuntimeError(f"missing Fedora providers: {', '.join(missing)}")
    return {"schema_version": 1, "requirements": records}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = audit(json.loads(args.manifest.read_text()))
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
