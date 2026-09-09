"""Regression coverage for "/" search on the repository list screen.

Issue #168: the repository list advertises "/ Filter" in its footer and shared
the job log view's defects. The filter box held focus on mount, so "/" was typed
into it as a literal character, and Escape left the screen instead of closing
the filter.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable, Input

from tests.test_tui_session import (
    MockForgeClient,
    MockForgeRegistry,
    make_app,
    make_repo,
    make_summary,
    settle,
)
from tongs.forges.models import ForgeHost
from tongs.scanner.repo import ForgeType
from tongs.views.inbox import InboxScreen
from tongs.views.repo_list import RepoListScreen


async def _open_repo_list(tmp_path: Path):
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    repos = [make_repo(tmp_path, "acme/widgets"), make_repo(tmp_path, "acme/gadgets")]
    client = MockForgeClient(host, [make_summary(host)])
    registry = MockForgeRegistry({"github.com": client})
    app, _session, _cache = make_app(tmp_path, repos, registry)
    return app


@pytest.mark.asyncio
async def test_slash_opens_the_repo_filter_without_typing_a_slash(
    tmp_path: Path,
) -> None:
    app = await _open_repo_list(tmp_path)

    async with app.run_test(size=(120, 34)) as pilot:
        await settle(app)
        await pilot.press("r")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, RepoListScreen)
        search = screen.query_one("#repo-search", Input)
        table = screen.query_one("#repo-table", DataTable)
        assert app.focused is table
        assert table.row_count == 2

        await pilot.press("slash")
        await settle(app)
        assert app.focused is search
        assert search.value == ""

        for key in "widget":
            await pilot.press(key)
        await settle(app)
        assert search.value == "widget"
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_escape_closes_the_repo_filter_before_leaving_the_screen(
    tmp_path: Path,
) -> None:
    app = await _open_repo_list(tmp_path)

    async with app.run_test(size=(120, 34)) as pilot:
        await settle(app)
        await pilot.press("r")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, RepoListScreen)
        search = screen.query_one("#repo-search", Input)
        table = screen.query_one("#repo-table", DataTable)

        await pilot.press("slash")
        for key in "widget":
            await pilot.press(key)
        await settle(app)
        assert table.row_count == 1

        await pilot.press("escape")
        await settle(app)
        assert app.screen is screen
        assert search.value == ""
        assert table.row_count == 2
        assert app.focused is table

        await pilot.press("escape")
        await settle(app)
        assert isinstance(app.screen, InboxScreen)
