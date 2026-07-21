"""Repository list screen with search and forge filtering."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, Static

from tongs.scanner.repo import ForgeType, Repo


def _forge_label(forge_type: ForgeType | None) -> str:
    if forge_type == ForgeType.GITLAB:
        return "[blue]GL[/]"
    if forge_type == ForgeType.GITHUB:
        return "[white]GH[/]"
    return "[dim]--[/]"


class RepoListScreen(Screen):
    """Searchable, filterable repo list with DataTable."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("q", "go_back", "Back", show=False),
        Binding("slash", "start_search", "Filter", show=True, key_display="/"),
        Binding("f", "cycle_forge", "Forge", show=True),
        Binding("s", "cycle_sort", "Sort", show=True, key_display="s"),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
    ]

    REPO_SORT_KEYS = ("name", "forge", "host")

    forge_filter: reactive[ForgeType | None] = reactive(None)
    search_text: reactive[str] = reactive("")

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(
            placeholder="type to filter...",
            id="repo-search",
        )
        yield Static("", id="repo-status")
        yield DataTable(id="repo-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#repo-table", DataTable)
        table.add_column("F", key="forge", width=4)
        table.add_column("Repository", key="repo")
        hostnames = {r.hostname for r in self.app.repos if r.hostname}
        self._show_host = len(hostnames) > 2
        if self._show_host:
            table.add_column("Host", key="host", width=20)
        table.cursor_type = "row"
        table.zebra_stripes = True
        self._repo_data: dict[str, Repo] = {}
        self._sort_key: str = "name"
        self._apply_filters()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_start_search(self) -> None:
        search = self.query_one("#repo-search", Input)
        search.focus()

    def action_cycle_forge(self) -> None:
        if self.forge_filter is None:
            self.forge_filter = ForgeType.GITHUB
        elif self.forge_filter == ForgeType.GITHUB:
            self.forge_filter = ForgeType.GITLAB
        else:
            self.forge_filter = None

    def watch_forge_filter(self) -> None:
        self._apply_filters()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "repo-search":
            self.search_text = event.value

    def watch_search_text(self) -> None:
        self._apply_filters()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        table = self.query_one("#repo-table", DataTable)
        table.focus()

    def _apply_filters(self) -> None:
        repos: list[Repo] = getattr(self.app, "repos", [])
        filtered = repos

        if self.forge_filter is not None:
            filtered = [r for r in filtered if r.forge_type == self.forge_filter]

        search = self.search_text.strip().lower()
        if search:
            filtered = [r for r in filtered if search in r.display_name.lower()]

        sort_key = getattr(self, "_sort_key", "name")
        if sort_key == "forge":
            filtered = sorted(
                filtered,
                key=lambda r: (
                    r.forge_type.value if r.forge_type else "",
                    r.display_name.lower(),
                ),
            )
        elif sort_key == "host":
            filtered = sorted(
                filtered, key=lambda r: (r.hostname or "", r.display_name.lower())
            )
        else:
            filtered = sorted(filtered, key=lambda r: r.display_name.lower())

        table = self.query_one("#repo-table", DataTable)
        table.clear()
        self._repo_data.clear()

        show_host = getattr(self, "_show_host", False)

        for repo in filtered:
            key = str(repo.path)
            self._repo_data[key] = repo
            row = [
                _forge_label(repo.forge_type),
                repo.display_name,
            ]
            if show_host:
                row.append(repo.hostname or "")
            table.add_row(*row, key=key)

        forge_label = {
            None: "All",
            ForgeType.GITHUB: "GH",
            ForgeType.GITLAB: "GL",
        }.get(self.forge_filter, "All")

        status = self.query_one("#repo-status", Static)
        total = len(repos)
        shown = len(filtered)
        sort_label = f"[dim]sort:{self._sort_key}[/]"
        if shown == total:
            status.update(f"[dim]{total} repositories[/]  [bold][{forge_label}][/]  {sort_label}")
        else:
            status.update(
                f"[dim]{shown} of {total} repositories[/]  [bold][{forge_label}][/]  {sort_label}"
            )

    def action_cycle_sort(self) -> None:
        keys = self.REPO_SORT_KEYS
        idx = keys.index(self._sort_key) if self._sort_key in keys else -1
        self._sort_key = keys[(idx + 1) % len(keys)]
        self._apply_filters()

    def action_refresh(self) -> None:
        self._apply_filters()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        repo = self._repo_data.get(key)
        if repo:
            from tongs.views.inbox import InboxScreen

            self.app.push_screen(InboxScreen(repo=repo))
