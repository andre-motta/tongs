"""Tests for tongs.widgets.pipeline_panel pure functions and messages."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.message import Message

from tongs.forges.models import CIStatus, Pipeline, PipelineJob
from tongs.widgets.pipeline_panel import (
    CancelJobRequested,
    CancelPipelineRequested,
    LoadJobLogRequested,
    LoadJobsRequested,
    PipelinePanel,
    RetryJobRequested,
    RetryPipelineRequested,
)


class PipelinePanelApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.posted: list[Message] = []

    def compose(self) -> ComposeResult:
        yield PipelinePanel(id="pipeline-panel")

    def on_cancel_pipeline_requested(self, message: CancelPipelineRequested) -> None:
        self.posted.append(message)

    def on_retry_pipeline_requested(self, message: RetryPipelineRequested) -> None:
        self.posted.append(message)

    def on_cancel_job_requested(self, message: CancelJobRequested) -> None:
        self.posted.append(message)

    def on_retry_job_requested(self, message: RetryJobRequested) -> None:
        self.posted.append(message)

    def on_load_jobs_requested(self, message: LoadJobsRequested) -> None:
        self.posted.append(message)

    def on_load_job_log_requested(self, message: LoadJobLogRequested) -> None:
        self.posted.append(message)


@pytest.mark.asyncio
async def test_editor_export_strips_ansi_sequences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PipelinePanelApp()
    exported: list[str] = []
    paths: list[Path] = []

    def capture(command: list[str], *, check: bool) -> None:
        assert check is False
        path = Path(command[-1])
        paths.append(path)
        exported.append(path.read_text())

    monkeypatch.setenv("EDITOR", "editor")
    monkeypatch.setattr(app, "suspend", lambda: nullcontext())
    monkeypatch.setattr("tongs.widgets.pipeline_panel.subprocess.run", capture)

    async with app.run_test():
        panel = app.query_one("#pipeline-panel", PipelinePanel)
        panel._view_level = 2
        panel._job_log_text = "\x1b[31mfailed\x1b[0m\nplain"
        panel.action_open_in_editor()

    assert exported == ["failed\nplain"]
    assert "\x1b" not in exported[0]
    # Job logs can hold secrets, so the exported copy must not outlive the editor.
    assert paths
    assert not paths[0].exists()


# ===================================================================
# Messages posted by the panel actions
# ===================================================================


def _pipeline(status: CIStatus) -> Pipeline:
    return Pipeline(
        id=7,
        status=status,
        ref="main",
        sha="abc1234",
        web_url="https://example.com/pipelines/7",
    )


def _job(status: CIStatus) -> PipelineJob:
    return PipelineJob(id=31, name="build", stage="build", status=status)


def _ids(messages: list[Message]) -> list[tuple]:
    return [
        (
            type(m).__name__,
            getattr(m, "pipeline_id", None),
            getattr(m, "job_id", None),
        )
        for m in messages
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "status", "expected"),
    [
        ("C", CIStatus.RUNNING, "CancelPipelineRequested"),
        ("R", CIStatus.FAILED, "RetryPipelineRequested"),
    ],
)
async def test_pipeline_mutation_posts_only_after_the_confirm_press(
    key: str, status: CIStatus, expected: str
) -> None:
    app = PipelinePanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(PipelinePanel)
        panel.set_pipelines([_pipeline(status)])
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()
        assert app.posted == []
        await pilot.press(key)
        await pilot.pause()

    assert _ids(app.posted) == [(expected, 7, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "status", "expected"),
    [
        ("C", CIStatus.RUNNING, "CancelJobRequested"),
        ("R", CIStatus.FAILED, "RetryJobRequested"),
    ],
)
async def test_job_mutation_posts_only_after_the_confirm_press(
    key: str, status: CIStatus, expected: str
) -> None:
    app = PipelinePanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(PipelinePanel)
        panel.set_jobs([_job(status)], _pipeline(CIStatus.RUNNING))
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()
        assert app.posted == []
        await pilot.press(key)
        await pilot.pause()

    assert _ids(app.posted) == [(expected, 7, 31)]


@pytest.mark.asyncio
async def test_enter_loads_the_focused_pipeline_then_the_focused_job_log() -> None:
    app = PipelinePanelApp()
    pipeline = _pipeline(CIStatus.SUCCESS)
    job = _job(CIStatus.SUCCESS)
    async with app.run_test() as pilot:
        panel = app.query_one(PipelinePanel)
        panel.set_pipelines([pipeline])
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        panel.set_jobs([job], pipeline)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    load_jobs, load_log = app.posted
    assert isinstance(load_jobs, LoadJobsRequested)
    assert load_jobs.pipeline is pipeline
    assert isinstance(load_log, LoadJobLogRequested)
    assert (load_log.job, load_log.pipeline) == (job, pipeline)


@pytest.mark.asyncio
async def test_job_log_search_is_case_insensitive() -> None:
    app = PipelinePanelApp()
    async with app.run_test():
        panel = app.query_one(PipelinePanel)
        panel._view_level = 2
        panel._log_search_lines = ["build ok", "error: failed"]
        panel._log_plain_lines = ["build ok", "error: failed"]
        panel._log_row_offset = 0
        panel._do_search("ERROR")

        assert panel._search_matches == [1]
