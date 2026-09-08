"""Textual-facing adapter for the shared application services."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

from tongs.diff.conversion import convert_forge_changes
from tongs.diff.models import DiffFile
from tongs.diff.position import DiffPosition
from tongs.forges.models import (
    Commit,
    Discussion,
    MRSummary,
    Pipeline,
    PipelineJob,
    ReviewDecision,
)
from tongs.scanner.repo import Repo
from tongs.services.ci_mutations import (
    CancelJobCommand,
    CancelPipelineCommand,
    CIMutationReceipt,
    JobMutationTarget,
    PipelineMutationTarget,
    RetryJobCommand,
    RetryPipelineCommand,
)
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import (
    HostFailure,
    JobRef,
    PipelineRef,
    RepositoryRef,
    ReviewListItem,
    ReviewPage,
    ReviewQuery,
    ReviewRef,
    ReviewRevision,
    ReviewScope,
    ReviewSnapshot,
    ServiceEvent,
)
from tongs.services.mr_actions import (
    CloseReviewCommand,
    MergeReviewCommand,
    MRActionReceipt,
    ReviewActionTarget,
    UnapproveReviewCommand,
)
from tongs.services.review_mutations import (
    DiffAnchor,
    DiffSide,
    GeneralComment,
    InlineComment,
    MutationOutcome,
    Reply,
    Resolve,
    ReviewVerdict,
)
from tongs.services.session import ApplicationSession


@dataclass(frozen=True, slots=True)
class TUIDiscoveryResult:
    """One repository refresh result suitable for publishing in the TUI."""

    generation: int
    repositories: tuple[Repo, ...]
    stale: bool = False


@dataclass(frozen=True, slots=True)
class TUIDiffResult:
    """Converted diff files paired with the revision that produced them."""

    files: tuple[DiffFile, ...]
    revision: ReviewRevision


class TUIServiceAdapter:
    """Preserve terminal models while routing reads through one session.

    The adapter retains admitted service identities for later TUI consumers. Local
    ``Repo`` metadata remains trusted in-process state and never enters desktop
    snapshots or protocol DTOs.
    """

    def __init__(self, session: ApplicationSession) -> None:
        self.session = session
        self._discovery_generation = 0
        self._repositories: tuple[Repo, ...] = ()
        self._repository_refs: dict[Repo, RepositoryRef] = {}
        self._review_refs: dict[tuple[str, str, int], ReviewRef] = {}
        self._review_snapshots: dict[ReviewRef, ReviewSnapshot] = {}
        self._review_revisions: dict[ReviewRef, ReviewRevision] = {}

    @property
    def discovery_generation(self) -> int:
        """Return the latest requested discovery generation."""
        return self._discovery_generation

    @property
    def repositories(self) -> tuple[Repo, ...]:
        """Return the latest repository inventory published by this adapter."""
        return self._repositories

    async def discover_repositories(self) -> TUIDiscoveryResult:
        """Refresh local repositories without publishing an older result late."""
        self._discovery_generation += 1
        generation = self._discovery_generation
        snapshots = await self.session.discover_repositories()
        if generation != self._discovery_generation:
            return TUIDiscoveryResult(generation, self._repositories, stale=True)

        repositories = self.session.local_repositories
        admitted = {snapshot.ref for snapshot in snapshots}
        refs: dict[Repo, RepositoryRef] = {}
        for repo in repositories:
            remote = repo.primary_remote
            if remote is None:
                continue
            try:
                ref = RepositoryRef(remote.hostname, remote.repo_path)
            except (TypeError, ValueError):
                continue
            if ref in admitted:
                refs[repo] = ref

        self._repositories = repositories
        self._repository_refs = refs
        return TUIDiscoveryResult(generation, repositories)

    def invalidate_discovery(self) -> None:
        """Prevent an active discovery generation from publishing afterward."""
        self._discovery_generation += 1

    def repository_ref(self, repo: Repo) -> RepositoryRef:
        """Return the admitted identity for an actual discovered repository."""
        try:
            return self._repository_refs[repo]
        except KeyError:
            raise ServiceError(
                ServiceErrorCode.UNSUPPORTED,
                "This local repository does not use a configured forge host.",
            ) from None

    def review_ref(self, summary: MRSummary) -> ReviewRef:
        """Return the admitted identity paired with a service list result."""
        key = (summary.forge_host.hostname, summary.repo_path, summary.number)
        try:
            return self._review_refs[key]
        except KeyError:
            raise ServiceError(
                ServiceErrorCode.RESOURCE_NOT_ISSUED,
                "The review was not issued by this application session.",
            ) from None

    async def list_reviews(
        self, scope: ReviewScope, *, repository: Repo | None = None
    ) -> ReviewPage:
        """Run an inbox read with the terminal's local workspace semantics."""
        if repository is not None:
            page = await self.session.list_reviews(
                ReviewQuery(scope, repository=self.repository_ref(repository))
            )
        elif scope is ReviewScope.ALL_OPEN:
            page = await self._list_all_open()
        else:
            page = await self._list_personal(scope)
        self._remember_review_refs(page.items)
        return page

    async def get_review(self, summary: MRSummary) -> ReviewSnapshot:
        """Read and retain a service-issued review snapshot for later writes."""
        ref = self.review_ref(summary)
        snapshot = await self.session.get_review(ref)
        self._review_snapshots[ref] = snapshot
        if snapshot.revision is not None:
            self._review_revisions[ref] = snapshot.revision
        else:
            self._review_revisions.pop(ref, None)
        return snapshot

    async def get_diff(self, summary: MRSummary) -> TUIDiffResult:
        """Read and centrally convert a revision-stable forge diff."""
        ref = self.review_ref(summary)
        raw = await self.session.get_raw_diff(ref)
        self._review_revisions[ref] = raw.revision
        return TUIDiffResult(convert_forge_changes(raw.changes), raw.revision)

    async def get_discussions(self, summary: MRSummary) -> tuple[Discussion, ...]:
        return await self.session.get_discussions(self.review_ref(summary))

    async def get_commits(self, summary: MRSummary) -> tuple[Commit, ...]:
        return await self.session.get_commits(self.review_ref(summary))

    async def list_review_pipelines(self, summary: MRSummary) -> tuple[Pipeline, ...]:
        return await self.session.list_review_pipelines(self.review_ref(summary))

    async def get_pipeline_jobs(
        self, summary: MRSummary, pipeline_id: int
    ) -> tuple[PipelineJob, ...]:
        return await self.session.get_pipeline_jobs(
            self._pipeline_ref(summary, pipeline_id)
        )

    async def get_job_log(self, summary: MRSummary, job_id: int) -> str:
        review = self.review_ref(summary)
        return await self.session.get_job_log(JobRef(review.repository, job_id))

    def events(self) -> AsyncIterator[ServiceEvent]:
        """Expose the session event stream for future terminal view extensions."""
        return self.session.events()

    async def post_general_comment(
        self, summary: MRSummary, body: str, *, operation_id: str
    ) -> MutationOutcome:
        ref = self.review_ref(summary)
        return await self.session.review_mutations.execute(
            GeneralComment(operation_id, ref, body)
        )

    async def post_inline_comment(
        self,
        summary: MRSummary,
        body: str,
        position: DiffPosition,
        *,
        revision: ReviewRevision,
        start_line: int | None = None,
        start_side: str | None = None,
        operation_id: str,
    ) -> MutationOutcome:
        ref = self.review_ref(summary)
        side = DiffSide(position.side)
        line = position.new_line if side is DiffSide.RIGHT else position.old_line
        if line is None:
            raise ServiceError(
                ServiceErrorCode.INVALID_INPUT,
                "The selected diff line cannot be commented on.",
            )
        anchor = DiffAnchor(
            old_path=position.old_path,
            new_path=position.new_path,
            line=line,
            side=side,
            start_line=start_line,
            start_side=DiffSide(start_side) if start_side is not None else None,
        )
        return await self.session.review_mutations.execute(
            InlineComment(
                operation_id,
                ref,
                revision,
                anchor,
                body,
            )
        )

    async def post_reply(
        self,
        summary: MRSummary,
        discussion_id: str,
        body: str,
        *,
        operation_id: str,
    ) -> MutationOutcome:
        ref = self.review_ref(summary)
        return await self.session.review_mutations.execute(
            Reply(
                operation_id,
                ref,
                await self._revision(summary),
                discussion_id,
                body,
            )
        )

    async def resolve_discussion(
        self,
        summary: MRSummary,
        discussion_id: str,
        resolved: bool,
        *,
        operation_id: str,
    ) -> MutationOutcome:
        ref = self.review_ref(summary)
        return await self.session.review_mutations.execute(
            Resolve(
                operation_id,
                ref,
                await self._revision(summary),
                discussion_id,
                resolved,
            )
        )

    async def approve(
        self, summary: MRSummary, *, operation_id: str
    ) -> MutationOutcome:
        ref = self.review_ref(summary)
        return await self.session.review_mutations.execute(
            ReviewVerdict(
                operation_id,
                ref,
                await self._revision(summary),
                ReviewDecision.APPROVED,
            )
        )

    async def unapprove(
        self, summary: MRSummary, *, operation_id: str
    ) -> MRActionReceipt:
        target = await self._review_action_target(summary)
        return await self.session.mr_actions.execute(
            UnapproveReviewCommand(operation_id, target)
        )

    async def merge(self, summary: MRSummary, *, operation_id: str) -> MRActionReceipt:
        target = await self._review_action_target(summary)
        return await self.session.mr_actions.execute(
            MergeReviewCommand(operation_id, target)
        )

    async def close_review(
        self, summary: MRSummary, *, operation_id: str
    ) -> MRActionReceipt:
        target = await self._review_action_target(summary)
        return await self.session.mr_actions.execute(
            CloseReviewCommand(operation_id, target)
        )

    async def retry_pipeline(
        self, summary: MRSummary, pipeline_id: int, *, operation_id: str
    ) -> CIMutationReceipt:
        target = PipelineMutationTarget(self._pipeline_ref(summary, pipeline_id))
        return await self.session.ci_mutations.execute(
            RetryPipelineCommand(operation_id, target)
        )

    async def cancel_pipeline(
        self, summary: MRSummary, pipeline_id: int, *, operation_id: str
    ) -> CIMutationReceipt:
        target = PipelineMutationTarget(self._pipeline_ref(summary, pipeline_id))
        return await self.session.ci_mutations.execute(
            CancelPipelineCommand(operation_id, target)
        )

    async def retry_job(
        self,
        summary: MRSummary,
        pipeline_id: int,
        job_id: int,
        *,
        operation_id: str,
    ) -> CIMutationReceipt:
        pipeline = self._pipeline_ref(summary, pipeline_id)
        target = JobMutationTarget(pipeline, JobRef(pipeline.repository, job_id))
        return await self.session.ci_mutations.execute(
            RetryJobCommand(operation_id, target)
        )

    async def cancel_job(
        self,
        summary: MRSummary,
        pipeline_id: int,
        job_id: int,
        *,
        operation_id: str,
    ) -> CIMutationReceipt:
        pipeline = self._pipeline_ref(summary, pipeline_id)
        target = JobMutationTarget(pipeline, JobRef(pipeline.repository, job_id))
        return await self.session.ci_mutations.execute(
            CancelJobCommand(operation_id, target)
        )

    async def _review_action_target(self, summary: MRSummary) -> ReviewActionTarget:
        ref = self.review_ref(summary)
        snapshot = self._review_snapshots.get(ref)
        if snapshot is None:
            snapshot = await self.get_review(summary)
        revision = await self._revision(summary)
        return ReviewActionTarget(ref, revision, snapshot.detail.state)

    async def _revision(self, summary: MRSummary) -> ReviewRevision:
        ref = self.review_ref(summary)
        revision = self._review_revisions.get(ref)
        if revision is not None:
            return revision
        snapshot = await self.get_review(summary)
        if snapshot.revision is not None:
            return snapshot.revision
        raise snapshot.revision_error or ServiceError(
            ServiceErrorCode.REVISION_UNAVAILABLE,
            "The review revision is unavailable.",
        )

    def _pipeline_ref(self, summary: MRSummary, pipeline_id: int) -> PipelineRef:
        return PipelineRef(self.review_ref(summary).repository, pipeline_id)

    @staticmethod
    def new_operation_id(action: str) -> str:
        """Allocate one stable ID for a complete user command."""
        return f"tui:{action}:{uuid4().hex}"

    async def _list_personal(self, scope: ReviewScope) -> ReviewPage:
        hostnames = tuple(
            sorted({ref.hostname for ref in self._repository_refs.values()})
        )
        if not hostnames:
            return ReviewPage((), ())
        return await self.session.list_reviews(ReviewQuery(scope, hostnames=hostnames))

    async def _list_all_open(self) -> ReviewPage:
        refs_by_host: dict[str, list[RepositoryRef]] = {}
        for ref in dict.fromkeys(self._repository_refs.values()):
            refs_by_host.setdefault(ref.hostname, []).append(ref)
        if not refs_by_host:
            return ReviewPage((), ())
        semaphore = asyncio.Semaphore(self.session.config.max_parallel)

        async def fetch_host(refs: list[RepositoryRef]) -> ReviewPage:
            async with semaphore:
                items: list[ReviewListItem] = []
                failures: list[HostFailure] = []
                for ref in refs:
                    page = await self.session.list_reviews(
                        ReviewQuery(ReviewScope.ALL_OPEN, repository=ref)
                    )
                    items.extend(page.items)
                    failures.extend(page.failures)
                    if page.failures:
                        break
                return ReviewPage(tuple(items), tuple(failures))

        pages = await asyncio.gather(
            *(fetch_host(refs) for refs in refs_by_host.values())
        )
        items = [item for page in pages for item in page.items]
        failures = [failure for page in pages for failure in page.failures]
        items.sort(key=lambda item: item.summary.updated_at, reverse=True)
        return ReviewPage(tuple(items), self._deduplicate_failures(failures))

    def _remember_review_refs(self, items: tuple[ReviewListItem, ...]) -> None:
        for item in items:
            key = (
                item.ref.repository.hostname,
                item.ref.repository.project_path,
                item.ref.number,
            )
            self._review_refs[key] = item.ref

    @staticmethod
    def _deduplicate_failures(failures: list[HostFailure]) -> tuple[HostFailure, ...]:
        unique: dict[
            tuple[str, RepositoryRef | None, ServiceErrorCode], HostFailure
        ] = {}
        for failure in failures:
            key = (failure.hostname, failure.repository, failure.code)
            unique.setdefault(key, failure)
        return tuple(unique.values())


__all__ = ["TUIDiffResult", "TUIDiscoveryResult", "TUIServiceAdapter"]
