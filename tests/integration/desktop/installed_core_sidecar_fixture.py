"""Run the installed sidecar with an exact, source-bound controlled forge."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path
from types import ModuleType
from typing import cast

import tongs
from tongs.desktop.protocol.messages import MAX_REQUEST_FRAME_BYTES
from tongs.desktop.protocol.server import DesktopSidecarServer
from tongs.desktop.sidecar import _PipeWriter, _WritePipeProtocol
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.scanner.repo import ForgeType, Remote, Repo

_EXPECTED_LEDGER = {
    "action": "add_comment",
    "body": "one controlled installed-core mutation",
    "project": "acceptance/shared-drafts",
    "remote_id": "remote-comment-106",
    "review_number": 106,
    "sequence": 1,
    "stage": "remote_accepted_before_ack",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _append_jsonl(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        payload = json.dumps(record, separators=(",", ":"), sort_keys=True)
        os.write(descriptor, (payload + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    if path.stat().st_size > 1024 * 1024:
        raise RuntimeError("controlled ledger exceeds 1 MiB")
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise TypeError("controlled ledger record must be an object")
        records.append(value)
    return records


def validate_prior_ledger(path: Path, forge_mode: str) -> None:
    records = _read_jsonl(path)
    if forge_mode == "block_after_accept" and records:
        raise RuntimeError("first generation started with a nonempty ledger")
    if forge_mode == "acknowledge" and records != [_EXPECTED_LEDGER]:
        raise RuntimeError("restart observed a missing, changed, or replayed mutation")
    if forge_mode not in {"block_after_accept", "acknowledge"}:
        raise RuntimeError("invalid controlled forge mode")


def _required_path(name: str, *, directory: bool = False) -> Path:
    value = os.environ.get(name)
    if value is None:
        raise RuntimeError(f"{name} is required")
    path = Path(value)
    if not path.is_absolute():
        raise RuntimeError(f"{name} must be absolute")
    path = path.resolve()
    if directory and not path.is_dir():
        raise RuntimeError(f"{name} must identify a directory")
    if not directory and not path.is_file():
        raise RuntimeError(f"{name} must identify a file")
    return path


def _load_fixture(path: Path, expected_sha256: str) -> ModuleType:
    if _sha256(path) != expected_sha256:
        raise RuntimeError("controlled fixture hash does not match")
    module_name = "_tongs_installed_core_draft_acceptance_fixture"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("controlled fixture could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


async def run() -> None:
    evidence_root = _required_path("TONGS_INSTALLED_CORE_EVIDENCE_ROOT", directory=True)
    package_root = _required_path("TONGS_INSTALLED_CORE_PACKAGE_ROOT", directory=True)
    source_root = _required_path("TONGS_INSTALLED_CORE_SOURCE_ROOT", directory=True)
    fixture_path = _required_path("TONGS_INSTALLED_CORE_DRAFT_FIXTURE")
    expected_version = os.environ["TONGS_INSTALLED_CORE_VERSION"]
    expected_commit = os.environ["TONGS_INSTALLED_CORE_SOURCE_COMMIT"]
    fixture_sha256 = os.environ["TONGS_INSTALLED_CORE_DRAFT_FIXTURE_SHA256"]
    forge_mode = os.environ["TONGS_INSTALLED_CORE_FORGE_MODE"]
    validate_prior_ledger(evidence_root / "mock-forge-ledger.jsonl", forge_mode)

    actual_package_root = Path(tongs.__file__).resolve().parent
    if actual_package_root != package_root:
        raise RuntimeError("sidecar imported tongs outside the installed candidate")
    if version("tongs") != expected_version:
        raise RuntimeError("sidecar distribution version does not match")
    checkout_paths = [
        item for item in sys.path if item and _inside(Path(item), source_root)
    ]
    if checkout_paths:
        raise RuntimeError("source checkout is present on sidecar sys.path")

    fixture = _load_fixture(fixture_path, fixture_sha256)
    repository_root = evidence_root / "controlled-repository"
    (repository_root / ".git").mkdir(parents=True, exist_ok=True)
    remote = Remote(
        "origin",
        "https://acceptance.example/acceptance/shared-drafts.git",
        fixture.HOST.hostname,
        fixture.PROJECT,
        ForgeType.GITHUB,
    )
    repository = Repo(repository_root, (remote,), remote)
    event_path = evidence_root / "installed-core-process-events.jsonl"
    _append_jsonl(
        event_path,
        {
            "event": "installed_core_process_started",
            "fixture_path": str(fixture_path),
            "fixture_sha256": fixture_sha256,
            "forge_mode": forge_mode,
            "package_root": str(actual_package_root),
            "pid": os.getpid(),
            "source_commit": expected_commit,
            "source_root": str(source_root),
            "python": str(Path(sys.executable).resolve()),
            "sys_path": [str(Path(item or ".").resolve()) for item in sys.path],
            "tongs_version": version("tongs"),
            "wrapper_path": str(Path(__file__).resolve()),
            "wrapper_sha256": _sha256(Path(__file__).resolve()),
        },
    )
    session = fixture.build_session(
        evidence_root,
        fixture.CHANGED_REVISION,
        forge_mode=forge_mode,
        event_path=event_path,
        discoverer=lambda *_args, **_kwargs: (repository,),
    )
    server = DesktopSidecarServer(
        session=session,
        plugin_registry=DesktopPluginRegistry(
            entry_point_source=lambda _group: (), host_version="1.0"
        ),
        shutdown_timeout=2,
    )

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_REQUEST_FRAME_BYTES + 1)
    reader_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: reader_protocol, sys.stdin.buffer)
    writer_protocol = _WritePipeProtocol()
    transport, _ = await loop.connect_write_pipe(
        lambda: writer_protocol, sys.stdout.buffer
    )
    writer = _PipeWriter(cast(asyncio.WriteTransport, transport), writer_protocol)
    try:
        await server.run(reader, writer)
    finally:
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    asyncio.run(run())
