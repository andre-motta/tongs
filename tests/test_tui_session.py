"""Integration tests for the Textual shell over the shared application session."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from textual.widgets import DataTable, TabbedContent
from textual.worker import WorkerCancelled

from tongs.app import TongsApp
from tongs.cache.store import CacheStore
from tongs.config import Config
from tongs.errors import NetworkError, NotFoundError
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
from tongs.services.models import (
    HostFailure,
    RepositoryRef,
    ReviewListItem,
    ReviewPage,
    ReviewQuery,
    ReviewRef,
    ReviewRevision,
    ReviewScope,
)
from tongs.services.session import ApplicationSession
from tongs.state.drafts import DraftContent, DraftStore, RecoveryWarning
from tongs.tui_services import TUIRepositoryProgress, TUIServiceAdapter
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
        # Errors raised only for one repository's list, keyed by project path.
        self.repo_errors: dict[str, Exception] = {}
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
        if repo_path in self.repo_errors:
            raise self.repo_errors[repo_path]
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
        assert app.screen.scoped_repository == app.services.repository_snapshot(repo)

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
async def test_personal_tabs_only_contact_discovered_hosts(tmp_path: Path) -> None:
    github = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    gitlab = ForgeHost("gitlab.com", ForgeType.GITLAB, "https://gitlab.com/api/v4")
    github_client = MockForgeClient(
        github,
        [
            make_summary(github),
            make_summary(github, "personal/elsewhere", number=9),
        ],
    )
    gitlab_client = MockForgeClient(
        gitlab, [make_summary(gitlab, "team/tools", number=10)]
    )
    registry = MockForgeRegistry(
        {github.hostname: github_client, gitlab.hostname: gitlab_client}
    )
    app, session, _cache = make_app(tmp_path, [make_repo(tmp_path)], registry)
    queries: list[ReviewQuery] = []
    list_reviews = session.list_reviews

    async def recording_list_reviews(query: ReviewQuery) -> ReviewPage:
        queries.append(query)
        return await list_reviews(query)

    session.list_reviews = recording_list_reviews  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await settle(app)
        assert app.screen.query_one("#reviews-table", MRTable).row_count == 2

        await pilot.press("2")
        await settle(app)
        assert app.screen.query_one("#my-mrs-table", MRTable).row_count == 2

    # The adapter restricts personal queries to discovered hosts; the session
    # tests cover how that restriction limits forge lookups.
    assert [(query.scope, query.hostnames) for query in queries] == [
        (ReviewScope.MY_REVIEWS, ("github.com",)),
        (ReviewScope.MY_MRS, ("github.com",)),
    ]


@pytest.mark.asyncio
async def test_successful_empty_refresh_clears_all_inbox_tabs(
    tmp_path: Path,
) -> None:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    client = MockForgeClient(host, [make_summary(host)])
    registry = MockForgeRegistry({host.hostname: client})
    repo = make_repo(tmp_path)
    discovery_results = iter(([repo], []))

    def discoverer(*args, **kwargs):
        return next(discovery_results)

    app, _session, _cache = make_app(tmp_path, [], registry, discoverer=discoverer)

    async with app.run_test() as pilot:
        await settle(app)
        inbox = cast(InboxScreen, app.screen)
        assert inbox.query_one("#reviews-table", MRTable).row_count == 1
        await pilot.press("2")
        await settle(app)
        assert inbox.query_one("#my-mrs-table", MRTable).row_count == 1
        await pilot.press("3")
        await settle(app)
        assert inbox.query_one("#all-open-table", MRTable).row_count == 1

        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, RepoListScreen)
        app.refresh_repositories()
        await settle(app)
        assert app.repos == []
        assert app.screen.query_one("#repo-table", DataTable).row_count == 0

        await pilot.press("escape")
        await settle(app)
        assert app.screen is inbox
        assert inbox.query_one("#reviews-table", MRTable).row_count == 0
        assert inbox.query_one("#my-mrs-table", MRTable).row_count == 0
        assert inbox.query_one("#all-open-table", MRTable).row_count == 0

        await pilot.press("2")
        await settle(app)
        assert inbox.query_one("#my-mrs-table", MRTable).row_count == 0
        await pilot.press("3")
        await settle(app)
        assert inbox.query_one("#all-open-table", MRTable).row_count == 0


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
async def test_empty_forge_notice_waits_for_discovery(tmp_path: Path) -> None:
    registry = MockForgeRegistry({})
    discovery_started = threading.Event()
    release_discovery = threading.Event()

    def discoverer(*args, **kwargs):
        discovery_started.set()
        assert release_discovery.wait(timeout=3)
        return []

    app, _session, _cache = make_app(tmp_path, [], registry, discoverer=discoverer)

    async with app.run_test(notifications=True):
        assert await asyncio.to_thread(discovery_started.wait, 2)
        await asyncio.sleep(0)
        assert not any(
            "No forges discovered yet" in notification.message
            for notification in app._notifications
        )

        release_discovery.set()
        await settle(app)
        assert app.repository_generation == 1
        assert any(
            "No forges discovered yet" in notification.message
            for notification in app._notifications
        )


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
    first_returning = threading.Event()
    calls = 0

    def discoverer(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            assert release_first.wait(timeout=3)
            first_returning.set()
            return [old_repo]
        return [new_repo]

    app, _session, _cache = make_app(tmp_path, [], registry, discoverer=discoverer)

    async with app.run_test():
        assert await asyncio.to_thread(first_started.wait, 2)
        app.refresh_repositories()
        await settle(app)
        assert app.repos == [new_repo]
        release_first.set()
        assert await asyncio.to_thread(first_returning.wait, 2)
        await settle(app)
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

    async with app.run_test(notifications=True):
        await asyncio.sleep(0)
        messages = [notice.message for notice in app._notifications]

    assert app.startup_error is not None
    assert messages == [app.startup_error.message]
    assert plugins.discover_calls == 0


@pytest.mark.asyncio
async def test_startup_notifies_skipped_recovery_attempt(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "drafts.db"
    setup = DraftStore(db_path)
    await setup.open()
    draft = await setup.create_draft(
        ReviewRef(RepositoryRef("github.com", "acme/widgets"), 3),
        ReviewRevision("head-1", "base-1"),
        DraftContent(body="private draft text"),
    )
    attempt = await setup.lock_submission(draft.id, draft.version)
    await setup.close()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE submission_attempts SET updated_at = 'bad' WHERE id = ?",
            (str(attempt.id),),
        )
    app, session, _cache = make_app(tmp_path, [], MockForgeRegistry({}))

    async with app.run_test():
        await settle(app)
        messages = [notice.message for notice in app._notifications]
        warnings = session.recovery_warnings

    assert app.startup_error is None
    assert warnings == (RecoveryWarning(attempt.id, review=draft.review),)
    # The toast names the review to check on the forge, not only the attempt.
    assert any(
        "Review: github.com/acme/widgets #3." in message and str(attempt.id) in message
        for message in messages
    )
    assert all("private draft text" not in message for message in messages)


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


@pytest.mark.asyncio
async def test_unmount_finishes_workers_before_session_close(tmp_path: Path) -> None:
    app, session, _cache = make_app(tmp_path, [], MockForgeRegistry({}))
    started = asyncio.Event()
    finished = asyncio.Event()
    original_close = session.close

    async def blocked_worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    async def close_after_workers() -> None:
        assert finished.is_set()
        await original_close()

    session.close = close_after_workers  # type: ignore[method-assign]

    async with app.run_test():
        app.run_worker(blocked_worker())
        await asyncio.wait_for(started.wait(), timeout=1)

    assert finished.is_set()


class InstrumentedAllOpenSession:
    """Session double that counts overlapping All Open repository reads."""

    def __init__(
        self,
        tmp_path: Path,
        refs: list[RepositoryRef],
        *,
        max_parallel: int,
        pages: dict[RepositoryRef, ReviewPage] | None = None,
        gates: dict[RepositoryRef, asyncio.Event] | None = None,
    ) -> None:
        self.config = Config(scan_root=str(tmp_path), max_parallel=max_parallel)
        self.refs = refs
        self.local_repositories = tuple(
            make_repo(tmp_path, ref.project_path, hostname=ref.hostname) for ref in refs
        )
        self.pages = pages or {}
        self.queries: list[ReviewQuery] = []
        self.in_flight = 0
        self.peak = 0
        self.saturated = asyncio.Event()
        self.release = asyncio.Event()
        # A repository with its own gate waits for it instead of release.
        self.gates = gates or {}
        self.cancelled: list[RepositoryRef] = []

    async def discover_repositories(self) -> tuple[SimpleNamespace, ...]:
        return tuple(SimpleNamespace(ref=ref) for ref in self.refs)

    async def list_reviews(self, query: ReviewQuery) -> ReviewPage:
        self.queries.append(query)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self.in_flight >= self.config.max_parallel:
                self.saturated.set()
            assert query.repository is not None
            await self.gates.get(query.repository, self.release).wait()
        except asyncio.CancelledError:
            assert query.repository is not None
            self.cancelled.append(query.repository)
            raise
        finally:
            self.in_flight -= 1
        assert query.repository is not None
        return self.pages.get(query.repository, ReviewPage(()))


def make_item(ref: RepositoryRef, number: int, age_hours: int) -> ReviewListItem:
    host = ForgeHost(ref.hostname, ForgeType.GITHUB, "https://api.github.com")
    summary = replace(
        make_summary(host, ref.project_path, number),
        updated_at=NOW - timedelta(hours=age_hours),
    )
    return ReviewListItem(ReviewRef(ref, number), summary)


async def discovered_adapter(
    session: InstrumentedAllOpenSession,
) -> TUIServiceAdapter:
    adapter = TUIServiceAdapter(cast(ApplicationSession, session))
    await adapter.discover_repositories()
    return adapter


@pytest.mark.asyncio
async def test_all_open_reads_repositories_in_parallel_up_to_max_parallel(
    tmp_path: Path,
) -> None:
    # Every repository is on one host, so a per-host loop would read one at a
    # time; per-repository reads overlap up to max_parallel instead.
    refs = [RepositoryRef("github.com", f"acme/repo-{index}") for index in range(5)]
    session = InstrumentedAllOpenSession(tmp_path, refs, max_parallel=2)
    adapter = await discovered_adapter(session)

    task = asyncio.create_task(adapter.list_reviews(ReviewScope.ALL_OPEN, fresh=True))
    await asyncio.wait_for(session.saturated.wait(), timeout=2)
    for _ in range(5):
        await asyncio.sleep(0)
    assert session.in_flight == 2
    assert len(session.queries) == 2

    session.release.set()
    page = await asyncio.wait_for(task, timeout=2)

    assert page == ReviewPage((), ())
    assert session.peak == 2
    assert [query.repository for query in session.queries] == refs
    assert all(query.scope is ReviewScope.ALL_OPEN for query in session.queries)
    assert all(query.hostnames is None for query in session.queries)
    assert all(query.fresh for query in session.queries)


@pytest.mark.asyncio
async def test_all_open_passes_a_cached_read_through_without_fresh(
    tmp_path: Path,
) -> None:
    refs = [RepositoryRef("github.com", "acme/widgets")]
    session = InstrumentedAllOpenSession(tmp_path, refs, max_parallel=4)
    session.release.set()
    adapter = await discovered_adapter(session)

    await adapter.list_reviews(ReviewScope.ALL_OPEN)

    assert [query.fresh for query in session.queries] == [False]


@pytest.mark.asyncio
async def test_all_open_failure_does_not_stop_the_host_and_results_stay_sorted(
    tmp_path: Path,
) -> None:
    broken = RepositoryRef("github.com", "acme/broken")
    older = RepositoryRef("github.com", "acme/older")
    newer = RepositoryRef("gitlab.com", "team/newer")
    middle = RepositoryRef("github.com", "acme/middle")
    failure = HostFailure(
        "github.com",
        ServiceErrorCode.NOT_FOUND,
        "The requested forge resource was not found.",
        retryable=False,
        repository=broken,
    )
    pages = {
        broken: ReviewPage((), (failure,)),
        older: ReviewPage((make_item(older, 1, 30), make_item(older, 2, 10))),
        newer: ReviewPage((make_item(newer, 3, 1),)),
        middle: ReviewPage((make_item(middle, 4, 5),)),
    }
    session = InstrumentedAllOpenSession(
        tmp_path, [broken, older, newer, middle], max_parallel=8, pages=pages
    )
    session.release.set()
    adapter = await discovered_adapter(session)

    page = await adapter.list_reviews(ReviewScope.ALL_OPEN)

    assert [query.repository for query in session.queries] == [
        broken,
        older,
        newer,
        middle,
    ]
    assert [item.ref.number for item in page.items] == [3, 4, 2, 1]
    assert page.failures == (failure,)


@pytest.mark.asyncio
async def test_all_open_tab_lists_other_repositories_and_names_the_failing_one(
    tmp_path: Path,
) -> None:
    host = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
    client = MockForgeClient(
        host,
        [
            make_summary(host, "acme/broken", 1),
            make_summary(host, "acme/gadgets", 2),
            make_summary(host, "acme/widgets", 3),
        ],
    )
    client.repo_errors["acme/broken"] = NotFoundError("private forge detail")
    registry = MockForgeRegistry({host.hostname: client})
    repos = [
        make_repo(tmp_path, "acme/broken"),
        make_repo(tmp_path, "acme/gadgets"),
        make_repo(tmp_path, "acme/widgets"),
    ]
    app, _session, _cache = make_app(tmp_path, repos, registry)

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        await settle(app)
        table = app.screen.query_one("#all-open-table", MRTable)
        assert table.row_count == 2
        assert sorted(path for name, path in client.calls if name == "all_open") == [
            "acme/broken",
            "acme/gadgets",
            "acme/widgets",
        ]
        messages = [
            str(notice.message)
            for notice in app._notifications
            if str(notice.message).startswith("All Open")
        ]
        expected = (
            "All Open skipped: github.com/acme/broken: "
            "The requested forge resource was not found."
        )
        assert messages == [expected]


@pytest.mark.asyncio
async def test_all_open_stream_yields_each_repository_as_it_finishes(
    tmp_path: Path,
) -> None:
    slow = RepositoryRef("github.com", "acme/slow")
    fast = RepositoryRef("github.com", "acme/fast")
    gates = {slow: asyncio.Event(), fast: asyncio.Event()}
    pages = {
        slow: ReviewPage((make_item(slow, 1, 1),)),
        fast: ReviewPage((make_item(fast, 2, 5),)),
    }
    session = InstrumentedAllOpenSession(
        tmp_path, [slow, fast], max_parallel=4, pages=pages, gates=gates
    )
    adapter = await discovered_adapter(session)
    steps = adapter.stream_all_open(fresh=True)

    first = await asyncio.wait_for(anext(steps), timeout=2)
    assert first == TUIRepositoryProgress(None, ReviewPage(()), 0, 2)
    gates[fast].set()
    second = await asyncio.wait_for(anext(steps), timeout=2)
    assert second == TUIRepositoryProgress(fast, pages[fast], 1, 2)
    # The finished repository's reviews are issued before the slow one ends.
    assert adapter.review_ref(pages[fast].items[0].summary) == ReviewRef(fast, 2)
    assert not gates[slow].is_set()

    gates[slow].set()
    third = await asyncio.wait_for(anext(steps), timeout=2)
    assert third == TUIRepositoryProgress(slow, pages[slow], 2, 2)
    with pytest.raises(StopAsyncIteration):
        await anext(steps)
    assert all(query.fresh for query in session.queries)


@pytest.mark.asyncio
async def test_closing_the_all_open_stream_cancels_reads_in_flight(
    tmp_path: Path,
) -> None:
    refs = [RepositoryRef("github.com", f"acme/repo-{index}") for index in range(4)]
    fast = refs[0]
    gates = {ref: asyncio.Event() for ref in refs}
    session = InstrumentedAllOpenSession(tmp_path, refs, max_parallel=2, gates=gates)
    adapter = await discovered_adapter(session)
    steps = adapter.stream_all_open()

    await asyncio.wait_for(anext(steps), timeout=2)
    gates[fast].set()
    step = await asyncio.wait_for(anext(steps), timeout=2)
    assert step.repository == fast

    async def third_read_started() -> None:
        while len(session.queries) < 3:
            await asyncio.sleep(0)

    await asyncio.wait_for(third_read_started(), timeout=2)
    assert session.in_flight == 2

    await steps.aclose()

    # The reads in flight are cancelled and the queued one never starts.
    assert session.in_flight == 0
    assert set(session.cancelled) == set(refs[1:3])
    assert len(session.cancelled) == 2
    assert [query.repository for query in session.queries] == refs[:3]


@pytest.mark.asyncio
async def test_all_open_page_still_waits_for_every_repository(
    tmp_path: Path,
) -> None:
    slow = RepositoryRef("github.com", "acme/slow")
    fast = RepositoryRef("gitlab.com", "team/fast")
    gates = {slow: asyncio.Event(), fast: asyncio.Event()}
    pages = {
        slow: ReviewPage((make_item(slow, 1, 1),)),
        fast: ReviewPage((make_item(fast, 2, 5),)),
    }
    session = InstrumentedAllOpenSession(
        tmp_path, [slow, fast], max_parallel=4, pages=pages, gates=gates
    )
    adapter = await discovered_adapter(session)

    task = asyncio.create_task(adapter.list_reviews(ReviewScope.ALL_OPEN))
    gates[fast].set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done()

    gates[slow].set()
    page = await asyncio.wait_for(task, timeout=2)
    assert [item.ref.number for item in page.items] == [1, 2]


@pytest.mark.asyncio
async def test_all_open_stream_reads_first_pages_without_ci(tmp_path: Path) -> None:
    refs = [RepositoryRef("github.com", f"acme/repo-{index}") for index in range(3)]
    session = InstrumentedAllOpenSession(tmp_path, refs, max_parallel=4)
    session.release.set()
    adapter = await discovered_adapter(session)

    steps = [step async for step in adapter.stream_all_open(fresh=True)]

    assert len(steps) == 4
    assert [query.repository for query in session.queries] == refs
    assert all(query.paged for query in session.queries)
    assert not any(query.with_ci for query in session.queries)
    assert all(query.cursor is None for query in session.queries)
    assert all(query.fresh for query in session.queries)


@pytest.mark.asyncio
async def test_scoped_all_open_stream_reads_only_its_repository(
    tmp_path: Path,
) -> None:
    refs = [RepositoryRef("github.com", f"acme/repo-{index}") for index in range(3)]
    session = InstrumentedAllOpenSession(tmp_path, refs, max_parallel=4)
    session.release.set()
    adapter = await discovered_adapter(session)

    steps = [
        step
        async for step in adapter.stream_all_open(
            repository=adapter.repository_ref(session.local_repositories[1])
        )
    ]

    assert [step.total for step in steps] == [1, 1]
    assert [query.repository for query in session.queries] == [refs[1]]


@pytest.mark.asyncio
async def test_load_more_reads_only_repositories_with_a_cursor(
    tmp_path: Path,
) -> None:
    more = RepositoryRef("github.com", "acme/more")
    done = RepositoryRef("github.com", "acme/done")
    pages = {more: ReviewPage((make_item(more, 3, 9),), (), "3")}
    session = InstrumentedAllOpenSession(
        tmp_path, [more, done], max_parallel=4, pages=pages
    )
    session.release.set()
    adapter = await discovered_adapter(session)

    steps = [step async for step in adapter.stream_all_open_more({more: "2"})]

    assert [(step.repository, step.completed, step.total) for step in steps] == [
        (None, 0, 1),
        (more, 1, 1),
    ]
    assert steps[1].page.next_cursor == "3"
    assert [(query.repository, query.cursor) for query in session.queries] == [
        (more, "2")
    ]
    assert session.queries[0].paged is True
    assert session.queries[0].with_ci is False
    assert session.queries[0].fresh is False
    # The later page's reviews are issued for detail reads and CI lookups.
    assert adapter.review_ref(pages[more].items[0].summary) == ReviewRef(more, 3)


@pytest.mark.asyncio
async def test_ci_lookup_goes_through_the_session_with_the_listed_head(
    tmp_path: Path,
) -> None:
    ref = RepositoryRef("github.com", "acme/widgets")
    item = make_item(ref, 7, 1)
    summary = replace(item.summary, head_commit_sha="sha-7")
    item = ReviewListItem(item.ref, summary)
    session = InstrumentedAllOpenSession(
        tmp_path, [ref], max_parallel=4, pages={ref: ReviewPage((item,))}
    )
    session.release.set()
    lookups: list[tuple[ReviewRef, str, bool]] = []

    async def get_review_ci_status(
        review: ReviewRef, *, head_sha: str = "", fresh: bool = False
    ) -> CIStatus:
        lookups.append((review, head_sha, fresh))
        return CIStatus.FAILED

    session.get_review_ci_status = get_review_ci_status  # type: ignore[attr-defined]
    adapter = await discovered_adapter(session)
    await adapter.list_reviews(ReviewScope.ALL_OPEN)

    assert await adapter.get_ci_status(summary) is CIStatus.FAILED
    assert await adapter.get_ci_status(summary, fresh=True) is CIStatus.FAILED
    assert lookups == [
        (ReviewRef(ref, 7), "sha-7", False),
        (ReviewRef(ref, 7), "sha-7", True),
    ]
