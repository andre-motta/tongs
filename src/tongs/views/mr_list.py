"""MR list screen for a specific repository."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header

from tongs.scanner.repo import Repo
from tongs.widgets.mr_table import MRTable


class MRListScreen(Screen):
    """List open MRs for a single repository."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("q", "go_back", "Back", show=False),
        Binding("o", "open_in_browser", "Open", show=True),
        Binding("ctrl+r", "refresh", "Refresh", show=True),
    ]

    def __init__(self, repo: Repo):
        super().__init__()
        self.repo = repo

    def compose(self) -> ComposeResult:
        yield Header()
        yield MRTable(id="repo-mr-table")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.repo.display_name
        table = self.query_one("#repo-mr-table", MRTable)
        table.setup_columns()
        table.cursor_type = "row"
        table.zebra_stripes = True
        self._load_mrs()

    @work(exclusive=True, group="repo-mrs")
    async def _load_mrs(self) -> None:
        table = self.query_one("#repo-mr-table", MRTable)
        table.loading = True
        try:
            if not self.repo.hostname or not self.repo.primary_remote:
                self.notify("[dim]No forge remote for this repo[/]")
                return
            client = await self.app.forge_registry.get_client(self.repo.hostname)
            mrs = await client.list_mrs(self.repo.primary_remote.repo_path)
            for mr in mrs:
                table.add_mr_row(mr, self.app.config.ascii_mode)
            if not mrs:
                self.notify("[dim]No open MRs[/]")
        except NotImplementedError:
            self.notify("[dim]Forge not yet supported[/]", severity="warning")
        except Exception as exc:
            self.notify(f"[dim]{exc}[/]", severity="warning")
        finally:
            table.loading = False

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_select_mr(self) -> None:
        table = self.query_one("#repo-mr-table", MRTable)
        mr = table.get_selected_mr()
        if mr:
            from tongs.views.mr_detail import MRDetailScreen

            self.app.push_screen(MRDetailScreen(mr))

    def action_open_in_browser(self) -> None:
        table = self.query_one("#repo-mr-table", MRTable)
        mr = table.get_selected_mr()
        if mr:
            self.app.open_url(mr.web_url)
        else:
            self.notify("No MR selected")

    def action_refresh(self) -> None:
        self.query_one("#repo-mr-table", MRTable).clear()
        self._load_mrs()

    def on_data_table_row_selected(self, event) -> None:
        self.action_select_mr()
