"""Tests for the tongs command palette provider."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from tongs.commands import TongsCommandProvider
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
    ("Repos", "app", call.push_screen("repo_list")),
    ("Inbox", "app", call.push_screen("inbox")),
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
