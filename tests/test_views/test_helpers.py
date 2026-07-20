"""Tests for inbox view helper functions."""

from datetime import datetime, timedelta, timezone

import pytest

from tongs.forges.models import CIStatus
from tongs.helpers import ci_icon, relative_time


class TestCiIconRichMode:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (CIStatus.SUCCESS, "[green]●[/]"),
            (CIStatus.FAILED, "[red]●[/]"),
            (CIStatus.RUNNING, "[yellow]▶[/]"),
            (CIStatus.PENDING, "[dim]○[/]"),
            (CIStatus.CANCELED, "[dim]—[/]"),
            (CIStatus.SKIPPED, "[dim]—[/]"),
            (CIStatus.UNKNOWN, "[dim]?[/]"),
        ],
    )
    def test_rich_icons(self, status, expected):
        assert ci_icon(status) == expected


class TestCiIconAsciiMode:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (CIStatus.SUCCESS, "[green]OK[/]"),
            (CIStatus.FAILED, "[red]FAIL[/]"),
            (CIStatus.RUNNING, "[yellow]RUN[/]"),
            (CIStatus.PENDING, "[dim]PEND[/]"),
            (CIStatus.CANCELED, "[dim]CANC[/]"),
            (CIStatus.SKIPPED, "[dim]SKIP[/]"),
            (CIStatus.UNKNOWN, "[dim]?[/]"),
        ],
    )
    def test_ascii_icons(self, status, expected):
        assert ci_icon(status, ascii_mode=True) == expected


class TestRelativeTime:
    def _ago(self, **kwargs) -> datetime:
        return datetime.now(timezone.utc) - timedelta(**kwargs)

    def test_seconds_ago_shows_just_now(self):
        assert relative_time(self._ago(seconds=30)) == "just now"

    def test_just_under_a_minute_shows_just_now(self):
        assert relative_time(self._ago(seconds=59)) == "just now"

    def test_five_minutes_ago(self):
        assert relative_time(self._ago(minutes=5)) == "5m ago"

    def test_three_hours_ago(self):
        assert relative_time(self._ago(hours=3)) == "3h ago"

    def test_two_days_ago(self):
        assert relative_time(self._ago(days=2)) == "2d ago"

    def test_boundary_60_seconds_shows_1m(self):
        assert relative_time(self._ago(seconds=60)) == "1m ago"

    def test_boundary_60_minutes_shows_1h(self):
        assert relative_time(self._ago(minutes=60)) == "1h ago"

    def test_boundary_24_hours_shows_1d(self):
        assert relative_time(self._ago(hours=24)) == "1d ago"

    def test_none_returns_empty(self):
        assert relative_time(None) == ""
