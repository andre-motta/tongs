#!/usr/bin/env python3
"""Record Fedora providers for issue 52 build and runtime requirements."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

_QUERY_FORMAT = "%{name}|%{epoch}|%{version}|%{release}|%{arch}|%{repoid}\n"


def _run(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    return {
        "command": command,
        "returncode": result.returncode,
        "stderr": result.stderr,
        "stdout": result.stdout,
    }


def _query(argument: str, *, whatprovides: bool) -> dict[str, Any]:
    command = [
        "dnf",
        "--quiet",
        "repoquery",
        "--available",
        "--latest-limit=1",
        "--queryformat",
        _QUERY_FORMAT,
    ]
    if whatprovides:
        command.append("--whatprovides")
    command.append(argument)
    result = _run(command)
    if result["returncode"] != 0:
        detail = str(result["stderr"]).strip()
        raise RuntimeError(f"repoquery failed for {argument}: {detail}")
    providers = []
    for line in sorted(set(str(result["stdout"]).splitlines())):
        if not line:
            continue
        fields = line.split("|")
        if len(fields) != 6:
            raise RuntimeError(f"unexpected repoquery output: {line!r}")
        providers.append(
            dict(zip(("name", "epoch", "version", "release", "arch", "repo"), fields))
        )
    result["packages"] = providers
    return result


def _package_provides(name: str) -> dict[str, Any]:
    return _run(
        [
            "dnf",
            "--quiet",
            "repoquery",
            "--available",
            "--latest-limit=1",
            "--queryformat",
            "%{provides}\n",
            name,
        ]
    )


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
        query = (
            "python3dist(mcp[cli])"
            if requirement == manifest["mcp_requirement"]
            else requirement
        )
        result = _query(query, whatprovides=True)
        records.append(
            {
                "provider_query": query,
                "requirement": requirement,
                "result": result,
            }
        )
        if not result["packages"] and "sigstore" not in requirement:
            missing.append(requirement)

    mcp_names = []
    for name in ("python3-mcp+cli", "python3-mcp"):
        mcp_names.append(
            {
                "name": name,
                "result": _query(name, whatprovides=False),
                "provides_result": _package_provides(name),
            }
        )

    return {
        "schema_version": 2,
        "environment": {
            "dnf_version": _run(["dnf", "--version"]),
            "enabled_repositories": _run(["dnf", "repolist", "--enabled"]),
            "os_release": Path("/etc/os-release").read_text(),
        },
        "mcp_direct_names": mcp_names,
        "missing_requirements": missing,
        "requirements": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = audit(json.loads(args.manifest.read_text()))
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if report["missing_requirements"]:
        missing = ", ".join(report["missing_requirements"])
        raise RuntimeError(f"missing Fedora providers: {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
