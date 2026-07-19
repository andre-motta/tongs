"""MR inbox dashboard -- the default screen."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, TabbedContent, TabPane

from tongs.forges.models import CIStatus, MRSummary


def _ci_icon(status: CIStatus, ascii_mode: bool = False) -> str:
    if ascii_mode:
        return {
            CIStatus.SUCCESS: "[OK]",
            CIStatus.FAILED: "[!!]",
            CIStatus.RUNNING: "[..]",
            CIStatus.PENDING: "[..]",
            CIStatus.CANCELED: "[--]",
            CIStatus.SKIPPED: "[--]",
            CIStatus.UNKNOWN: "[??]",
        }.get(status, "[??]")
    return {
        CIStatus.SUCCESS: "[green]●[/]",
        CIStatus.FAILED: "[red]●[/]",
        CIStatus.RUNNING: "[yellow]▶[/]",
        CIStatus.PENDING: "[dim]○[/]",
        CIStatus.CANCELED: "[dim]—[/]",
        CIStatus.SKIPPED: "[dim]—[/]",
        CIStatus.UNKNOWN: "[dim]?[/]",
    }.get(status, "[dim]?[/]")


def _relative_time(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 0:
        return "now"
    if seconds < 60:
        return "now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


class MRTable(DataTable):
    """MR list table with consistent columns."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._mr_data: dict[str, MRSummary] = {}

    def setup_columns(self) -> None:
        self.add_column("CI", key="ci", width=4)
        self.add_column("#", key="number", width=6)
        self.add_column("Title", key="title")
        self.add_column("Author", key="author", width=14)
        self.add_column("Repo", key="repo", width=35)
        self.add_column("Updated", key="updated", width=8)

    def add_mr_row(self, mr: MRSummary, ascii_mode: bool = False) -> None:
        ci = _ci_icon(mr.ci_status, ascii_mode)
        draft = "[dim]D [/]" if mr.is_draft else "  "
        row_key = f"{mr.forge_host.hostname}:{mr.repo_path}:{mr.number}"
        self._mr_data[row_key] = mr
        self.add_row(
            ci,
            str(mr.number),
            f"{draft}{mr.title}",
            mr.author.username,
            mr.repo_path,
            _relative_time(mr.updated_at),
            key=row_key,
        )

    def get_selected_mr(self) -> MRSummary | None:
        if self.cursor_row is None or self.row_count == 0:
            return None
        try:
            row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
            return self._mr_data.get(row_key.value)
        except Exception:
            return None

    def clear(self, *args, **kwargs) -> None:
        self._mr_data.clear()
        super().clear(*args, **kwargs)


class InboxScreen(Screen):
    """MR inbox dashboard with tabs for My Reviews / My MRs / All Open."""

    BINDINGS = [
        Binding("r", "switch_tab('repos')", "Repos", show=True, key_display="R"),
        Binding("1", "focus_tab('reviews')", "Reviews", show=True),
        Binding("2", "focus_tab('my-mrs')", "My MRs", show=True),
        Binding("3", "focus_tab('all-open')", "All Open", show=True),
        Binding("o", "open_in_browser", "Open", show=True),
        Binding("enter", "select_mr", "Open MR", show=True),
        Binding("ctrl+r", "refresh", "Refresh", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    loading_reviews: reactive[bool] = reactive(False)
    loading_my_mrs: reactive[bool] = reactive(False)
    loading_all_open: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="reviews"):
            with TabPane("My Reviews", id="reviews"):
                yield MRTable(id="reviews-table")
            with TabPane("My MRs", id="my-mrs"):
                yield MRTable(id="my-mrs-table")
            with TabPane("All Open", id="all-open"):
                yield MRTable(id="all-open-table")
        yield Footer()

    def on_mount(self) -> None:
        self._loaded_tabs: set[str] = set()
        for table in self.query(MRTable):
            table.setup_columns()
            table.cursor_type = "row"
            table.zebra_stripes = True

    def action_refresh(self) -> None:
        tabbed = self.query_one(TabbedContent)
        self._loaded_tabs.discard(tabbed.active)
        self.action_focus_tab(tabbed.active)

    def action_switch_tab(self, tab: str) -> None:
        if tab == "repos":
            self.app.push_screen("repo_list")

    def action_focus_tab(self, tab_id: str) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = tab_id
        if tab_id in self._loaded_tabs:
            return
        self._loaded_tabs.add(tab_id)
        table_id = {
            "reviews": "#reviews-table",
            "my-mrs": "#my-mrs-table",
            "all-open": "#all-open-table",
        }.get(tab_id)
        if table_id:
            self.query_one(table_id, MRTable).clear()
        if tab_id == "reviews":
            self.load_reviews()
        elif tab_id == "my-mrs":
            self.load_my_mrs()
        elif tab_id == "all-open":
            self.load_all_open()

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
        if table and table.cursor_row is not None:
            self.notify("MR detail view coming in Phase 2")

    @work(exclusive=True, group="reviews")
    async def load_reviews(self) -> None:
        self.loading_reviews = True
        table = self.query_one("#reviews-table", MRTable)
        table.loading = True
        try:
            hostnames = self.app.get_repo_hostnames()
            if not hostnames:
                self.notify("[dim]No forges discovered yet[/]")
                return
            registry = self.app.forge_registry
            for hostname in hostnames:
                try:
                    client = await registry.get_client(hostname)
                    mrs = await client.list_my_reviews()
                    for mr in mrs:
                        table.add_mr_row(mr, self.app.config.ascii_mode)
                except NotImplementedError:
                    pass
                except Exception as exc:
                    self.notify(
                        f"[dim]Reviews {hostname}:[/] {exc}",
                        severity="warning",
                    )
        finally:
            self.loading_reviews = False
            table.loading = False

    @work(exclusive=True, group="my-mrs")
    async def load_my_mrs(self) -> None:
        self.loading_my_mrs = True
        table = self.query_one("#my-mrs-table", MRTable)
        table.loading = True
        try:
            hostnames = self.app.get_repo_hostnames()
            if not hostnames:
                return
            registry = self.app.forge_registry
            for hostname in hostnames:
                try:
                    client = await registry.get_client(hostname)
                    mrs = await client.list_my_mrs()
                    for mr in mrs:
                        table.add_mr_row(mr, self.app.config.ascii_mode)
                except NotImplementedError:
                    pass
                except Exception as exc:
                    self.notify(
                        f"[dim]My MRs {hostname}:[/] {exc}",
                        severity="warning",
                    )
        finally:
            self.loading_my_mrs = False
            table.loading = False

    @work(exclusive=True, group="all-open")
    async def load_all_open(self) -> None:
        self.loading_all_open = True
        table = self.query_one("#all-open-table", MRTable)
        table.loading = True
        try:
            repos = self.app.repos
            if not repos:
                return
            registry = self.app.forge_registry
            semaphore = asyncio.Semaphore(self.app.config.max_parallel)
            failed_hosts: set[str] = set()

            async def fetch_repo(repo):
                async with semaphore:
                    if not repo.hostname or not repo.primary_remote:
                        return
                    if repo.hostname in failed_hosts:
                        return
                    try:
                        client = await registry.get_client(repo.hostname)
                        mrs = await client.list_mrs(repo.primary_remote.repo_path)
                        for mr in mrs:
                            table.add_mr_row(mr, self.app.config.ascii_mode)
                    except NotImplementedError:
                        failed_hosts.add(repo.hostname)
                    except Exception:
                        failed_hosts.add(repo.hostname)

            await asyncio.gather(*(fetch_repo(r) for r in repos))

            if failed_hosts:
                self.notify(
                    f"[dim]Skipped {len(failed_hosts)} unreachable host(s)[/]",
                    severity="warning",
                )
        finally:
            self.loading_all_open = False
            table.loading = False

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.notify("MR detail view coming in Phase 2")

    def _active_table(self) -> MRTable | None:
        tabbed = self.query_one(TabbedContent)
        tab_map = {
            "reviews": "#reviews-table",
            "my-mrs": "#my-mrs-table",
            "all-open": "#all-open-table",
        }
        table_id = tab_map.get(tabbed.active, "#reviews-table")
        return self.query_one(table_id, MRTable)
