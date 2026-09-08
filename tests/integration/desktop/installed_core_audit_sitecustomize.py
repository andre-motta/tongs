"""Audit ordinary installed-core startup without importing project code."""

from __future__ import annotations

import atexit
import json
import os
import sys
from importlib.util import find_spec
from pathlib import Path

_AUDIT_PATH = Path(os.environ["TONGS_INSTALLED_CORE_AUDIT_PATH"]).resolve()
_EXPECTED_PYTHON = Path(os.environ["TONGS_INSTALLED_CORE_EXPECTED_PYTHON"]).resolve()
_SOURCE_ROOT = Path(os.environ["TONGS_INSTALLED_CORE_SOURCE_ROOT"]).resolve()
_WRITING = False
_BLOCKED = False
_FORBIDDEN_IMPORTS = (
    "mcp",
    "sigstore",
    "tongs.desktop.installer",
    "tongs.mcp.server",
)
_FORBIDDEN_EVENTS = frozenset(
    {
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "socket.connect",
        "subprocess.Popen",
    }
)


def _append(record: dict[str, object]) -> None:
    global _WRITING
    if _WRITING:
        return
    _WRITING = True
    try:
        descriptor = os.open(_AUDIT_PATH, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            payload = json.dumps(
                record, ensure_ascii=True, separators=(",", ":"), sort_keys=True
            )
            os.write(descriptor, (payload + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        _WRITING = False


def _is_forbidden_import(name: object) -> bool:
    return isinstance(name, str) and any(
        name == prefix or name.startswith(f"{prefix}.") for prefix in _FORBIDDEN_IMPORTS
    )


def _audit(event: str, args: tuple[object, ...]) -> None:
    global _BLOCKED
    forbidden = event in _FORBIDDEN_EVENTS or (
        event == "import" and args and _is_forbidden_import(args[0])
    )
    if not forbidden:
        return
    _BLOCKED = True
    details: dict[str, object] = {
        "argument_types": [type(value).__name__ for value in args[:20]]
    }
    if event == "import" and args and isinstance(args[0], str):
        details["module"] = args[0]
    _append(
        {
            "event": "blocked",
            "audit_event": event,
            "details": details,
            "pid": os.getpid(),
        }
    )
    raise RuntimeError(f"forbidden measured-startup activity: {event}")


def _finish() -> None:
    origins: dict[str, str] = {}
    for name, module in sorted(sys.modules.items()):
        if name != "tongs" and not name.startswith("tongs."):
            continue
        origin = getattr(module, "__file__", None)
        if isinstance(origin, str):
            origins[name] = str(Path(origin).resolve())
    loaded_forbidden = sorted(
        name
        for name in sys.modules
        if any(
            name == prefix or name.startswith(f"{prefix}.")
            for prefix in _FORBIDDEN_IMPORTS
        )
    )
    _append(
        {
            "event": "audit_finished",
            "blocked": _BLOCKED,
            "loaded_forbidden_modules": loaded_forbidden,
            "pid": os.getpid(),
            "tongs_module_origins": origins,
        }
    )


_mcp_spec = find_spec("mcp")
_append(
    {
        "event": "audit_started",
        "cwd": str(Path.cwd().resolve()),
        "executable": str(Path(sys.executable).resolve()),
        "expected_executable": str(_EXPECTED_PYTHON),
        "mcp_available": _mcp_spec is not None,
        "pid": os.getpid(),
        "source_root": str(_SOURCE_ROOT),
        "sys_path": [str(Path(item or ".").resolve()) for item in sys.path],
    }
)
sys.addaudithook(_audit)
atexit.register(_finish)
