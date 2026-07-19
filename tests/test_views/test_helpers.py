"""Tests for inbox view helper functions."""

from datetime import datetime, timedelta, timezone

import pytest

from tongs.forges.models import CIStatus
from tongs.widgets.mr_table import _ci_icon, _relative_time


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
        assert _ci_icon(status) == expected


class TestCiIconAsciiMode:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (CIStatus.SUCCESS, "[OK]"),
            (CIStatus.FAILED, "[!!]"),
            (CIStatus.RUNNING, "[..]"),
            (CIStatus.PENDING, "[..]"),
            (CIStatus.CANCELED, "[--]"),
            (CIStatus.SKIPPED, "[--]"),
            (CIStatus.UNKNOWN, "[??]"),
        ],
    )
    def test_ascii_icons(self, status, expected):
        assert _ci_icon(status, ascii_mode=True) == expected


class TestRelativeTime:
    def _ago(self, **kwargs) -> datetime:
        return datetime.now(timezone.utc) - timedelta(**kwargs)

    def test_seconds_ago_shows_now(self):
        assert _relative_time(self._ago(seconds=30)) == "now"

    def test_just_under_a_minute_shows_now(self):
        assert _relative_time(self._ago(seconds=59)) == "now"

    def test_five_minutes_ago(self):
        assert _relative_time(self._ago(minutes=5)) == "5m"

    def test_three_hours_ago(self):
        assert _relative_time(self._ago(hours=3)) == "3h"

    def test_two_days_ago(self):
        assert _relative_time(self._ago(days=2)) == "2d"

    def test_boundary_60_seconds_shows_1m(self):
        assert _relative_time(self._ago(seconds=60)) == "1m"

    def test_boundary_60_minutes_shows_1h(self):
        assert _relative_time(self._ago(minutes=60)) == "1h"

    def test_boundary_24_hours_shows_1d(self):
        assert _relative_time(self._ago(hours=24)) == "1d"
