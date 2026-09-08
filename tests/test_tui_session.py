"""Integration tests for the Textual shell over the shared application session."""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from textual.widgets import DataTable, TabbedContent
from textual.worker import WorkerCancelled

from tongs.app import TongsApp
from tongs.cache.store import CacheStore
from tongs.config import Config
from tongs.errors import NetworkError
from tongs.forges.base import ForgeClient
from tongs.forges.models import (
    CIStatus,
    ForgeHost,
    MRState,
    MRSummary,
    User,
)
from tongs.plugins.base import TongsPlugin
from tongs.plugins.context import PluginContext
from tongs.plugins.registry import PluginRegistry
from tongs.scanner.repo import ForgeType, Remote, Repo
from tongs.services.errors import ServiceErrorCode
from tongs.services.models import RepositoryRef, ReviewRef
from tongs.services.session import ApplicationSession
from tongs.views.inbox import InboxScreen
from tongs.views.repo_list import RepoListScreen
from tongs.widgets.mr_table import MRTable

NOW = datetime(2026, 9, 7, tzinfo=UTC)


def make_repo(
    root: Path,
    project: str = "acme/widgets",
    *,
    hostname: str = "github.com",
    forge_type: ForgeType = ForgeType.GITHUB,
) -> Repo:
    remote = Remote(
        "origin",
        f"https://{hostname}/{project}.git",
        hostname,
        project,
        forge_type,
    )
    return Repo(root / project.rsplit("/", 1)[-1], (remote,), remote)


def make_summary(
    host: ForgeHost, project: str = "acme/widgets", number: int = 7
) -> MRSummary:
    return MRSummary(
        forge_host=host,
        repo_path=project,
        local_path="/private/local/path",
        number=number,
        title=f"Review {project}",
        author=User("alice", "Alice"),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.SUCCESS,
        created_at=NOW,
        updated_at=NOW,
        web_url=f"https://{host.hostname}/{project}/pull/{number}",
    )


class MockForgeClient:
    def __init__(self, host: ForgeHost, summaries: list[MRSummary]) -> None:
        self.host = host
        self.summaries = summaries
        self.error: Exception | None = None
        self.calls: list[tuple[str, str | None]] = []
        self.block_personal = False
        self.personal_started = asyncio.Event()

    async def list_my_reviews(self) -> list[MRSummary]:
        self.calls.append(("my_reviews", None))
        self.personal_started.set()
        if self.block_personal:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return self.summaries

    async def list_my_mrs(self) -> list[MRSummary]:
        self.calls.append(("my_mrs", None))
        if self.error is not None:
            raise self.error
        return self.summaries

    async def list_mrs(
        self, repo_path: str, state: str = "open", per_page: int = 100
    ) -> list[MRSummary]:
        self.calls.append(("all_open", repo_path))
        if self.error is not None:
            raise self.error
        return [summary for summary in self.summaries if summary.repo_path == repo_path]


class MockForgeRegistry:
    def __init__(self, clients: dict[str, MockForgeClient]) -> None:
        self.clients = clients
        self.close_calls = 0
        self.closed = False

    def active_hostnames(self) -> list[str]:
        return list(self.clients)

    def get_host(self, hostname: str) -> ForgeHost | None:
        client = self.clients.get(hostname)
        return client.host if client is not None else None

    async def get_client(self, hostname: str) -> ForgeClient:
        return cast(ForgeClient, self.clients[hostname])

    async def close_all(self) -> None:
        self.close_calls += 1
        self.closed = True


class TrackingPlugin(TongsPlugin):
    def __init__(
        self, expected_registry: MockForgeRegistry, expected_cache: CacheStore
    ):
        self.expected_registry = expected_registry
        self.expected_cache = expected_cache
        self.events: list[tuple[str, int]] = []

    @property
    def name(self) -> str:
        return "tracking"

    async def on_app_ready(self, ctx: PluginContext) -> None:
        assert ctx.forge_registry is self.expected_registry
        assert ctx.cache is self.expected_cache
        self.events.append(("ready", len(ctx.repos)))

    async def on_app_shutdown(self, ctx: PluginContext) -> None:
        assert ctx.forge_registry is self.expected_registry
        assert ctx.cache is self.expected_cache
        assert not self.expected_registry.closed
        self.events.append(("shutdown", len(ctx.repos)))


class FixedPluginRegistry(PluginRegistry):
    def __init__(self, plugin: TrackingPlugin | None = None) -> None:
        super().__init__()
        if plugin is not None:
            self._plugins.append(plugin)
        self.discover_calls = 0

    def discover(self, plugin_config: dict[str, dict] | None = None) -> None:
        self.discover_calls += 1


class BrokenCache:
    def __init__(self) -> None:
        self.open_calls = 0
        self.close_calls = 0

    async def open(self) -> None:
        self.open_calls += 1
        raise RuntimeError("private startup detail")

    async def close(self) -> None:
        self.close_calls += 1


def make_app(
    tmp_path: Path,
    repos: list[Repo],
    registry: MockForgeRegistry,
    *,
    discoverer=None,
    plugin_registry: PluginRegistry | None = None,
    cache: CacheStore | BrokenCache | None = None,
) -> tuple[TongsApp, ApplicationSession, CacheStore | BrokenCache]:
    config = Config(scan_root=str(tmp_path), max_parallel=2)
    owned_cache = cache or CacheStore(tmp_path / "cache" / "cache.db")
    session = ApplicationSession(
        config=config,
        cache=owned_cache,
        draft_db_path=tmp_path / "data" / "drafts.db",
        forge_registry=registry,
        discoverer=discoverer or (lambda *args, **kwargs: repos),
        shutdown_timeout=0.5,
    )
    return (
        TongsApp(
            config=config,
            session=session,
            plugin_registry=plugin_registry or FixedPluginRegistry(),
        ),
        session,
        owned_cache,
    )


async def settle(app: TongsApp, count: int = 3) -> None:
    for _ in range(count):
        await asyncio.sleep(0)
        with suppress(WorkerCancelled):
            await app.workers.wait_for_complete()


@pytest.mark.asyncio
async def test_actual_textual_navigation_uses_one_production_session(
    tmp_path: Path,
) -> None:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    client = MockForgeClient(host, [make_summary(host)])
    registry = MockForgeRegistry({host.hostname: client})
    repo = make_repo(tmp_path)
    cache = CacheStore(tmp_path / "cache" / "cache.db")
    plugin = TrackingPlugin(registry, cache)
    plugins = FixedPluginRegistry(plugin)
    app, session, _cache = make_app(
        tmp_path, [repo], registry, plugin_registry=plugins, cache=cache
    )
    legacy_repos = app.repos

    async with app.run_test() as pilot:
        await settle(app)
        assert isinstance(app.screen, InboxScreen)
        assert app.session is session
        assert app.cache is cache
        assert app.forge_registry is registry
        assert app.repos is legacy_repos
        assert app.repos == [repo]
        reviews = app.screen.query_one("#reviews-table", MRTable)
        assert reviews.row_count == 1
        summary = next(iter(reviews._mr_data.values()))
        assert summary.local_path == ""
        assert app.services.review_ref(summary) == ReviewRef(
            RepositoryRef("github.com", "acme/widgets"), 7
        )

        await pilot.press("2")
        await settle(app)
        assert app.screen.query_one("#my-mrs-table", MRTable).row_count == 1
        await pilot.press("3")
        await settle(app)
        assert app.screen.query_one("#all-open-table", MRTable).row_count == 1

        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, RepoListScreen)
        repo_table = app.screen.query_one("#repo-table", DataTable)
        assert repo_table.row_count == 1
        repo_table.focus()
        await pilot.press("enter")
        await settle(app)
        assert isinstance(app.screen, InboxScreen)
        assert app.screen.scoped_repo is repo

        await pilot.press("3")
        await settle(app)
        assert app.screen.query_one(TabbedContent).active == "all-open"
        all_open = app.screen.query_one("#all-open-table", MRTable)
        assert all_open.row_count == 1
        all_open.focus()
        await pilot.press("s")
        assert all_open._sort_key == "title"

    assert plugins.discover_calls == 1
    assert plugin.events == [("ready", 0), ("shutdown", 1)]
    assert registry.close_calls == 1
    assert cache._db is None


@pytest.mark.asyncio
async def test_empty_workspace_does_not_query_configured_forge(tmp_path: Path) -> None:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    client = MockForgeClient(host, [make_summary(host)])
    registry = MockForgeRegistry({host.hostname: client})
    app, _session, _cache = make_app(tmp_path, [], registry)

    async with app.run_test():
        await settle(app)
        assert app.repos == []
        assert app.screen.query_one("#reviews-table", MRTable).row_count == 0
        assert client.calls == []


@pytest.mark.asyncio
async def test_unconfigured_local_repository_stays_visible_but_cannot_open(
    tmp_path: Path,
) -> None:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    registry = MockForgeRegistry({host.hostname: MockForgeClient(host, [])})
    local_repo = make_repo(
        tmp_path,
        "internal/tools",
        hostname="code.example.test",
        forge_type=ForgeType.GITLAB,
    )
    app, _session, _cache = make_app(tmp_path, [local_repo], registry)

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        assert app.repos == [local_repo]
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, RepoListScreen)
        table = app.screen.query_one("#repo-table", DataTable)
        assert table.row_count == 1
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RepoListScreen)


@pytest.mark.asyncio
async def test_partial_inbox_result_is_visible_and_refresh_retries(
    tmp_path: Path,
) -> None:
    github = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    gitlab = ForgeHost("gitlab.com", ForgeType.GITLAB, "https://gitlab.com/api/v4")
    github_client = MockForgeClient(github, [make_summary(github)])
    gitlab_client = MockForgeClient(
        gitlab, [make_summary(gitlab, "team/tools", number=9)]
    )
    gitlab_client.error = NetworkError("private transport detail")
    registry = MockForgeRegistry(
        {github.hostname: github_client, gitlab.hostname: gitlab_client}
    )
    repos = [
        make_repo(tmp_path),
        make_repo(
            tmp_path,
            "team/tools",
            hostname="gitlab.com",
            forge_type=ForgeType.GITLAB,
        ),
    ]
    app, _session, _cache = make_app(tmp_path, repos, registry)

    async with app.run_test():
        await settle(app)
        table = app.screen.query_one("#reviews-table", MRTable)
        assert table.row_count == 1

        gitlab_client.error = None
        cast(InboxScreen, app.screen).action_refresh()
        await settle(app)
        assert table.row_count == 2


@pytest.mark.asyncio
async def test_late_cancelled_discovery_cannot_replace_refresh(
    tmp_path: Path,
) -> None:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    registry = MockForgeRegistry({host.hostname: MockForgeClient(host, [])})
    old_repo = make_repo(tmp_path, "acme/old")
    new_repo = make_repo(tmp_path, "acme/new")
    first_started = threading.Event()
    release_first = threading.Event()
    calls = 0

    def discoverer(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            assert release_first.wait(timeout=3)
            return [old_repo]
        return [new_repo]

    app, _session, _cache = make_app(tmp_path, [], registry, discoverer=discoverer)

    async with app.run_test():
        assert await asyncio.to_thread(first_started.wait, 2)
        app.refresh_repositories()
        await settle(app)
        assert app.repos == [new_repo]
        release_first.set()
        await asyncio.sleep(0.05)
        assert app.repos == [new_repo]


@pytest.mark.asyncio
async def test_startup_failure_is_safe_and_closes_partial_resources(
    tmp_path: Path,
) -> None:
    registry = MockForgeRegistry({})
    cache = BrokenCache()
    plugins = FixedPluginRegistry()
    app, _session, _cache = make_app(
        tmp_path, [], registry, plugin_registry=plugins, cache=cache
    )

    async with app.run_test():
        await asyncio.sleep(0)

    assert app.startup_error is not None
    assert app.startup_error.code is ServiceErrorCode.INTERNAL
    assert "private startup detail" not in str(app.startup_error)
    assert cache.open_calls == 1
    assert cache.close_calls == 1
    assert plugins.discover_calls == 0
    assert registry.close_calls == 0


@pytest.mark.asyncio
async def test_shutdown_cancels_active_inbox_read_before_session_close(
    tmp_path: Path,
) -> None:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    client = MockForgeClient(host, [make_summary(host)])
    client.block_personal = True
    registry = MockForgeRegistry({host.hostname: client})
    app, _session, _cache = make_app(tmp_path, [make_repo(tmp_path)], registry)

    async with app.run_test():
        await asyncio.wait_for(client.personal_started.wait(), timeout=2)

    assert registry.close_calls == 1
