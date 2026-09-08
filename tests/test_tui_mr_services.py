"""Functional coverage for the Textual MR detail service adapter."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from tongs.app import TongsApp
from tongs.cache.store import CacheStore
from tongs.config import Config
from tongs.diff.position import position_from_diff_line
from tongs.forges.base import ForgeClient
from tongs.forges.models import (
    CIStatus,
    Commit,
    Discussion,
    ForgeHost,
    ForgeMergeResult,
    ForgeMutationResult,
    InlineComment,
    MRDetail,
    MRState,
    MRSummary,
    Pipeline,
    PipelineJob,
    ReviewDecision,
    User,
)
from tongs.plugins.registry import PluginRegistry
from tongs.scanner.repo import ForgeType, Remote, Repo
from tongs.services.review_mutations import MutationOutcome, MutationStatus
from tongs.services.session import ApplicationSession
from tongs.views.mr_detail import MRDetailScreen
from tongs.widgets.comment_editor import GeneralCommentSubmitted
from tongs.widgets.pipeline_panel import PipelinePanel

NOW = datetime(2026, 9, 7, tzinfo=UTC)


class _PluginRegistry(PluginRegistry):
    def discover(self, plugin_config: dict[str, dict] | None = None) -> None:
        pass


class _Forge:
    def __init__(self, host: ForgeHost) -> None:
        self.host = host
        self.calls: list[tuple[object, ...]] = []
        self.summary = MRSummary(
            host,
            "acme/widgets",
            "/private/repository/path",
            7,
            "Service migration",
            User("alice", "Alice"),
            MRState.OPEN,
            False,
            "feature",
            "main",
            CIStatus.FAILED,
            NOW,
            NOW,
            "https://github.com/acme/widgets/pull/7",
        )
        self.detail = MRDetail(
            **self.summary.__dict__,
            description="Detail loaded through the session",
            head_sha="head-7",
            base_sha="base-7",
        )
        root = InlineComment(
            "comment-1", User("bob"), "Please adjust", NOW, "widget.py", None, 1
        )
        self.discussion = Discussion("discussion-1", True, root)
        self.pipeline = Pipeline(
            101,
            CIStatus.FAILED,
            "feature",
            "head-7",
            "https://github.com/acme/widgets/actions/runs/101",
        )
        self.job = PipelineJob(201, "test", "verify", CIStatus.FAILED)

    @property
    def supports_batched_review(self) -> bool:
        return True

    @property
    def supports_thread_resolution(self) -> bool:
        return True

    @property
    def supports_draft_notes(self) -> bool:
        return False

    @property
    def supports_unapprove(self) -> bool:
        return True

    @property
    def supports_job_cancel(self) -> bool:
        return True

    async def list_my_reviews(self) -> list[MRSummary]:
        self.calls.append(("list_my_reviews",))
        return [self.summary]

    async def list_my_mrs(self) -> list[MRSummary]:
        return [self.summary]

    async def list_mrs(
        self, repo_path: str, state: str = "open", per_page: int = 100
    ) -> list[MRSummary]:
        return [self.summary]

    async def get_mr_fresh(self, repo_path: str, number: int) -> MRDetail:
        self.calls.append(("get_review", repo_path, number))
        return self.detail

    async def get_mr_diff_fresh(
        self, repo_path: str, number: int
    ) -> list[dict[str, object]]:
        self.calls.append(("get_diff", repo_path, number))
        return [
            {
                "filename": "widget.py",
                "status": "modified",
                "patch": "@@ -1 +1 @@\n-old\n+new",
                "additions": 1,
                "deletions": 1,
            }
        ]

    async def get_mr_discussions(self, repo_path: str, number: int) -> list[Discussion]:
        self.calls.append(("get_discussions", repo_path, number))
        return [self.discussion]

    async def list_mr_commits(self, repo_path: str, number: int) -> list[Commit]:
        self.calls.append(("get_commits", repo_path, number))
        return [
            Commit("abcdef0", "abcdef0", "Commit title", "Commit title", User("alice"))
        ]

    async def list_mr_pipelines(
        self, repo_path: str, number: int, per_page: int = 20
    ) -> list[Pipeline]:
        self.calls.append(("list_review_pipelines", repo_path, number))
        return [self.pipeline]

    async def get_pipeline_jobs(
        self, repo_path: str, pipeline_id: int
    ) -> list[PipelineJob]:
        self.calls.append(("get_pipeline_jobs", repo_path, pipeline_id))
        return [self.job]

    async def get_job_log(self, repo_path: str, job_id: int) -> str:
        self.calls.append(("get_job_log", repo_path, job_id))
        return "service log"

    async def add_comment(
        self, repo_path: str, number: int, body: str
    ) -> ForgeMutationResult:
        self.calls.append(("comment", repo_path, number, body))
        return ForgeMutationResult("remote-comment")

    async def create_inline_comment(
        self,
        repo_path: str,
        number: int,
        file_path: str,
        line: int,
        side: str,
        body: str,
        start_line: int | None = None,
        start_side: str | None = None,
        **kwargs: object,
    ) -> ForgeMutationResult:
        self.calls.append(
            (
                "inline",
                repo_path,
                number,
                file_path,
                line,
                side,
                body,
                kwargs.get("head_sha"),
            )
        )
        return ForgeMutationResult("remote-inline")

    async def submit_review(
        self,
        repo_path: str,
        number: int,
        verdict: ReviewDecision,
        body: str,
        inline_comments: list[dict] | None = None,
        *,
        head_sha: str | None = None,
    ) -> ForgeMutationResult:
        self.calls.append(("verdict", repo_path, number, verdict, head_sha))
        return ForgeMutationResult("remote-verdict")

    async def reply_to_discussion(
        self,
        repo_path: str,
        number: int,
        discussion_id: str,
        body: str,
        *,
        root_comment_id: str | None = None,
    ) -> ForgeMutationResult:
        self.calls.append(("reply", discussion_id, body, root_comment_id))
        return ForgeMutationResult("remote-reply")

    async def resolve_discussion(
        self,
        repo_path: str,
        number: int,
        discussion_id: str,
        resolved: bool,
    ) -> ForgeMutationResult:
        self.calls.append(("resolve", discussion_id, resolved))
        return ForgeMutationResult("remote-resolve")

    async def retry_pipeline(self, repo_path: str, pipeline_id: int) -> None:
        self.calls.append(("retry_pipeline", repo_path, pipeline_id))

    async def cancel_pipeline(self, repo_path: str, pipeline_id: int) -> None:
        self.calls.append(("cancel_pipeline", repo_path, pipeline_id))

    async def retry_job(self, repo_path: str, job_id: int) -> None:
        self.calls.append(("retry_job", repo_path, job_id))

    async def cancel_job(self, repo_path: str, job_id: int) -> None:
        self.calls.append(("cancel_job", repo_path, job_id))

    async def unapprove_mr(self, repo_path: str, number: int) -> ForgeMutationResult:
        self.calls.append(("unapprove", repo_path, number))
        return ForgeMutationResult("remote-unapprove")

    async def close_mr(self, repo_path: str, number: int) -> ForgeMutationResult:
        self.calls.append(("close", repo_path, number))
        return ForgeMutationResult("remote-close")

    async def merge_mr(
        self,
        repo_path: str,
        number: int,
        squash: bool = False,
        delete_source_branch: bool = False,
        **kwargs: object,
    ) -> ForgeMergeResult:
        self.calls.append(("merge", repo_path, number, kwargs.get("head_sha")))
        return ForgeMergeResult("remote-merge", "merge-sha")

    async def invalidate_review_reads(self, repo_path: str, number: int) -> bool:
        self.calls.append(("invalidate", repo_path, number))
        return True


class _Registry:
    def __init__(self, forge: _Forge) -> None:
        self.forge = forge

    def active_hostnames(self) -> list[str]:
        return [self.forge.host.hostname]

    def get_host(self, hostname: str) -> ForgeHost | None:
        return self.forge.host if hostname == self.forge.host.hostname else None

    async def get_client(self, hostname: str) -> ForgeClient:
        assert hostname == self.forge.host.hostname
        return cast(ForgeClient, self.forge)

    async def close_all(self) -> None:
        pass


async def _settle(app: TongsApp) -> None:
    for _ in range(4):
        await asyncio.sleep(0)
        with suppress(Exception):
            await app.workers.wait_for_complete()


def _app(tmp_path: Path) -> tuple[TongsApp, _Forge]:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    forge = _Forge(host)
    registry = _Registry(forge)
    remote = Remote(
        "origin",
        "https://github.com/acme/widgets.git",
        "github.com",
        "acme/widgets",
        ForgeType.GITHUB,
    )
    repo = Repo(tmp_path / "widgets", (remote,), remote)
    config = Config(scan_root=str(tmp_path), max_parallel=2)
    session = ApplicationSession(
        config=config,
        cache=CacheStore(tmp_path / "cache" / "cache.db"),
        draft_db_path=tmp_path / "data" / "drafts.db",
        forge_registry=registry,
        discoverer=lambda *args, **kwargs: [repo],
    )
    return TongsApp(
        config=config, session=session, plugin_registry=_PluginRegistry()
    ), forge


@pytest.mark.asyncio
async def test_actual_textual_mr_detail_uses_production_services(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        await _settle(app)
        table = app.screen.query_one("#reviews-table")
        table.focus()
        await pilot.press("enter")
        await _settle(app)
        screen = cast(MRDetailScreen, app.screen)
        assert isinstance(screen, MRDetailScreen)
        assert screen.mr_detail is not None
        assert screen.mr_detail.description == "Detail loaded through the session"

        screen.on_general_comment_submitted(GeneralCommentSubmitted("ship it"))
        await _settle(app)
        assert ("comment", "acme/widgets", 7, "ship it") in forge.calls

        app.clear_notifications()
        assert not screen._review_mutation_succeeded(
            MutationOutcome("unknown-1", MutationStatus.UNKNOWN, None), "Comment"
        )
        await pilot.pause()
        assert any(
            notification.message
            == "Comment outcome is unknown. Refresh before acting again."
            for notification in app._notifications
        )

        await pilot.press("2")
        await _settle(app)
        await pilot.press("3")
        await _settle(app)
        await pilot.press("4")
        await _settle(app)
        await pilot.press("5")
        await _settle(app)
        panel = screen.query_one("#pipeline-panel", PipelinePanel)
        assert panel._pipelines == [forge.pipeline]

        screen._load_pipeline_jobs(forge.pipeline)
        await _settle(app)
        screen._load_job_log(forge.job, forge.pipeline)
        await _settle(app)
        screen._do_retry_job(forge.pipeline.id, forge.job.id)
        await _settle(app)

        assert ("get_diff", "acme/widgets", 7) in forge.calls
        assert ("get_discussions", "acme/widgets", 7) in forge.calls
        assert ("get_commits", "acme/widgets", 7) in forge.calls
        assert ("list_review_pipelines", "acme/widgets", 7) in forge.calls
        assert ("get_pipeline_jobs", "acme/widgets", 101) in forge.calls
        assert ("get_job_log", "acme/widgets", 201) in forge.calls
        assert ("retry_job", "acme/widgets", 201) in forge.calls


@pytest.mark.asyncio
async def test_adapter_keeps_quick_writes_revision_bound_and_immediate(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)

    async with app.run_test():
        await _settle(app)
        summary = replace(forge.summary, local_path="")
        await app.services.get_review(summary)

        comment = await app.services.post_general_comment(
            summary, "quick", operation_id="quick-comment-1"
        )
        diff = await app.services.get_diff(summary)
        addition = next(
            line
            for line in diff.files[0].hunks[0].lines
            if line.new_lineno == 1 and line.old_lineno is None
        )
        inline = await app.services.post_inline_comment(
            summary,
            "inline quick comment",
            position_from_diff_line(diff.files[0], addition),
            operation_id="inline-1",
        )
        verdict = await app.services.approve(summary, operation_id="approve-1")
        reply = await app.services.post_reply(
            summary, "discussion-1", "thanks", operation_id="reply-1"
        )
        resolved = await app.services.resolve_discussion(
            summary, "discussion-1", True, operation_id="resolve-1"
        )
        unapprove = await app.services.unapprove(summary, operation_id="unapprove-1")
        merged = await app.services.merge(summary, operation_id="merge-1")
        closed = await app.services.close_review(summary, operation_id="close-1")
        await app.services.get_pipeline_jobs(summary, forge.pipeline.id)
        retried_pipeline = await app.services.retry_pipeline(
            summary, forge.pipeline.id, operation_id="retry-pipeline-1"
        )
        cancelled_pipeline = await app.services.cancel_pipeline(
            summary, forge.pipeline.id, operation_id="cancel-pipeline-1"
        )
        retried_job = await app.services.retry_job(
            summary, forge.pipeline.id, forge.job.id, operation_id="retry-job-1"
        )
        cancelled_job = await app.services.cancel_job(
            summary, forge.pipeline.id, forge.job.id, operation_id="cancel-job-1"
        )

        assert comment.operation_id == "quick-comment-1"
        assert inline.operation_id == "inline-1"
        assert verdict.operation_id == "approve-1"
        assert reply.operation_id == "reply-1"
        assert resolved.operation_id == "resolve-1"
        assert unapprove.operation_id == "unapprove-1"
        assert merged.operation_id == "merge-1"
        assert closed.operation_id == "close-1"
        assert retried_pipeline.operation_id == "retry-pipeline-1"
        assert cancelled_pipeline.operation_id == "cancel-pipeline-1"
        assert retried_job.operation_id == "retry-job-1"
        assert cancelled_job.operation_id == "cancel-job-1"
        assert (
            "verdict",
            "acme/widgets",
            7,
            ReviewDecision.APPROVED,
            "head-7",
        ) in forge.calls
        assert (
            "inline",
            "acme/widgets",
            7,
            "widget.py",
            1,
            "RIGHT",
            "inline quick comment",
            "head-7",
        ) in forge.calls
        assert ("reply", "discussion-1", "thanks", "comment-1") in forge.calls
        assert ("resolve", "discussion-1", True) in forge.calls
        assert ("unapprove", "acme/widgets", 7) in forge.calls
        assert ("merge", "acme/widgets", 7, "head-7") in forge.calls
        assert ("close", "acme/widgets", 7) in forge.calls
        assert ("retry_pipeline", "acme/widgets", 101) in forge.calls
        assert ("cancel_pipeline", "acme/widgets", 101) in forge.calls
        assert ("retry_job", "acme/widgets", 201) in forge.calls
        assert ("cancel_job", "acme/widgets", 201) in forge.calls
