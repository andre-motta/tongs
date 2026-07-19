"""MR detail screen with tabbed interface."""

from __future__ import annotations

from rich.markup import escape

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from tongs.forges.models import CIStatus, MRDetail, MRSummary
from tongs.widgets.diff_panel import DiffPanel


def _ci_label(status: CIStatus) -> str:
    labels = {
        CIStatus.SUCCESS: "[green]passing[/]",
        CIStatus.FAILED: "[red]failed[/]",
        CIStatus.RUNNING: "[yellow]running[/]",
        CIStatus.PENDING: "[dim]pending[/]",
        CIStatus.CANCELED: "[dim]canceled[/]",
        CIStatus.SKIPPED: "[dim]skipped[/]",
        CIStatus.UNKNOWN: "[dim]unknown[/]",
    }
    return labels.get(status, "[dim]unknown[/]")


class MROverview(Static):
    """MR overview panel showing metadata and description."""

    def set_mr(self, mr: MRDetail) -> None:
        approvals = ", ".join(u.username for u in mr.approvals) or "none"
        reviewers = ", ".join(u.username for u in mr.reviewers) or "none"
        assignees = ", ".join(u.username for u in mr.assignees) or "none"
        labels = ", ".join(mr.labels) or "none"
        draft = "[yellow]DRAFT[/]  " if mr.is_draft else ""
        conflicts = "[red]HAS CONFLICTS[/]  " if mr.has_conflicts else ""

        meta = (
            f"[bold]!{mr.number} {escape(mr.title)}[/]\n"
            f"{draft}{conflicts}"
            f"{mr.source_branch} -> {mr.target_branch}  "
            f"by @{mr.author.username}\n\n"
            f"CI: {_ci_label(mr.ci_status)}  "
            f"Approvals: {approvals}\n"
            f"Reviewers: {reviewers}  "
            f"Assignees: {assignees}\n"
            f"Labels: {labels}  "
            f"Changes: +{mr.additions or 0} -{mr.deletions or 0}\n"
        )

        if mr.description:
            meta += f"\n---\n\n{escape(mr.description)}"

        self.update(meta)


class MRDetailScreen(Screen):
    """MR detail view with tabs for Overview, Diff, Discussion, Pipeline."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("q", "go_back", "Back", show=False),
        Binding("1", "focus_tab('overview')", "Overview", show=True),
        Binding("2", "focus_tab('diff')", "Diff", show=True),
        Binding("3", "focus_tab('discussion')", "Discussion", show=True),
        Binding("4", "focus_tab('pipeline')", "Pipeline", show=True),
        Binding("o", "open_in_browser", "Open", show=True),
        Binding("y", "yank_url", "Copy URL", show=True),
        Binding("ctrl+r", "refresh", "Refresh", show=True),
    ]

    def __init__(self, mr_summary: MRSummary):
        super().__init__()
        self.mr_summary = mr_summary
        self.mr_detail: MRDetail | None = None
        self._diff_loaded = False

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                yield MROverview(id="mr-overview")
            with TabPane("Diff", id="diff"):
                yield DiffPanel(id="diff-panel")
            with TabPane("Discussion", id="discussion"):
                yield Static("[dim]Discussion view planned for Phase 4[/]")
            with TabPane("Pipeline", id="pipeline"):
                yield Static("[dim]Pipeline view planned for Phase 5[/]")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"!{self.mr_summary.number} {escape(self.mr_summary.title)}"
        self._load_detail()

    @work(exclusive=True, group="mr-detail")
    async def _load_detail(self) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            self.mr_detail = await client.get_mr(
                self.mr_summary.repo_path, self.mr_summary.number
            )
            overview = self.query_one("#mr-overview", MROverview)
            overview.set_mr(self.mr_detail)
        except Exception as exc:
            self.notify(
                f"Could not load MR details. Try Ctrl+R to refresh. ({exc})",
                severity="warning",
            )

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_focus_tab(self, tab_id: str) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = tab_id
        self._on_tab_switch(tab_id)

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        tab_id = event.pane.id
        if tab_id:
            self._on_tab_switch(tab_id)

    def _on_tab_switch(self, tab_id: str) -> None:
        if tab_id == "diff" and not self._diff_loaded:
            self._diff_loaded = True
            self._load_diff()

    @work(exclusive=True, group="mr-diff")
    async def _load_diff(self) -> None:
        panel = self.query_one("#diff-panel", DiffPanel)
        content = panel.query_one("#diff-content")
        content.show_placeholder("Loading diff...")
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            changes = await client.get_mr_diff(
                self.mr_summary.repo_path, self.mr_summary.number
            )
            if not changes:
                content.show_placeholder("No changes in this MR")
                return

            from tongs.diff.parser import parse_diff

            diff_text = self._changes_to_diff_text(changes)
            files = parse_diff(diff_text)
            panel.set_files(files)
        except Exception as exc:
            content.show_placeholder(f"Could not load diff. Try Ctrl+R. ({exc})")

    def _changes_to_diff_text(self, changes: list[dict]) -> str:
        """Convert GitLab changes API response to unified diff text."""
        parts = []
        for change in changes:
            old_path = change.get("old_path", "")
            new_path = change.get("new_path", "")
            diff = change.get("diff", "").rstrip("\n")
            if diff:
                if not diff.lstrip().startswith("--- "):
                    parts.append(f"--- a/{old_path}")
                    parts.append(f"+++ b/{new_path}")
                parts.append(diff)
        return "\n".join(parts)

    def action_open_in_browser(self) -> None:
        self.app.open_url(self.mr_summary.web_url)

    def action_yank_url(self) -> None:
        try:
            import pyperclip

            pyperclip.copy(self.mr_summary.web_url)
            self.notify("URL copied to clipboard")
        except Exception:
            self.notify(f"URL: {self.mr_summary.web_url}")

    def action_refresh(self) -> None:
        self._diff_loaded = False
        self._load_detail()
