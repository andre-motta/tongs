"""Tests for MR table sort_mrs function."""

from __future__ import annotations

from datetime import datetime, timezone


from tongs.forges.models import (
    CIStatus,
    ForgeHost,
    MRState,
    MRSummary,
    User,
)
from tongs.scanner.repo import ForgeType
from tongs.widgets.mr_table import sort_mrs


def _make_mr(
    number: int = 1,
    title: str = "Fix bug",
    username: str = "alice",
    ci_status: CIStatus = CIStatus.SUCCESS,
    updated_at: datetime | None = None,
    created_at: datetime | None = None,
) -> MRSummary:
    return MRSummary(
        forge_host=ForgeHost(
            hostname="gitlab.example.com",
            forge_type=ForgeType.GITLAB,
            api_base="https://gitlab.example.com/api/v4",
        ),
        repo_path="org/repo",
        local_path="/tmp/repo",
        number=number,
        title=title,
        author=User(username=username),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=ci_status,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=updated_at or datetime(2026, 1, 2, tzinfo=timezone.utc),
        web_url="https://gitlab.example.com/org/repo/-/merge_requests/1",
    )


class TestSortMrsByUpdated:
    def test_sorts_by_updated_at_descending(self):
        older = _make_mr(number=1, updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        newer = _make_mr(number=2, updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
        result = sort_mrs([older, newer], "updated")
        assert result[0].number == 2
        assert result[1].number == 1

    def test_updated_is_default_sort(self):
        """Any unrecognized key falls back to updated sort."""
        older = _make_mr(number=1, updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        newer = _make_mr(number=2, updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
        result = sort_mrs([older, newer], "nonsense")
        assert result[0].number == 2


class TestSortMrsByTitle:
    def test_sorts_alphabetically(self):
        mr_b = _make_mr(number=1, title="Beta feature")
        mr_a = _make_mr(number=2, title="Alpha fix")
        mr_c = _make_mr(number=3, title="Charlie update")
        result = sort_mrs([mr_b, mr_a, mr_c], "title")
        assert [m.title for m in result] == ["Alpha fix", "Beta feature", "Charlie update"]

    def test_case_insensitive(self):
        mr_upper = _make_mr(number=1, title="Zebra")
        mr_lower = _make_mr(number=2, title="apple")
        result = sort_mrs([mr_upper, mr_lower], "title")
        assert result[0].title == "apple"
        assert result[1].title == "Zebra"


class TestSortMrsByCi:
    def test_failed_first_success_last(self):
        mr_success = _make_mr(number=1, ci_status=CIStatus.SUCCESS)
        mr_failed = _make_mr(number=2, ci_status=CIStatus.FAILED)
        mr_running = _make_mr(number=3, ci_status=CIStatus.RUNNING)
        result = sort_mrs([mr_success, mr_failed, mr_running], "ci")
        assert result[0].ci_status == CIStatus.FAILED
        assert result[1].ci_status == CIStatus.RUNNING
        assert result[2].ci_status == CIStatus.SUCCESS

    def test_same_ci_breaks_tie_by_title(self):
        mr_b = _make_mr(number=1, title="Beta", ci_status=CIStatus.FAILED)
        mr_a = _make_mr(number=2, title="Alpha", ci_status=CIStatus.FAILED)
        result = sort_mrs([mr_b, mr_a], "ci")
        assert result[0].title == "Alpha"
        assert result[1].title == "Beta"


class TestSortMrsByAuthor:
    def test_sorts_by_username(self):
        mr_charlie = _make_mr(number=1, username="charlie")
        mr_alice = _make_mr(number=2, username="alice")
        mr_bob = _make_mr(number=3, username="bob")
        result = sort_mrs([mr_charlie, mr_alice, mr_bob], "author")
        assert [m.author.username for m in result] == ["alice", "bob", "charlie"]

    def test_same_author_breaks_tie_by_title(self):
        mr_b = _make_mr(number=1, title="Beta", username="alice")
        mr_a = _make_mr(number=2, title="Alpha", username="alice")
        result = sort_mrs([mr_b, mr_a], "author")
        assert result[0].title == "Alpha"
        assert result[1].title == "Beta"
