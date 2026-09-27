"""Forge text is always shown literally in the terminal app (issue #290).

Titles, labels, names, branches and paths come from the forge. Handing them to
Rich or Textual as markup let a title such as ``Fix [/] x`` raise MarkupError on
every launch, made ``[docs] y`` lose its bracketed word, and turned
``[link=...]`` into a terminal hyperlink. These tests drive real widgets and
screens and assert the rendered plain text.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.content import Content
from textual.widgets import DataTable, Static

from tests.test_tui_mr_services import _app, _settle
from tests.test_tui_session import (
    MockForgeClient,
    MockForgeRegistry,
    make_app,
    make_repo,
    make_summary,
    settle,
)
from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
from tongs.forges.models import ForgeHost, MRDetail, User
from tongs.scanner.repo import ForgeType
from tongs.views.mr_detail import MRDetailScreen, MROverview
from tongs.views.repo_list import RepoListScreen
from tongs.widgets.comment_editor import CommentEditor
from tongs.widgets.diff_panel import DiffFileTree
from tongs.widgets.mr_table import MRTable
from tongs.widgets.pipeline_panel import PipelinePanel

# A markup regression crashes the app inside a render, and a pilot wait after
# that never resolves. Bound each app so a regression reports in seconds.
CRASH_BUDGET_SECONDS = 20

TITLES = (
    "Fix [/] x",
    "[docs] y",
    "[skip ci] x",
    "[link=https://x]click[/link]",
    "Fix route [/api/v1]",
    "[WIP] :smile: title\\",
)


def _rendered_lines(widget) -> list[str]:
    return [widget.render_line(y).text for y in range(widget.size.height)]


def _has_link(content: Content | Text) -> bool:
    spans = content.spans
    for span in spans:
        style = span.style
        if "link" in str(style) or getattr(style, "link", None):
            return True
    return False


@pytest.mark.asyncio
async def test_inbox_renders_bracketed_titles_literally(tmp_path: Path) -> None:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    summaries = [
        replace(make_summary(host, number=number), title=title)
        for number, title in enumerate(TITLES, start=1)
    ]
    client = MockForgeClient(host, summaries)
    registry = MockForgeRegistry({host.hostname: client})
    app, _session, _cache = make_app(tmp_path, [make_repo(tmp_path)], registry)

    async with (
        asyncio.timeout(CRASH_BUDGET_SECONDS),
        app.run_test(size=(200, 40)) as pilot,
    ):
        await settle(app)
        await pilot.pause()
        assert app.is_running
        table = app.screen.query_one("#reviews-table", MRTable)
        assert table.row_count == len(TITLES)

        cells = [table.get_row_at(row) for row in range(table.row_count)]
        shown_titles = {cast(Text, row[3]).plain.strip() for row in cells}
        assert shown_titles == set(TITLES)
        assert not any(_has_link(cast(Text, row[3])) for row in cells)

        rendered = "\n".join(_rendered_lines(table))
        for title in TITLES:
            assert title in rendered, title


@pytest.mark.asyncio
async def test_mr_table_keeps_author_repo_and_draft_prefix(tmp_path: Path) -> None:
    host = ForgeHost("gitlab.example.com", ForgeType.GITLAB, "https://x/api/v4")
    mr = replace(
        make_summary(host, project="acme/[api]widgets"),
        title="[docs] y",
        author=User("[bot]"),
        is_draft=True,
    )

    class TableApp(App):
        def compose(self) -> ComposeResult:
            yield MRTable()

    app = TableApp()
    async with asyncio.timeout(CRASH_BUDGET_SECONDS), app.run_test(size=(200, 10)):
        table = app.query_one(MRTable)
        table.setup_columns()
        table.add_mr_row(mr)
        _forge, _ci, _number, title, author, repo, _updated = table.get_row_at(0)
        assert title.plain == "D [docs] y"
        assert not title.style
        assert [(span.start, span.end, span.style) for span in title.spans] == [
            (0, 2, "dim")
        ]
        assert author.plain == "[bot]"
        assert repo.plain == "acme/[api]widgets"


def _detail(**overrides: object) -> MRDetail:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    summary = make_summary(host)
    fields = {
        **summary.__dict__,
        "title": "Fix [/] x",
        "source_branch": "fix/[/]",
        "target_branch": "[main]",
        "labels": ("[/]", "[WIP]", "[skip ci]", "[link=https://x]l[/link]"),
        "author": User("[author]"),
        "approvals": (User("[a]"),),
        "reviewers": (User("[r]"), User("$rev")),
        "assignees": (User("[/as]"),),
        "detailed_merge_status": "[/]odd_status",
    }
    fields.update(overrides)
    return MRDetail(**fields)


@pytest.mark.asyncio
async def test_mr_overview_shows_labels_names_and_branches_literally() -> None:
    class OverviewApp(App):
        def compose(self) -> ComposeResult:
            yield MROverview()

    app = OverviewApp()
    async with asyncio.timeout(CRASH_BUDGET_SECONDS), app.run_test(size=(200, 20)):
        overview = app.query_one(MROverview)
        overview.set_mr(_detail())
        content = overview.render()
        plain = content.plain
        assert f"!{7} Fix [/] x" in plain
        assert "fix/[/] -> [main]  by @[author]" in plain
        assert "Approvals: [a]" in plain
        assert "Reviewers: [r], $rev" in plain
        assert "Assignees: [/as]" in plain
        assert "Labels: [/], [WIP], [skip ci], [link=https://x]l[/link]" in plain
        assert "blocked -- [/]odd status" in plain
        assert not _has_link(cast(Content, content))


@pytest.mark.asyncio
@pytest.mark.parametrize("title", TITLES)
async def test_mr_detail_screen_shows_title_literally(
    tmp_path: Path, title: str
) -> None:
    app, forge = _app(tmp_path)
    forge.summary = replace(forge.summary, title=title)
    forge.detail = replace(forge.detail, title=title, labels=("[/]",))

    async with (
        asyncio.timeout(CRASH_BUDGET_SECONDS),
        app.run_test(size=(160, 40)) as pilot,
    ):
        await _settle(app)
        app.screen.query_one("#reviews-table").focus()
        await pilot.press("enter")
        await _settle(app)
        screen = app.screen
        assert isinstance(screen, MRDetailScreen)
        assert app.is_running
        assert screen.sub_title == f"!7 {title}"
        overview = screen.query_one(MROverview).render().plain
        assert f"!7 {title}" in overview
        assert "Labels: [/]" in overview


@pytest.mark.asyncio
async def test_commits_and_stage_names_render_literally(tmp_path: Path) -> None:
    app, forge = _app(tmp_path)
    forge.job = replace(forge.job, name="[Build] x", stage="[Deploy]")

    async def list_mr_commits(repo_path: str, number: int):
        commits = await original_commits(repo_path, number)
        return [
            replace(
                commit,
                title="[skip ci] x",
                message="[skip ci] x\n\n[WIP] body [/]",
                author=User("[c]"),
            )
            for commit in commits
        ]

    original_commits = forge.list_mr_commits
    forge.list_mr_commits = list_mr_commits

    async with (
        asyncio.timeout(CRASH_BUDGET_SECONDS),
        app.run_test(size=(160, 40), notifications=True) as pilot,
    ):
        await _settle(app)
        app.screen.query_one("#reviews-table").focus()
        await pilot.press("enter")
        await _settle(app)
        screen = cast(MRDetailScreen, app.screen)
        await pilot.press("3")
        await _settle(app)
        commits = screen.query_one("#commits-content", Static).render().plain
        assert "[skip ci] x  @[c]" in commits
        assert "[WIP] body [/]" in commits

        await pilot.press("5")
        await _settle(app)
        panel = screen.query_one("#pipeline-panel", PipelinePanel)
        panel.focus()
        await pilot.press("enter")
        await _settle(app)
        assert app.is_running
        stage_headers = [
            static.render().plain for static in panel.query("#job-list-scroll Static")
        ]
        assert any("[Deploy]" in text for text in stage_headers)


@pytest.mark.asyncio
async def test_repo_list_shows_bracketed_repository_names(tmp_path: Path) -> None:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    repos = [make_repo(tmp_path, "acme/[docs]widgets"), make_repo(tmp_path, "a/[/]")]
    client = MockForgeClient(host, [make_summary(host)])
    registry = MockForgeRegistry({host.hostname: client})
    app, _session, _cache = make_app(tmp_path, repos, registry)

    async with (
        asyncio.timeout(CRASH_BUDGET_SECONDS),
        app.run_test(size=(120, 34)) as pilot,
    ):
        await settle(app)
        await pilot.press("r")
        await settle(app)
        screen = app.screen
        assert isinstance(screen, RepoListScreen)
        table = screen.query_one("#repo-table", DataTable)
        names = {
            cast(Text, table.get_row_at(row)[1]).plain for row in range(table.row_count)
        }
        assert names == {"acme/[docs]widgets", "a/[/]"}
        rendered = "\n".join(_rendered_lines(table))
        assert "acme/[docs]widgets" in rendered
        assert "a/[/]" in rendered


def _bracket_file() -> DiffFile:
    line = DiffLine(
        old_lineno=None, new_lineno=3, content="x", line_type=LineType.ADDITION
    )
    hunk = DiffHunk("@@ -0,0 +3,1 @@", 0, 0, 3, 1, (line,))
    return DiffFile(
        old_path="pages/[id].tsx",
        new_path="pages/[id].tsx",
        status=FileStatus.ADDED,
        hunks=(hunk,),
        additions=1,
    )


@pytest.mark.asyncio
async def test_comment_editor_reply_header_keeps_bracketed_path() -> None:
    class EditorApp(App):
        def compose(self) -> ComposeResult:
            yield CommentEditor()

    app = EditorApp()
    file = _bracket_file()
    async with (
        asyncio.timeout(CRASH_BUDGET_SECONDS),
        app.run_test(size=(120, 20)) as pilot,
    ):
        editor = app.query_one(CommentEditor)
        editor.open_reply("d-1", file, file.hunks[0].lines[0], author="[bot]")
        await pilot.pause()
        header = editor.query_one("#editor-header", Static).render().plain
        assert header == "Reply to @[bot] on pages/[id].tsx:3"

        editor.open_reply_general("d-2", author="[/]")
        await pilot.pause()
        header = editor.query_one("#editor-header", Static).render().plain
        assert header == "Reply to @[/]"


@pytest.mark.asyncio
async def test_diff_file_tree_keeps_bracketed_file_names() -> None:
    class TreeApp(App):
        def compose(self) -> ComposeResult:
            yield DiffFileTree("files")

    app = TreeApp()
    async with asyncio.timeout(CRASH_BUDGET_SECONDS), app.run_test(size=(80, 10)):
        tree = app.query_one(DiffFileTree)
        tree.set_files([_bracket_file()])
        labels = [child.label.plain for child in tree.root.children]
        assert labels == ["A [id].tsx"]
