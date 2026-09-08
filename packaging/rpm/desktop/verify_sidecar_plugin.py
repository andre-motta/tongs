#!/usr/bin/env python3
"""Exercise an installed desktop plugin through the production sidecar command."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any, TextIO


def _request(
    process: subprocess.Popen[str],
    output: TextIO,
    request_id: str,
    method: str,
    params: dict[str, object],
) -> dict[str, Any]:
    assert process.stdin is not None
    assert process.stdout is not None
    frame = {
        "v": 1,
        "type": "request",
        "id": request_id,
        "method": method,
        "params": params,
    }
    process.stdin.write(json.dumps(frame, separators=(",", ":")) + "\n")
    process.stdin.flush()
    while line := process.stdout.readline():
        output.write(line)
        response = json.loads(line)
        if response.get("id") != request_id:
            continue
        if "error" in response:
            raise RuntimeError(f"sidecar {method} failed: {response['error']}")
        return response["result"]
    raise RuntimeError(f"sidecar exited before responding to {method}")


def verify(expected_version: str, output_path: Path) -> None:
    command = ["/usr/bin/python3", "-E", "-P", "-m", "tongs.desktop.sidecar"]
    environment = os.environ.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    with output_path.open("w") as output:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            handshake = _request(
                process,
                output,
                "handshake",
                "handshake",
                {
                    "protocol_major": 1,
                    "core_version": expected_version,
                    "capabilities": ["plugins"],
                    "client": "fedora-rpm-lifecycle",
                },
            )
            plugins = _request(process, output, "list", "plugins.list", {})
            invocation = _request(
                process,
                output,
                "rpm-invoke",
                "plugins.invoke",
                {"plugin": "rpm-test", "method": "echo", "params": {"value": 52}},
            )
            shutdown = _request(process, output, "shutdown", "shutdown", {})
            assert handshake["accepted_capabilities"] == ["plugins"]
            plugin = next(
                item for item in plugins["plugins"] if item["plugin_id"] == "rpm-test"
            )
            assert plugin["state"] == "started"
            assert plugin["manifest"]["assets_available"] is True
            assert invocation["value"] == {"invocation": "rpm-invoke", "value": 52}
            assert shutdown == {"accepted": True}
            assert process.stdin is not None
            process.stdin.close()
            status = process.wait(timeout=10)
            if status != 0:
                raise RuntimeError(f"sidecar exited with status {status}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            assert process.stderr is not None
            stderr = process.stderr.read()
            output.write(json.dumps({"command": command, "stderr": stderr}) + "\n")

    distribution = importlib.metadata.distribution("tongs-desktop-test-plugin")
    module = importlib.util.find_spec("tongs_rpm_test_plugin")
    if module is None or module.origin is None:
        raise RuntimeError("installed RPM test plugin module is unavailable")
    paths = [module.origin, os.fspath(distribution.locate_file(""))]
    forbidden = ("/checkout", "/.venv", "/site-packages/.local", "/root/")
    if any(marker in path for path in paths for marker in forbidden):
        raise RuntimeError(f"plugin resolved outside the system installation: {paths}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    verify(args.expected_version, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
