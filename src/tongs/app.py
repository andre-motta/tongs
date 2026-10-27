"""Main Textual application."""

from __future__ import annotations

import logging
import webbrowser
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import App, get_system_commands_provider
from textual.binding import Binding
from textual.command import CommandListItem, SimpleProvider
from textual.content import Content
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.scrollbar import ScrollBar, ScrollBarRender
from textual.theme import ThemeProvider
from textual.widget import AwaitMount, Widget
from textual.worker import WorkerCancelled, WorkerFailed

from tongs.commands import TongsCommandProvider
from tongs.config import DEFAULT_THEME, Config, load_config
from tongs.plugins.registry import PluginRegistry
from tongs.scanner.repo import Repo
from tongs.services.errors import ServiceError
from tongs.services.session import (
    ApplicationSession,
    CacheResource,
    ForgeRegistryResource,
)
from tongs.tui_services import TUIServiceAdapter
from tongs.views.inbox import InboxScreen
from tongs.views.leave_guard import LeaveGuard
from tongs.views.repo_list import RepoListScreen
from tongs.widgets.ascii import (
    ASCII_CLASS,
    PALETTE_PLACEHOLDER,
    THEME_PLACEHOLDER,
    TITLE_SEPARATOR,
    AsciiCommandPalette,
    AsciiLoadingIndicator,
    AsciiScrollBarRender,
    ascii_key_display,
)

log = logging.getLogger(__name__)


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

    /* [ui] ascii_mode: the same borders in the same colours, drawn with
       + - | only, and no Unicode lines under tabs, in tables or in the help
       and footer. App rules beat a widget's own CSS, so each one names the
       widgets that draw a border today and keeps their states. */
    .-ascii {
        DiscussionCard { border: ascii $accent-darken-2; }
        DiscussionCard.focused { border: ascii $accent; }
        DiscussionCard.resolved { border: ascii $surface-lighten-2; }
        DiscussionCard.resolved.focused { border: ascii $accent; }
        PipelineCard { border: ascii $accent-darken-2; }
        PipelineCard.focused { border: ascii $accent; }
        PipelineCard.failed { border: ascii $error; }
        PipelineCard.running { border: ascii $warning; }
        ReviewSubmitScreen > Vertical { border: ascii $accent; }
        CommentEditor { border-top: ascii $accent; }
        DiffPanel DiffFileTree { border-right: ascii $accent; }
        SplitDiffColumn#split-old { border-right: ascii $accent; }

        #repo-search, #log-search-input, #comment-input,
        #review-submit-body, #review-submit-comments {
            border: ascii $border-blurred;
        }
        #repo-search:focus, #log-search-input:focus, #comment-input:focus,
        #review-submit-body:focus, #review-submit-comments:focus {
            border: ascii $border;
        }

        Tabs Underline { visibility: hidden; }
        Tabs Tab.-active { text-style: bold underline; }
        FooterKey.-command-palette { border-left: ascii $foreground 20%; }
        HelpPanel { border-left: ascii $foreground 30%; }
        HelpPanel BindingsTable > .bindings-table--divider { color: transparent; }
        Toast.-information { border-left: ascii $success; }
        Toast.-warning { border-left: ascii $warning; }
        Toast.-error { border-left: ascii $error; }
        MarkdownHorizontalRule { border-bottom: ascii $secondary; }
        MarkdownTableContent > .cell, MarkdownTableContent > .header {
            text-overflow: clip;
        }
        MarkdownBlockQuote { border-left: ascii $text-primary 50%; }
        MarkdownTableContent { keyline: none; }
        HeaderTitle { text-overflow: clip; }

        CommandPalette #--input { border: ascii $foreground 20%; }
        CommandPalette #--input.--list-visible { border-bottom: none; }
        CommandPalette CommandList { border-bottom: ascii $foreground 20%; }
        CommandPalette CommandList:focus { border-bottom: blank; }
        CommandPalette CommandList.--populating { border-bottom: none; }
        CommandPalette LoadingIndicator { display: none; border-bottom: none; }
    }
    """

    COMMANDS: ClassVar[set] = {TongsCommandProvider, get_system_commands_provider}

    BINDINGS: ClassVar[list] = [
        Binding("question_mark", "help", "Help", show=True),
    ]

    SCREENS: ClassVar[dict] = {
        "inbox": InboxScreen,
        "repo_list": RepoListScreen,
    }

    current_repo: reactive[Repo | None] = reactive(None)
    current_mr_number: reactive[int | None] = reactive(None)
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
        self.shutdown_error: ServiceError | None = None
        self.repository_generation = 0
        self._plugins_ready = False
        # The process-wide scrollbar renderer that ``ascii_mode`` replaced.
        self._scrollbar_renderer: type[ScrollBarRender] | None = None

    @property
    def cache(self) -> CacheResource:
        """Compatibility alias for the cache owned by the shared session."""
        return self.session.cache

    @property
    def forge_registry(self) -> ForgeRegistryResource:
        """Compatibility alias for the registry owned by the shared session."""
        return self.session.forge_registry

    def apply_theme(self) -> None:
        """Use the configured ``[ui] theme``, or the default when it is unknown.

        Plugins may register themes, so this runs again once they are ready.
        """
        name = self.config.theme
        if name not in self.available_themes:
            message = f"Unknown [ui] theme {name!r}; using {DEFAULT_THEME}."
            log.warning(message)
            self.notify(message, severity="warning", timeout=10, markup=False)
            name = DEFAULT_THEME
        self.theme = name

    def apply_ascii_mode(self) -> None:
        """Draw only ASCII when ``[ui] ascii_mode`` is on, on every screen.

        The CSS class redraws the borders; the scrollbars take a renderer
        without block glyphs. ``ScrollBar.renderer`` is shared by the whole
        process, so the app puts the original back when it exits.
        """
        ascii_mode = self.config.ascii_mode
        self.set_class(ascii_mode, ASCII_CLASS)
        if ascii_mode and self._scrollbar_renderer is None:
            self._scrollbar_renderer = ScrollBar.renderer
            ScrollBar.renderer = AsciiScrollBarRender
        elif not ascii_mode:
            self._restore_scrollbars()

    def _restore_scrollbars(self) -> None:
        if self._scrollbar_renderer is not None:
            ScrollBar.renderer = self._scrollbar_renderer
            self._scrollbar_renderer = None

    def get_key_display(self, binding: Binding) -> str:
        display = super().get_key_display(binding)
        if self.config.ascii_mode and not display.isascii():
            return ascii_key_display(binding)
        return display

    def format_title(self, title: str, sub_title: str) -> Content:
        if not (self.config.ascii_mode and sub_title):
            return super().format_title(title, sub_title)
        return Content.assemble(
            Content(title),
            (TITLE_SEPARATOR, "dim"),
            Content(sub_title).stylize("dim"),
        )

    def get_loading_widget(self) -> Widget:
        if self.config.ascii_mode:
            return AsciiLoadingIndicator()
        return super().get_loading_widget()

    def action_command_palette(self) -> None:
        if not self.config.ascii_mode:
            super().action_command_palette()
        elif self.use_command_palette and not AsciiCommandPalette.is_open(self):
            self.push_screen(AsciiCommandPalette(id="--command-palette"))

    def search_commands(
        self,
        commands: Sequence[CommandListItem],
        placeholder: str | None = None,
    ) -> AwaitMount:
        if not self.config.ascii_mode:
            if placeholder is None:
                return super().search_commands(commands)
            return super().search_commands(commands, placeholder)
        if placeholder is None:
            placeholder = PALETTE_PLACEHOLDER
        return self.push_screen(
            AsciiCommandPalette(
                providers=[SimpleProvider(self.screen, commands)],
                placeholder=placeholder.replace("\N{HORIZONTAL ELLIPSIS}", "..."),
            )
        )

    def search_themes(self) -> None:
        if not self.config.ascii_mode:
            super().search_themes()
            return
        self.push_screen(
            AsciiCommandPalette(
                providers=[ThemeProvider], placeholder=THEME_PLACEHOLDER
            )
        )

    def open_screen(self, screen_name: str) -> None:
        """Show a named screen without stacking a second copy of it.

        The name resolves to the one installed instance, so a
        repository-scoped inbox never stands in for the main one. The screen
        on top stays put, one deeper in the stack is popped back to, and one
        that is not on the stack is pushed. Popping back asks every screen
        above the target, topmost first, to agree to leave, so an open dialog
        or MR detail that would lose work keeps the app where it is.
        """
        target = self.get_screen(screen_name)
        if self.screen is target:
            return
        stack = self.screen_stack
        if target not in stack:
            self.push_screen(target)
            return
        for screen in reversed(stack[stack.index(target) + 1 :]):
            if isinstance(screen, LeaveGuard) and not screen.prepare_leave():
                return
        target.pop_until_active()

    async def on_mount(self) -> None:
        self.apply_ascii_mode()
        if self.config.theme in self.available_themes:
            # Before startup, so the first screen already has the theme.
            self.theme = self.config.theme
        try:
            await self.session.start()
        except ServiceError as error:
            self.startup_error = error
            self.notify(error.message, severity="error")
            self.exit()
            return
        for warning in self.session.recovery_warnings:
            self.notify(
                warning.describe(), severity="warning", timeout=15, markup=False
            )
        self.config = self.session.config
        self.apply_ascii_mode()
        self.plugin_registry.discover(self.config.plugin_config)
        await self.plugin_registry.on_app_ready(self)
        self._plugins_ready = True
        self.apply_theme()
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
        self.repository_generation += 1
        if isinstance(screen, InboxScreen):
            screen.on_repositories_refreshed(self.repository_generation)
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
            self._restore_scrollbars()
            await self._close_session()

    async def _close_session(self) -> None:
        """Close the session; on failure, close its stores anyway for exit.

        A failed close keeps the stores open for a retry, but the app is
        exiting, so the final close runs. The error is kept in
        ``shutdown_error`` for the entry point to report: raised from here,
        it would surface as an unrelated Textual shutdown error.
        """
        try:
            await self.session.close()
        except ServiceError:
            try:
                await self.session.close(final=True)
            except ServiceError as error:
                self.shutdown_error = error

    def action_help(self) -> None:
        if self.screen.query("HelpPanel"):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()
