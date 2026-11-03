"""Repository list screen with search and forge filtering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Static

from tongs.helpers import forge_label
from tongs.scanner.repo import ForgeType, Repo
from tongs.services.errors import ServiceError
from tongs.services.models import RepositorySnapshot
from tongs.tui_services import TUIOpenedRepository
from tongs.views.open_repository import OpenRepositoryScreen
from tongs.widgets.ascii import tongs_header

REMOTE_MARKER = " (remote)"


@dataclass(frozen=True, slots=True)
class _RepoRow:
    """One listed repository: a local checkout, or a project opened remotely."""

    key: str
    display_name: str
    forge_type: ForgeType | None
    hostname: str | None
    local: Repo | None = None
    opened: RepositorySnapshot | None = None

    @classmethod
    def for_local(cls, repo: Repo) -> _RepoRow:
        return cls(
            str(repo.path), repo.display_name, repo.forge_type, repo.hostname, repo
        )

    @classmethod
    def for_opened(cls, snapshot: RepositorySnapshot) -> _RepoRow:
        ref = snapshot.ref
        return cls(
            f"remote:{ref.hostname}/{ref.project_path}",
            snapshot.display_name,
            snapshot.forge_type,
            ref.hostname,
            opened=snapshot,
        )


class RepoListScreen(Screen):
    """Searchable, filterable repo list with DataTable."""

    DEFAULT_CSS = """
    RepoListScreen #repo-search {
        display: none;
    }
    """

    BINDINGS: ClassVar[list] = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("q", "go_back", "Back", show=False),
        Binding("slash", "start_search", "Filter", show=True, key_display="/"),
        Binding("f", "cycle_forge", "Forge", show=True),
        Binding("s", "cycle_sort", "Sort", show=True, key_display="s"),
        Binding("a", "open_repository", "Open repo", show=True),
        Binding("x", "remove_repository", "Remove", show=True),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
    ]

    REPO_SORT_KEYS = ("name", "forge", "host")

    forge_filter: reactive[ForgeType | None] = reactive(None)
    search_text: reactive[str] = reactive("")

    def compose(self) -> ComposeResult:
        yield tongs_header(self.app)
        yield Input(
            placeholder="type to filter...",
            id="repo-search",
        )
        yield Static("", id="repo-status")
        yield DataTable(id="repo-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#repo-table", DataTable)
        self._show_host: bool | None = None
        table.cursor_type = "row"
        table.zebra_stripes = True
        self._repo_data: dict[str, _RepoRow] = {}
        self._sort_key: str = "name"
        self._apply_filters()
        # The filter box stays hidden until "/" reveals it, so the table keeps
        # the focus and the advertised bindings reach the screen.
        table.focus()

    def action_go_back(self) -> None:
        search = self.query_one("#repo-search", Input)
        if self.focused is search:
            # Escape closes the filter and restores the unfiltered list.
            search.value = ""
            search.display = False
            self.query_one("#repo-table", DataTable).focus()
            return
        self.app.pop_screen()

    def action_start_search(self) -> None:
        search = self.query_one("#repo-search", Input)
        search.display = True
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

    def _rows(self) -> list[_RepoRow]:
        repos: list[Repo] = getattr(self.app, "repos", [])
        rows = [_RepoRow.for_local(repo) for repo in repos]
        rows.extend(
            _RepoRow.for_opened(snapshot)
            for snapshot in self.app.services.opened_repositories
        )
        return rows

    def _set_columns(self, table: DataTable, rows: list[_RepoRow]) -> None:
        """Add the columns, with a host column once more than two hosts show."""
        show_host = len({row.hostname for row in rows if row.hostname}) > 2
        if show_host == self._show_host:
            return
        self._show_host = show_host
        table.clear(columns=True)
        table.add_column("F", key="forge", width=4)
        table.add_column("Repository", key="repo")
        if show_host:
            table.add_column("Host", key="host", width=20)

    def _apply_filters(self) -> None:
        repos = self._rows()
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
        self._set_columns(table, repos)
        table.clear()
        self._repo_data.clear()

        for repo in filtered:
            self._repo_data[repo.key] = repo
            # Repository names and hosts are shown literally, never as markup.
            name = Text(repo.display_name)
            if repo.opened is not None:
                name.append(REMOTE_MARKER, style="dim")
            row: list[str | Text] = [forge_label(repo.forge_type), name]
            if self._show_host:
                row.append(Text(repo.hostname or ""))
            table.add_row(*row, key=repo.key)

        filter_label = {
            None: "All",
            ForgeType.GITHUB: "GH",
            ForgeType.GITLAB: "GL",
        }.get(self.forge_filter, "All")

        status = self.query_one("#repo-status", Static)
        total = len(repos)
        shown = len(filtered)
        sort_label = f"[dim]sort:{self._sort_key}[/]"
        if shown == total:
            status.update(
                f"[dim]{total} repositories[/]  [bold][{filter_label}][/]  {sort_label}"
            )
        else:
            status.update(
                f"[dim]{shown} of {total} repositories[/]  [bold][{filter_label}][/]  {sort_label}"
            )

    def action_cycle_sort(self) -> None:
        keys = self.REPO_SORT_KEYS
        idx = keys.index(self._sort_key) if self._sort_key in keys else -1
        self._sort_key = keys[(idx + 1) % len(keys)]
        self._apply_filters()

    def action_refresh(self) -> None:
        self.app.refresh_repositories()

    def refresh_rows(self) -> None:
        """Render the latest repository inventory after discovery."""
        self._apply_filters()

    def action_open_repository(self) -> None:
        self.app.push_screen(OpenRepositoryScreen(), self._on_repository_opened)

    def _on_repository_opened(self, result: TUIOpenedRepository | None) -> None:
        if result is None:
            return
        self._apply_filters()
        if result.already_local:
            self.notify(
                f"{result.snapshot.display_name} is already a local repository, "
                "so the local one is open.",
                markup=False,
            )
        self._open_inbox(result.snapshot)

    def action_remove_repository(self) -> None:
        table = self.query_one("#repo-table", DataTable)
        if table.row_count == 0:
            return
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        repo = self._repo_data.get(str(row_key.value))
        if repo is None:
            return
        if repo.opened is None:
            self.notify(
                "Only opened repositories can be removed; this one is local.",
                severity="warning",
            )
            return
        self.app.services.remove_repository(repo.opened.ref)
        self._apply_filters()
        self.notify(f"Removed {repo.display_name}", markup=False)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        repo = self._repo_data.get(key)
        if repo is None:
            return
        snapshot = repo.opened
        if repo.local is not None:
            try:
                snapshot = self.app.services.repository_snapshot(repo.local)
            except ServiceError as error:
                self.notify(error.message, severity="warning", markup=False)
                return
        if snapshot is not None:
            self._open_inbox(snapshot)

    def _open_inbox(self, snapshot: RepositorySnapshot) -> None:
        from tongs.views.inbox import InboxScreen

        self.app.push_screen(InboxScreen(snapshot))
