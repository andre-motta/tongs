"""Tests for the shared CI icon, relative time and duration helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from rich.style import Style

from tongs.forges.models import CIStatus
from tongs.helpers import ci_icon, ci_icon_text, format_duration, relative_time


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


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (CIStatus.SUCCESS, ("●", Style(color="green"))),
        (CIStatus.FAILED, ("●", Style(color="red"))),
        (CIStatus.RUNNING, ("▶", Style(color="yellow"))),
        (CIStatus.PENDING, ("○", Style(dim=True))),
        (CIStatus.CANCELED, ("—", Style(dim=True))),
        (CIStatus.SKIPPED, ("—", Style(dim=True))),
        (CIStatus.UNKNOWN, ("?", Style(dim=True))),
    ],
)
def test_ci_icon_text_table(status: CIStatus, expected: tuple[str, Style]) -> None:
    """The pipeline and job rows draw from this table, not from the markup one."""
    assert ci_icon_text(status) == expected


_REFERENCE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (timedelta(0), "just now"),
        (timedelta(seconds=59), "just now"),
        (timedelta(seconds=60), "1m ago"),
        (timedelta(minutes=30), "30m ago"),
        (timedelta(minutes=59), "59m ago"),
        (timedelta(minutes=60), "1h ago"),
        (timedelta(hours=5), "5h ago"),
        (timedelta(hours=23), "23h ago"),
        (timedelta(hours=24), "1d ago"),
        (timedelta(days=7), "7d ago"),
        (timedelta(days=365), "365d ago"),
    ],
)
def test_relative_time_units_and_boundaries(elapsed: timedelta, expected: str) -> None:
    frozen = _REFERENCE + elapsed
    with patch("tongs.helpers.datetime", wraps=datetime, now=lambda tz=None: frozen):
        assert relative_time(_REFERENCE) == expected


def test_relative_time_of_missing_timestamp_is_empty() -> None:
    assert relative_time(None) == ""


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, ""),
        (0, "0s"),
        (59, "59s"),
        (59.9, "59s"),
        (60, "1m 00s"),
        (154, "2m 34s"),
        (362.27, "6m 02s"),
        (3600, "1h 00m"),
        (3960, "1h 06m"),
        (36000, "10h 00m"),
    ],
)
def test_format_duration(seconds: float | None, expected: str) -> None:
    assert format_duration(seconds) == expected
