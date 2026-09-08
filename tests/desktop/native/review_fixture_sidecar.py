"""Native desktop review proof with production services and mocked forge writes."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from tongs.config import Config
from tongs.desktop.protocol.messages import MAX_REQUEST_FRAME_BYTES
from tongs.desktop.protocol.server import DesktopSidecarServer
from tongs.desktop.sidecar import _PipeWriter, _WritePipeProtocol
from tongs.errors import ConflictError, NetworkError
from tongs.forges.base import ForgeClient
from tongs.forges.models import (
    CIStatus,
    Discussion,
    ForgeHost,
    ForgeMergeResult,
    ForgeMutationResult,
    MRDetail,
    MRState,
    MRSummary,
    SourceCleanupStatus,
    User,
)
from tongs.forges.models import InlineComment as ForgeInlineComment
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.scanner.repo import ForgeType
from tongs.services import (
    ForgeCapabilities,
    MRActionService,
    RawDiffSnapshot,
    RepositoryRef,
    RepositorySnapshot,
    ReviewListItem,
    ReviewMutationService,
    ReviewPage,
    ReviewQuery,
    ReviewRef,
    ReviewRevision,
    ReviewSnapshot,
    ReviewSubmissionService,
    ServiceEvent,
    ServiceEventKind,
)
from tongs.state.drafts import DraftStore

HOST = ForgeHost("fixture.example", ForgeType.GITHUB, "https://fixture.invalid")
REPOSITORY = RepositoryRef(HOST.hostname, "proof/desktop-review")
REVIEW = ReviewRef(REPOSITORY, 46)
REVISION = ReviewRevision("a" * 40, "b" * 40)
NOW = datetime(2026, 9, 8, tzinfo=UTC)
SUMMARY = MRSummary(
    forge_host=HOST,
    repo_path=REPOSITORY.project_path,
    local_path="",
    number=REVIEW.number,
    title="Controlled desktop review proof",
    author=User("fixture", "Controlled fixture"),
    state=MRState.OPEN,
    is_draft=False,
    source_branch="feat/desktop-review-proof",
    target_branch="feat/desktop-app",
    ci_status=CIStatus.SUCCESS,
    created_at=NOW,
    updated_at=NOW,
    web_url="https://fixture.invalid/proof/desktop-review/pull/46",
    additions=46,
    deletions=3,
)
DETAIL = MRDetail(
    **SUMMARY.__dict__,
    description="Production review services with isolated mocked forge writes.",
    merge_status="can_be_merged",
    changes_count=1,
    head_sha=REVISION.head_sha,
    base_sha=REVISION.base_sha,
)
PATCH = "@@ -3,3 +3,3 @@\n before\n-old value\n+new value\n after"


def _discussion() -> Discussion:
    root = ForgeInlineComment(
        "root-46",
        User("reviewer", "Fixture reviewer"),
        "Please verify this path.",
        NOW,
        "src/example.py",
        old_line=None,
        new_line=4,
    )
    return Discussion("thread-46", True, root, resolvable=True)


class _MockForgeClient:
    supports_unapprove = True

    def __init__(self, action_log: Path) -> None:
        self._action_log = action_log

    async def add_comment(
        self, project: str, review_number: int, body: str
    ) -> ForgeMutationResult:
        self._record("add_comment", project, review_number, body=body)
        if "unknown" in body.lower():
            raise NetworkError("controlled ambiguous write")
        return ForgeMutationResult(f"comment-{body[:12]}", "comment-46")

    async def create_inline_comment(
        self, *args: object, **kwargs: object
    ) -> ForgeMutationResult:
        self._record("create_inline_comment", str(args[0]), int(args[1]))
        return ForgeMutationResult("inline-46", "inline-note-46", "thread-46")

    async def reply_to_discussion(
        self,
        project: str,
        review_number: int,
        discussion_id: str,
        body: str,
        **_kwargs: object,
    ) -> ForgeMutationResult:
        self._record(
            "reply", project, review_number, body=body, discussion=discussion_id
        )
        return ForgeMutationResult("reply-46", "reply-note-46", discussion_id)

    async def resolve_discussion(
        self, project: str, review_number: int, discussion_id: str, resolved: bool
    ) -> ForgeMutationResult:
        self._record(
            "resolve",
            project,
            review_number,
            discussion=discussion_id,
            resolved=resolved,
        )
        return ForgeMutationResult("resolve-46", discussion_id=discussion_id)

    async def submit_review(
        self,
        project: str,
        review_number: int,
        _verdict: object,
        body: str,
        _inline: object,
        **_kwargs: object,
    ) -> ForgeMutationResult:
        self._record("submit_review", project, review_number, body=body)
        return ForgeMutationResult("review-46", "review-note-46")

    async def merge_mr(
        self,
        project: str,
        review_number: int,
        squash: bool,
        delete_source_branch: bool,
        **_kwargs: object,
    ) -> ForgeMergeResult:
        self._record(
            "merge",
            project,
            review_number,
            squash=squash,
            delete_source_branch=delete_source_branch,
        )
        return ForgeMergeResult("merge-46", "c" * 40, SourceCleanupStatus.NOT_REQUESTED)

    async def close_mr(self, project: str, review_number: int) -> ForgeMutationResult:
        self._record("close", project, review_number)
        raise ConflictError("controlled known rejection")

    async def reopen_mr(self, project: str, review_number: int) -> ForgeMutationResult:
        self._record("reopen", project, review_number)
        return ForgeMutationResult("reopen-46")

    async def unapprove_mr(
        self, project: str, review_number: int
    ) -> ForgeMutationResult:
        self._record("unapprove", project, review_number)
        return ForgeMutationResult("unapprove-46")

    async def invalidate_review_reads(self, _project: str, _number: int) -> None:
        return None

    def _record(
        self,
        action: str,
        project: str,
        review_number: int,
        **details: object,
    ) -> None:
        document = json.dumps(
            {
                "action": action,
                "project": project,
                "review_number": review_number,
                **details,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        descriptor = os.open(
            self._action_log,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, f"{document}\n".encode())
        finally:
            os.close(descriptor)


class _FixtureSession:
    config = Config()

    def __init__(self, evidence_root: Path) -> None:
        self._events: asyncio.Queue[ServiceEvent | None] = asyncio.Queue()
        self._sequence = 0
        self._store = DraftStore(evidence_root / "drafts.db")
        self.drafts = self._store
        self._client = _MockForgeClient(evidence_root / "mock-forge-actions.jsonl")
        self.review_mutations = ReviewMutationService(
            get_client=self._get_client,
            get_review=self.get_review,
            get_diff=self._get_diff,
            get_discussions=self._get_discussions,
            emit_change=self._emit_review_change,
            timeout=0.15,
        )
        self.mr_actions = MRActionService(
            get_client=self._get_client,
            get_review=self.get_review,
            emit_change=self._emit_review_change,
        )
        self.review_submissions = ReviewSubmissionService(
            store=self._store,
            mutations=self.review_mutations,
            get_review=self.get_review,
        )

    async def start(self) -> _FixtureSession:
        await self._store.open()
        return self

    async def close(self) -> None:
        await self.review_submissions.close()
        await self.mr_actions.close()
        await self.review_mutations.close()
        await self._store.close()
        self._events.put_nowait(None)

    async def discover_repositories(self) -> tuple[RepositorySnapshot, ...]:
        return (
            RepositorySnapshot(REPOSITORY, REPOSITORY.project_path, ForgeType.GITHUB),
        )

    async def list_reviews(self, _query: ReviewQuery) -> ReviewPage:
        return ReviewPage((ReviewListItem(REVIEW, SUMMARY),))

    async def get_review(self, review: ReviewRef) -> ReviewSnapshot:
        if review != REVIEW:
            raise ValueError("fixture review mismatch")
        return ReviewSnapshot(
            REVIEW,
            DETAIL,
            REVISION,
            ForgeCapabilities(True, True, True, True, True),
        )

    async def get_discussions(self, review: ReviewRef) -> tuple[Discussion, ...]:
        return await self._get_discussions(review)

    async def _get_discussions(self, review: ReviewRef) -> tuple[Discussion, ...]:
        await self.get_review(review)
        return (_discussion(),)

    async def _get_diff(self, review: ReviewRef) -> RawDiffSnapshot:
        await self.get_review(review)
        return RawDiffSnapshot(
            REVIEW,
            REVISION,
            (
                {
                    "filename": "src/example.py",
                    "status": "modified",
                    "patch": PATCH,
                },
            ),
        )

    async def _get_client(self, review: ReviewRef, _operation: str) -> ForgeClient:
        await self.get_review(review)
        return cast(ForgeClient, self._client)

    async def events(self) -> AsyncIterator[ServiceEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    def _emit_review_change(
        self,
        kind: ServiceEventKind,
        resource: ReviewRef,
        revision: ReviewRevision | None = None,
    ) -> None:
        self._sequence += 1
        self._events.put_nowait(
            ServiceEvent(self._sequence, kind, resource, revision=revision)
        )


async def run() -> None:
    evidence_root = Path(os.environ["TONGS_REVIEW_PROOF_EVIDENCE"]).resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    session = _FixtureSession(evidence_root)
    server = DesktopSidecarServer(
        session=cast(object, session),  # type: ignore[arg-type]
        plugin_registry=DesktopPluginRegistry(
            entry_point_source=lambda _group: (), host_version="1.0"
        ),
    )
    loop = asyncio.get_running_loop()
    protocol_fd = os.dup(sys.stdout.fileno())
    os.set_inheritable(protocol_fd, False)
    protocol_stdout = os.fdopen(protocol_fd, "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno(), inheritable=True)
    sys.stdout = sys.stderr
    reader = asyncio.StreamReader(limit=MAX_REQUEST_FRAME_BYTES + 1)
    reader_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: reader_protocol, sys.stdin.buffer)
    writer_protocol = _WritePipeProtocol()
    transport, _ = await loop.connect_write_pipe(
        lambda: writer_protocol, protocol_stdout
    )
    writer = _PipeWriter(cast(asyncio.WriteTransport, transport), writer_protocol)
    try:
        await server.run(reader, writer)
    finally:
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    asyncio.run(run())
