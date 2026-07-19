"""Main Textual application."""

from __future__ import annotations

import webbrowser
from pathlib import Path

from textual import work
from textual.app import App
from textual.binding import Binding
from textual.reactive import reactive

from tongs.config import Config, load_config
from tongs.forges.registry import ForgeRegistry
from tongs.scanner.discovery import discover_repos
from tongs.scanner.repo import Repo
from tongs.state.app_state import MRFilter, ReviewDraft
from tongs.views.inbox import InboxScreen
from tongs.views.repo_list import RepoListScreen


class TongsApp(App):
    """Multi-forge MR/CI management TUI."""

    TITLE = "tongs"
    CSS = """
    Screen {
        background: $surface;
    }

    DataTable {
        height: 1fr;
    }

    Tree {
        height: 1fr;
    }

    TabbedContent {
        height: 1fr;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+p", "command_palette", "Command Palette", show=True),
        Binding("question_mark", "help", "Help", show=True),
    ]

    SCREENS = {
        "inbox": InboxScreen,
        "repo_list": RepoListScreen,
    }

    current_repo: reactive[Repo | None] = reactive(None)
    current_mr_number: reactive[int | None] = reactive(None)
    mr_filter: reactive[MRFilter] = reactive(MRFilter)
    pending_review: reactive[ReviewDraft | None] = reactive(None)
    offline: reactive[bool] = reactive(False)

    def __init__(
        self,
        config: Config | None = None,
        config_path: Path | None = None,
    ):
        super().__init__()
        self.config = config or load_config(config_path)
        self.forge_registry = ForgeRegistry(
            extra_gitlab_hosts=self.config.extra_gitlab_hosts,
            extra_github_hosts=self.config.extra_github_hosts,
            request_timeout=self.config.request_timeout,
        )
        self.repos: list[Repo] = []

    def on_mount(self) -> None:
        self.push_screen("inbox")
        self._discover_repos()

    @work(thread=True, group="discovery")
    def _discover_repos(self) -> None:
        """Run repo discovery in a background thread to avoid blocking the UI."""
        self.repos = discover_repos(
            self.config.scan_root_path,
            max_depth=self.config.scan_depth,
            extra_gitlab_hosts=self.config.extra_gitlab_hosts,
            extra_github_hosts=self.config.extra_github_hosts,
        )
        self.call_from_thread(self._on_discovery_complete)

    def _on_discovery_complete(self) -> None:
        """Trigger inbox refresh after repo discovery finishes."""
        screen = self.screen
        if hasattr(screen, "action_refresh"):
            screen.action_refresh()

    def get_repo_hostnames(self) -> list[str]:
        """Return unique hostnames from discovered repos (not hardcoded)."""
        hostnames = set()
        for repo in self.repos:
            if repo.hostname:
                hostnames.add(repo.hostname)
        return sorted(hostnames)

    def open_url(self, url: str) -> None:
        """Open a URL in the default browser."""
        if url:
            webbrowser.open(url)

    async def on_unmount(self) -> None:
        await self.forge_registry.close_all()

    def action_help(self) -> None:
        self.notify("Help: press ? for keybindings, Ctrl+P for command palette")

    def action_command_palette(self) -> None:
        self.notify("Command palette not yet implemented")
