"""Tests for GitLab client parsing logic."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from tongs.errors import ForgeError, NotFoundError
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
    SourceCleanupStatus,
)
from tongs.scanner.repo import ForgeType


class TestEncodeProject:
    def test_nested_path(self):
        assert (
            _encode_project("redhat/rhel-ai/wheels/builder")
            == "redhat%2Frhel-ai%2Fwheels%2Fbuilder"
        )


class TestParseDatetime:
    def test_iso_format_with_z(self):
        dt = _parse_datetime("2026-01-15T10:30:00Z")
        assert dt == datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)

    def test_iso_format_with_offset(self):
        dt = _parse_datetime("2026-01-15T10:30:00+05:30")
        offset = timezone(timedelta(hours=5, minutes=30))
        assert dt == datetime(2026, 1, 15, 10, 30, 0, tzinfo=offset)
        assert dt.utcoffset() == timedelta(hours=5, minutes=30)

    def test_none_returns_none(self):
        assert _parse_datetime(None) is None

    def test_empty_returns_none(self):
        assert _parse_datetime("") is None


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("success", CIStatus.SUCCESS),
        ("failed", CIStatus.FAILED),
        ("running", CIStatus.RUNNING),
        ("pending", CIStatus.PENDING),
        ("canceled", CIStatus.CANCELED),
        ("cancelled", CIStatus.CANCELED),
        ("created", CIStatus.PENDING),
        ("manual", CIStatus.PENDING),
        ("canceling", CIStatus.RUNNING),
        ("waiting_for_callback", CIStatus.PENDING),
        ("something_new", CIStatus.UNKNOWN),
        (None, CIStatus.UNKNOWN),
    ],
)
def test_parse_ci_status(wire: str | None, expected: CIStatus) -> None:
    assert _parse_ci_status(wire) == expected


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("closed", MRState.CLOSED),
        ("merged", MRState.MERGED),
        ("unknown", MRState.OPEN),
    ],
)
def test_parse_mr_state(wire: str, expected: MRState) -> None:
    assert _parse_mr_state(wire) == expected


@pytest.mark.parametrize(
    ("data", "username", "display_name"),
    [
        ({"username": "dev", "name": "Developer"}, "dev", "Developer"),
        ({"username": "dev"}, "dev", ""),
        (None, "unknown", ""),
    ],
)
def test_parse_user(data: dict | None, username: str, display_name: str) -> None:
    user = _parse_user(data)
    assert (user.username, user.display_name) == (username, display_name)


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
        assert summary.created_at == datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC)

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
        assert detail.head_sha == "ccc333"
        assert detail.base_sha == "aaa111"
        assert detail.start_sha == "bbb222"
        assert detail.changes_count == 5
        assert detail.detailed_merge_status == "mergeable"
        assert len(detail.reviewers) == 1
        assert detail.reviewers[0].username == "bob"
        assert len(detail.assignees) == 1
        assert detail.assignees[0].username == "alice"
        # approved_by uses {"user": {...}} wrapper; _parse_user receives the
        # wrapper dict, so the username key is not at the top level.
        assert len(detail.approvals) == 1

    @pytest.mark.parametrize(
        ("raw", "expected", "overflow"),
        [
            ("1000+", 1000, True),
            (" 1000+ ", 1000, True),
            ("5", 5, False),
            (5, 5, False),
            (7, 7, False),
            (None, 0, False),
            ("", 0, False),
            ("n/a", 0, False),
            ("+", 0, False),
            ("1000++", 0, False),
            (True, 0, False),
            (False, 0, False),
        ],
    )
    def test_parse_mr_detail_changes_count_shapes(
        self, raw: object, expected: int, overflow: bool
    ):
        client = self._client()
        data = _mr_api_json({"changes_count": raw})
        detail = client._parse_mr_detail(data, "acme/widgets")
        assert detail.changes_count == expected
        assert detail.changes_overflow is overflow

    def test_parse_mr_detail_without_changes_count(self):
        client = self._client()
        data = _mr_api_json()
        del data["changes_count"]
        detail = client._parse_mr_detail(data, "acme/widgets")
        assert detail.changes_count == 0
        assert detail.changes_overflow is False

    def test_parse_mr_detail_complete_list_does_not_overflow(self):
        detail = self._client()._parse_mr_detail(_mr_api_json(), "acme/widgets")
        assert detail.changes_count == 5
        assert detail.changes_overflow is False

    @pytest.mark.asyncio
    async def test_get_mr_keeps_overflow_when_approvals_are_merged(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/approvals"):
                return httpx.Response(
                    200,
                    json={"approved_by": [{"user": {"username": "carol"}}]},
                )
            return httpx.Response(200, json=_mr_api_json({"changes_count": "1000+"}))

        client, _ = _make_gitlab_client(handler)
        detail = await client.get_mr("acme/widgets", 42)
        assert [user.username for user in detail.approvals] == ["carol"]
        assert detail.changes_count == 1000
        assert detail.changes_overflow is True


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
        assert pipeline.created_at == datetime(2026, 6, 1, 8, 0, 0, tzinfo=UTC)
        assert pipeline.finished_at == datetime(2026, 6, 1, 8, 10, 0, tzinfo=UTC)


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
            if req.url.path.endswith("/user"):
                return httpx.Response(200, json={"username": "testuser"})
            assert req.url.params["scope"] == "all"
            assert req.url.params["reviewer_username"] == "testuser"
            assert req.url.params["state"] == "opened"
            assert req.url.params["non_archived"] == "true"
            return httpx.Response(200, json=[mr_data])

        client, http = _make_gitlab_client(handler)
        async with http:
            reviews = await client.list_my_reviews()
        assert len(reviews) == 1
        assert reviews[0].number == 42

    @pytest.mark.asyncio
    async def test_list_my_mrs_uses_correct_params(self):
        mr_data = _mr_api_json()

        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.params["scope"] == "created_by_me"
            assert req.url.params["state"] == "opened"
            assert req.url.params["non_archived"] == "true"
            return httpx.Response(200, json=[mr_data])

        client, http = _make_gitlab_client(handler)
        async with http:
            mrs = await client.list_my_mrs()
        assert len(mrs) == 1
        assert mrs[0].number == 42


    @pytest.mark.asyncio
    async def test_approve_mr_posts_correctly(self):
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            if "/notes" in str(req.url):
                return httpx.Response(200, json={"id": 91})
            return httpx.Response(200, json={"id": 42})

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
            return httpx.Response(200, json={"id": 420, "iid": 42, "state": "closed"})

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
            return httpx.Response(
                200, json={"id": 91 if "/notes" in str(req.url) else 42}
            )

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

    @pytest.mark.asyncio
    async def test_explicit_revision_and_rename_range_never_refetch_latest(self):
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            assert req.method == "POST"
            return httpx.Response(
                200,
                json={"id": "thread-1", "notes": [{"id": 8, "body": "body"}]},
            )

        client, http = _make_gitlab_client(handler)
        async with http:
            await client.create_inline_comment(
                "acme/widgets",
                42,
                "new.py",
                11,
                "RIGHT",
                "body",
                10,
                "RIGHT",
                old_path="old.py",
                new_path="new.py",
                head_sha="head",
                base_sha="base",
                start_sha="start",
                old_line=11,
                new_line=11,
                start_old_line=10,
                start_new_line=10,
            )
        assert [request.method for request in requests_made] == ["POST"]
        position = json.loads(requests_made[0].content)["position"]
        assert position["old_path"] == "old.py"
        assert position["new_path"] == "new.py"
        assert position["head_sha"] == "head"
        assert position["old_line"] == 11
        assert position["new_line"] == 11
        assert position["line_range"]["start"]["type"] == "old"
        assert position["line_range"]["start"]["old_line"] == 10
        assert position["line_range"]["start"]["new_line"] == 10
        assert position["line_range"]["end"]["type"] == "old"
        assert position["line_range"]["end"]["line_code"].endswith("_11_11")

    @pytest.mark.asyncio
    async def test_context_position_preserves_both_native_coordinates(self):
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            return httpx.Response(
                200,
                json={"id": "thread-1", "notes": [{"id": 8, "body": "body"}]},
            )

        client, http = _make_gitlab_client(handler)
        async with http:
            await client.create_inline_comment(
                "acme/widgets",
                42,
                "new.py",
                11,
                "RIGHT",
                "body",
                old_path="old.py",
                new_path="new.py",
                head_sha="head",
                base_sha="base",
                start_sha="start",
                old_line=10,
                new_line=11,
            )

        position = json.loads(requests_made[0].content)["position"]
        assert position["old_path"] == "old.py"
        assert position["new_path"] == "new.py"
        assert position["old_line"] == 10
        assert position["new_line"] == 11
        assert "line_range" not in position

    @pytest.mark.asyncio
    async def test_partial_explicit_revision_is_rejected_without_refetch(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            raise AssertionError("explicit revision must never trigger a GET")

        client, http = _make_gitlab_client(handler)
        async with http:
            with pytest.raises(ValueError, match="required together"):
                await client.create_inline_comment(
                    "acme/widgets",
                    42,
                    "new.py",
                    11,
                    "RIGHT",
                    "body",
                    head_sha="captured-head",
                )

    @pytest.mark.asyncio
    async def test_approval_passes_sha_precondition(self):
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            return httpx.Response(200, json={"id": 42})

        client, http = _make_gitlab_client(handler)
        async with http:
            await client.approve_mr("acme/widgets", 42, head_sha="captured-head")
        assert json.loads(requests_made[0].content) == {"sha": "captured-head"}


class TestGitLabLifecycleActions:
    @pytest.mark.asyncio
    async def test_merge_binds_head_and_reports_cleanup_as_unknown(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "id": 420,
                    "iid": 42,
                    "state": "merged",
                    "source_branch": "feature",
                    "target_branch": "main",
                    "merge_commit_sha": "merge-sha",
                    "squash_commit_sha": None,
                    "should_remove_source_branch": True,
                },
            )

        client, http = _make_gitlab_client(handler)
        async with http:
            result = await client.merge_mr(
                "acme/widgets",
                42,
                True,
                True,
                head_sha="captured-head",
                expected_source_repository="acme/widgets",
                expected_source_branch="feature",
                expected_target_branch="main",
            )

        assert result.remote_id == "420"
        assert result.merge_sha == "merge-sha"
        assert result.source_cleanup is SourceCleanupStatus.UNKNOWN
        assert json.loads(requests[0].content) == {
            "squash": True,
            "should_remove_source_branch": True,
            "sha": "captured-head",
        }

    @pytest.mark.asyncio
    async def test_merge_without_cleanup_uses_squash_commit_identity(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "id": 420,
                    "iid": 42,
                    "state": "merged",
                    "source_branch": "feature",
                    "target_branch": "main",
                    "merge_commit_sha": None,
                    "squash_commit_sha": "squash-sha",
                },
            )

        client, http = _make_gitlab_client(handler)
        async with http:
            result = await client.merge_mr(
                "acme/widgets",
                42,
                delete_branch=False,
                expected_target_branch="main",
            )

        assert result.merge_sha == "squash-sha"
        assert result.source_cleanup is SourceCleanupStatus.NOT_REQUESTED
        assert json.loads(requests[0].content) == {
            "squash": False,
            "should_remove_source_branch": False,
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("identity", "number", "state"),
        [
            (True, 1, "merged"),
            (42.0, 42, "merged"),
            (0, 42, "merged"),
            (-1, 42, "merged"),
            (42, 42, "opened"),
        ],
    )
    async def test_merge_rejects_malformed_native_identity(
        self, identity: object, number: int, state: str
    ) -> None:
        client, http = _make_gitlab_client(
            lambda _: httpx.Response(
                200,
                json={
                    "id": 420,
                    "iid": identity,
                    "state": state,
                    "source_branch": "feature",
                    "target_branch": "main",
                    "merge_commit_sha": "merge-sha",
                },
            )
        )
        async with http:
            with pytest.raises(ValueError, match="merge response"):
                await client.merge_mr("acme/widgets", number)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("identity", "number"), [(True, 1), (42.0, 42), (0, 42), (-1, 42)]
    )
    async def test_state_action_rejects_malformed_native_identity(
        self, identity: object, number: int
    ) -> None:
        client, http = _make_gitlab_client(
            lambda _: httpx.Response(
                200, json={"id": 420, "iid": identity, "state": "closed"}
            )
        )
        async with http:
            with pytest.raises(ValueError, match="action response"):
                await client.close_mr("acme/widgets", number)

    @pytest.mark.asyncio
    async def test_close_reopen_and_unapprove_validate_native_results(self) -> None:
        responses = iter(
            [
                {"id": 420, "iid": 42, "state": "closed"},
                {"id": 420, "iid": 42, "state": "opened"},
                {"id": 420, "iid": 42, "state": "opened"},
                {"id": 420, "iid": 42, "state": "opened"},
                {"id": 420, "iid": 42, "state": "closed"},
                {"id": 420, "iid": 7, "state": "opened"},
            ]
        )
        client, http = _make_gitlab_client(
            lambda _: httpx.Response(200, json=next(responses))
        )
        async with http:
            closed = await client.close_mr("acme/widgets", 42)
            reopened = await client.reopen_mr("acme/widgets", 42)
            unapproved = await client.unapprove_mr("acme/widgets", 42)
            with pytest.raises(ValueError, match="action response"):
                await client.close_mr("acme/widgets", 42)
            with pytest.raises(ValueError, match="action response"):
                await client.reopen_mr("acme/widgets", 42)
            with pytest.raises(ValueError, match="action response"):
                await client.unapprove_mr("acme/widgets", 42)

        assert closed.remote_id == "420"
        assert reopened.remote_id == "420"
        assert unapproved.remote_id == "420"


def test_parse_note_on_renamed_file_uses_new_path() -> None:
    client, _ = _make_gitlab_client(lambda r: httpx.Response(200))
    comment = client._parse_note(
        {
            "id": 103,
            "body": "renamed",
            "author": {"username": "bob", "name": "Bob"},
            "created_at": "2026-06-02T10:00:00Z",
            "resolved": False,
            "position": {
                "old_path": "src/old.py",
                "new_path": "src/new.py",
                "old_line": 3,
                "new_line": 4,
            },
        }
    )
    assert comment.file_path == "src/new.py"


def _gitlab_note(note_id: int, **fields: object) -> dict:
    return {
        "id": note_id,
        "body": f"note {note_id}",
        "author": {"username": "bob", "name": "Bob"},
        "created_at": "2026-06-02T10:00:00Z",
        "system": False,
        **fields,
    }


class TestDiscussionResolution:
    @pytest.mark.asyncio
    async def test_resolution_is_read_per_note(self) -> None:
        position = {"new_path": "src/a.py", "old_path": "src/a.py", "new_line": 3}
        discussions = [
            {
                "id": "individual",
                "individual_note": True,
                "notes": [_gitlab_note(1, resolvable=False, resolved=False)],
            },
            {
                "id": "resolved",
                "individual_note": False,
                "notes": [
                    _gitlab_note(2, resolvable=True, resolved=True, position=position),
                    _gitlab_note(3, resolvable=True, resolved=True),
                ],
            },
            {
                "id": "open",
                "individual_note": False,
                "notes": [
                    _gitlab_note(4, resolvable=True, resolved=True, position=position),
                    _gitlab_note(5, resolvable=True, resolved=False),
                ],
            },
            {
                "id": "not-resolvable-thread",
                "individual_note": False,
                "notes": [_gitlab_note(6, resolvable=False)],
            },
        ]
        client, http = _make_gitlab_client(
            lambda _: httpx.Response(200, json=discussions)
        )
        async with http:
            parsed = await client.get_mr_discussions("acme/widgets", 42)

        by_id = {d.id: (d.resolvable, d.is_resolved) for d in parsed}
        assert by_id == {
            "individual": (False, False),
            "resolved": (True, True),
            "open": (True, False),
            "not-resolvable-thread": (False, False),
        }

    @pytest.mark.asyncio
    async def test_individual_note_is_never_resolvable(self) -> None:
        """Even a note flagged resolvable is a standalone comment, not a thread."""
        discussions = [
            {
                "id": "individual",
                "individual_note": True,
                "notes": [_gitlab_note(1, resolvable=True, resolved=False)],
            }
        ]
        client, http = _make_gitlab_client(
            lambda _: httpx.Response(200, json=discussions)
        )
        async with http:
            (parsed,) = await client.get_mr_discussions("acme/widgets", 42)

        assert (parsed.resolvable, parsed.is_resolved) == (False, False)


class TestLazyCI:
    """All Open pages without CI, and CI looked up for one merge request (#396)."""

    @staticmethod
    def _listed(iid: int) -> dict:
        data = _mr_api_json({"iid": iid, "sha": f"sha-{iid}"})
        # List responses carry no pipeline and no diff_refs.
        del data["head_pipeline"]
        del data["diff_refs"]
        return data

    @pytest.mark.asyncio
    async def test_a_page_without_ci_is_one_request(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(200, json=[self._listed(1), self._listed(2)])

        client, http = _make_gitlab_client(handler)
        async with http:
            page = await client.list_mrs_page("acme/widgets", with_ci=False)

        assert client.supports_lazy_ci is True
        assert paths == ["/api/v4/projects/acme/widgets/merge_requests"]
        assert [item.head_commit_sha for item in page.items] == ["sha-1", "sha-2"]
        assert {item.ci_status for item in page.items} == {CIStatus.UNKNOWN}

    @pytest.mark.asyncio
    async def test_a_page_with_ci_still_reads_each_pipeline(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.endswith("/pipelines"):
                return httpx.Response(200, json=[{"id": 1, "status": "failed"}])
            return httpx.Response(200, json=[self._listed(1)])

        client, http = _make_gitlab_client(handler)
        async with http:
            page = await client.list_mrs_page("acme/widgets")

        assert paths == [
            "/api/v4/projects/acme/widgets/merge_requests",
            "/api/v4/projects/acme/widgets/merge_requests/1/pipelines",
        ]
        assert page.items[0].ci_status is CIStatus.FAILED

    @pytest.mark.asyncio
    async def test_lookup_reads_the_newest_merge_request_pipeline(self) -> None:
        seen: list[tuple[str, dict[str, str]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, dict(request.url.params)))
            return httpx.Response(200, json=[{"id": 2, "status": "running"}])

        client, http = _make_gitlab_client(handler)
        async with http:
            status = await client.get_mr_ci_status("acme/widgets", 42, "sha-42")

        assert status is CIStatus.RUNNING
        assert seen == [
            (
                "/api/v4/projects/acme/widgets/merge_requests/42/pipelines",
                {"per_page": "1"},
            )
        ]

    @pytest.mark.asyncio
    async def test_a_merge_request_without_pipelines_is_unknown(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        client, http = _make_gitlab_client(handler)
        async with http:
            assert await client.get_mr_ci_status("acme/widgets", 42) is (
                CIStatus.UNKNOWN
            )

    @pytest.mark.asyncio
    async def test_a_failed_lookup_raises_instead_of_reporting_unknown(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"message": "boom"})

        client, http = _make_gitlab_client(handler)
        async with http:
            with pytest.raises(ForgeError):
                await client.get_mr_ci_status("acme/widgets", 42, "sha-42")

    def test_detail_keeps_its_revision_head_and_lists_the_head_commit(self) -> None:
        client, _http = _make_gitlab_client(lambda request: httpx.Response(200))
        detail = client._parse_mr_detail(
            _mr_api_json({"sha": "listed-head"}), "acme/widgets"
        )
        without_sha = client._parse_mr_summary(_mr_api_json(), "acme/widgets")

        assert detail.head_commit_sha == "listed-head"
        assert detail.head_sha == _mr_api_json()["diff_refs"]["head_sha"]
        assert without_sha.head_commit_sha == _mr_api_json()["diff_refs"]["head_sha"]


@pytest.mark.asyncio
async def test_get_mr_revision_reads_only_the_merge_request() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        if request.url.raw_path == b"/api/v4/projects/acme%2Fwidgets/merge_requests/42":
            return httpx.Response(200, json=_mr_api_json())
        return httpx.Response(500, json={"message": "unexpected request"})

    client, http = _make_gitlab_client(handler)
    async with http:
        detail = await client.get_mr_revision("acme/widgets", 42)

    assert paths == ["/api/v4/projects/acme%2Fwidgets/merge_requests/42"]
    assert detail.number == 42
    assert detail.head_sha == "ccc333"
    assert detail.base_sha == "aaa111"
    assert detail.start_sha == "bbb222"


def _diff_entry(index: int) -> dict:
    path = f"src/file_{index}.py"
    return {
        "old_path": path,
        "new_path": path,
        "a_mode": "100644",
        "b_mode": "100644",
        "new_file": False,
        "renamed_file": False,
        "deleted_file": False,
        "diff": "@@ -1 +1 @@\n-old\n+new\n",
    }


@pytest.mark.asyncio
async def test_get_mr_diff_reads_every_page_of_the_diffs_endpoint() -> None:
    """``/diffs`` replaces the deprecated ``/changes`` and is paginated."""
    seen: list[tuple[str, str, str]] = []
    pages = {
        "1": [_diff_entry(index) for index in range(100)],
        "2": [_diff_entry(index) for index in range(100, 130)],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        seen.append(
            (
                request.url.raw_path.decode().split("?", 1)[0],
                params.get("page", ""),
                params.get("per_page", ""),
            )
        )
        return httpx.Response(200, json=pages.get(params.get("page", ""), []))

    client, http = _make_gitlab_client(handler)
    async with http:
        changes = await client.get_mr_diff("acme/widgets", 42)

    path = "/api/v4/projects/acme%2Fwidgets/merge_requests/42/diffs"
    assert seen == [(path, "1", "100"), (path, "2", "100")]
    assert [change["new_path"] for change in changes] == [
        f"src/file_{index}.py" for index in range(130)
    ]


@pytest.mark.asyncio
async def test_get_mr_diff_keeps_the_file_modes_gitlab_reports() -> None:
    chmod = {
        **_diff_entry(0),
        "new_path": "bin/run.sh",
        "old_path": "bin/run.sh",
        "b_mode": "100755",
        "diff": "",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[chmod])

    client, http = _make_gitlab_client(handler)
    async with http:
        changes = await client.get_mr_diff("acme/widgets", 42)

    assert changes == [chmod]
    assert (changes[0]["a_mode"], changes[0]["b_mode"]) == ("100644", "100755")


@pytest.mark.asyncio
async def test_overflow_signal_still_comes_from_the_merge_request() -> None:
    """Moving to ``/diffs`` keeps the capped ``changes_count`` overflow signal."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.raw_path.decode().endswith("/approvals"):
            return httpx.Response(200, json={"approved_by": []})
        return httpx.Response(200, json=_mr_api_json({"changes_count": "1000+"}))

    client, http = _make_gitlab_client(handler)
    async with http:
        detail = await client.get_mr("acme/widgets", 42)

    assert detail.changes_overflow is True


@pytest.mark.asyncio
async def test_get_mr_diff_falls_back_to_changes_before_gitlab_15_7() -> None:
    """GitLab older than 15.7 has no ``/diffs``; ``/changes`` is read once."""
    seen: list[str] = []
    legacy = {**_diff_entry(0), "b_mode": "100755"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?", 1)[0]
        seen.append(path)
        if path.endswith("/diffs"):
            return httpx.Response(404, json={"message": "404 Not Found"})
        if path.endswith("/changes"):
            return httpx.Response(200, json={"iid": 42, "changes": [legacy]})
        return httpx.Response(500, json={"message": "unexpected request"})

    client, http = _make_gitlab_client(handler)
    async with http:
        changes = await client.get_mr_diff("acme/widgets", 42)

    base = "/api/v4/projects/acme%2Fwidgets/merge_requests/42"
    assert seen == [f"{base}/diffs", f"{base}/changes"]
    assert changes == [legacy]


@pytest.mark.asyncio
async def test_get_mr_diff_reports_a_missing_merge_request_after_one_fallback() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode().split("?", 1)[0])
        return httpx.Response(404, json={"message": "404 Not Found"})

    client, http = _make_gitlab_client(handler)
    async with http:
        with pytest.raises(NotFoundError):
            await client.get_mr_diff("acme/widgets", 42)

    assert [path.rsplit("/", 1)[1] for path in seen] == ["diffs", "changes"]


@pytest.mark.asyncio
async def test_get_mr_diff_stops_paging_at_gitlab_largest_diff() -> None:
    """GitLab returns at most 3000 files, so paging stops after 30 pages."""
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "")
        pages.append(page)
        start = (int(page) - 1) * 100
        return httpx.Response(
            200, json=[_diff_entry(index) for index in range(start, start + 100)]
        )

    client, http = _make_gitlab_client(handler)
    async with http:
        changes = await client.get_mr_diff("acme/widgets", 42)

    assert pages == [str(page) for page in range(1, 31)]
    assert len(changes) == 3000


class TestAbsentOptionalText:
    """A forge's explicit null for an optional text keeps the empty string (#184).

    The core models type these fields as text and spell "not provided" as the
    empty string, so a null in the payload must not leak in as ``None``.
    """

    def test_job_and_pipeline_without_optional_text(self) -> None:
        client, _ = _make_gitlab_client(lambda _: httpx.Response(200))

        job = client._parse_job(
            {
                "id": 5001,
                "name": "unit-tests",
                "stage": None,
                "status": "failed",
                "web_url": None,
            }
        )
        pipeline = client._parse_pipeline(
            {
                "id": 9001,
                "status": "success",
                "ref": "main",
                "sha": "abc123",
                "web_url": None,
                "source": None,
            }
        )

        assert (job.name, job.stage, job.web_url) == ("unit-tests", "", "")
        assert (pipeline.web_url, pipeline.source) == ("", "")

    def test_detail_without_merge_status(self) -> None:
        client, _ = _make_gitlab_client(lambda _: httpx.Response(200))

        detail = client._parse_mr_detail(
            _mr_api_json({"merge_status": None}), "acme/widgets"
        )

        assert detail.merge_status == ""

    @pytest.mark.asyncio
    async def test_commit_without_link(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/merge_requests/42/commits")
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "abc123def456",
                        "short_id": "abc123de",
                        "title": "Fix the build",
                        "message": "Fix the build",
                        "author_name": "Dev",
                        "web_url": None,
                    }
                ],
            )

        client, http = _make_gitlab_client(handler)
        async with http:
            commits = await client.list_mr_commits("acme/widgets", 42)

        assert [(commit.short_sha, commit.web_url) for commit in commits] == [
            ("abc123de", "")
        ]
