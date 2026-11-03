"""MR inbox dashboard -- the default screen."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import ClassVar

from rich.markup import escape
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.dom import NoScreen
from textual.reactive import reactive
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Static, TabbedContent, TabPane

from tongs.config import app_config
from tongs.forges.models import CIStatus, MRSummary
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import (
    HostFailure,
    RepositoryRef,
    RepositorySnapshot,
    ReviewListItem,
    ReviewScope,
)
from tongs.tui_services import TUIRepositoryProgress
from tongs.widgets.ascii import tongs_header
from tongs.widgets.mr_table import MRTable, mr_row_key

# Each inbox tab and the table that shows it.
_TAB_TABLES = {
    "reviews": "#reviews-table",
    "my-mrs": "#my-mrs-table",
    "all-open": "#all-open-table",
}
_TABLE_TABS = {table.removeprefix("#"): tab_id for tab_id, table in _TAB_TABLES.items()}


class ListsChange(str, Enum):
    """What an MR detail changed in the lists its inbox shows.

    ``REVIEWED`` changed only the viewer's own review state, which the
    personal lists read and All Open does not. ``CLOSED`` merged or closed
    the MR with a known outcome, so it left the open lists.
    ``MAYBE_CLOSED`` merged or closed it with an unknown outcome, so the
    lists re-read because the MR may still be open.
    """

    REVIEWED = "reviewed"
    CLOSED = "closed"
    MAYBE_CLOSED = "maybe_closed"


# CI lookups in flight per tab and host: ``max_parallel``, the host's request
# limit, since each GitLab or GitHub REST lookup is its own read and lookups
# beyond it would only wait for a request slot, past the point where a scroll
# can still drop them. While GitHub batches lookups into GraphQL queries,
# enough for one batch (about 50) may be in flight, since they become one
# request (``TUIServiceAdapter.ci_lookup_limit``). A host's first lookup
# learns its limit before the rest start.

# Scrolling settles for this long before the rows it shows are looked up, and
# rows that arrive within it are looked up together. While the first All Open
# load streams in, rows are looked up once the stream ends or pauses for this
# long, so repositories that re-sort the list do not each look up a window.
_CI_WINDOW_DEBOUNCE_SECONDS = 0.1

# How long the All Open status line shows the rows a load more added.
_LOAD_MORE_STATUS_SECONDS = 5.0


@dataclass
class _CILookups:
    """The per-row CI lookups of one inbox tab.

    Only rows in view, plus about one screen above and below, are looked up:
    ``queue`` holds their row keys in table order and is rebuilt whenever the
    view changes, so rows that scrolled away before their lookup started are
    dropped. Each reload or stop of the tab takes the next generation, and a
    lookup of an older one never lands.
    """

    group: str
    generation: int = 0
    queue: deque[str] = field(default_factory=deque)
    in_flight: set[str] = field(default_factory=set)
    # Rows from an explicit refresh, whose lookup reads past the CI cache.
    fresh: set[str] = field(default_factory=set)
    # Lookup workers running per host.
    workers: dict[str, int] = field(default_factory=dict)
    window_timer: Timer | None = None
    # A first load is streaming in, so view updates wait for it to settle.
    streaming: bool = False
    # Hosts whose rate limit this tab load already reported.
    rate_limited: set[str] = field(default_factory=set)


class InboxScreen(Screen):
    """MR inbox dashboard with tabs for My Reviews / My MRs / All Open.

    When `repository` is provided, the inbox is scoped to that one repository,
    which may be a local checkout or a project opened without cloning it.
    """

    DEFAULT_CSS = """
    InboxScreen #all-open-status {
        height: 1;
        background: $accent 15%;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[list] = [
        Binding("r", "repos_or_back", "Repos", show=True, key_display="r"),
        Binding("1", "focus_tab('reviews')", "Reviews", show=False),
        Binding("2", "focus_tab('my-mrs')", "My MRs", show=False),
        Binding("3", "focus_tab('all-open')", "All Open", show=False),
        Binding("o", "open_in_browser", "Open", show=True),
        Binding("m", "load_more", "More", show=True),
        Binding("ctrl+r", "refresh", "Refresh", show=True),
        Binding("escape", "go_back", "Back", show=False),
        Binding("q", "quit_or_back", "Quit", show=True),
    ]

    loading_reviews: reactive[bool] = reactive(False)
    loading_my_mrs: reactive[bool] = reactive(False)
    loading_all_open: reactive[bool] = reactive(False)

    def __init__(self, repository: RepositorySnapshot | None = None):
        super().__init__()
        self.scoped_repository = repository
        self._loaded_tabs: set[str] = set()
        self._repository_generation = 0
        # Each All Open load takes the next number; a load whose number is no
        # longer current has been replaced or stopped and must not touch the tab.
        self._all_open_load = 0
        self._all_open_pending = False
        # The next page of each All Open repository that has more to show.
        self._all_open_cursors: dict[RepositoryRef, str] = {}
        self._all_open_total = 0
        # Rows of every tab arrive without CI and are filled in per row.
        self._ci = {tab_id: _CILookups(f"{tab_id}-ci") for tab_id in _TAB_TABLES}
        # Whether the work in flight in the all-open group is a load more.
        self._all_open_more = False
        # The All Open status text before any rate-limit notice, the rows the
        # last load more added (shown for a few seconds), and its timer.
        self._all_open_status_text = ""
        self._all_open_added = 0
        self._all_open_added_timer: Timer | None = None
        # The row each table last highlighted, to tell a user's cursor move
        # from a reorder that keeps the cursor on its row.
        self._highlighted: dict[str, str | None] = {}
        # How many CI lookups of each host may be in flight, as last learned.
        self._ci_limits: dict[str, int] = {}
        # One timer per rate-limited host, resuming its lookups at the reset.
        self._rate_limit_timers: dict[str, Timer] = {}

    def compose(self) -> ComposeResult:
        yield tongs_header(self.app)
        with TabbedContent(initial="reviews"):
            with TabPane("My Reviews", id="reviews"):
                yield MRTable(id="reviews-table")
            with TabPane("My MRs", id="my-mrs"):
                yield MRTable(id="my-mrs-table")
            with TabPane("All Open", id="all-open"):
                yield Static("", id="all-open-status")
                yield MRTable(id="all-open-table")
        yield Footer()

    def on_mount(self) -> None:
        self._repository_generation = self.app.repository_generation
        show_repo = self.scoped_repository is None
        for table in self.query(MRTable):
            table.setup_columns(show_repo=show_repo)
            table.cursor_type = "row"
            table.zebra_stripes = True
        if self.scoped_repository:
            self.sub_title = self.scoped_repository.display_name

    def on_screen_resume(self) -> None:
        generation = self.app.repository_generation
        if generation != self._repository_generation:
            self.on_repositories_refreshed(generation)

    def on_repositories_refreshed(self, generation: int) -> None:
        """Reconcile inbox tabs with one successful repository refresh."""
        self._repository_generation = generation
        self._stop_all_open()
        for tab_id in _TAB_TABLES:
            self._stop_ci_lookups(tab_id)
        self._all_open_cursors.clear()
        self._loaded_tabs.clear()
        # All Open loads again when it is next shown, so nothing is left to
        # load more from and the status line must not offer it.
        self._all_open_added = 0
        with suppress(NoMatches):
            self._set_all_open_status("")
        scoped_repo_removed = (
            self.scoped_repository is not None
            and not self.app.services.has_repository(self.scoped_repository.ref)
        )
        # An opened project needs no local repositories to keep its rows.
        unscoped_empty = self.scoped_repository is None and not self.app.repos
        if unscoped_empty or scoped_repo_removed:
            for table in self.query(MRTable):
                table.clear()
        if unscoped_empty:
            self.notify("[dim]No forges discovered yet[/]")
        # Stay on the tab the user is reading; each one reloads when it is
        # next shown, or right away when it is the active one.
        self.action_focus_tab(self.query_one(TabbedContent).active)

    def invalidate(self, mr: MRSummary, change: ListsChange) -> None:
        """Refresh the loaded lists after the MR this screen came from changed.

        My Reviews and My MRs always read from the forge, so they re-read:
        the tab on screen now, the others when each is next shown. All Open
        pages through load more, so a merged or closed MR leaves by dropping
        its row in place, keeping the pages and the cursor where the user
        left them; when even the outcome is unknown the tab re-reads past
        the list cache instead, because the row may not have left the open
        list and only the forge knows. An approve, unapprove, or submitted
        review changes only the viewer's own review state, which the open
        list does not show.
        """
        if change is ListsChange.CLOSED:
            self._remove_all_open_row(mr)
        elif change is ListsChange.MAYBE_CLOSED:
            # A read still running would leave its bookkeeping half done
            # while the re-read replaces the rows, so it stops first, the
            # same way the closed branch stops one.
            self._stop_all_open()
            self._loaded_tabs.discard("all-open")
        self._loaded_tabs.discard("reviews")
        self._loaded_tabs.discard("my-mrs")
        active = self.query_one(TabbedContent).active
        if active in _TAB_TABLES and active not in self._loaded_tabs:
            self._loaded_tabs.add(active)
            # An unknown merge or close outcome is exactly what the list
            # cache cannot answer, so that re-read goes past it.
            self._load_tab(active, fresh=change is ListsChange.MAYBE_CLOSED)

    def _remove_all_open_row(self, mr: MRSummary) -> None:
        """Drop a merged or closed MR's row from All Open in place.

        The pages load more added and the cursor stay where they are. A read
        still running is stopped first, or its bookkeeping would leave the
        "load more" offer stuck half done.
        """
        self._stop_all_open()
        table = self.query_one("#all-open-table", MRTable)
        if not table.remove_mr(mr):
            return
        # Moving the cursor to a nearby row is this removal's doing, not the
        # user's, so it must not read as a request to load more.
        self._highlighted["all-open-table"] = table.selected_row_key()
        self._set_all_open_status(self._all_open_summary(table))

    def action_refresh(self) -> None:
        """Reload the current tab from the forge, past the list cache."""
        tab_id = self.query_one(TabbedContent).active
        self._loaded_tabs.add(tab_id)
        self._load_tab(tab_id, fresh=True)

    def action_go_back(self) -> None:
        if self.scoped_repository:
            self.app.pop_screen()

    def action_quit_or_back(self) -> None:
        if self.scoped_repository:
            self.app.pop_screen()
        else:
            self.app.exit()

    def action_repos_or_back(self) -> None:
        if self.scoped_repository:
            self.app.pop_screen()
        else:
            self.app.open_screen("repo_list")

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        tab_id = event.pane.id
        if self.query_one(TabbedContent).active != "all-open":
            # Leaving All Open mid-load stops it; coming back loads it again.
            # CI lookups stop too and resume for the rows still waiting. The
            # Reviews and My MRs lookups finish in the background, like the
            # list reads they follow.
            self._stop_all_open()
            self._stop_ci_lookups("all-open")
        if tab_id and tab_id not in self._loaded_tabs:
            self._loaded_tabs.add(tab_id)
            self._load_tab(tab_id)
        elif tab_id in _TAB_TABLES:
            # Rows of this tab that came into view get their lookups once the
            # tab is laid out.
            self.call_after_refresh(self._update_ci_window, tab_id)
        self.refresh_bindings()

    def action_focus_tab(self, tab_id: str) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = tab_id
        if tab_id in self._loaded_tabs:
            return
        self._loaded_tabs.add(tab_id)
        self._load_tab(tab_id)

    def on_unmount(self) -> None:
        for tab_id in _TAB_TABLES:
            self._stop_ci_lookups(tab_id)
        for timer in self._rate_limit_timers.values():
            timer.stop()
        self._rate_limit_timers.clear()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "load_more":
            return self._can_load_more()
        return True

    def action_load_more(self) -> None:
        """Read the next page of every All Open repository that has one."""
        if not self._can_load_more():
            return
        self._all_open_pending = True
        self._all_open_more = True
        self.loading_all_open = True
        self.refresh_bindings()
        self.load_more_all_open(self._all_open_load)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Load more once the user moves the cursor to the last All Open row.

        A CI sort reorder or a merge keeps the cursor on its row, which
        highlights the same row again; that is not a move and loads nothing.
        """
        table = event.data_table
        key = event.row_key.value if event.row_key is not None else None
        table_id = table.id or ""
        previous = self._highlighted.get(table_id)
        self._highlighted[table_id] = key
        if table_id != "all-open-table" or table.row_count == 0 or key == previous:
            return
        if table.cursor_row >= table.row_count - 1:
            self.action_load_more()

    @on(MRTable.ViewportChanged)
    def _viewport_changed(self, message: MRTable.ViewportChanged) -> None:
        """Look up the rows a scroll or resize brought into view, debounced."""
        tab_id = _TABLE_TABS.get(message.table.id or "")
        if tab_id is not None:
            self._schedule_ci_window(tab_id, restart=True)

    def _schedule_ci_window(self, tab_id: str, *, restart: bool) -> None:
        """Update a tab's CI lookups shortly, once for a burst of changes.

        ``restart`` waits for scrolling to settle; otherwise changes that
        arrive while an update is scheduled join it, so repositories that
        land together are looked up as one view rather than one each. While
        a first load streams in, every change waits for the stream to pause.
        """
        lookups = self._ci[tab_id]
        if lookups.window_timer is not None:
            if not restart and not lookups.streaming:
                return
            lookups.window_timer.stop()

        def update() -> None:
            lookups.window_timer = None
            self._update_ci_window(tab_id)

        lookups.window_timer = self.set_timer(_CI_WINDOW_DEBOUNCE_SECONDS, update)

    def _can_load_more(self) -> bool:
        try:
            active = self.query_one(TabbedContent).active
        except NoMatches:
            return False
        return (
            active == "all-open"
            and "all-open" in self._loaded_tabs
            and not self._all_open_pending
            and bool(self._all_open_cursors)
        )

    def _load_tab(self, tab_id: str, *, fresh: bool = False) -> None:
        if tab_id == "reviews":
            self.load_reviews(fresh=fresh)
        elif tab_id == "my-mrs":
            self.load_my_mrs(fresh=fresh)
        elif tab_id == "all-open":
            self._all_open_load += 1
            self._all_open_pending = True
            self.load_all_open(fresh=fresh, load=self._all_open_load)

    def action_open_in_browser(self) -> None:
        table = self._active_table()
        if table:
            mr = table.get_selected_mr()
            if mr:
                self.app.open_url(mr.web_url)
            else:
                self.notify("No MR selected")

    def action_select_mr(self) -> None:
        table = self._active_table()
        if table:
            mr = table.get_selected_mr()
            if mr:
                from tongs.views.mr_detail import MRDetailScreen

                self.app.push_screen(MRDetailScreen(mr))

    @work(exclusive=True, group="reviews")
    async def load_reviews(self, fresh: bool = False) -> None:
        self._ci["reviews"].rate_limited.clear()
        self.loading_reviews = True
        table = self.query_one("#reviews-table", MRTable)
        table.loading = True
        try:
            if not self.scoped_repository and not self.app.repos:
                table.clear()
                return
            await self._load_page(
                table, "reviews", ReviewScope.MY_REVIEWS, "Reviews", fresh=fresh
            )
        finally:
            self.loading_reviews = False
            with suppress(NoScreen):
                table.loading = False

    @work(exclusive=True, group="my-mrs")
    async def load_my_mrs(self, fresh: bool = False) -> None:
        self._ci["my-mrs"].rate_limited.clear()
        self.loading_my_mrs = True
        table = self.query_one("#my-mrs-table", MRTable)
        table.loading = True
        try:
            if not self.scoped_repository and not self.app.repos:
                table.clear()
                return
            await self._load_page(
                table, "my-mrs", ReviewScope.MY_MRS, "My MRs", fresh=fresh
            )
        finally:
            self.loading_my_mrs = False
            with suppress(NoScreen):
                table.loading = False

    @work(exclusive=True, group="all-open")
    async def load_all_open(self, fresh: bool = False, load: int | None = None) -> None:
        if load is None:
            self._all_open_load += 1
            load = self._all_open_load
            self._all_open_pending = True
        elif load != self._all_open_load:
            return
        self.loading_all_open = True
        table = self.query_one("#all-open-table", MRTable)
        table.loading = True
        self._all_open_added = 0
        self._set_all_open_status("")
        # A new load replaces the rows, so lookups for the old ones stop.
        self._stop_ci_lookups("all-open")
        self._ci["all-open"].rate_limited.clear()
        self._all_open_cursors.clear()
        try:
            if not self.scoped_repository and not self.app.repos:
                table.clear()
                return
            await self._stream_all_open(table, load, fresh=fresh)
        finally:
            if load == self._all_open_load:
                self._all_open_pending = False
                self.loading_all_open = False
                with suppress(NoScreen):
                    table.loading = False
                    self.refresh_bindings()

    @work(exclusive=True, group="all-open")
    async def load_more_all_open(self, load: int) -> None:
        """Read the next page of the repositories that have one."""
        if load != self._all_open_load:
            return
        table = self.query_one("#all-open-table", MRTable)
        cursors = dict(self._all_open_cursors)
        items: list[ReviewListItem] = []
        failures: list[HostFailure] = []
        shown = set(table.row_keys())
        selected = table.selected_row_key()
        on_last_row = table.row_count > 0 and table.cursor_row >= table.row_count - 1
        self._all_open_added = 0
        self._set_all_open_status(_loading_more(len(cursors)))
        completed = False
        try:
            async with aclosing(
                self.app.services.stream_all_open_more(cursors)
            ) as steps:
                async for step in steps:
                    if load != self._all_open_load:
                        return
                    self._apply_all_open_step(table, step, replace=False, fresh=False)
                    items.extend(step.page.items)
                    failures.extend(step.page.failures)
                    if step.completed < step.total:
                        self._set_all_open_status(
                            _loading_more(step.total - step.completed)
                        )
            completed = True
        except ServiceError as error:
            if load == self._all_open_load:
                self.notify(
                    f"All Open: {error.message}", severity="warning", markup=False
                )
        finally:
            if load == self._all_open_load:
                self._all_open_pending = False
                self._all_open_more = False
                self.loading_all_open = False
                with suppress(NoScreen):
                    self._set_all_open_status(self._all_open_summary(table))
                    self.refresh_bindings()
        if load != self._all_open_load:
            return
        added = [key for key in table.row_keys() if key not in shown]
        if completed:
            self._report_load_more(table, added)
            if on_last_row and added and table.selected_row_key() == selected:
                # The user was at the end of the list, so take them to the
                # first row that just arrived.
                self._move_cursor(table, added[0])
        self._notify_failures(
            "all-open",
            "All Open",
            items,
            self.app.services.deduplicate_failures(failures),
        )

    async def _stream_all_open(self, table: MRTable, load: int, *, fresh: bool) -> None:
        """Show each repository's rows, then look up CI for the rows in view.

        Repositories land in any order and each re-sorts the list, so CI
        lookups wait until the stream ends or pauses for longer than the
        debounce; then the rows near the view are looked up once and the
        lookups follow scrolling. Rows of an earlier load still waiting for
        CI, kept after a failed read, get their lookups the same way.
        """
        lookups = self._ci["all-open"]
        lookups.streaming = True
        try:
            await self._read_all_open(table, load, fresh=fresh)
        finally:
            if load == self._all_open_load:
                lookups.streaming = False
        if load == self._all_open_load:
            # The last rows may still scroll the view to keep the cursor on
            # its row, so the lookup waits for that to settle too.
            self._schedule_ci_window("all-open", restart=True)

    async def _read_all_open(self, table: MRTable, load: int, *, fresh: bool) -> None:
        """Show each repository's rows as soon as that repository finishes."""
        items: list[ReviewListItem] = []
        failures: list[HostFailure] = []
        total = 0
        stream = (
            self.app.services.stream_all_open(
                fresh=fresh, repository=self.scoped_repository.ref
            )
            if self.scoped_repository is not None
            else self.app.services.stream_all_open(fresh=fresh)
        )
        try:
            async with aclosing(stream) as steps:
                async for step in steps:
                    if load != self._all_open_load:
                        return
                    total = step.total
                    self._all_open_total = total
                    # The previous rows stay until the first new ones arrive.
                    self._apply_all_open_step(
                        table, step, replace=not items, fresh=fresh
                    )
                    if step.page.items:
                        table.loading = False
                        items.extend(step.page.items)
                    failures.extend(step.page.failures)
                    if step.completed < step.total:
                        self._set_all_open_status(
                            f"Loading {step.completed} of {step.total} "
                            f"{_repositories(step.total)}"
                        )
        except ServiceError as error:
            if load == self._all_open_load:
                # Rows that already arrived replaced the earlier list, so say
                # the list is incomplete rather than clearing the status.
                shown = len(self._shown(items))
                self._set_all_open_status(
                    f"List incomplete: {_open_count(shown)} loaded" if shown else ""
                )
                self.notify(
                    f"All Open: {error.message}", severity="warning", markup=False
                )
                self._notify_failures(
                    "all-open",
                    "All Open",
                    items,
                    self.app.services.deduplicate_failures(failures),
                )
            return
        if load != self._all_open_load:
            return
        if not items and not failures:
            table.clear()
        if not items and failures:
            failed = len(
                {failure.repository or failure.hostname for failure in failures}
            )
            unloaded = f"{failed} of {total} {_repositories(total)} could not be loaded"
            # Nothing new arrived, so any rows on screen are the earlier load's.
            self._set_all_open_status(
                f"Showing earlier results: {unloaded}"
                if table.row_count
                else f"{unloaded[0].upper()}{unloaded[1:]}"
            )
        else:
            self._set_all_open_status(self._all_open_summary(table))
        self._notify_failures(
            "all-open",
            "All Open",
            items,
            self.app.services.deduplicate_failures(failures),
        )

    def _apply_all_open_step(
        self,
        table: MRTable,
        step: TUIRepositoryProgress,
        *,
        replace: bool,
        fresh: bool,
    ) -> None:
        """Show one repository page, keep its cursor, and queue its CI lookups.

        ``fresh`` marks rows from an explicit refresh, whose lookups read past
        the CI cache.
        """
        if step.repository is not None:
            if step.page.next_cursor is not None:
                self._all_open_cursors[step.repository] = step.page.next_cursor
            else:
                self._all_open_cursors.pop(step.repository, None)
        if not step.page.items:
            return
        summaries = self._shown([item.summary for item in step.page.items])
        table.merge_mrs(
            summaries, self.app.config.ascii_mode, replace=replace, ci_pending=True
        )
        self._want_ci("all-open", summaries, fresh=fresh)

    def _shown(self, summaries: list[MRSummary]) -> list[MRSummary]:
        """Drop draft MRs when ``[ui] show_draft_mrs`` is false."""
        if app_config(self.app).show_draft_mrs:
            return summaries
        return [summary for summary in summaries if not summary.is_draft]

    def _all_open_summary(self, table: MRTable) -> str:
        """Count the loaded rows and say whether more can be loaded.

        For a few seconds after a load more, the rows it added are counted
        too, as "(+616)".
        """
        count = table.row_count
        total = self._all_open_total
        loaded = " loaded" if self._all_open_cursors else ""
        text = (
            f"{_open_count(count)}{loaded}"
            if self.scoped_repository is not None
            else f"{_open_count(count)}{loaded} across {total} {_repositories(total)}"
        )
        if self._all_open_added:
            text += f" (+{self._all_open_added})"
        if self._all_open_cursors:
            text += ", more to load (m)"
        return text

    def _report_load_more(self, table: MRTable, added: list[str]) -> None:
        """Say what a finished load more added and whether more is left."""
        left = len(self._all_open_cursors)
        if left and not added:
            # Nothing arrived; any failure was reported on its own.
            return
        if left:
            has = "has" if left == 1 else "have"
            self.notify(
                f"Loaded {len(added)} more open MR{'' if len(added) == 1 else 's'}; "
                f"{left} {_repositories(left)} {has} more (m)",
                markup=False,
            )
        else:
            self.notify("All open MRs loaded", markup=False)
        if not added:
            return
        self._all_open_added = len(added)
        self._set_all_open_status(self._all_open_summary(table))
        if self._all_open_added_timer is not None:
            self._all_open_added_timer.stop()
        load = self._all_open_load

        def forget_added() -> None:
            self._all_open_added_timer = None
            if load != self._all_open_load or self._all_open_pending:
                return
            self._all_open_added = 0
            self._set_all_open_status(self._all_open_summary(table))

        self._all_open_added_timer = self.set_timer(
            _LOAD_MORE_STATUS_SECONDS, forget_added
        )

    def _move_cursor(self, table: MRTable, key: str) -> None:
        """Move the cursor to a row without it counting as the user's move."""
        self._highlighted[table.id or ""] = key
        table.move_cursor(row=table.get_row_index(key))

    def _set_all_open_status(self, text: str) -> None:
        """Show ``text`` on the All Open status line, with any rate limit."""
        self._all_open_status_text = text
        self._render_all_open_status()

    def _render_all_open_status(self) -> None:
        parts = [self._all_open_status_text] if self._all_open_status_text else []
        parts.extend(
            self._rate_limit_text(hostname)
            for hostname in sorted(self._ci["all-open"].rate_limited)
            if self.app.services.rate_limit(hostname) is not None
        )
        text = ". ".join(parts)
        with suppress(NoMatches):
            self.query_one("#all-open-status", Static).update(
                f"[dim]{escape(text)}[/]" if text else ""
            )

    def _want_ci(
        self, tab_id: str, summaries: Iterable[MRSummary], *, fresh: bool = False
    ) -> None:
        """Look up CI for these new rows once they are in view.

        ``fresh`` marks rows from an explicit refresh, whose lookups read past
        the CI cache.
        """
        if fresh:
            self._ci[tab_id].fresh.update(mr_row_key(summary) for summary in summaries)
        self._schedule_ci_window(tab_id, restart=False)

    def _update_ci_window(self, tab_id: str) -> None:
        """Queue lookups for the rows near the view that still wait for CI.

        Rows in view, plus about one screen above and below, are looked up
        in table order; queued rows that left that window are dropped before
        their lookup starts, and rows of a host that is rate limited wait for
        its reset. A tab that is not shown keeps its queue.
        """
        try:
            if self.query_one(TabbedContent).active != tab_id:
                return
            table = self.query_one(_TAB_TABLES[tab_id], MRTable)
        except (NoMatches, NoScreen):
            return
        keys = table.keys_near_viewport()
        if keys is None:
            return
        lookups = self._ci[tab_id]
        if lookups.window_timer is not None:
            # This update covers any that was scheduled.
            lookups.window_timer.stop()
            lookups.window_timer = None
        queue: deque[str] = deque()
        for key in keys:
            summary = table.get_mr(key)
            if (
                summary is None
                or key in lookups.in_flight
                or not table.is_ci_pending(summary)
            ):
                continue
            hostname = summary.forge_host.hostname
            if self.app.services.rate_limit(hostname) is not None:
                self._on_rate_limited(tab_id, hostname)
                continue
            queue.append(key)
        lookups.queue = queue
        self._start_ci_workers(tab_id, table)

    def _start_ci_workers(self, tab_id: str, table: MRTable) -> None:
        """Run lookup workers for the queued rows, per host up to its limit.

        A host whose limit is not known yet gets one worker, which learns it
        and starts the rest.
        """
        lookups = self._ci[tab_id]
        queued: dict[str, int] = {}
        for key in lookups.queue:
            summary = table.get_mr(key)
            if summary is not None:
                hostname = summary.forge_host.hostname
                queued[hostname] = queued.get(hostname, 0) + 1
        for hostname, count in queued.items():
            wanted = min(self._ci_limits.get(hostname, 1), count)
            while lookups.workers.get(hostname, 0) < wanted:
                lookups.workers[hostname] = lookups.workers.get(hostname, 0) + 1
                self.run_worker(
                    self._run_ci_lookups(tab_id, table, lookups.generation, hostname),
                    group=lookups.group,
                    exit_on_error=False,
                )

    async def _run_ci_lookups(
        self, tab_id: str, table: MRTable, generation: int, hostname: str
    ) -> None:
        """Look up the queued rows of one host, in table order."""
        lookups = self._ci[tab_id]
        try:
            while generation == lookups.generation:
                limit = await self.app.services.ci_lookup_limit(hostname)
                if generation != lookups.generation:
                    return
                if limit != self._ci_limits.get(hostname):
                    self._ci_limits[hostname] = limit
                    # More (or fewer) lookups of this host may run at once.
                    self._start_ci_workers(tab_id, table)
                if lookups.workers.get(hostname, 0) > limit:
                    break
                key = _pop_queued(lookups.queue, table, hostname)
                if key is None:
                    break
                summary = table.get_mr(key)
                if (
                    summary is None
                    or key in lookups.in_flight
                    or not table.is_ci_pending(summary)
                ):
                    continue
                if self.app.services.rate_limit(hostname) is not None:
                    self._on_rate_limited(tab_id, hostname)
                    continue
                # A row is never looked up twice at once.
                lookups.in_flight.add(key)
                try:
                    status = await self.app.services.get_ci_status(
                        summary, fresh=key in lookups.fresh
                    )
                except ServiceError as error:
                    if generation != lookups.generation:
                        return
                    lookups.in_flight.discard(key)
                    if error.code is ServiceErrorCode.RATE_LIMITED:
                        # The row keeps its loading marker until the reset.
                        self._on_rate_limited(tab_id, hostname)
                        continue
                    status = CIStatus.UNKNOWN
                if generation != lookups.generation:
                    return
                lookups.in_flight.discard(key)
                if table.set_ci_status(summary, status, self.app.config.ascii_mode):
                    lookups.fresh.discard(key)
                    if table.sort_key == "ci":
                        # The row moved up into the known rows, so rows that
                        # still wait may have shifted into view without a
                        # scroll. Look at the view again once landings settle.
                        self._schedule_ci_window(tab_id, restart=False)
                # A newer summary that replaced the row meanwhile is looked
                # up next.
                current = table.get_mr(key)
                if current is not None and table.is_ci_pending(current):
                    lookups.queue.append(key)
        finally:
            if generation == lookups.generation:
                left = lookups.workers.get(hostname, 0) - 1
                if left > 0:
                    lookups.workers[hostname] = left
                else:
                    lookups.workers.pop(hostname, None)

    def _on_rate_limited(self, tab_id: str, hostname: str) -> None:
        """Hold a host's lookups until its reset and say so once per load."""
        lookups = self._ci[tab_id]
        lookups.queue = deque(
            key
            for key in lookups.queue
            if (summary := _summary_for(self, tab_id, key)) is None
            or summary.forge_host.hostname != hostname
        )
        if hostname not in lookups.rate_limited:
            lookups.rate_limited.add(hostname)
            self.notify(
                self._rate_limit_text(hostname), severity="warning", markup=False
            )
            if tab_id == "all-open":
                self._render_all_open_status()
        self._resume_after_rate_limit(hostname)

    def _resume_after_rate_limit(self, hostname: str) -> None:
        """Look up the waiting rows of a host again once its limit resets."""
        limit = self.app.services.rate_limit(hostname)
        if limit is None or hostname in self._rate_limit_timers:
            return
        delay = max(0.0, (limit.until - datetime.now(UTC)).total_seconds()) + 1

        def resume() -> None:
            self._rate_limit_timers.pop(hostname, None)
            if self.app.services.rate_limit(hostname) is not None:
                self._resume_after_rate_limit(hostname)
                return
            self._render_all_open_status()
            for tab_id in _TAB_TABLES:
                self._update_ci_window(tab_id)

        self._rate_limit_timers[hostname] = self.set_timer(delay, resume)

    def _rate_limit_text(self, hostname: str) -> str:
        """Say that a host is rate limited and when it resets."""
        name = self.app.services.forge_name(hostname)
        limit = self.app.services.rate_limit(hostname)
        if limit is not None and limit.resets_at is not None:
            return (
                f"{name} rate limit reached, resets at "
                f"{limit.resets_at.astimezone():%H:%M}"
            )
        return f"{name} rate limit reached, try again in a few minutes"

    def _stop_ci_lookups(self, tab_id: str) -> None:
        """Drop a tab's queued CI lookups and make any in flight land nowhere."""
        lookups = self._ci[tab_id]
        lookups.generation += 1
        lookups.queue = deque()
        lookups.in_flight = set()
        lookups.workers = {}
        if lookups.window_timer is not None:
            lookups.window_timer.stop()
            lookups.window_timer = None
        self.workers.cancel_group(self, lookups.group)

    def _stop_all_open(self) -> None:
        """Cancel an All Open read in flight.

        A first load stops and the tab loads again on return. A load more only
        stops: the rows and the pages already shown stay, and ``m`` reads the
        pages that did not arrive.
        """
        if not self._all_open_pending:
            return
        self._all_open_load += 1
        self._all_open_pending = False
        self._ci["all-open"].streaming = False
        self.workers.cancel_group(self, "all-open")
        self.loading_all_open = False
        if self._all_open_more:
            self._all_open_more = False
            with suppress(NoScreen, NoMatches):
                table = self.query_one("#all-open-table", MRTable)
                self._set_all_open_status(self._all_open_summary(table))
            return
        self._loaded_tabs.discard("all-open")
        with suppress(NoScreen):
            self.query_one("#all-open-table", MRTable).loading = False
            self._set_all_open_status("")

    async def _load_page(
        self,
        table: MRTable,
        tab_id: str,
        scope: ReviewScope,
        label: str,
        *,
        fresh: bool = False,
    ) -> None:
        """Show one Reviews or My MRs read, then fill in its CI per row.

        The list arrives without CI, so each row shows the loading marker
        until its own lookup lands. A refresh reads CI past the cache too.
        """
        try:
            page = await self.app.services.list_reviews(
                scope,
                repository=(
                    self.scoped_repository.ref
                    if self.scoped_repository is not None
                    else None
                ),
                fresh=fresh,
            )
        except ServiceError as error:
            if error.code is ServiceErrorCode.RATE_LIMITED:
                hostname = dict(error.details).get("hostname")
                if hostname:
                    self._on_rate_limited(tab_id, hostname)
            else:
                self.notify(
                    f"{label}: {error.message}", severity="warning", markup=False
                )
            # The earlier rows stay, so any still waiting for CI get lookups.
            self._update_ci_window(tab_id)
            return

        if page.items or not page.failures:
            # The new rows replace the old ones, so lookups for those stop.
            self._stop_ci_lookups(tab_id)
            summaries = self._shown([item.summary for item in page.items])
            table.merge_mrs(
                summaries, self.app.config.ascii_mode, replace=True, ci_pending=True
            )
            self._want_ci(tab_id, summaries, fresh=fresh)
        else:
            self._update_ci_window(tab_id)
        self._notify_failures(tab_id, label, page.items, page.failures)

    def _notify_failures(
        self,
        tab_id: str,
        label: str,
        items: Sequence[ReviewListItem],
        failures: Sequence[HostFailure],
    ) -> None:
        # A rate limit is reported once per host, with its reset time.
        for hostname in dict.fromkeys(
            failure.hostname
            for failure in failures
            if failure.code is ServiceErrorCode.RATE_LIMITED
        ):
            self._on_rate_limited(tab_id, hostname)
        failures = [
            failure
            for failure in failures
            if failure.code is not ServiceErrorCode.RATE_LIMITED
        ]
        if failures:
            # A host or repository that still contributed rows returned a
            # partial page rather than being skipped, so it is reported apart.
            shown = {item.ref.repository for item in items}
            shown_hosts = {repository.hostname for repository in shown}
            partial: list[HostFailure] = []
            skipped: list[HostFailure] = []
            for failure in failures:
                contributed = (
                    failure.repository in shown
                    if failure.repository is not None
                    else failure.hostname in shown_hosts
                )
                (partial if contributed else skipped).append(failure)
            # Failure messages can carry forge text, so show them literally.
            if partial:
                self.notify(
                    f"{label}: {_failure_details(partial)}",
                    severity="warning",
                    markup=False,
                )
            if skipped:
                self.notify(
                    f"{label} skipped: {_failure_details(skipped)}",
                    severity="warning",
                    markup=False,
                )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_select_mr()

    def _active_table(self) -> MRTable | None:
        tabbed = self.query_one(TabbedContent)
        table_id = _TAB_TABLES.get(tabbed.active, "#reviews-table")
        return self.query_one(table_id, MRTable)


def _pop_queued(queue: deque[str], table: MRTable, hostname: str) -> str | None:
    """Take the first queued row of a host off the queue, if any."""
    for index, key in enumerate(queue):
        summary = table.get_mr(key)
        if summary is not None and summary.forge_host.hostname == hostname:
            del queue[index]
            return key
    return None


def _summary_for(screen: InboxScreen, tab_id: str, key: str) -> MRSummary | None:
    try:
        return screen.query_one(_TAB_TABLES[tab_id], MRTable).get_mr(key)
    except (NoMatches, NoScreen):
        return None


def _open_count(count: int) -> str:
    return f"{count} open MR{'' if count == 1 else 's'}"


def _repositories(count: int) -> str:
    return "repository" if count == 1 else "repositories"


def _loading_more(count: int) -> str:
    return f"Loading more from {count} {_repositories(count)}"


# Codes that describe the whole host rather than one repository, so every
# repository on that host fails the same way and is reported once.
_HOST_WIDE_CODES = frozenset(
    {
        ServiceErrorCode.AUTHENTICATION_FAILED,
        ServiceErrorCode.RATE_LIMITED,
        ServiceErrorCode.NETWORK_UNAVAILABLE,
        ServiceErrorCode.CONFIGURATION_INVALID,
    }
)


def _failure_details(failures: list[HostFailure]) -> str:
    """Join failures into one notification, one entry per repository or host.

    A failure names its repository when it has one. Repository failures that
    share a host-wide code collapse into one entry for the host with a count.
    """
    groups: dict[tuple[str, ServiceErrorCode | int], list[HostFailure]] = {}
    for index, failure in enumerate(failures):
        if failure.repository is not None and failure.code in _HOST_WIDE_CODES:
            key: tuple[str, ServiceErrorCode | int] = (failure.hostname, failure.code)
        else:
            key = (failure.hostname, index)
        groups.setdefault(key, []).append(failure)
    entries: list[str] = []
    for group in groups.values():
        first = group[0]
        repositories = {failure.repository for failure in group}
        if len(repositories) > 1:
            entries.append(
                f"{first.hostname}: {first.message} ({len(repositories)} repositories)"
            )
        elif first.repository is not None:
            entries.append(
                f"{first.hostname}/{first.repository.project_path}: {first.message}"
            )
        else:
            entries.append(f"{first.hostname}: {first.message}")
    return "; ".join(entries)
