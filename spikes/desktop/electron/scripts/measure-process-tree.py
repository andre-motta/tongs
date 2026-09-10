"""Measure peak resident memory for a native Electron smoke command."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path


def process_snapshot() -> dict[int, tuple[int, int, int]]:
    """Return pid -> (parent pid, RSS KiB, PSS KiB) for Linux processes."""
    result: dict[int, tuple[int, int, int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = {}
            for line in (entry / "status").read_text().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    fields[key] = value.strip()
            pss_kib = 0
            try:
                for line in (entry / "smaps_rollup").read_text().splitlines():
                    if line.startswith("Pss:"):
                        pss_kib = int(line.split()[1])
                        break
            except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
                pass
            result[int(entry.name)] = (
                int(fields["PPid"]),
                int(fields.get("VmRSS", "0 kB").split()[0]),
                pss_kib,
            )
        except (FileNotFoundError, KeyError, PermissionError, ValueError):
            continue
    return result


def descendants(root: int, snapshot: dict[int, tuple[int, int, int]]) -> set[int]:
    """Find the root and all transitive children in a process snapshot."""
    found = {root}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _rss, _pss) in snapshot.items():
            if parent in found and pid not in found:
                found.add(pid)
                changed = True
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("provide a command after --")

    started = time.monotonic()
    process = subprocess.Popen(command)
    peak_rss_kib = 0
    peak_pss_kib = 0
    peak_processes = 0
    samples = 0
    while process.poll() is None:
        snapshot = process_snapshot()
        tree = descendants(process.pid, snapshot)
        peak_rss_kib = max(
            peak_rss_kib,
            sum(snapshot.get(pid, (0, 0, 0))[1] for pid in tree),
        )
        peak_pss_kib = max(
            peak_pss_kib,
            sum(snapshot.get(pid, (0, 0, 0))[2] for pid in tree),
        )
        peak_processes = max(peak_processes, len(tree))
        samples += 1
        time.sleep(0.05)
    report = {
        "command": command,
        "exit_code": process.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "peak_processes": peak_processes,
        "peak_rss_kib": peak_rss_kib,
        "peak_pss_kib": peak_pss_kib,
        "samples": samples,
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(output)
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
