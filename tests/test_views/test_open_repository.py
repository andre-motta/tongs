"""Opening a configured-host project from the repo list without cloning it."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from textual.pilot import Pilot
from textual.widgets import DataTable, Input, Static

from tests.test_tui_ascii_mode import _assert_ascii
from tests.test_tui_mr_services import _app, _Forge, _settle, _wait_until
from tongs.app import TongsApp
from tongs.errors import ForgePermissionError, NotFoundError
from tongs.forges.models import ForgeHost, MRSummary
from tongs.scanner.repo import ForgeType
from tongs.services.models import RepositoryRef
from tongs.views.inbox import InboxScreen
from tongs.views.open_repository import USAGE_MESSAGE, OpenRepositoryScreen
from tongs.views.repo_list import REMOTE_MARKER, RepoListScreen
from tongs.widgets.mr_table import MRTable

GADGETS = RepositoryRef("github.com", "acme/gadgets")


class _ProjectsForge(_Forge):
    """Serves ``acme/widgets`` (local) and ``acme/gadgets`` (not cloned)."""

    def __init__(self, host: ForgeHost) -> None:
        super().__init__(host)
        self.listed: list[str] = []

    async def list_mrs(
        self, repo_path: str, state: str = "open", per_page: int = 100
    ) -> list[MRSummary]:
        self.listed.append(repo_path)
        if repo_path == "acme/secret":
            raise ForgePermissionError("403")
        if repo_path.casefold() == "acme/gadgets":
            return [replace(self.summary, repo_path=repo_path, number=3)]
        if repo_path == "acme/widgets":
            return [self.summary]
        raise NotFoundError("404")


def _build(tmp_path: Path) -> tuple[TongsApp, _ProjectsForge]:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    forge = _ProjectsForge(host)
    app, _forge = _app(tmp_path, forge)
    return app, forge


async def _repo_list(app: TongsApp, pilot: Pilot) -> RepoListScreen:
    await _settle(app)
    await pilot.press("r")
    await _wait_until(app, lambda: isinstance(app.screen, RepoListScreen))
    return cast(RepoListScreen, app.screen)


async def _submit(app: TongsApp, pilot: Pilot, text: str) -> None:
    await pilot.press("a")
    await _wait_until(app, lambda: isinstance(app.screen, OpenRepositoryScreen))
    app.screen.query_one("#open-repo-input", Input).value = text
    await pilot.press("enter")
    await _settle(app)
    await pilot.pause()


def _message(app: TongsApp) -> str:
    return str(app.screen.query_one("#open-repo-message", Static).render())


def _rows(screen: RepoListScreen) -> list[str]:
    table = screen.query_one("#repo-table", DataTable)
    return [str(table.get_row_at(index)[1]) for index in range(table.row_count)]


@pytest.mark.asyncio
async def test_an_opened_project_gets_its_own_inbox_and_a_marked_row(
    tmp_path: Path,
) -> None:
    app, forge = _build(tmp_path)
    config = replace(app.config)

    async with app.run_test(notifications=True) as pilot:
        repo_list = await _repo_list(app, pilot)
        await _submit(app, pilot, "https://github.com/acme/gadgets/pull/3")

        await _wait_until(app, lambda: isinstance(app.screen, InboxScreen))
        inbox = cast(InboxScreen, app.screen)
        assert inbox.scoped_repository is not None
        assert inbox.scoped_repository.ref == GADGETS
        assert inbox.sub_title == "acme/gadgets"
        await pilot.press("3")
        await _settle(app)
        table = inbox.query_one("#all-open-table", MRTable)
        assert table.row_count == 1

        await pilot.press("r")
        await _wait_until(app, lambda: app.screen is repo_list)
        assert _rows(repo_list) == ["acme/gadgets" + REMOTE_MARKER, "acme/widgets"]

        # The unscoped inbox still reads only the local repository.
        forge.listed.clear()
        await pilot.press("escape")
        await _wait_until(app, lambda: isinstance(app.screen, InboxScreen))
        app.screen.action_refresh()
        await pilot.press("3")
        await _settle(app)
        assert "acme/widgets" in forge.listed
        assert "acme/gadgets" not in forge.listed

    assert app.config == config
    assert not list(tmp_path.rglob("config.toml"))


@pytest.mark.asyncio
async def test_removing_an_opened_project_keeps_local_ones(tmp_path: Path) -> None:
    app, _forge = _build(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        repo_list = await _repo_list(app, pilot)
        await _submit(app, pilot, "github.com/acme/gadgets")
        await _wait_until(app, lambda: isinstance(app.screen, InboxScreen))
        await pilot.press("r")
        await _wait_until(app, lambda: app.screen is repo_list)

        table = repo_list.query_one("#repo-table", DataTable)
        table.move_cursor(row=1)
        await pilot.press("x")
        await pilot.pause()
        assert _rows(repo_list) == ["acme/gadgets" + REMOTE_MARKER, "acme/widgets"]
        assert any("this one is local" in str(n.message) for n in app._notifications)

        table.move_cursor(row=0)
        await pilot.press("x")
        await pilot.pause()
        assert _rows(repo_list) == ["acme/widgets"]
        assert app.services.opened_repositories == ()


@pytest.mark.asyncio
async def test_an_already_local_project_opens_the_local_one(tmp_path: Path) -> None:
    app, forge = _build(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        await _repo_list(app, pilot)
        forge.listed.clear()
        await _submit(app, pilot, "git@github.com:ACME/Widgets.git")

        await _wait_until(app, lambda: isinstance(app.screen, InboxScreen))
        inbox = cast(InboxScreen, app.screen)
        assert inbox.scoped_repository == app.services.repository_snapshot(app.repos[0])
        assert app.services.opened_repositories == ()
        assert any(
            "already a local repository" in str(n.message) for n in app._notifications
        )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("acme", USAGE_MESSAGE),
        (
            "git.example.com/acme/widgets",
            "git.example.com is not a configured forge host.",
        ),
        (
            "github.com/acme/missing",
            "github.com/acme/missing was not found, or you do not have access",
        ),
        (
            "github.com/acme/secret",
            "github.com/acme/secret was not found, or you do not have access",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_project_that_cannot_be_opened_keeps_the_dialog_open(
    tmp_path: Path, text: str, expected: str
) -> None:
    app, _forge = _build(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        repo_list = await _repo_list(app, pilot)
        await _submit(app, pilot, text)

        assert isinstance(app.screen, OpenRepositoryScreen)
        assert expected in _message(app)
        field = app.screen.query_one("#open-repo-input", Input)
        assert not field.disabled
        assert field.value == text
        assert app.services.opened_repositories == ()

        await pilot.press("escape")
        await _wait_until(app, lambda: app.screen is repo_list)
        assert _rows(repo_list) == ["acme/widgets"]


@pytest.mark.asyncio
async def test_an_opened_project_survives_a_refresh(tmp_path: Path) -> None:
    app, _forge = _build(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        repo_list = await _repo_list(app, pilot)
        await _submit(app, pilot, "github.com/acme/gadgets")
        await _wait_until(app, lambda: isinstance(app.screen, InboxScreen))
        inbox = cast(InboxScreen, app.screen)
        await pilot.press("3")
        await _settle(app)

        app.refresh_repositories()
        await _settle(app)
        await pilot.pause()
        assert inbox.query_one("#all-open-table", MRTable).row_count == 1

        await pilot.press("r")
        await _wait_until(app, lambda: app.screen is repo_list)
        assert _rows(repo_list) == ["acme/gadgets" + REMOTE_MARKER, "acme/widgets"]


@pytest.mark.asyncio
async def test_the_dialog_and_marked_rows_are_ascii_only(tmp_path: Path) -> None:
    app, _forge = _build(tmp_path)
    app.config.ascii_mode = True

    async with app.run_test(size=(120, 40), notifications=True) as pilot:
        repo_list = await _repo_list(app, pilot)
        await pilot.press("a")
        await _wait_until(app, lambda: isinstance(app.screen, OpenRepositoryScreen))
        await _assert_ascii(
            app, pilot, "open dialog", "Open repository", "github.com/owner/project"
        )
        app.screen.query_one("#open-repo-input", Input).value = "acme"
        await pilot.press("enter")
        await pilot.pause()
        await _assert_ascii(app, pilot, "open dialog error", "paste its URL")

        app.screen.query_one(
            "#open-repo-input", Input
        ).value = "github.com/acme/gadgets"
        await pilot.press("enter")
        await _wait_until(app, lambda: isinstance(app.screen, InboxScreen))
        await pilot.press("r")
        await _wait_until(app, lambda: app.screen is repo_list)
        await _assert_ascii(app, pilot, "marked row", "acme/gadgets (remote)")
