"""Tests for GitLab client parsing logic."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from tongs.forges.gitlab import (
    GitLabClient,
    _encode_project,
    _parse_ci_status,
    _parse_datetime,
    _parse_mr_state,
    _parse_user,
)
from tongs.forges.models import (
    CIStatus,
    ForgeHost,
    MRState,
    ReviewDecision,
)
from tongs.scanner.repo import ForgeType


class TestEncodeProject:
    def test_simple_path(self):
        assert _encode_project("org/repo") == "org%2Frepo"

    def test_nested_path(self):
        assert (
            _encode_project("redhat/rhel-ai/wheels/builder")
            == "redhat%2Frhel-ai%2Fwheels%2Fbuilder"
        )


class TestParseDatetime:
    def test_iso_format_with_z(self):
        dt = _parse_datetime("2026-01-15T10:30:00Z")
        assert dt == datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)

    def test_iso_format_with_offset(self):
        dt = _parse_datetime("2026-01-15T10:30:00+00:00")
        assert dt is not None
        assert dt.year == 2026

    def test_none_returns_none(self):
        assert _parse_datetime(None) is None

    def test_empty_returns_none(self):
        assert _parse_datetime("") is None


class TestParseCIStatus:
    def test_success(self):
        assert _parse_ci_status("success") == CIStatus.SUCCESS

    def test_failed(self):
        assert _parse_ci_status("failed") == CIStatus.FAILED

    def test_running(self):
        assert _parse_ci_status("running") == CIStatus.RUNNING

    def test_pending(self):
        assert _parse_ci_status("pending") == CIStatus.PENDING

    def test_canceled(self):
        assert _parse_ci_status("canceled") == CIStatus.CANCELED

    def test_cancelled_british(self):
        assert _parse_ci_status("cancelled") == CIStatus.CANCELED

    def test_created_maps_to_pending(self):
        assert _parse_ci_status("created") == CIStatus.PENDING

    def test_manual_maps_to_pending(self):
        assert _parse_ci_status("manual") == CIStatus.PENDING

    def test_unknown_value(self):
        assert _parse_ci_status("something_new") == CIStatus.UNKNOWN

    def test_none(self):
        assert _parse_ci_status(None) == CIStatus.UNKNOWN


class TestParseMRState:
    def test_opened(self):
        assert _parse_mr_state("opened") == MRState.OPEN

    def test_closed(self):
        assert _parse_mr_state("closed") == MRState.CLOSED

    def test_merged(self):
        assert _parse_mr_state("merged") == MRState.MERGED

    def test_unknown_defaults_to_open(self):
        assert _parse_mr_state("unknown") == MRState.OPEN


class TestParseUser:
    def test_full_user(self):
        user = _parse_user({"username": "dev", "name": "Developer"})
        assert user.username == "dev"
        assert user.display_name == "Developer"

    def test_minimal_user(self):
        user = _parse_user({"username": "dev"})
        assert user.username == "dev"
        assert user.display_name == ""

    def test_none_returns_unknown(self):
        user = _parse_user(None)
        assert user.username == "unknown"

    def test_empty_dict_returns_unknown(self):
        user = _parse_user({})
        assert user.username == "unknown"


# ---------------------------------------------------------------------------
# Shared helpers for GitLabClient tests
# ---------------------------------------------------------------------------

_TEST_HOST = ForgeHost(
    hostname="gitlab.example.com",
    forge_type=ForgeType.GITLAB,
    api_base="https://gitlab.example.com/api/v4",
)


def _make_gitlab_client(handler) -> tuple[GitLabClient, httpx.AsyncClient]:
    """Create a GitLabClient backed by a MockTransport."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        transport=transport,
        base_url=_TEST_HOST.api_base,
    )
    return GitLabClient(_TEST_HOST, http), http


def _mr_api_json(overrides: dict | None = None) -> dict:
    """Return a realistic GitLab MR API response dict."""
    data = {
        "iid": 42,
        "title": "Add widget support",
        "state": "opened",
        "draft": False,
        "source_branch": "feature/widgets",
        "target_branch": "main",
        "web_url": "https://gitlab.example.com/acme/widgets/-/merge_requests/42",
        "created_at": "2026-06-01T09:00:00Z",
        "updated_at": "2026-06-02T14:30:00Z",
        "has_conflicts": False,
        "user_notes_count": 3,
        "labels": ["enhancement", "review"],
        "author": {"username": "alice", "name": "Alice Dev"},
        "head_pipeline": {
            "id": 9001,
            "status": "success",
            "ref": "feature/widgets",
            "sha": "abc123def",
            "web_url": "https://gitlab.example.com/acme/widgets/-/pipelines/9001",
        },
        "reviewers": [
            {"username": "bob", "name": "Bob Rev"},
        ],
        "assignees": [
            {"username": "alice", "name": "Alice Dev"},
        ],
        "approved_by": [
            {"user": {"username": "carol", "name": "Carol Approver"}},
        ],
        "description": "Adds the new widget framework.\n\nCloses #123.",
        "merge_status": "can_be_merged",
        "changes_count": "5",
        "detailed_merge_status": "mergeable",
        "references": {"full": "acme/widgets!42"},
        "diff_refs": {
            "base_sha": "aaa111",
            "start_sha": "bbb222",
            "head_sha": "ccc333",
        },
    }
    if overrides:
        data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# C. GitLabClient parsing methods
# ---------------------------------------------------------------------------


class TestParseMRSummary:
    def _client(self) -> GitLabClient:
        client, _ = _make_gitlab_client(lambda r: httpx.Response(200))
        return client

    def test_parse_mr_summary_from_api_json(self):
        client = self._client()
        data = _mr_api_json()
        summary = client._parse_mr_summary(data, "acme/widgets")

        assert summary.number == 42
        assert summary.title == "Add widget support"
        assert summary.author.username == "alice"
        assert summary.author.display_name == "Alice Dev"
        assert summary.state == MRState.OPEN
        assert summary.is_draft is False
        assert summary.source_branch == "feature/widgets"
        assert summary.target_branch == "main"
        assert summary.ci_status == CIStatus.SUCCESS
        assert summary.web_url.endswith("/merge_requests/42")
        assert summary.comment_count == 3
        assert summary.has_conflicts is False
        assert summary.labels == ("enhancement", "review")
        assert summary.forge_host == _TEST_HOST
        assert summary.repo_path == "acme/widgets"
        assert summary.created_at == datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)

    def test_parse_mr_summary_missing_pipeline(self):
        client = self._client()
        data = _mr_api_json({"head_pipeline": None})
        summary = client._parse_mr_summary(data, "acme/widgets")
        assert summary.ci_status == CIStatus.UNKNOWN


class TestParseMRDetail:
    def _client(self) -> GitLabClient:
        client, _ = _make_gitlab_client(lambda r: httpx.Response(200))
        return client

    def test_parse_mr_detail_from_api_json(self):
        client = self._client()
        data = _mr_api_json()
        detail = client._parse_mr_detail(data, "acme/widgets")

        # Summary fields carry over
        assert detail.number == 42
        assert detail.title == "Add widget support"
        # Detail-specific fields
        assert detail.description == "Adds the new widget framework.\n\nCloses #123."
        assert detail.merge_status == "can_be_merged"
        assert detail.changes_count == 5
        assert detail.detailed_merge_status == "mergeable"
        assert len(detail.reviewers) == 1
        assert detail.reviewers[0].username == "bob"
        assert len(detail.assignees) == 1
        assert detail.assignees[0].username == "alice"
        # approved_by uses {"user": {...}} wrapper; _parse_user receives the
        # wrapper dict, so the username key is not at the top level.
        assert len(detail.approvals) == 1


class TestParseNote:
    def _client(self) -> GitLabClient:
        client, _ = _make_gitlab_client(lambda r: httpx.Response(200))
        return client

    def test_parse_note_with_position(self):
        client = self._client()
        note_data = {
            "id": 101,
            "body": "Consider using a constant here.",
            "author": {"username": "bob", "name": "Bob Rev"},
            "created_at": "2026-06-02T10:00:00Z",
            "resolved": False,
            "position": {
                "new_path": "src/widget.py",
                "old_path": "src/widget.py",
                "new_line": 15,
                "old_line": None,
                "position_type": "text",
            },
        }
        comment = client._parse_note(note_data)
        assert comment.id == "101"
        assert comment.author.username == "bob"
        assert comment.body == "Consider using a constant here."
        assert comment.file_path == "src/widget.py"
        assert comment.new_line == 15
        assert comment.old_line is None
        assert comment.is_resolved is False

    def test_parse_note_without_position(self):
        client = self._client()
        note_data = {
            "id": 102,
            "body": "LGTM, nice work!",
            "author": {"username": "carol", "name": "Carol Approver"},
            "created_at": "2026-06-02T11:00:00Z",
            "resolved": False,
        }
        comment = client._parse_note(note_data)
        assert comment.id == "102"
        assert comment.author.username == "carol"
        assert comment.body == "LGTM, nice work!"
        assert comment.file_path == ""
        assert comment.new_line is None
        assert comment.old_line is None


class TestParsePipeline:
    def _client(self) -> GitLabClient:
        client, _ = _make_gitlab_client(lambda r: httpx.Response(200))
        return client

    def test_parse_pipeline_from_api_json(self):
        client = self._client()
        data = {
            "id": 9001,
            "status": "success",
            "ref": "main",
            "sha": "abc123def456",
            "web_url": "https://gitlab.example.com/acme/widgets/-/pipelines/9001",
            "source": "push",
            "created_at": "2026-06-01T08:00:00Z",
            "finished_at": "2026-06-01T08:10:00Z",
            "duration": 600,
        }
        pipeline = client._parse_pipeline(data)
        assert pipeline.id == 9001
        assert pipeline.status == CIStatus.SUCCESS
        assert pipeline.ref == "main"
        assert pipeline.sha == "abc123def456"
        assert pipeline.source == "push"
        assert pipeline.duration_seconds == 600
        assert pipeline.created_at == datetime(2026, 6, 1, 8, 0, 0, tzinfo=timezone.utc)
        assert pipeline.finished_at == datetime(
            2026, 6, 1, 8, 10, 0, tzinfo=timezone.utc
        )


class TestParseJob:
    def _client(self) -> GitLabClient:
        client, _ = _make_gitlab_client(lambda r: httpx.Response(200))
        return client

    def test_parse_job_from_api_json(self):
        client = self._client()
        data = {
            "id": 5001,
            "name": "unit-tests",
            "stage": "test",
            "status": "failed",
            "web_url": "https://gitlab.example.com/acme/widgets/-/jobs/5001",
            "started_at": "2026-06-01T08:01:00Z",
            "finished_at": "2026-06-01T08:05:00Z",
            "duration": 240,
            "allow_failure": False,
        }
        job = client._parse_job(data)
        assert job.id == 5001
        assert job.name == "unit-tests"
        assert job.stage == "test"
        assert job.status == CIStatus.FAILED
        assert job.duration_seconds == 240
        assert job.allow_failure is False


class TestExtractRepoPath:
    def _client(self) -> GitLabClient:
        client, _ = _make_gitlab_client(lambda r: httpx.Response(200))
        return client

    def test_extract_repo_path_from_references(self):
        client = self._client()
        mr_data = {"references": {"full": "acme/widgets!42"}}
        assert client._extract_repo_path(mr_data) == "acme/widgets"

    def test_extract_repo_path_from_web_url(self):
        client = self._client()
        mr_data = {
            "references": {"full": ""},
            "web_url": "https://gitlab.example.com/acme/widgets/-/merge_requests/42",
        }
        assert client._extract_repo_path(mr_data) == "acme/widgets"


# ---------------------------------------------------------------------------
# D. GitLabClient async methods
# ---------------------------------------------------------------------------


class TestGitLabClientAsync:
    @pytest.mark.asyncio
    async def test_list_mrs_returns_summaries(self):
        mr_list = [_mr_api_json(), _mr_api_json({"iid": 43, "title": "Second MR"})]

        def handler(req: httpx.Request) -> httpx.Response:
            assert "/projects/acme%2Fwidgets/merge_requests" in str(req.url)
            return httpx.Response(200, json=mr_list)

        client, http = _make_gitlab_client(handler)
        async with http:
            summaries = await client.list_mrs("acme/widgets")
        assert len(summaries) == 2
        assert summaries[0].number == 42
        assert summaries[1].number == 43
        assert summaries[1].title == "Second MR"

    @pytest.mark.asyncio
    async def test_get_mr_returns_detail(self):
        mr_data = _mr_api_json()

        def handler(req: httpx.Request) -> httpx.Response:
            assert "/projects/acme%2Fwidgets/merge_requests/42" in str(req.url)
            return httpx.Response(200, json=mr_data)

        client, http = _make_gitlab_client(handler)
        async with http:
            detail = await client.get_mr("acme/widgets", 42)
        assert detail.number == 42
        assert detail.description == "Adds the new widget framework.\n\nCloses #123."
        assert detail.merge_status == "can_be_merged"

    @pytest.mark.asyncio
    async def test_list_my_reviews_uses_correct_params(self):
        mr_data = _mr_api_json()

        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.params["scope"] == "all"
            assert req.url.params["reviewer_username"] == "self"
            assert req.url.params["state"] == "opened"
            return httpx.Response(200, json=[mr_data])

        client, http = _make_gitlab_client(handler)
        async with http:
            reviews = await client.list_my_reviews()
        assert len(reviews) == 1
        assert reviews[0].number == 42

    @pytest.mark.asyncio
    async def test_approve_mr_posts_correctly(self):
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            return httpx.Response(200, json={})

        client, http = _make_gitlab_client(handler)
        async with http:
            await client.approve_mr("acme/widgets", 42)
        assert len(requests_made) == 1
        assert requests_made[0].method == "POST"
        assert "/projects/acme%2Fwidgets/merge_requests/42/approve" in str(
            requests_made[0].url
        )

    @pytest.mark.asyncio
    async def test_close_mr_sends_close_event(self):
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            return httpx.Response(200, json={})

        client, http = _make_gitlab_client(handler)
        async with http:
            await client.close_mr("acme/widgets", 42)
        assert len(requests_made) == 1
        assert requests_made[0].method == "PUT"
        body = json.loads(requests_made[0].content)
        assert body["state_event"] == "close"

    @pytest.mark.asyncio
    async def test_submit_review_approved_calls_approve_and_comment(self):
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            return httpx.Response(200, json={})

        client, http = _make_gitlab_client(handler)
        async with http:
            await client.submit_review(
                "acme/widgets",
                42,
                ReviewDecision.APPROVED,
                "Ship it!",
            )
        # Should make two requests: approve + comment
        assert len(requests_made) == 2
        assert "/approve" in str(requests_made[0].url)
        assert "/notes" in str(requests_made[1].url)
        body = json.loads(requests_made[1].content)
        assert body["body"] == "Ship it!"

    @pytest.mark.asyncio
    async def test_submit_review_approved_without_body_skips_comment(self):
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            return httpx.Response(200, json={})

        client, http = _make_gitlab_client(handler)
        async with http:
            await client.submit_review(
                "acme/widgets",
                42,
                ReviewDecision.APPROVED,
                "",
            )
        # Only the approve request, no comment because body is empty
        assert len(requests_made) == 1
        assert "/approve" in str(requests_made[0].url)
