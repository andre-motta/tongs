"""Main Textual application."""

from __future__ import annotations

import webbrowser
from contextlib import suppress
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import App
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.worker import WorkerCancelled, WorkerFailed

from tongs.commands import TongsCommandProvider
from tongs.config import Config, load_config
from tongs.plugins.registry import PluginRegistry
from tongs.scanner.repo import Repo
from tongs.services.errors import ServiceError
from tongs.services.session import (
    ApplicationSession,
    CacheResource,
    ForgeRegistryResource,
)
from tongs.state.app_state import MRFilter, ReviewDraft
from tongs.tui_services import TUIServiceAdapter
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

    VerticalScroll {
        height: 1fr;
    }

    .disc-status-bar {
        height: 1;
        background: $accent 15%;
        padding: 0 1;
    }

    .pipeline-status-bar {
        height: 1;
        background: $accent 15%;
        padding: 0 1;
    }
    """

    COMMANDS: ClassVar[set] = {TongsCommandProvider}

    BINDINGS: ClassVar[list] = [
        Binding("question_mark", "help", "Help", show=True),
    ]

    SCREENS: ClassVar[dict] = {
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
        *,
        session: ApplicationSession | None = None,
        plugin_registry: PluginRegistry | None = None,
    ) -> None:
        super().__init__()
        self.config = config or load_config(config_path)
        self.session = session or ApplicationSession(config=self.config)
        self.services = TUIServiceAdapter(self.session)
        self.repos: list[Repo] = []
        self.plugin_registry = plugin_registry or PluginRegistry()
        self.startup_error: ServiceError | None = None
        self._plugins_ready = False

    @property
    def cache(self) -> CacheResource:
        """Compatibility alias for the cache owned by the shared session."""
        return self.session.cache

    @property
    def forge_registry(self) -> ForgeRegistryResource:
        """Compatibility alias for the registry owned by the shared session."""
        return self.session.forge_registry

    async def on_mount(self) -> None:
        try:
            await self.session.start()
        except ServiceError as error:
            self.startup_error = error
            self.notify(error.message, severity="error")
            self.exit()
            return
        self.config = self.session.config
        self.plugin_registry.discover(self.config.plugin_config)
        await self.plugin_registry.on_app_ready(self)
        self._plugins_ready = True
        await self.push_screen("inbox")
        self.refresh_repositories()

    @work(exclusive=True, group="discovery")
    async def _discover_repos(self) -> None:
        """Run repo discovery in a background thread to avoid blocking the UI."""
        try:
            result = await self.services.discover_repositories()
        except ServiceError as error:
            self.notify(
                f"Repository discovery: {error.message}",
                severity="warning",
            )
            return
        if result.stale:
            return
        self.repos[:] = result.repositories
        self._on_discovery_complete()

    def refresh_repositories(self) -> None:
        """Start a new local discovery generation."""
        self._discover_repos()

    def _on_discovery_complete(self) -> None:
        """Trigger inbox load after repo discovery finishes."""
        screen = self.screen
        if isinstance(screen, InboxScreen):
            try:
                screen.query_one("#reviews-table")
            except NoMatches:
                self.call_after_refresh(self._on_discovery_complete)
                return
            screen._loaded_tabs.clear()
            screen.action_focus_tab("reviews")
        if isinstance(screen, RepoListScreen):
            screen.refresh_rows()

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
        self.services.invalidate_discovery()
        workers = list(self.workers)
        self.workers.cancel_all()
        for worker in workers:
            with suppress(WorkerCancelled, WorkerFailed):
                await worker.wait()
        try:
            if self._plugins_ready:
                await self.plugin_registry.on_app_shutdown(self)
        finally:
            await self.session.close()

    def action_help(self) -> None:
        self.notify("Help: press ? for keybindings, Ctrl+P for command palette")
