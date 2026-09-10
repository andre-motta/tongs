"""Regression coverage for "/" search in the pipelines, jobs and logs view.

Issue #168: the binding advertised as "/ search" in the job log header did not
behave as promised. These tests drive the real MR detail screen with a fake
forge that returns a multi-line job log.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from rich.text import Text
from textual.widgets import Input, RichLog, Static

from tests.test_tui_mr_services import _app, _settle
from tongs.app import TongsApp
from tongs.views.mr_detail import MRDetailScreen
from tongs.widgets.pipeline_panel import PipelinePanel

JOB_LOG = "\n".join(
    f"step {i:02d} error: boom" if i % 7 == 3 else f"step {i:02d} starting"
    for i in range(60)
)
MATCH_LINES = [i for i, line in enumerate(JOB_LOG.split("\n")) if "error" in line]


async def _open_job_log(app: TongsApp, pilot) -> tuple[MRDetailScreen, PipelinePanel]:
    await _settle(app)
    app.screen.query_one("#reviews-table").focus()
    await pilot.press("enter")
    await _settle(app)
    screen = cast(MRDetailScreen, app.screen)
    await pilot.press("5")
    await _settle(app)
    panel = screen.query_one("#pipeline-panel", PipelinePanel)
    panel.focus()
    await pilot.press("enter")  # pipelines -> jobs
    await _settle(app)
    await pilot.press("enter")  # jobs -> log
    await _settle(app)
    assert panel._view_level == 2
    return screen, panel


def _with_log(app: TongsApp, forge) -> None:
    async def get_job_log(repo_path: str, job_id: int) -> str:
        forge.calls.append(("get_job_log", repo_path, job_id))
        return JOB_LOG

    forge.get_job_log = get_job_log


# A markup regression crashes the app inside a render, and a pilot wait after
# that never resolves. Bound the two tests that drive forge-supplied brackets so
# a regression reports in seconds instead of stalling a runner. The crashed app
# also leaves its sqlite worker threads open, which delays interpreter exit; that
# leak lives in the session cleanup path, not in these tests.
CRASH_BUDGET_SECONDS = 20


def _status_text(panel: PipelinePanel) -> str:
    return panel.query_one("#log-search-status", Static).render().plain


@pytest.mark.asyncio
async def test_slash_searches_the_job_log_and_reports_match_count(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)
    _with_log(app, forge)

    async with app.run_test(size=(120, 34), notifications=True) as pilot:
        _screen, panel = await _open_job_log(app, pilot)
        log_widget = panel.query_one("#job-log-content", RichLog)
        search = panel.query_one("#log-search-input", Input)

        await pilot.press("slash")
        await _settle(app)
        assert search.display is True
        assert app.focused is search

        for key in "error":
            await pilot.press(key)
        await _settle(app)
        assert search.value == "error"
        assert panel._search_matches == MATCH_LINES
        assert f"1/{len(MATCH_LINES)}" in _status_text(panel)
        assert log_widget.scroll_offset.y == MATCH_LINES[0]


@pytest.mark.asyncio
async def test_n_and_shift_n_walk_matches_with_wraparound(tmp_path: Path) -> None:
    app, forge = _app(tmp_path)
    _with_log(app, forge)

    async with app.run_test(size=(120, 34), notifications=True) as pilot:
        _screen, panel = await _open_job_log(app, pilot)
        log_widget = panel.query_one("#job-log-content", RichLog)

        await pilot.press("slash")
        for key in "error":
            await pilot.press(key)
        await pilot.press("enter")
        await _settle(app)
        assert app.focused is panel
        assert panel.query_one("#log-search-input", Input).display is False
        assert f"1/{len(MATCH_LINES)}" in _status_text(panel)

        await pilot.press("n")
        await _settle(app)
        assert panel._search_index == 1
        assert f"2/{len(MATCH_LINES)}" in _status_text(panel)
        assert log_widget.scroll_offset.y == MATCH_LINES[1]

        await pilot.press("N")
        await _settle(app)
        assert panel._search_index == 0

        await pilot.press("N")
        await _settle(app)
        assert panel._search_index == len(MATCH_LINES) - 1

        await pilot.press("n")
        await _settle(app)
        assert panel._search_index == 0


@pytest.mark.asyncio
async def test_escape_closes_search_and_restores_the_log_view(tmp_path: Path) -> None:
    app, forge = _app(tmp_path)
    _with_log(app, forge)

    async with app.run_test(size=(120, 34), notifications=True) as pilot:
        screen, panel = await _open_job_log(app, pilot)
        log_widget = panel.query_one("#job-log-content", RichLog)

        opened_at = log_widget.scroll_offset.y
        assert opened_at > 0  # the log opens tailed to its end

        await pilot.press("slash")
        for key in "error":
            await pilot.press(key)
        await _settle(app)
        assert log_widget.scroll_offset.y == MATCH_LINES[0]

        await pilot.press("escape")
        await _settle(app)
        assert app.screen is screen
        assert panel._view_level == 2
        assert panel.query_one("#log-search-input", Input).display is False
        assert panel._search_matches == []
        assert _status_text(panel) == ""
        assert log_widget.scroll_offset.y == opened_at
        assert app.focused is panel

        await pilot.press("escape")
        await _settle(app)
        assert panel._view_level == 1


@pytest.mark.asyncio
async def test_slash_outside_the_log_view_tells_the_user_why(tmp_path: Path) -> None:
    app, forge = _app(tmp_path)
    _with_log(app, forge)

    async with app.run_test(size=(120, 34), notifications=True) as pilot:
        await _settle(app)
        app.screen.query_one("#reviews-table").focus()
        await pilot.press("enter")
        await _settle(app)
        screen = cast(MRDetailScreen, app.screen)
        await pilot.press("5")
        await _settle(app)
        panel = screen.query_one("#pipeline-panel", PipelinePanel)
        panel.focus()
        assert panel._view_level == 0

        app.clear_notifications()
        await pilot.press("slash")
        await _settle(app)
        messages = [notification.message for notification in app._notifications]
        assert any("job log" in message for message in messages), messages


def test_rendered_row_accounts_for_dropped_log_lines() -> None:
    panel = PipelinePanel()
    assert panel._rendered_row(12) == 12
    panel._log_row_offset = 10
    assert panel._rendered_row(12) == 2
    assert panel._rendered_row(3) == 0


@pytest.mark.asyncio
async def test_bracketed_log_text_and_query_do_not_crash_the_app(
    tmp_path: Path,
) -> None:
    """Log content and the query reach the status line as text, not markup."""
    app, forge = _app(tmp_path)
    linker_log = (
        "[INFO] build starting\n"
        "[/usr/bin/ld] error: undefined reference to `foo'\n"
        "[INFO] build failed"
    )

    async def get_job_log(repo_path: str, job_id: int) -> str:
        forge.calls.append(("get_job_log", repo_path, job_id))
        return linker_log

    forge.get_job_log = get_job_log

    async with (
        asyncio.timeout(CRASH_BUDGET_SECONDS),
        app.run_test(size=(120, 34), notifications=True) as pilot,
    ):
        _screen, panel = await _open_job_log(app, pilot)

        await pilot.press("slash")
        for key in "error":
            await pilot.press(key)
        await _settle(app)
        assert app.is_running
        assert panel._search_matches == [1]
        status = _status_text(panel)
        assert "Match 1/1" in status
        assert "[/usr/bin/ld] error" in status

        search = panel.query_one("#log-search-input", Input)
        search.value = "[/"
        await _settle(app)
        assert app.is_running
        assert panel._search_matches == [1]
        assert "[/usr/bin/ld] error" in _status_text(panel)

        search.value = "[/nothing-matches"
        await _settle(app)
        assert app.is_running
        assert panel._search_matches == []
        assert "[/nothing-matches" in _status_text(panel)


@pytest.mark.asyncio
async def test_bracketed_job_name_and_stage_render_in_the_pipeline_panel(
    tmp_path: Path,
) -> None:
    """Job names and stage names come from the forge, so brackets are literal."""
    app, forge = _app(tmp_path)
    _with_log(app, forge)
    forge.job = replace(forge.job, name="build [/all]", stage="deploy [/prod]")

    async with (
        asyncio.timeout(CRASH_BUDGET_SECONDS),
        app.run_test(size=(120, 34), notifications=True) as pilot,
    ):
        _screen, panel = await _open_job_log(app, pilot)
        assert app.is_running

        header = panel.query_one("#job-log-header", Static).render().plain
        assert "build [/all]" in header
        assert "deploy [/prod]" in header

        stage_headers = [
            static.render().plain for static in panel.query("#job-list-scroll Static")
        ]
        assert any("deploy [/prod]" in text for text in stage_headers)


@pytest.mark.asyncio
async def test_live_search_does_not_re_decode_the_log_per_keystroke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, forge = _app(tmp_path)
    _with_log(app, forge)

    async with app.run_test(size=(120, 34), notifications=True) as pilot:
        _screen, panel = await _open_job_log(app, pilot)
        assert len(panel._log_search_lines) == len(JOB_LOG.split("\n"))

        decoded: list[str] = []
        original = Text.from_ansi

        def counting_from_ansi(
            _cls, text: str, *args: object, **kwargs: object
        ) -> Text:
            decoded.append(text)
            return original(text, *args, **kwargs)

        monkeypatch.setattr(Text, "from_ansi", classmethod(counting_from_ansi))

        await pilot.press("slash")
        for key in "error":
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.press("n")
        await pilot.press("N")
        await _settle(app)

        assert panel._search_matches == MATCH_LINES
        assert panel._search_index == 0
        assert decoded == []
