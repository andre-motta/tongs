#!/usr/bin/env python3
"""Bind the installed MCP closure to the audited Fedora provider."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


def _closure(path: Path) -> dict[str, dict[str, str]]:
    result = {}
    for line in path.read_text().splitlines():
        fields = line.split("|")
        if len(fields) != 6:
            raise RuntimeError(f"invalid installed closure record: {line!r}")
        record = dict(
            zip(("name", "epoch", "version", "release", "arch", "repo"), fields)
        )
        result[record["name"]] = record
    return result


def verify(audit_path: Path, before_path: Path, after_path: Path, output: Path) -> None:
    audit = json.loads(audit_path.read_text())
    resolution = audit["mcp_resolution"]
    if not resolution["usable"] or resolution["mode"] != "direct-package-provides":
        raise RuntimeError("provider audit did not approve the direct MCP package")
    direct_records = {item["name"]: item for item in audit["mcp_direct_names"]}
    before = _closure(before_path)
    after = _closure(after_path)
    changed = {
        name: {"before": before.get(name), "after": after.get(name)}
        for name in sorted(before.keys() | after.keys())
        if before.get(name) != after.get(name)
    }
    if any(value["after"] is None for value in changed.values()):
        raise RuntimeError("MCP installation removed an installed package")

    installed_records = {}
    for name in ("python3-mcp", "python3-mcp+cli"):
        direct = direct_records[name]["result"]["packages"]
        if len(direct) != 1:
            raise RuntimeError(f"provider audit did not resolve one {name} candidate")
        expected = direct[0]
        installed = after.get(name)
        if installed is None:
            raise RuntimeError(f"MCP transaction did not install {name}")
        for field in ("version", "release", "arch"):
            if installed[field] != expected[field]:
                raise RuntimeError(
                    f"installed {name} {field} differs from provider audit"
                )
        if installed["repo"] != expected["repo"]:
            raise RuntimeError(
                f"installed {name} repository differs from provider audit"
            )
        provides = _run(["rpm", "-q", "--provides", name]).stdout
        installed_records[name] = {
            "audit": expected,
            "installed": installed,
            "provides": provides,
        }

    capability = f"python3dist(mcp[cli]) = {resolution['version']}"
    if not re.search(
        rf"^{re.escape(capability)}$",
        installed_records["python3-mcp+cli"]["provides"],
        re.MULTILINE,
    ):
        raise RuntimeError("installed MCP CLI capability differs from provider audit")
    output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "capability": capability,
                "changed_closure": changed,
                "installed_providers": installed_records,
                "unchanged_count": len(before.keys() & after.keys())
                - sum(name in before for name in changed),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify(args.audit, args.before, args.after, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
