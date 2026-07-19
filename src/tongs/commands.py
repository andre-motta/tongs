"""Command palette provider for the tongs TUI."""

from __future__ import annotations

from textual.command import DiscoveryHit, Hit, Hits, Provider


class TongsCommandProvider(Provider):
    """Context-aware command provider for the tongs command palette."""

    async def discover(self) -> Hits:
        """Yield default commands based on the active screen."""
        commands = self._get_commands()
        for display, help_text, callback in commands:
            yield DiscoveryHit(
                display=display,
                command=callback,
                help=help_text,
            )

    async def search(self, query: str) -> Hits:
        """Search commands matching the query."""
        matcher = self.matcher(query)
        commands = self._get_commands()
        for display, help_text, callback in commands:
            text = display if isinstance(display, str) else help_text or ""
            score = matcher.match(text)
            if score > 0:
                yield Hit(
                    score=score,
                    match_display=matcher.highlight(text),
                    command=callback,
                    help=help_text,
                )

    def _get_commands(self) -> list[tuple[str, str, object]]:
        """Build the command list based on current screen context."""
        commands: list[tuple[str, str, object]] = []
        screen = self.screen
        app = self.app
        screen_name = type(screen).__name__

        commands.extend(self._global_commands(app))

        if screen_name == "InboxScreen":
            commands.extend(self._inbox_commands(screen))
        elif screen_name == "RepoListScreen":
            commands.extend(self._repo_list_commands(screen))
        elif screen_name == "MRDetailScreen":
            commands.extend(self._mr_detail_commands(screen))

        return commands

    def _global_commands(self, app) -> list[tuple[str, str, object]]:
        return [
            ("Repos", "Open repository list", lambda: app.push_screen("repo_list")),
            ("Inbox", "Open MR inbox", lambda: app.push_screen("inbox")),
            ("Help", "Show help", lambda: app.action_help()),
        ]

    def _inbox_commands(self, screen) -> list[tuple[str, str, object]]:
        return [
            (
                "My Reviews",
                "Show reviews assigned to me",
                lambda: screen.action_focus_tab("reviews"),
            ),
            (
                "My MRs",
                "Show my merge requests",
                lambda: screen.action_focus_tab("my-mrs"),
            ),
            (
                "All Open",
                "Show all open MRs",
                lambda: screen.action_focus_tab("all-open"),
            ),
            ("Refresh", "Reload current tab", lambda: screen.action_refresh()),
            (
                "Open in Browser",
                "Open selected MR in browser",
                lambda: screen.action_open_in_browser(),
            ),
        ]

    def _repo_list_commands(self, screen) -> list[tuple[str, str, object]]:
        return [
            (
                "Filter Repos",
                "Start typing to filter",
                lambda: screen.action_start_search(),
            ),
            (
                "Cycle Forge",
                "Filter by forge type",
                lambda: screen.action_cycle_forge(),
            ),
            ("Refresh", "Rescan repos", lambda: screen.action_refresh()),
        ]

    def _mr_detail_commands(self, screen) -> list[tuple[str, str, object]]:
        commands = [
            (
                "Overview",
                "Show MR overview tab",
                lambda: screen.action_focus_tab("overview"),
            ),
            ("Diff", "Show diff tab", lambda: screen.action_focus_tab("diff")),
            ("Commits", "Show commits tab", lambda: screen.action_focus_tab("commits")),
            (
                "Discussion",
                "Show discussion tab",
                lambda: screen.action_focus_tab("discussion"),
            ),
            (
                "Pipeline",
                "Show pipeline tab",
                lambda: screen.action_focus_tab("pipeline"),
            ),
            ("Comment", "Add a general comment", lambda: screen.action_add_comment()),
            ("Approve", "Approve this MR", lambda: screen.action_approve()),
            ("Unapprove", "Remove approval", lambda: screen.action_unapprove()),
            ("Merge", "Merge this MR", lambda: screen.action_merge()),
            ("Close", "Close this MR", lambda: screen.action_close_mr()),
            (
                "Open in Browser",
                "Open MR in default browser",
                lambda: screen.action_open_in_browser(),
            ),
            ("Copy URL", "Copy MR URL to clipboard", lambda: screen.action_yank_url()),
            ("Refresh", "Reload MR details", lambda: screen.action_refresh()),
        ]
        return commands
