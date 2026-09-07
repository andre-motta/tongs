"""Run, capture, and measure the Fedora KDE native smoke workflow."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _descendants(root_pid: int) -> set[int]:
    found = {root_pid}
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        children = Path(f"/proc/{pid}/task/{pid}/children")
        try:
            child_pids = {int(value) for value in children.read_text().split()}
        except (FileNotFoundError, PermissionError, ValueError):
            continue
        new = child_pids - found
        found.update(new)
        pending.extend(new)
    return found


def _rss_kib(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    return 0


def _process_record(pid: int) -> dict[str, object]:
    try:
        cmdline = [
            value.decode(errors="replace")
            for value in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if value
        ]
    except (FileNotFoundError, PermissionError):
        cmdline = []
    return {"pid": pid, "rss_kib": _rss_kib(pid), "cmdline": cmdline}


def _snapshot(root_pid: int) -> dict[str, object]:
    pids = sorted(_descendants(root_pid))
    processes = [_process_record(pid) for pid in pids]
    return {
        "process_count": len(pids),
        "rss_kib": sum(int(process["rss_kib"]) for process in processes),
        "processes": processes,
    }


def _read_report(path: Path) -> dict[str, object] | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def run_capture(
    executable: Path,
    screenshot: Path,
    evidence: Path,
    *,
    timeout: float,
    hold: float,
    ui_probe: Path | None,
) -> int:
    """Launch the installed command, self-capture its Qt window, and record RSS."""
    screenshot = screenshot.resolve()
    evidence = evidence.resolve()
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tongs-webview-smoke-") as temporary:
        report = Path(temporary) / "native-report.json"
        command = [
            str(executable.resolve()),
            "--smoke-report",
            str(report),
            "--smoke-timeout",
            str(timeout),
            "--smoke-close-after",
            str(hold),
            "--screenshot",
            str(screenshot),
        ]
        if ui_probe is not None:
            command.extend(["--ui-probe", str(ui_probe.resolve())])
        started = time.monotonic()
        environment = dict(os.environ)
        environment["PYTHONNOUSERSITE"] = "1"
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        peak = _snapshot(process.pid)
        ready: dict[str, object] | None = None
        deadline = started + timeout
        while time.monotonic() < deadline and process.poll() is None:
            snapshot = _snapshot(process.pid)
            if int(snapshot["rss_kib"]) > int(peak["rss_kib"]):
                peak = snapshot
            ready = _read_report(report)
            if ready is not None and ready.get("phase") == "ready":
                break
            time.sleep(0.1)
        if ready is None or ready.get("phase") != "ready":
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
            raise RuntimeError(f"native smoke never became ready: {stdout}{stderr}")

        steady = _snapshot(process.pid)
        stdout, stderr = process.communicate(timeout=hold + 10)
        final = _read_report(report)
        result = {
            "command": command,
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "ready": ready,
            "final": final,
            "steady": steady,
            "peak": peak,
            "wall_ms": round((time.monotonic() - started) * 1000, 1),
            "screenshot": str(screenshot),
            "screenshot_bytes": screenshot.stat().st_size
            if screenshot.is_file()
            else 0,
        }
        evidence.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return (
        0
        if result["exit_code"] == 0
        and result["screenshot_bytes"]
        and final is not None
        and final.get("ok") is True
        and final.get("assets_stopped") is True
        else 1
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--hold", type=float, default=3.0)
    parser.add_argument("--ui-probe", type=Path)
    args = parser.parse_args(argv)
    try:
        return run_capture(
            args.executable,
            args.screenshot,
            args.evidence,
            timeout=args.timeout,
            hold=args.hold,
            ui_probe=args.ui_probe,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
