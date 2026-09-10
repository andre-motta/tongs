"""Run the Electron prototype's Python sidecar over newline-delimited JSON."""

from __future__ import annotations

import argparse
import json
import sys

from backend import Backend, start_assets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend", required=True)
    args = parser.parse_args()
    backend = Backend()
    assets = start_assets(args.frontend, backend)
    print(json.dumps({"event": "ready", "url": assets.url}), flush=True)
    try:
        for line in sys.stdin:
            request = {}
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("Request must be an object")  # noqa: TRY004 - protocol validation error.
                result = backend.invoke(request.get("method"), request.get("params"))
                response = {"id": request.get("id"), "result": result}
                encoded = json.dumps(response)
            except Exception:  # noqa: BLE001 - isolate plugin failures at the RPC boundary.
                response = {
                    "id": request.get("id") if isinstance(request, dict) else None,
                    "error": {"message": "Prototype request failed"},
                }
                encoded = json.dumps(response)
            print(encoded, flush=True)
    finally:
        assets.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
