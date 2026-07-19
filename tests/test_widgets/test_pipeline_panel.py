"""Tests for tongs.widgets.pipeline_panel pure functions and messages."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from rich.style import Style

from tongs.forges.models import CIStatus, Pipeline, PipelineJob
from tongs.widgets.pipeline_panel import (
    CancelJobRequested,
    CancelPipelineRequested,
    LoadJobLogRequested,
    LoadJobsRequested,
    RetryJobRequested,
    RetryPipelineRequested,
    _ci_icon_markup,
    _ci_icon_text,
    _format_duration,
    _relative_time,
)


# ===================================================================
# _format_duration
# ===================================================================


class TestFormatDuration:
    """Tests for _format_duration()."""

    def test_none_returns_empty(self):
        assert _format_duration(None) == ""

    def test_zero_returns_zero_s(self):
        assert _format_duration(0) == "0s"

    def test_seconds_only(self):
        assert _format_duration(59) == "59s"

    def test_boundary_exactly_60(self):
        assert _format_duration(60) == "1m 00s"

    def test_minutes_and_seconds(self):
        assert _format_duration(90) == "1m 30s"

    def test_boundary_exactly_3600(self):
        assert _format_duration(3600) == "1h 00m"

    def test_float_truncated(self):
        assert _format_duration(362.27) == "6m 02s"

    def test_one_second(self):
        assert _format_duration(1) == "1s"

    def test_hours_with_remaining_minutes(self):
        assert _format_duration(3661) == "1h 01m"

    def test_large_hours(self):
        assert _format_duration(36000) == "10h 00m"


# ===================================================================
# _relative_time
# ===================================================================


class TestRelativeTime:
    """Tests for _relative_time()."""

    def _fixed_now(self, **kwargs):
        """Return a patcher that freezes datetime.now to a fixed offset from the reference dt."""
        ref = datetime(2026, 1, 1, tzinfo=timezone.utc)
        frozen = ref + timedelta(**kwargs)

        original_now = datetime.now

        def fake_now(tz=None):
            if tz is not None:
                return frozen
            return original_now(tz)

        return patch(
            "tongs.widgets.pipeline_panel.datetime",
            wraps=datetime,
            **{"now": fake_now},
        )

    def test_none_returns_empty(self):
        assert _relative_time(None) == ""

    def test_just_now_zero_seconds(self):
        with self._fixed_now(seconds=0):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "just now"

    def test_just_now_under_60_seconds(self):
        with self._fixed_now(seconds=59):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "just now"

    def test_boundary_exactly_60_seconds(self):
        with self._fixed_now(seconds=60):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "1m ago"

    def test_minutes_plural(self):
        with self._fixed_now(minutes=30):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "30m ago"

    def test_boundary_exactly_59_minutes(self):
        with self._fixed_now(minutes=59):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "59m ago"

    def test_boundary_exactly_60_minutes(self):
        with self._fixed_now(minutes=60):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "1h ago"

    def test_hours_plural(self):
        with self._fixed_now(hours=5):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "5h ago"

    def test_boundary_exactly_23_hours(self):
        with self._fixed_now(hours=23):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "23h ago"

    def test_boundary_exactly_24_hours(self):
        with self._fixed_now(hours=24):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "1d ago"

    def test_days_plural(self):
        with self._fixed_now(days=7):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "7d ago"

    def test_large_day_count(self):
        with self._fixed_now(days=365):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "365d ago"


# ===================================================================
# _ci_icon_text
# ===================================================================


class TestCiIconText:
    """Tests for _ci_icon_text()."""

    def test_success(self):
        char, style = _ci_icon_text(CIStatus.SUCCESS)
        assert char == "●"
        assert style == Style(color="green")

    def test_failed(self):
        char, style = _ci_icon_text(CIStatus.FAILED)
        assert char == "●"
        assert style == Style(color="red")

    def test_running(self):
        char, style = _ci_icon_text(CIStatus.RUNNING)
        assert char == "▶"
        assert style == Style(color="yellow")

    def test_pending(self):
        char, style = _ci_icon_text(CIStatus.PENDING)
        assert char == "○"
        assert style == Style(dim=True)

    def test_canceled(self):
        char, style = _ci_icon_text(CIStatus.CANCELED)
        assert char == "—"
        assert style == Style(dim=True)

    def test_skipped(self):
        char, style = _ci_icon_text(CIStatus.SKIPPED)
        assert char == "—"
        assert style == Style(dim=True)

    def test_unknown(self):
        char, style = _ci_icon_text(CIStatus.UNKNOWN)
        assert char == "?"
        assert style == Style(dim=True)


# ===================================================================
# _ci_icon_markup
# ===================================================================


class TestCiIconMarkup:
    """Tests for _ci_icon_markup()."""

    def test_success(self):
        assert _ci_icon_markup(CIStatus.SUCCESS) == "[green]●[/]"

    def test_failed(self):
        assert _ci_icon_markup(CIStatus.FAILED) == "[red]●[/]"

    def test_running(self):
        assert _ci_icon_markup(CIStatus.RUNNING) == "[yellow]▶[/]"

    def test_pending(self):
        assert _ci_icon_markup(CIStatus.PENDING) == "[dim]○[/]"

    def test_canceled(self):
        assert _ci_icon_markup(CIStatus.CANCELED) == "[dim]—[/]"

    def test_skipped(self):
        assert _ci_icon_markup(CIStatus.SKIPPED) == "[dim]—[/]"

    def test_unknown(self):
        assert _ci_icon_markup(CIStatus.UNKNOWN) == "[dim]?[/]"


# ===================================================================
# Messages
# ===================================================================


class TestMessages:
    """Tests for pipeline panel message classes."""

    def test_cancel_pipeline_requested(self):
        msg = CancelPipelineRequested(pipeline_id=42)
        assert msg.pipeline_id == 42

    def test_retry_pipeline_requested(self):
        msg = RetryPipelineRequested(pipeline_id=99)
        assert msg.pipeline_id == 99

    def test_cancel_job_requested(self):
        msg = CancelJobRequested(job_id=101)
        assert msg.job_id == 101

    def test_retry_job_requested(self):
        msg = RetryJobRequested(job_id=202)
        assert msg.job_id == 202

    def test_load_jobs_requested(self):
        pipeline = Pipeline(
            id=1,
            status=CIStatus.SUCCESS,
            ref="main",
            sha="abc1234",
            web_url="https://example.com/pipelines/1",
        )
        msg = LoadJobsRequested(pipeline=pipeline)
        assert msg.pipeline is pipeline
        assert msg.pipeline.id == 1

    def test_load_job_log_requested(self):
        pipeline = Pipeline(
            id=5,
            status=CIStatus.RUNNING,
            ref="feature",
            sha="def5678",
            web_url="https://example.com/pipelines/5",
        )
        job = PipelineJob(
            id=10,
            name="build",
            stage="build",
            status=CIStatus.RUNNING,
        )
        msg = LoadJobLogRequested(job=job, pipeline=pipeline)
        assert msg.job is job
        assert msg.pipeline is pipeline
        assert msg.job.id == 10
        assert msg.pipeline.id == 5
