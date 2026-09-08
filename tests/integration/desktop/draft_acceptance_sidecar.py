"""Source-bound desktop sidecar fixture with a persistent mock-forge ledger."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

_CHECKOUT_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_ROOT = _CHECKOUT_ROOT / "src"
sys.path.insert(0, str(_SOURCE_ROOT))

import tongs
from tongs.config import Config
from tongs.desktop.protocol.messages import MAX_REQUEST_FRAME_BYTES
from tongs.desktop.protocol.server import DesktopSidecarServer
from tongs.desktop.sidecar import _PipeWriter, _WritePipeProtocol
from tongs.forges.base import ForgeClient
from tongs.forges.models import (
    CIStatus,
    ForgeHost,
    ForgeMutationResult,
    MRDetail,
    MRState,
    MRSummary,
    User,
)
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.scanner.repo import ForgeType
from tongs.services import ApplicationSession, ReviewRevision

HOST = ForgeHost("acceptance.example", ForgeType.GITHUB, "https://fixture.invalid")
PROJECT = "acceptance/shared-drafts"
REVIEW_NUMBER = 106
ORIGINAL_REVISION = ReviewRevision("a" * 40, "b" * 40)
CHANGED_REVISION = ReviewRevision("c" * 40, "b" * 40)
NOW = datetime(2026, 9, 8, tzinfo=UTC)


def _append_jsonl(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(
            descriptor,
            (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode(),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _AcceptanceForge:
    supports_batched_review = True
    supports_thread_resolution = True
    supports_draft_notes = False
    supports_unapprove = True
    supports_job_cancel = False

    def __init__(
        self,
        *,
        revision: ReviewRevision,
        ledger_path: Path,
        forge_mode: str,
    ) -> None:
        self._revision = revision
        self._ledger_path = ledger_path
        self._forge_mode = forge_mode

    @property
    def summary(self) -> MRSummary:
        return MRSummary(
            forge_host=HOST,
            repo_path=PROJECT,
            local_path="",
            number=REVIEW_NUMBER,
            title="Shared draft acceptance fixture",
            author=User("fixture", "Controlled fixture"),
            state=MRState.OPEN,
            is_draft=False,
            source_branch="feat/draft-acceptance",
            target_branch="feat/desktop-app",
            ci_status=CIStatus.SUCCESS,
            created_at=NOW,
            updated_at=NOW,
            web_url="https://fixture.invalid/acceptance/shared-drafts/pull/106",
        )

    async def list_mrs(
        self, repo_path: str, state: str = "open", per_page: int = 100
    ) -> list[MRSummary]:
        assert (repo_path, state) == (PROJECT, "open")
        assert per_page > 0
        return [self.summary]

    async def list_my_reviews(self) -> list[MRSummary]:
        return [self.summary]

    async def list_my_mrs(self) -> list[MRSummary]:
        return [self.summary]

    async def get_mr(self, repo_path: str, number: int) -> MRDetail:
        assert (repo_path, number) == (PROJECT, REVIEW_NUMBER)
        return MRDetail(
            **self.summary.__dict__,
            description="Controlled acceptance review.",
            merge_status="can_be_merged",
            head_sha=self._revision.head_sha,
            base_sha=self._revision.base_sha,
            start_sha=self._revision.start_sha,
        )

    async def get_mr_fresh(self, repo_path: str, number: int) -> MRDetail:
        return await self.get_mr(repo_path, number)

    async def add_comment(
        self, repo_path: str, number: int, body: str
    ) -> ForgeMutationResult:
        assert (repo_path, number) == (PROJECT, REVIEW_NUMBER)
        existing = _read_jsonl(self._ledger_path)
        record = {
            "sequence": len(existing) + 1,
            "stage": "remote_accepted_before_ack",
            "action": "add_comment",
            "project": repo_path,
            "review_number": number,
            "body": body,
            "remote_id": "remote-comment-106",
        }
        _append_jsonl(self._ledger_path, record)
        if self._forge_mode == "block_after_accept":
            await asyncio.Event().wait()
        if self._forge_mode != "acknowledge":
            raise RuntimeError("invalid controlled forge mode")
        return ForgeMutationResult("remote-comment-106")

    async def invalidate_review_reads(self, _repo_path: str, _number: int) -> None:
        return None

    async def close(self) -> None:
        return None


class _Registry:
    def __init__(self, forge: _AcceptanceForge) -> None:
        self._forge = forge

    def active_hostnames(self) -> list[str]:
        return [HOST.hostname]

    def get_host(self, hostname: str) -> ForgeHost | None:
        return HOST if hostname == HOST.hostname else None

    async def get_client(self, hostname: str) -> ForgeClient:
        assert hostname == HOST.hostname
        return cast(ForgeClient, self._forge)

    async def close_all(self) -> None:
        await self._forge.close()


class _BoundedCache:
    def __init__(self, event_path: Path | None = None, forge_mode: str = "") -> None:
        self._event_path = event_path
        self._forge_mode = forge_mode

    async def open(self) -> None:
        if self._event_path is not None:
            _append_jsonl(
                self._event_path,
                {
                    "event": "session_started",
                    "forge_mode": self._forge_mode,
                    "pid": os.getpid(),
                    "source_root": str(_SOURCE_ROOT),
                },
            )

    async def close(self) -> None:
        return None


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def build_session(
    evidence_root: Path,
    revision: ReviewRevision,
    *,
    forge_mode: str = "acknowledge",
    event_path: Path | None = None,
    discoverer: object | None = None,
) -> ApplicationSession:
    """Build a production session around isolated, controlled dependencies."""
    forge = _AcceptanceForge(
        revision=revision,
        ledger_path=evidence_root / "mock-forge-ledger.jsonl",
        forge_mode=forge_mode,
    )
    return ApplicationSession(
        config=Config(scan_root=str(evidence_root), max_parallel=2),
        cache=_BoundedCache(event_path, forge_mode),  # type: ignore[arg-type]
        draft_db_path=evidence_root / "drafts.db",
        forge_registry=_Registry(forge),  # type: ignore[arg-type]
        discoverer=discoverer or (lambda *_args, **_kwargs: ()),  # type: ignore[arg-type]
        shutdown_timeout=2,
    )


async def run() -> None:
    expected_source = _SOURCE_ROOT / "tongs"
    actual_source = Path(tongs.__file__).resolve().parent
    if actual_source != expected_source:
        raise RuntimeError("acceptance fixture imported tongs from the wrong checkout")

    evidence_root = Path(os.environ["TONGS_DRAFT_ACCEPTANCE_ROOT"]).resolve()
    revision = ReviewRevision(
        os.environ["TONGS_DRAFT_ACCEPTANCE_HEAD"],
        ORIGINAL_REVISION.base_sha,
    )
    forge_mode = os.environ["TONGS_DRAFT_ACCEPTANCE_FORGE_MODE"]
    session = build_session(
        evidence_root,
        revision,
        forge_mode=forge_mode,
        event_path=evidence_root / "process-events.jsonl",
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
