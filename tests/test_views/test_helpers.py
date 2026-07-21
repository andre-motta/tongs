"""Tests for inbox view helper functions."""

from datetime import datetime, timedelta, timezone

import pytest

from tongs.forges.models import CIStatus
from tongs.helpers import ci_icon, format_duration, relative_time


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


class TestFormatDuration:
    def test_none_returns_empty(self):
        assert format_duration(None) == ""

    def test_zero_seconds(self):
        assert format_duration(0) == "0s"

    def test_under_one_minute(self):
        assert format_duration(59) == "59s"

    def test_exactly_one_minute(self):
        assert format_duration(60) == "1m 00s"

    def test_minutes_and_seconds(self):
        assert format_duration(154) == "2m 34s"

    def test_exactly_one_hour(self):
        assert format_duration(3600) == "1h 00m"

    def test_hours_and_minutes(self):
        assert format_duration(3960) == "1h 06m"

    def test_float_input_truncates(self):
        assert format_duration(59.9) == "59s"

    def test_float_over_minute(self):
        assert format_duration(90.7) == "1m 30s"
