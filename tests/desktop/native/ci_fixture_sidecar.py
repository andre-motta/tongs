"""Native desktop proof sidecar backed by isolated in-memory CI fixtures."""

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
from tongs.errors import NetworkError, NotFoundError
from tongs.forges.base import ForgeClient
from tongs.forges.models import (
    CIStatus,
    ForgeHost,
    MRDetail,
    MRState,
    MRSummary,
    Pipeline,
    PipelineJob,
    User,
)
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.scanner.repo import ForgeType
from tongs.services import (
    CIMutationService,
    ForgeCapabilities,
    JobRef,
    PipelineRef,
    RepositoryRef,
    RepositorySnapshot,
    ReviewListItem,
    ReviewPage,
    ReviewQuery,
    ReviewRef,
    ReviewRevision,
    ReviewSnapshot,
    ServiceEvent,
    ServiceEventKind,
)

HOST = ForgeHost("fixture.example", ForgeType.GITLAB, "https://fixture.invalid")
REPOSITORY = RepositoryRef(HOST.hostname, "proof/desktop-ci")
REVIEW = ReviewRef(REPOSITORY, 47)
PIPELINE_REF = PipelineRef(REPOSITORY, 101)
JOB_REF = JobRef(REPOSITORY, 201)
NOW = datetime(2026, 9, 8, tzinfo=UTC)
SUMMARY = MRSummary(
    forge_host=HOST,
    repo_path=REPOSITORY.project_path,
    local_path="",
    number=REVIEW.number,
    title="Controlled desktop CI proof",
    author=User("fixture", "Controlled fixture"),
    state=MRState.OPEN,
    is_draft=False,
    source_branch="feat/desktop-ci-proof",
    target_branch="feat/desktop-app",
    ci_status=CIStatus.RUNNING,
    created_at=NOW,
    updated_at=NOW,
    web_url="https://fixture.example/proof/desktop-ci/merge_requests/47",
    additions=47,
    deletions=2,
)
DETAIL = MRDetail(
    **SUMMARY.__dict__,
    description="Isolated native proof with mocked forge writes.",
    merge_status="can_be_merged",
    changes_count=3,
    head_sha="a" * 40,
    base_sha="b" * 40,
)
PIPELINE = Pipeline(
    101,
    CIStatus.RUNNING,
    "feat/desktop-ci-proof",
    "a" * 40,
    "https://fixture.invalid/pipelines/101",
    source="push",
    created_at=NOW,
)
JOB = PipelineJob(
    201,
    "desktop-production",
    "verify",
    CIStatus.RUNNING,
    "https://fixture.invalid/jobs/201",
    started_at=NOW,
)


class _FixtureClient:
    supports_job_cancel = True

    def __init__(self, action_log: Path) -> None:
        self._action_log = action_log

    async def retry_pipeline(self, project: str, item_id: int) -> None:
        self._record("retry_pipeline", project, item_id)
        raise NotFoundError("controlled missing pipeline")

    async def cancel_pipeline(self, project: str, item_id: int) -> None:
        self._record("cancel_pipeline", project, item_id)
        await asyncio.sleep(1.8)

    async def retry_job(self, project: str, item_id: int) -> None:
        self._record("retry_job", project, item_id)

    async def cancel_job(self, project: str, item_id: int) -> None:
        self._record("cancel_job", project, item_id)
        raise NetworkError("controlled interrupted response")

    def _record(self, action: str, project: str, item_id: int) -> None:
        print(f"[native-ci-fixture] {action} {project} {item_id}", file=sys.stderr)
        document = json.dumps(
            {"action": action, "project": project, "item_id": item_id},
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
    def __init__(self, action_log: Path) -> None:
        self.config = Config(editor_command=os.environ.get("TONGS_EDITOR_COMMAND", ""))
        self._events: asyncio.Queue[ServiceEvent | None] = asyncio.Queue()
        self._sequence = 0
        self._client = _FixtureClient(action_log)
        self.ci_mutations = CIMutationService(
            get_client=self._get_client,
            get_pipeline_jobs=self.get_pipeline_jobs,
            emit_change=self._emit_change,
        )

    async def start(self) -> _FixtureSession:
        return self

    async def close(self) -> None:
        await self.ci_mutations.close()
        self._events.put_nowait(None)

    async def discover_repositories(self) -> tuple[RepositorySnapshot, ...]:
        return (
            RepositorySnapshot(
                REPOSITORY,
                REPOSITORY.project_path,
                ForgeType.GITLAB,
            ),
        )

    async def list_reviews(self, _query: ReviewQuery) -> ReviewPage:
        return ReviewPage((ReviewListItem(REVIEW, SUMMARY),))

    async def get_review(self, _review: ReviewRef) -> ReviewSnapshot:
        return ReviewSnapshot(
            REVIEW,
            DETAIL,
            ReviewRevision("a" * 40, "b" * 40),
            ForgeCapabilities(True, True, False, False, True),
        )

    async def list_review_pipelines(
        self, _review: ReviewRef, *, per_page: int = 20
    ) -> tuple[Pipeline, ...]:
        assert per_page >= 1
        return (PIPELINE,)

    async def get_pipeline_jobs(
        self, _pipeline: PipelineRef
    ) -> tuple[PipelineJob, ...]:
        return (JOB,)

    async def get_job_log(self, _job: JobRef) -> str:
        return (
            "starting controlled build\n"
            "\x1b[32mPASS\x1b[0m renderer bridge\n"
            "literal <script>alert('inert')</script> remains text\n"
            "search-target mutation receipt\n"
        )

    async def clear_cache(self) -> None:
        self._client._record("clear_cache", REPOSITORY.project_path, 0)

    def emit_change(
        self,
        kind: ServiceEventKind,
        resource: RepositoryRef | ReviewRef | PipelineRef | None = None,
    ) -> None:
        """Expose the production session event facade to protocol adapters."""
        self._emit_change(kind, resource)

    async def events(self) -> AsyncIterator[ServiceEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def _get_client(
        self, repository: RepositoryRef, _operation: str
    ) -> ForgeClient:
        if repository != REPOSITORY:
            raise ValueError("fixture repository mismatch")
        return cast(ForgeClient, self._client)

    def _emit_change(
        self,
        kind: ServiceEventKind,
        resource: RepositoryRef | ReviewRef | PipelineRef | None = None,
    ) -> None:
        self._sequence += 1
        self._events.put_nowait(ServiceEvent(self._sequence, kind, resource))


async def run() -> None:
    """Run the production server and CI service with isolated fixture state."""
    evidence_root = Path(os.environ["TONGS_CI_PROOF_EVIDENCE"]).resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    action_log = evidence_root / "mock-forge-actions.jsonl"
    action_log.unlink(missing_ok=True)
    session = _FixtureSession(action_log)
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
