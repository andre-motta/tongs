"""All Open reads the newest page per repository and fills CI per row (#396).

The tab shows each repository's newest page as soon as it lands, without
waiting for CI. Every row starts with a loading marker in the CI column and is
filled in place once its own lookup finishes. Reaching the last row, or the
``m`` key, reads the next page of the repositories that have one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from textual.pilot import Pilot
from textual.widgets import Static

from tests.test_forges.github_ci_batch import (
    answer_ci_batch,
    batch_pulls,
    check_run,
    is_ci_batch,
    pull_node,
)
from tests.test_tui_session import (
    NOW,
    MockForgeClient,
    MockForgeRegistry,
    make_app,
    make_repo,
    make_summary,
    settle,
)
from tongs.app import TongsApp
from tongs.cache.cached_client import CachedForgeClient
from tongs.cache.store import CacheStore
from tongs.errors import NetworkError
from tongs.forges.github import GitHubClient
from tongs.forges.models import CIStatus, ForgeHost, MRPage, MRSummary
from tongs.scanner.repo import ForgeType
from tongs.services.models import RepositoryRef, ReviewListItem, ReviewPage, ReviewRef
from tongs.tui_services import TUIRepositoryProgress
from tongs.widgets.mr_table import MRTable, mr_row_key

HOST = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
LOADING = "…"


class LazyForgeClient(MockForgeClient):
    """Forge double with pages that skip CI and per-review CI lookups.

    Pages hold ``page_size`` reviews, newest first. A lookup whose head commit
    has a gate waits for it, and a cancelled lookup is recorded.
    """

    supports_lazy_ci = True

    def __init__(self, summaries: list[MRSummary], page_size: int = 100) -> None:
        super().__init__(HOST, summaries)
        self.page_size = page_size
        self.page_reads: list[tuple[str, int, bool]] = []
        self.ci_calls: list[tuple[str, int, str, bool]] = []
        self.ci_status: dict[str, CIStatus] = {}
        self.ci_errors: dict[str, Exception] = {}
        self.ci_gates: dict[str, asyncio.Event] = {}
        self.ci_cancelled: list[str] = []

    async def list_mrs_page(
        self,
        repo_path: str,
        state: str = "open",
        page: int = 1,
        per_page: int = 100,
        *,
        with_ci: bool = True,
    ) -> MRPage:
        self.page_reads.append((repo_path, page, with_ci))
        ordered = sorted(
            (item for item in self.summaries if item.repo_path == repo_path),
            key=lambda item: item.updated_at,
            reverse=True,
        )
        start = (page - 1) * self.page_size
        items = ordered[start : start + self.page_size]
        if not with_ci:
            items = [replace(item, ci_status=CIStatus.UNKNOWN) for item in items]
        return MRPage(tuple(items), has_next=len(ordered) > start + self.page_size)

    async def get_mr_ci_status(
        self, repo_path: str, number: int, head_sha: str = ""
    ) -> CIStatus:
        return await self._lookup(repo_path, number, head_sha, fresh=False)

    async def get_mr_ci_status_fresh(
        self, repo_path: str, number: int, head_sha: str = ""
    ) -> CIStatus:
        return await self._lookup(repo_path, number, head_sha, fresh=True)

    async def _lookup(
        self, repo_path: str, number: int, head_sha: str, *, fresh: bool
    ) -> CIStatus:
        self.ci_calls.append((repo_path, number, head_sha, fresh))
        gate = self.ci_gates.get(head_sha)
        if gate is not None:
            try:
                await gate.wait()
            except asyncio.CancelledError:
                self.ci_cancelled.append(head_sha)
                raise
        if head_sha in self.ci_errors:
            raise self.ci_errors[head_sha]
        return self.ci_status.get(head_sha, CIStatus.SUCCESS)


def summary(
    project: str, number: int, age_hours: int, *, head: str = "", title: str = ""
) -> MRSummary:
    return replace(
        make_summary(HOST, project, number),
        title=title or f"Review {project} {number}",
        updated_at=NOW - timedelta(hours=age_hours),
        head_commit_sha=head or f"sha-{number}",
    )


def build(tmp_path: Path, client: MockForgeClient, projects: list[str]) -> TongsApp:
    registry = MockForgeRegistry({HOST.hostname: client})
    repos = [make_repo(tmp_path, project) for project in projects]
    app, _session, _cache = make_app(tmp_path, repos, registry)
    return app


def numbers(table: MRTable) -> list[int]:
    return [table._mr_data[row.key.value].number for row in table.ordered_rows]


def ci_cells(table: MRTable) -> dict[int, str]:
    return {
        mr.number: str(table.get_cell(mr_row_key(mr), "ci"))
        for mr in table._mr_data.values()
    }


def status_text(app: TongsApp) -> str:
    return str(app.screen.query_one("#all-open-status", Static).render())


def notices(app: TongsApp) -> list[str]:
    return [str(notice.message) for notice in app._notifications]


async def wait_until(pilot: Pilot, condition: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await pilot.pause()

    await asyncio.wait_for(poll(), timeout=5)


@pytest.mark.asyncio
async def test_first_paint_reads_one_page_per_repository_before_any_ci_request(
    tmp_path: Path,
) -> None:
    """End to end over the GitHub client: rows show before CI is requested."""
    projects = ["acme/one", "acme/two", "acme/three"]
    table_holder: list[MRTable] = []
    requests: list[tuple[str, int]] = []
    batches: list[list[tuple[str, str, int]]] = []
    release_ci = asyncio.Event()

    def pull(project: str, number: int) -> dict[str, object]:
        return {
            "number": number,
            "title": f"PR {number}",
            "state": "open",
            "draft": False,
            "user": {"login": "alice"},
            "head": {"ref": f"feature-{number}", "sha": f"sha-{number}"},
            "base": {"ref": "main", "sha": "base"},
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": f"2026-09-0{number % 9 + 1}T00:00:00Z",
            "html_url": f"https://github.com/{project}/pull/{number}",
            "labels": [],
        }

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        rows = table_holder[0].row_count if table_holder else 0
        requests.append((path, rows))
        if path == "/search/issues":
            return httpx.Response(200, json={"items": [], "total_count": 0})
        for index, project in enumerate(projects):
            if path == f"/repos/{project}/pulls":
                base = (index + 1) * 10
                return httpx.Response(
                    200, json=[pull(project, base + 1), pull(project, base + 2)]
                )
        if is_ci_batch(request):
            await release_ci.wait()
            batches.append(batch_pulls(request))
            return answer_ci_batch(
                request,
                lambda _owner, _repo, number: pull_node(
                    f"sha-{number}", [check_run("failure")]
                ),
            )
        raise AssertionError(path)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=HOST.api_base
    )
    cache = CacheStore(tmp_path / "cache" / "cache.db")
    client = CachedForgeClient(GitHubClient(HOST, http), cache, hostname=HOST.hostname)
    client.host = HOST  # type: ignore[attr-defined]
    registry = MockForgeRegistry({HOST.hostname: client})  # type: ignore[dict-item]
    repos = [make_repo(tmp_path, project) for project in projects]
    app, _session, _cache = make_app(tmp_path, repos, registry, cache=cache)

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        table = app.screen.query_one("#all-open-table", MRTable)
        table_holder.append(table)
        await pilot.press("3")
        await wait_until(pilot, lambda: table.row_count == 6)
        await wait_until(pilot, lambda: not app.screen.loading_all_open)

        lists = [path for path, _rows in requests if path.endswith("/pulls")]
        assert sorted(lists) == sorted(f"/repos/{p}/pulls" for p in projects)
        assert set(ci_cells(table).values()) == {LOADING}
        assert status_text(app) == "6 open MRs across 3 repositories"

        release_ci.set()
        await wait_until(pilot, lambda: LOADING not in ci_cells(table).values())

        # The six rows' CI goes out batched over GraphQL, sent with rows on
        # screen, and no REST CI read. Lookups that start a few milliseconds
        # apart on a loaded machine may split into a second batch, but each
        # row is looked up exactly once.
        loaded = {11, 12, 21, 22, 31, 32}
        assert 1 <= len(batches) <= 2
        assert sorted(pull for batch in batches for pull in batch) == sorted(
            ("acme", projects[number // 10 - 1].split("/")[1], number)
            for number in loaded
        )
        ci_requests = [(path, rows) for path, rows in requests if path == "/graphql"]
        assert len(ci_requests) == len(batches)
        assert all(rows > 0 for _path, rows in ci_requests)
        assert not any(
            path.endswith(("/check-runs", "/status")) for path, _rows in requests
        )
        assert {mr.ci_status for mr in table._mr_data.values()} == {CIStatus.FAILED}
        assert len(lists) == 3
    await http.aclose()


@pytest.mark.asyncio
async def test_rows_show_loading_ci_then_their_own_status(tmp_path: Path) -> None:
    client = LazyForgeClient(
        [summary("acme/a", 1, 1), summary("acme/a", 2, 2), summary("acme/b", 3, 3)]
    )
    client.ci_status = {"sha-1": CIStatus.RUNNING, "sha-3": CIStatus.FAILED}
    gate = asyncio.Event()
    client.ci_gates = {"sha-1": gate, "sha-2": gate, "sha-3": gate}
    app = build(tmp_path, client, ["acme/a", "acme/b"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: table.row_count == 3)

        assert ci_cells(table) == {1: LOADING, 2: LOADING, 3: LOADING}
        assert sorted(client.page_reads) == [("acme/a", 1, False), ("acme/b", 1, False)]
        # Each lookup of this forge is its own read, so max_parallel (2 here)
        # bounds the lookups in flight; the third waits for a slot.
        await wait_until(pilot, lambda: len(client.ci_calls) == 2)
        await pilot.pause(0.05)
        assert len(client.ci_calls) == 2

        gate.set()
        await wait_until(pilot, lambda: LOADING not in ci_cells(table).values())

        assert ci_cells(table) == {1: "▶", 2: "●", 3: "●"}
        assert {mr.number: mr.ci_status for mr in table._mr_data.values()} == {
            1: CIStatus.RUNNING,
            2: CIStatus.SUCCESS,
            3: CIStatus.FAILED,
        }
        assert sorted(call[:3] for call in client.ci_calls) == [
            ("acme/a", 1, "sha-1"),
            ("acme/a", 2, "sha-2"),
            ("acme/b", 3, "sha-3"),
        ]
        assert not any(call[3] for call in client.ci_calls)


@pytest.mark.asyncio
async def test_ascii_mode_has_its_own_loading_marker(tmp_path: Path) -> None:
    client = LazyForgeClient([summary("acme/a", 1, 1)])
    gate = asyncio.Event()
    client.ci_gates = {"sha-1": gate}
    app = build(tmp_path, client, ["acme/a"])
    app.config.ascii_mode = True

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: table.row_count == 1)
        assert ci_cells(table) == {1: ".."}

        gate.set()
        await wait_until(pilot, lambda: ci_cells(table) == {1: "OK"})


@pytest.mark.asyncio
async def test_a_failed_lookup_shows_unknown_and_others_still_fill(
    tmp_path: Path,
) -> None:
    client = LazyForgeClient([summary("acme/a", 1, 1), summary("acme/a", 2, 2)])
    client.ci_errors = {"sha-1": NetworkError("private detail")}
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: ci_cells(table) == {1: "?", 2: "●"})


@pytest.mark.asyncio
async def test_load_more_reads_only_repositories_with_a_next_page(
    tmp_path: Path,
) -> None:
    client = LazyForgeClient(
        [summary("acme/big", number, number * 2) for number in range(1, 6)]
        + [summary("acme/small", 9, 5)],
        page_size=2,
    )
    app = build(tmp_path, client, ["acme/big", "acme/small"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        await settle(app)
        table = app.screen.query_one("#all-open-table", MRTable)

        assert sorted(client.page_reads) == [
            ("acme/big", 1, False),
            ("acme/small", 1, False),
        ]
        assert numbers(table) == [1, 2, 9]
        assert status_text(app) == (
            "3 open MRs loaded across 2 repositories, more to load (m)"
        )

        client.page_reads.clear()
        await pilot.press("m")
        await settle(app)

        assert client.page_reads == [("acme/big", 2, False)]
        # The next page lands in update order among the rows already shown.
        assert numbers(table) == [1, 2, 9, 3, 4]
        # For a few seconds the status line counts what the load added.
        assert status_text(app) == (
            "5 open MRs loaded across 2 repositories (+2), more to load (m)"
        )
        assert notices(app)[-1] == "Loaded 2 more open MRs; 1 repository has more (m)"

        # Reaching the last row loads the next page as well.
        client.page_reads.clear()
        table.focus()
        table.move_cursor(row=table.row_count - 1)
        await wait_until(pilot, lambda: table.row_count == 6)
        await settle(app)

        assert client.page_reads == [("acme/big", 3, False)]
        assert numbers(table) == [1, 2, 9, 3, 4, 5]
        assert status_text(app) == "6 open MRs across 2 repositories (+1)"
        assert notices(app)[-1] == "All open MRs loaded"
        # The cursor was on the last row, so it moved to the row that arrived.
        assert table.get_selected_mr().number == 5  # type: ignore[union-attr]

        # Nothing has a next page now, so the key reads nothing.
        client.page_reads.clear()
        await pilot.press("m")
        await settle(app)
        assert client.page_reads == []
        await wait_until(pilot, lambda: LOADING not in ci_cells(table).values())
        # CI was looked up once for each loaded row and for nothing else.
        assert sorted(call[1] for call in client.ci_calls) == [1, 2, 3, 4, 5, 9]


@pytest.mark.asyncio
async def test_a_failed_later_page_keeps_its_cursor_for_another_try(
    tmp_path: Path,
) -> None:
    class FlakyClient(LazyForgeClient):
        def __init__(self, summaries: list[MRSummary]) -> None:
            super().__init__(summaries, page_size=2)
            self.fail_next = False

        async def list_mrs_page(self, repo_path: str, *args, **kwargs) -> MRPage:
            page = await super().list_mrs_page(repo_path, *args, **kwargs)
            if self.fail_next:
                self.fail_next = False
                raise NetworkError("down")
            return page

    client = FlakyClient([summary("acme/big", n, n) for n in range(1, 4)])
    app = build(tmp_path, client, ["acme/big"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        await settle(app)
        table = app.screen.query_one("#all-open-table", MRTable)
        assert numbers(table) == [1, 2]

        client.fail_next = True
        await pilot.press("m")
        await settle(app)
        assert numbers(table) == [1, 2]
        assert status_text(app) == (
            "2 open MRs loaded across 1 repository, more to load (m)"
        )
        assert any(
            str(notice.message).startswith("All Open skipped: github.com/acme/big")
            for notice in app._notifications
        )

        await pilot.press("m")
        await settle(app)
        assert [read[1] for read in client.page_reads] == [1, 2, 2]
        assert numbers(table) == [1, 2, 3]
        assert status_text(app) == "3 open MRs across 1 repository (+1)"


@pytest.mark.asyncio
async def test_a_refresh_drops_lookups_of_the_rows_it_replaced(
    tmp_path: Path,
) -> None:
    client = LazyForgeClient([summary("acme/a", 1, 1, head="old-head")])
    client.ci_status = {"old-head": CIStatus.FAILED, "new-head": CIStatus.RUNNING}
    old_gate = asyncio.Event()
    client.ci_gates = {"old-head": old_gate}
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: len(client.ci_calls) == 1)
        assert ci_cells(table) == {1: LOADING}

        # The review got a new head commit; a refresh lists it.
        client.summaries = [summary("acme/a", 1, 0, head="new-head")]
        await pilot.press("ctrl+r")
        await wait_until(pilot, lambda: ci_cells(table) == {1: "▶"})
        assert client.ci_cancelled == ["old-head"]

        # The old lookup finishing late changes nothing.
        old_gate.set()
        await settle(app)
        assert ci_cells(table) == {1: "▶"}
        assert table._mr_data[mr_row_key(summary("acme/a", 1, 0))].ci_status is (
            CIStatus.RUNNING
        )
        # The refresh reads past the CI cache too.
        assert client.ci_calls[-1] == ("acme/a", 1, "new-head", True)


@pytest.mark.asyncio
async def test_leaving_the_tab_stops_lookups_and_returning_resumes_them(
    tmp_path: Path,
) -> None:
    client = LazyForgeClient([summary("acme/a", 1, 1)])
    gate = asyncio.Event()
    client.ci_gates = {"sha-1": gate}
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: len(client.ci_calls) == 1)

        await pilot.press("1")
        await wait_until(pilot, lambda: client.ci_cancelled == ["sha-1"])
        assert ci_cells(table) == {1: LOADING}

        gate.set()
        await pilot.press("3")
        await wait_until(pilot, lambda: ci_cells(table) == {1: "●"})
        # The rows stayed; only the lookup ran again.
        assert client.page_reads == [("acme/a", 1, False)]
        assert len(client.ci_calls) == 2


@pytest.mark.asyncio
async def test_ci_sort_follows_arriving_values_and_keeps_the_cursor(
    tmp_path: Path,
) -> None:
    client = LazyForgeClient(
        [
            summary("acme/a", 1, 1, title="Alpha"),
            summary("acme/a", 2, 2, title="Bravo"),
            summary("acme/a", 3, 3, title="Charlie"),
        ]
    )
    client.ci_status = {"sha-3": CIStatus.FAILED, "sha-1": CIStatus.SUCCESS}
    gates = {head: asyncio.Event() for head in ("sha-1", "sha-2", "sha-3")}
    client.ci_gates = dict(gates)
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: table.row_count == 3)
        await wait_until(pilot, lambda: not app.screen.loading_all_open)
        table.focus()
        await pilot.press("s", "s")
        assert table._sort_key == "ci"
        # Every row still waits for CI, so the title breaks the tie.
        assert numbers(table) == [1, 2, 3]
        await pilot.press("down")
        assert table.get_selected_mr().number == 2  # type: ignore[union-attr]

        # Two lookups run at once here, so the third starts once one ends.
        gates["sha-1"].set()
        await wait_until(pilot, lambda: ci_cells(table)[1] == "●")
        assert numbers(table) == [1, 2, 3]
        gates["sha-3"].set()
        await wait_until(pilot, lambda: ci_cells(table)[3] == "●")
        assert numbers(table) == [3, 1, 2]
        selected = table.get_selected_mr()
        assert selected is not None and selected.number == 2
        assert table.cursor_row == 2

        gates["sha-2"].set()
        await wait_until(pilot, lambda: LOADING not in ci_cells(table).values())
        assert numbers(table) == [3, 1, 2]
        selected = table.get_selected_mr()
        assert selected is not None and selected.number == 2


@pytest.mark.asyncio
async def test_a_scoped_inbox_pages_its_repository_too(tmp_path: Path) -> None:
    client = LazyForgeClient(
        [summary("acme/a", number, number) for number in range(1, 4)]
        + [summary("acme/b", 9, 1)],
        page_size=2,
    )
    app = build(tmp_path, client, ["acme/a", "acme/b"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        from tongs.views.inbox import InboxScreen

        await app.push_screen(
            InboxScreen(app.services.repository_snapshot(app.repos[0]))
        )
        await settle(app)
        await pilot.press("3")
        await settle(app)
        table = app.screen.query_one("#all-open-table", MRTable)

        assert client.page_reads == [("acme/a", 1, False)]
        assert numbers(table) == [1, 2]
        assert status_text(app) == "2 open MRs loaded, more to load (m)"

        await pilot.press("m")
        await settle(app)
        assert numbers(table) == [1, 2, 3]
        assert status_text(app) == "3 open MRs (+1)"


@pytest.mark.asyncio
async def test_a_row_replaced_mid_lookup_gets_its_own_lookup_afterward(
    tmp_path: Path,
) -> None:
    client = LazyForgeClient([summary("acme/a", 1, 1, head="old-head")])
    client.ci_status = {"old-head": CIStatus.FAILED, "new-head": CIStatus.RUNNING}
    old_gate = asyncio.Event()
    client.ci_gates = {"old-head": old_gate}
    app = build(tmp_path, client, ["acme/a"])

    async with app.run_test(notifications=True) as pilot:
        await settle(app)
        await pilot.press("3")
        table = app.screen.query_one("#all-open-table", MRTable)
        await wait_until(pilot, lambda: len(client.ci_calls) == 1)
        await wait_until(pilot, lambda: not app.screen.loading_all_open)
        shown = table._mr_data[mr_row_key(summary("acme/a", 1, 1))]
        ref = RepositoryRef("github.com", "acme/a")
        newer = replace(shown, head_commit_sha="new-head", title="Pushed again")
        step = TUIRepositoryProgress(
            ref, ReviewPage((ReviewListItem(ReviewRef(ref, 1), newer),)), 1, 1
        )

        app.screen._apply_all_open_step(table, step, replace=False, fresh=False)
        for _ in range(5):
            await pilot.pause()
        # The row is not looked up twice at once.
        assert len(client.ci_calls) == 1
        assert ci_cells(table) == {1: LOADING}

        old_gate.set()
        await wait_until(pilot, lambda: ci_cells(table) == {1: "▶"})
        assert [call[2] for call in client.ci_calls] == ["old-head", "new-head"]
        assert table.get_mr(mr_row_key(newer)) == replace(
            newer, ci_status=CIStatus.RUNNING
        )
