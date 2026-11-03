"""Tests for the tongs command palette provider."""

from __future__ import annotations

import inspect
import logging
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from textual.app import App

from tongs.commands import TongsCommandProvider
from tongs.plugins.base import TongsPlugin
from tongs.plugins.registry import PluginRegistry
from tongs.views.inbox import InboxScreen
from tongs.views.mr_detail import MRDetailScreen
from tongs.views.repo_list import RepoListScreen

_plugin_callback = MagicMock(name="plugin_callback")


def _screen(screen_cls: type | None, *, draft: bool) -> MagicMock:
    """Build a mock screen whose class name matches a real tongs screen."""
    name = screen_cls.__name__ if screen_cls is not None else "OtherScreen"
    screen = type(name, (MagicMock,), {})()
    screen._review_draft = object() if draft else None
    return screen


_GLOBAL = [
    ("Repos", "app", call.open_screen("repo_list")),
    ("Inbox", "app", call.open_screen("inbox")),
    ("Clear Cache", "app", call.cache.clear()),
    ("Help", "app", call.action_help()),
    ("Plugin Command", "plugin", call()),
]
_INBOX = [
    ("My Reviews", "screen", call.action_focus_tab("reviews")),
    ("My MRs", "screen", call.action_focus_tab("my-mrs")),
    ("All Open", "screen", call.action_focus_tab("all-open")),
    ("Refresh", "screen", call.action_refresh()),
    ("Open in Browser", "screen", call.action_open_in_browser()),
]
_REPO_LIST = [
    ("Filter Repos", "screen", call.action_start_search()),
    ("Cycle Forge", "screen", call.action_cycle_forge()),
    ("Open Repository...", "screen", call.action_open_repository()),
    ("Remove Repository", "screen", call.action_remove_repository()),
    ("Refresh", "screen", call.action_refresh()),
]
_MR_DETAIL = [
    ("Overview", "screen", call.action_focus_tab("overview")),
    ("Diff", "screen", call.action_focus_tab("diff")),
    ("Commits", "screen", call.action_focus_tab("commits")),
    ("Discussion", "screen", call.action_focus_tab("discussion")),
    ("Pipeline", "screen", call.action_focus_tab("pipeline")),
    ("Comment", "screen", call.action_add_comment()),
    ("Start Review", "screen", call.action_review_draft()),
    ("Next Review Draft", "screen", call.action_next_review_draft()),
    ("Approve", "screen", call.action_approve()),
    ("Unapprove", "screen", call.action_unapprove()),
    ("Merge", "screen", call.action_merge()),
    ("Close", "screen", call.action_close_mr()),
    ("Open in Browser", "screen", call.action_open_in_browser()),
    ("Copy URL", "screen", call.action_yank_url()),
    ("Refresh", "screen", call.action_refresh()),
]
_CASES = (
    [(None, False, *row) for row in _GLOBAL]
    + [(InboxScreen, False, *row) for row in _INBOX]
    + [(RepoListScreen, False, *row) for row in _REPO_LIST]
    + [(MRDetailScreen, False, *row) for row in _MR_DETAIL]
    + [(MRDetailScreen, True, "Submit Review", "screen", call.action_review_draft())]
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("screen_cls", "draft", "display", "target", "expected"),
    _CASES,
    ids=[
        f"{cls.__name__ if cls else 'global'}-{display}"
        for cls, _draft, display, _target, _expected in _CASES
    ],
)
async def test_palette_callback_triggers_its_action(
    screen_cls: type | None,
    draft: bool,
    display: str,
    target: str,
    expected: object,
) -> None:
    """Each palette entry offered on a screen runs exactly its own action."""
    _plugin_callback.reset_mock()
    screen = _screen(screen_cls, draft=draft)
    app = MagicMock()
    app.cache.clear = AsyncMock()
    app.plugin_registry.get_all_commands.return_value = [
        ("Plugin Command", "Contributed by a plugin", _plugin_callback)
    ]
    screen.app = app
    provider = TongsCommandProvider(screen)

    hits = {hit.display: hit.command async for hit in provider.discover()}
    assert display in hits

    observed = {"app": app, "screen": screen, "plugin": _plugin_callback}[target]
    before = len(observed.mock_calls)
    result = hits[display]()
    if inspect.isawaitable(result):
        await result

    triggered = observed.mock_calls[before:]
    if target == "app":
        assert triggered[0] == expected
    else:
        assert triggered == [expected]


class _RaisingPlugin(TongsPlugin):
    """A plugin whose palette callbacks fail, synchronously or when awaited."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return "raising"

    def get_commands(self) -> list[tuple[str, str, object]]:
        def explode() -> None:
            self.calls.append("sync")
            raise RuntimeError("plugin bug")

        async def explode_later() -> None:
            self.calls.append("async")
            raise RuntimeError("plugin bug")

        return [
            ("Explode Now", "Raises synchronously", explode),
            ("Explode Later", "Raises when awaited", explode_later),
        ]


class _PaletteApp(App[None]):
    """The smallest app that runs the real tongs palette provider."""

    COMMANDS: ClassVar[set] = {TongsCommandProvider}

    def __init__(self, plugin_registry: PluginRegistry) -> None:
        super().__init__()
        self.plugin_registry = plugin_registry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "kind"), [("Explode Now", "sync"), ("Explode Later", "async")]
)
async def test_raising_plugin_command_leaves_the_app_running(
    query: str, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A plugin callback that raises is logged and noticed, not fatal."""
    plugin = _RaisingPlugin()
    registry = PluginRegistry()
    registry._plugins.append(plugin)
    app = _PaletteApp(registry)

    with caplog.at_level(logging.WARNING, logger="tongs.plugins.registry"):
        async with app.run_test(notifications=True) as pilot:
            app.action_command_palette()
            await pilot.pause()
            await pilot.press(*query)
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)

            assert plugin.calls == [kind]
            assert app.is_running
            assert app.return_code is None
            assert [
                (notice.message, notice.severity) for notice in app._notifications
            ] == [("The raising plugin command failed. See the log.", "error")]

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [f"Plugin raising command {query!r} failed"]
