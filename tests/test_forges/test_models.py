"""Tests for forge data models."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from tongs.forges.models import (
    CIStatus,
    Discussion,
    ForgeHost,
    InlineComment,
    MRDetail,
    MRState,
    MRSummary,
    Pipeline,
    ReviewDecision,
    User,
)
from tongs.scanner.repo import ForgeType


class TestForgeModels:
    def _host(self) -> ForgeHost:
        return ForgeHost(
            hostname="gitlab.com",
            forge_type=ForgeType.GITLAB,
            api_base="https://gitlab.com/api/v4",
        )

    def _summary(self) -> MRSummary:
        return MRSummary(
            forge_host=self._host(),
            repo_path="org/repo",
            local_path="/tmp/repo",
            number=42,
            title="Fix bug",
            author=User(username="dev"),
            state=MRState.OPEN,
            is_draft=False,
            source_branch="fix-bug",
            target_branch="main",
            ci_status=CIStatus.SUCCESS,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            web_url="https://gitlab.com/org/repo/-/merge_requests/42",
        )

    def test_mr_summary_frozen(self):
        summary = self._summary()
        with pytest.raises(FrozenInstanceError):
            summary.title = "changed"

    def test_mr_detail_extends_summary(self):
        detail = MRDetail(
            **{
                f.name: getattr(self._summary(), f.name)
                for f in self._summary().__dataclass_fields__.values()
            },
            description="Fixes the bug",
            merge_status="can_be_merged",
        )
        assert detail.title == "Fix bug"
        assert detail.description == "Fixes the bug"

    def test_mr_summary_optional_fields_default_none(self):
        summary = self._summary()
        assert summary.review_decision is None
        assert summary.additions is None

    def test_user_display_name_default(self):
        user = User(username="dev")
        assert user.display_name == ""

    def test_pipeline_frozen(self):
        pipeline = Pipeline(
            id=1,
            status=CIStatus.RUNNING,
            ref="main",
            sha="abc123",
            web_url="https://gitlab.com/pipelines/1",
        )
        with pytest.raises(FrozenInstanceError):
            pipeline.status = CIStatus.SUCCESS

    def test_discussion_with_inline_comment(self):
        comment = InlineComment(
            id="1",
            author=User(username="reviewer"),
            body="Fix this",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            file_path="src/main.py",
            new_line=42,
        )
        discussion = Discussion(
            id="d1",
            is_inline=True,
            root_comment=comment,
            is_resolved=False,
        )
        assert discussion.root_comment.file_path == "src/main.py"
        assert discussion.root_comment.new_line == 42

    def test_ci_status_values(self):
        assert CIStatus.SUCCESS.value == "success"
        assert CIStatus.FAILED.value == "failed"

    def test_review_decision_values(self):
        assert ReviewDecision.APPROVED.value == "approved"
        assert ReviewDecision.CHANGES_REQUESTED.value == "changes_requested"
