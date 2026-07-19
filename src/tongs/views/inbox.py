"""MR inbox dashboard -- the default screen."""

from __future__ import annotations

import asyncio

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, TabbedContent, TabPane

from tongs.scanner.repo import Repo
from tongs.widgets.mr_table import MRTable


class InboxScreen(Screen):
    """MR inbox dashboard with tabs for My Reviews / My MRs / All Open.

    When `repo` is provided, the inbox is scoped to that single repo.
    """

    BINDINGS = [
        Binding("r", "repos_or_back", "Repos", show=True, key_display="R"),
        Binding("1", "focus_tab('reviews')", "Reviews", show=True),
        Binding("2", "focus_tab('my-mrs')", "My MRs", show=True),
        Binding("3", "focus_tab('all-open')", "All Open", show=True),
        Binding("o", "open_in_browser", "Open", show=True),
        Binding("ctrl+r", "refresh", "Refresh", show=True),
        Binding("escape", "go_back", "Back", show=False),
        Binding("q", "quit_or_back", "Quit/Back", show=True),
    ]

    loading_reviews: reactive[bool] = reactive(False)
    loading_my_mrs: reactive[bool] = reactive(False)
    loading_all_open: reactive[bool] = reactive(False)

    def __init__(self, repo: Repo | None = None):
        super().__init__()
        self.scoped_repo = repo

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
        show_repo = self.scoped_repo is None
        for table in self.query(MRTable):
            table.setup_columns(show_repo=show_repo)
            table.cursor_type = "row"
            table.zebra_stripes = True
        if self.scoped_repo:
            self.sub_title = self.scoped_repo.display_name

    def action_refresh(self) -> None:
        tabbed = self.query_one(TabbedContent)
        self._loaded_tabs.discard(tabbed.active)
        self.action_focus_tab(tabbed.active)

    def action_go_back(self) -> None:
        if self.scoped_repo:
            self.app.pop_screen()

    def action_quit_or_back(self) -> None:
        if self.scoped_repo:
            self.app.pop_screen()
        else:
            self.app.exit()

    def action_repos_or_back(self) -> None:
        if self.scoped_repo:
            self.app.pop_screen()
        else:
            self.app.push_screen("repo_list")

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        tab_id = event.pane.id
        if tab_id and tab_id not in self._loaded_tabs:
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
        if table:
            mr = table.get_selected_mr()
            if mr:
                from tongs.views.mr_detail import MRDetailScreen

                self.app.push_screen(MRDetailScreen(mr))

    def _get_hostnames(self) -> list[str]:
        if self.scoped_repo and self.scoped_repo.hostname:
            return [self.scoped_repo.hostname]
        return self.app.get_repo_hostnames()

    def _filter_by_repo(self, mrs: list) -> list:
        if not self.scoped_repo or not self.scoped_repo.primary_remote:
            return mrs
        target = self.scoped_repo.primary_remote.repo_path
        return [mr for mr in mrs if mr.repo_path == target]

    @work(exclusive=True, group="reviews")
    async def load_reviews(self) -> None:
        self.loading_reviews = True
        table = self.query_one("#reviews-table", MRTable)
        table.loading = True
        try:
            hostnames = self._get_hostnames()
            if not hostnames:
                self.notify("[dim]No forges discovered yet[/]")
                return
            registry = self.app.forge_registry
            for hostname in hostnames:
                try:
                    client = await registry.get_client(hostname)
                    mrs = self._filter_by_repo(await client.list_my_reviews())
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
            hostnames = self._get_hostnames()
            if not hostnames:
                return
            registry = self.app.forge_registry
            for hostname in hostnames:
                try:
                    client = await registry.get_client(hostname)
                    mrs = self._filter_by_repo(await client.list_my_mrs())
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
            if self.scoped_repo:
                repos = [self.scoped_repo]
            else:
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
        self.action_select_mr()

    def _active_table(self) -> MRTable | None:
        tabbed = self.query_one(TabbedContent)
        tab_map = {
            "reviews": "#reviews-table",
            "my-mrs": "#my-mrs-table",
            "all-open": "#all-open-table",
        }
        table_id = tab_map.get(tabbed.active, "#reviews-table")
        return self.query_one(table_id, MRTable)
