#!/usr/bin/env python3
"""Exercise an installed desktop plugin through the production sidecar command."""

from __future__ import annotations

import argparse
import base64
import hashlib
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


def verify(expected_version: str, expected_module: Path, output_path: Path) -> None:
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
            assets = _request(process, output, "assets", "assets.list", {})
            descriptor = next(
                item
                for item in assets["assets"]
                if item["plugin_id"] == "rpm-test" and item["asset_id"] == "module"
            )
            asset = _request(
                process,
                output,
                "asset-read",
                "assets.read",
                {
                    "asset": descriptor["handle"],
                    "offset": 0,
                    "length": descriptor["byte_count"],
                },
            )
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
            expected_bytes = expected_module.read_bytes()
            assert descriptor["byte_count"] == len(expected_bytes)
            assert descriptor["sha256"] == hashlib.sha256(expected_bytes).hexdigest()
            assert (
                base64.b64decode(asset["data_base64"], validate=True) == expected_bytes
            )
            assert asset["next_offset"] is None
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
    assets_module = importlib.util.find_spec("tongs_rpm_test_plugin_assets")
    if (
        assets_module is None
        or assets_module.submodule_search_locations is None
        or len(assets_module.submodule_search_locations) != 1
    ):
        raise RuntimeError("installed RPM test plugin asset package is unavailable")
    asset_root = Path(next(iter(assets_module.submodule_search_locations)))
    installed_asset = asset_root / "assets/module.mjs"
    if installed_asset.read_bytes() != expected_module.read_bytes():
        raise RuntimeError("installed asset module differs from its source")
    paths = [
        module.origin,
        os.fspath(distribution.locate_file("")),
        os.fspath(asset_root),
        os.fspath(installed_asset),
    ]
    forbidden = ("/checkout", "/.venv", "/root/.local", "/home/")
    if any(marker in path for path in paths for marker in forbidden):
        raise RuntimeError(f"plugin resolved outside the system installation: {paths}")
    with output_path.open("a") as output:
        output.write(
            json.dumps(
                {
                    "asset_package_root": os.fspath(asset_root),
                    "asset_sha256": hashlib.sha256(
                        installed_asset.read_bytes()
                    ).hexdigest(),
                    "distribution_root": os.fspath(distribution.locate_file("")),
                    "module_origin": module.origin,
                }
            )
            + "\n"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-module", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    verify(args.expected_version, args.expected_module, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
