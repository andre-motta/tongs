"""MR detail screen with tabbed interface."""

from __future__ import annotations

from rich.markup import escape

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Markdown, Static, TabbedContent, TabPane

from tongs.forges.models import (
    CIStatus,
    MRDetail,
    MRState,
    MRSummary,
    Pipeline,
    PipelineJob,
)
from tongs.views.suggestion import (
    build_suggestion_template,
    extract_new_side_lines,
    format_suggestion_block,
    parse_suggestion_template,
    resolve_suggestion_position,
)
from tongs.widgets.comment_editor import (
    CommentEditor,
    CommentSubmitted,
    GeneralCommentSubmitted,
    ReplySubmitted,
)
from tongs.widgets.diff_panel import (
    CommentMode,
    CommentRequested,
    DiffPanel,
    ReplyRequested,
    ResolveRequested,
)
from tongs.widgets.discussion_list import (
    DiscussionPanel,
    DiscussionReplyRequested,
    JumpToDiffDiscussion,
)
from tongs.widgets.pipeline_panel import (
    CancelJobRequested,
    CancelPipelineRequested,
    LoadJobLogRequested,
    LoadJobsRequested,
    PipelinePanel,
    RetryJobRequested,
    RetryPipelineRequested,
)


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


def _merge_readiness(mr: MRDetail) -> str:
    if mr.state == MRState.MERGED:
        return "[green bold]MERGED[/]"
    if mr.state == MRState.CLOSED:
        return "[red]CLOSED[/]"
    blockers = []
    if mr.is_draft:
        blockers.append("draft")
    if mr.has_conflicts:
        blockers.append("has conflicts")
    if mr.ci_status == CIStatus.FAILED:
        blockers.append("CI failing")
    elif mr.ci_status == CIStatus.RUNNING:
        blockers.append("CI running")
    if mr.detailed_merge_status and mr.detailed_merge_status not in (
        "mergeable",
        "can_be_merged",
    ):
        status = mr.detailed_merge_status.replace("_", " ")
        if status not in " ".join(blockers):
            blockers.append(status)
    if not blockers:
        return "[green]ready[/]"
    return "[yellow]blocked[/] -- " + ", ".join(blockers)


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
            f"Merge: {_merge_readiness(mr)}\n"
            f"Reviewers: {reviewers}  "
            f"Assignees: {assignees}\n"
            f"Labels: {labels}  "
            f"Changes: +{mr.additions or 0} -{mr.deletions or 0}\n"
        )

        self.update(meta)


class MRDetailScreen(Screen):
    """MR detail view with tabs for Overview, Diff, Discussion, Pipeline."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("q", "go_back", "Back", show=False),
        Binding("1", "focus_tab('overview')", "1 Overview", show=False),
        Binding("2", "focus_tab('diff')", "2 Diff", show=False),
        Binding("3", "focus_tab('commits')", "3 Commits", show=False),
        Binding("4", "focus_tab('discussion')", "4 Discussion", show=False),
        Binding("5", "focus_tab('pipeline')", "5 Pipeline", show=False),
        Binding("c", "add_comment", "Comment", show=True),
        Binding("A", "approve", "Approve", show=True, key_display="A"),
        Binding("U", "unapprove", "Unapprove", show=False, key_display="U"),
        Binding("M", "merge", "Merge", show=True, key_display="M"),
        Binding("X", "close_mr", "Close", show=False, key_display="X"),
        Binding("o", "open_in_browser", "Open", show=False),
        Binding("ctrl+y", "yank_url", "Copy URL", show=True),
        Binding("ctrl+r", "refresh", "Refresh", show=True),
    ]

    def __init__(self, mr_summary: MRSummary):
        super().__init__()
        self.mr_summary = mr_summary
        self.mr_detail: MRDetail | None = None
        self._diff_loaded = False
        self._discussions_loaded = False
        self._pipeline_loaded = False
        self._cached_diff_files: list | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                with VerticalScroll():
                    yield MROverview(id="mr-overview")
                    yield Markdown(id="mr-description")
            with TabPane("Diff", id="diff"):
                yield DiffPanel(id="diff-panel")
            with TabPane("Commits", id="commits"):
                with VerticalScroll(id="commits-scroll"):
                    yield Static("[dim]Loading commits...[/]", id="commits-content")
            with TabPane("Discussion", id="discussion"):
                yield Static("", id="disc-status-bar", classes="disc-status-bar")
                yield DiscussionPanel(id="disc-panel")
            with TabPane("Pipeline", id="pipeline"):
                yield Static(
                    "", id="pipeline-status-bar", classes="pipeline-status-bar"
                )
                yield PipelinePanel(id="pipeline-panel")
        yield CommentEditor(id="comment-editor")
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
            description_widget = self.query_one("#mr-description", Markdown)
            description_widget.update(self.mr_detail.description or "")
        except Exception as exc:
            self.notify(
                f"Could not load MR details. Try Ctrl+R to refresh. ({exc})",
                severity="warning",
            )

    def _pipeline_drilled_in(self) -> bool:
        try:
            panel = self.query_one("#pipeline-panel", PipelinePanel)
            return panel._view_level > 0
        except Exception:
            return False

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if self._pipeline_drilled_in() and action in (
            "add_comment",
            "approve",
            "unapprove",
            "merge",
            "close_mr",
            "yank_url",
        ):
            return False
        return True

    def action_go_back(self) -> None:
        try:
            panel = self.query_one("#pipeline-panel", PipelinePanel)
            if panel._view_level > 0:
                panel.action_drill_out()
                return
        except Exception:
            pass
        screen_stack = self.app.screen_stack
        if len(screen_stack) >= 2:
            parent = screen_stack[-2]
            if hasattr(parent, "_loaded_tabs"):
                parent._loaded_tabs.clear()
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

    _commits_loaded: bool = False

    def _on_tab_switch(self, tab_id: str) -> None:
        if tab_id == "diff" and not self._diff_loaded:
            self._diff_loaded = True
            self._load_diff()
        elif tab_id == "commits" and not self._commits_loaded:
            self._commits_loaded = True
            self._load_commits()
        elif tab_id == "discussion" and not self._discussions_loaded:
            self._discussions_loaded = True
            self._load_discussions()
        elif tab_id == "pipeline" and not self._pipeline_loaded:
            self._pipeline_loaded = True
            self._load_pipelines()

    @work(exclusive=True, group="mr-diff")
    async def _load_diff(self) -> None:
        panel = self.query_one("#diff-panel", DiffPanel)
        content = panel.query_one("#diff-content")
        content.show_placeholder("Loading diff...")
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            changes, discussions = await self._fetch_diff_and_discussions(client)
            if not changes:
                content.show_placeholder("No changes in this MR")
                return

            from tongs.diff.parser import parse_diff

            diff_text = self._changes_to_diff_text(changes)
            files = parse_diff(diff_text)
            self._cached_diff_files = files
            panel.set_files(files, discussions)
        except Exception as exc:
            content.show_placeholder(f"Could not load diff. Try Ctrl+R. ({exc})")

    async def _fetch_diff_and_discussions(self, client):
        """Fetch diff changes and discussions in parallel."""
        import asyncio

        changes_task = asyncio.create_task(
            client.get_mr_diff(self.mr_summary.repo_path, self.mr_summary.number)
        )
        discussions_task = asyncio.create_task(
            client.get_mr_discussions(self.mr_summary.repo_path, self.mr_summary.number)
        )
        changes = await changes_task
        try:
            discussions = await discussions_task
        except Exception:
            discussions = []
        return changes, discussions

    def _changes_to_diff_text(self, changes: list[dict]) -> str:
        """Convert forge API response to unified diff text.

        Handles both GitLab (old_path/new_path/diff) and GitHub (filename/patch).
        """
        parts = []
        for change in changes:
            old_path = (
                change.get("old_path")
                or change.get("previous_filename")
                or change.get("filename", "")
            )
            new_path = change.get("new_path") or change.get("filename", "")
            diff = (change.get("diff") or change.get("patch") or "").rstrip("\n")
            if diff:
                if not diff.lstrip().startswith("--- "):
                    parts.append(f"--- a/{old_path}")
                    parts.append(f"+++ b/{new_path}")
                parts.append(diff)
        return "\n".join(parts)

    @work(exclusive=True, group="mr-commits")
    async def _load_commits(self) -> None:
        content = self.query_one("#commits-content", Static)
        content.update("[dim]Loading commits...[/]")
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            commits = await client.list_mr_commits(
                self.mr_summary.repo_path, self.mr_summary.number
            )
            if not commits:
                content.update("[dim]No commits[/]")
                return

            lines = []
            for c in commits:
                sha = f"[yellow]{c.short_sha}[/]"
                author = f"[dim]@{c.author.username}[/]"
                title = escape(c.title)
                lines.append(f"{sha} {title}  {author}")
                if c.message and c.message != c.title:
                    body = c.message[len(c.title) :].strip()
                    if body:
                        for body_line in body.split("\n"):
                            lines.append(f"        [dim]{escape(body_line)}[/]")
                lines.append("")

            content.update("\n".join(lines) if lines else "[dim]No commits[/]")
        except Exception as exc:
            content.update(f"Could not load commits. ({exc})")

    @work(exclusive=True, group="mr-discussions")
    async def _load_discussions(self) -> None:
        status = self.query_one("#disc-status-bar", Static)
        status.update("[dim]Loading discussions...[/]")
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            discussions = await client.get_mr_discussions(
                self.mr_summary.repo_path, self.mr_summary.number
            )
            if self._cached_diff_files is None:
                try:
                    changes = await client.get_mr_diff(
                        self.mr_summary.repo_path, self.mr_summary.number
                    )
                    from tongs.diff.parser import parse_diff

                    diff_text = self._changes_to_diff_text(changes or [])
                    self._cached_diff_files = parse_diff(diff_text)
                except Exception:
                    self._cached_diff_files = []

            panel = self.query_one("#disc-panel", DiscussionPanel)
            panel.set_discussions(discussions, self._cached_diff_files)
            unresolved = sum(1 for d in discussions if not d.is_resolved)
            resolved = sum(1 for d in discussions if d.is_resolved)
            status.update(
                f"[yellow]{unresolved} unresolved[/]  [dim]{resolved} resolved[/]"
            )
        except Exception as exc:
            status.update(f"Could not load discussions. ({exc})")

    def on_jump_to_diff_discussion(self, event: JumpToDiffDiscussion) -> None:
        """Switch to Diff tab and navigate to a discussion's location."""
        self.action_focus_tab("diff")
        panel = self.query_one("#diff-panel", DiffPanel)
        panel.jump_to_discussion(event.file_path, event.line, event.discussion_id)

    @work(exclusive=True, group="mr-pipelines")
    async def _load_pipelines(self) -> None:
        status = self.query_one("#pipeline-status-bar", Static)
        status.update("[dim]Loading pipelines...[/]")
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            pipelines = await client.list_mr_pipelines(
                self.mr_summary.repo_path, self.mr_summary.number
            )
            panel = self.query_one("#pipeline-panel", PipelinePanel)
            panel.set_pipelines(pipelines)
            running = sum(1 for p in pipelines if p.status == CIStatus.RUNNING)
            failed = sum(1 for p in pipelines if p.status == CIStatus.FAILED)
            total = len(pipelines)
            parts = [f"{total} pipeline{'s' if total != 1 else ''}"]
            if running:
                parts.append(f"[yellow]{running} running[/]")
            if failed:
                parts.append(f"[red]{failed} failed[/]")
            status.update("  ".join(parts))
        except Exception as exc:
            status.update(f"Could not load pipelines. ({exc})")

    def on_load_jobs_requested(self, event: LoadJobsRequested) -> None:
        self._load_pipeline_jobs(event.pipeline)

    @work(exclusive=True, group="mr-pipelines")
    async def _load_pipeline_jobs(self, pipeline: Pipeline) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            jobs = await client.get_pipeline_jobs(
                self.mr_summary.repo_path, pipeline.id
            )
            panel = self.query_one("#pipeline-panel", PipelinePanel)
            panel.set_jobs(jobs, pipeline)
        except Exception as exc:
            self.notify(f"Could not load jobs: {exc}", severity="error")

    def on_load_job_log_requested(self, event: LoadJobLogRequested) -> None:
        self._load_job_log(event.job, event.pipeline)

    @work(exclusive=True, group="mr-pipelines")
    async def _load_job_log(self, job: PipelineJob, pipeline: Pipeline) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            log_text = await client.get_job_log(self.mr_summary.repo_path, job.id)
            panel = self.query_one("#pipeline-panel", PipelinePanel)
            panel.set_job_log(log_text, job, pipeline)
        except Exception as exc:
            self.notify(f"Could not load job log: {exc}", severity="error")

    def on_cancel_pipeline_requested(self, event: CancelPipelineRequested) -> None:
        self._do_cancel_pipeline(event.pipeline_id)

    @work(exclusive=True, group="mr-pipelines")
    async def _do_cancel_pipeline(self, pipeline_id: int) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            await client.cancel_pipeline(self.mr_summary.repo_path, pipeline_id)
            self.notify("[green]Pipeline cancelled[/]")
            self._pipeline_loaded = False
            self._on_tab_switch("pipeline")
        except Exception as exc:
            self.notify(f"Cancel failed: {exc}", severity="error")

    def on_retry_pipeline_requested(self, event: RetryPipelineRequested) -> None:
        self._do_retry_pipeline(event.pipeline_id)

    @work(exclusive=True, group="mr-pipelines")
    async def _do_retry_pipeline(self, pipeline_id: int) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            await client.retry_pipeline(self.mr_summary.repo_path, pipeline_id)
            self.notify("[green]Pipeline retried[/]")
            self._pipeline_loaded = False
            self._on_tab_switch("pipeline")
        except Exception as exc:
            self.notify(f"Retry failed: {exc}", severity="error")

    def on_cancel_job_requested(self, event: CancelJobRequested) -> None:
        self._do_cancel_job(event.job_id)

    @work(exclusive=True, group="mr-pipelines")
    async def _do_cancel_job(self, job_id: int) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            await client.cancel_job(self.mr_summary.repo_path, job_id)
            self.notify("[green]Job cancelled[/]")
        except Exception as exc:
            self.notify(f"Cancel job failed: {exc}", severity="error")

    def on_retry_job_requested(self, event: RetryJobRequested) -> None:
        self._do_retry_job(event.job_id)

    @work(exclusive=True, group="mr-pipelines")
    async def _do_retry_job(self, job_id: int) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            await client.retry_job(self.mr_summary.repo_path, job_id)
            self.notify("[green]Job retried[/]")
        except Exception as exc:
            self.notify(f"Retry job failed: {exc}", severity="error")

    def on_discussion_reply_requested(self, event: DiscussionReplyRequested) -> None:
        """Open reply editor from the Discussion tab."""
        editor = self.query_one("#comment-editor", CommentEditor)
        if event.file_path and event.line is not None:
            from tongs.diff.models import DiffLine, LineType

            dummy_line = DiffLine(
                old_lineno=None,
                new_lineno=event.line,
                content="",
                line_type=LineType.CONTEXT,
            )
            from tongs.diff.models import DiffFile, FileStatus

            dummy_file = DiffFile(
                old_path=event.file_path,
                new_path=event.file_path,
                status=FileStatus.MODIFIED,
                hunks=(),
            )
            editor.open_reply(event.discussion_id, dummy_file, dummy_line, event.author)
        else:
            editor.open_reply_general(event.discussion_id, event.author)

    def action_open_in_browser(self) -> None:
        self.app.open_url(self.mr_summary.web_url)

    def action_yank_url(self) -> None:
        import pyperclip

        try:
            pyperclip.copy(self.mr_summary.web_url)
            self.notify("URL copied to clipboard")
        except Exception:
            self.notify(f"URL: {self.mr_summary.web_url}")

    def action_add_comment(self) -> None:
        editor = self.query_one("#comment-editor", CommentEditor)
        editor.open_general()

    _pending_action: str = ""
    _action_taken: bool = False

    def _check_open(self) -> bool:
        if self.mr_detail and self.mr_detail.state != MRState.OPEN:
            self.notify("MR is no longer open", severity="warning")
            return False
        return True

    def action_approve(self) -> None:
        if not self._check_open():
            return
        if self._pending_action == "approve":
            self._pending_action = ""
            self._do_approve()
        else:
            self._pending_action = "approve"
            self.notify(
                f"Approve !{self.mr_summary.number}? Press A again to confirm.",
                severity="warning",
            )

    def action_unapprove(self) -> None:
        if not self._check_open():
            return
        if self._pending_action == "unapprove":
            self._pending_action = ""
            self._do_unapprove()
        else:
            self._pending_action = "unapprove"
            self.notify(
                f"Revoke approval on !{self.mr_summary.number}? Press U again.",
                severity="warning",
            )

    def action_merge(self) -> None:
        if not self._check_open():
            return
        if self._pending_action == "merge":
            self._pending_action = ""
            self._do_merge()
        else:
            self._pending_action = "merge"
            self.notify(
                f"Merge !{self.mr_summary.number} into {self.mr_summary.target_branch}? "
                "Press M again to confirm.",
                severity="warning",
            )

    def action_close_mr(self) -> None:
        if not self._check_open():
            return
        if self._pending_action == "close":
            self._pending_action = ""
            self._do_close()
        else:
            self._pending_action = "close"
            self.notify(
                f"Close !{self.mr_summary.number}? Press X again to confirm.",
                severity="warning",
            )

    @work(exclusive=True, group="mr-action")
    async def _do_approve(self) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            await client.approve_mr(self.mr_summary.repo_path, self.mr_summary.number)
            self.notify(
                f"[green]Approved !{self.mr_summary.number}[/]",
                severity="information",
            )
            self._action_taken = True
            self._load_detail()
        except Exception as exc:
            self.notify(f"Approve failed: {exc}", severity="error")

    @work(exclusive=True, group="mr-action")
    async def _do_unapprove(self) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            if not client.supports_unapprove:
                self.notify("Unapprove not supported on this forge", severity="warning")
                return
            await client.unapprove_mr(self.mr_summary.repo_path, self.mr_summary.number)
            self.notify(
                f"[yellow]Approval revoked on !{self.mr_summary.number}[/]",
                severity="information",
            )
            self._action_taken = True
            self._load_detail()
        except Exception as exc:
            self.notify(f"Unapprove failed: {exc}", severity="error")

    @work(exclusive=True, group="mr-action")
    async def _do_merge(self) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            await client.merge_mr(self.mr_summary.repo_path, self.mr_summary.number)
            self.notify(
                f"[green]Merged !{self.mr_summary.number}[/]",
                severity="information",
            )
            self._action_taken = True
            self._load_detail()
        except Exception as exc:
            self.notify(f"Merge failed: {exc}", severity="error")

    @work(exclusive=True, group="mr-action")
    async def _do_close(self) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            await client.close_mr(self.mr_summary.repo_path, self.mr_summary.number)
            self.notify(
                f"[yellow]Closed !{self.mr_summary.number}[/]",
                severity="information",
            )
            self._action_taken = True
            self._load_detail()
        except Exception as exc:
            self.notify(f"Close failed: {exc}", severity="error")

    def on_comment_requested(self, event: CommentRequested) -> None:
        """Handle comment request from DiffPanel."""
        if event.mode == CommentMode.SUGGEST and event.file and event.line:
            self._open_suggestion(event)
            return
        editor = self.query_one("#comment-editor", CommentEditor)
        if event.file and event.line:
            editor.open_inline(event.file, event.line)
        else:
            editor.open_general()

    def _open_suggestion(self, event: CommentRequested) -> None:
        """Open external editor for suggesting changes."""
        import os
        import shlex
        import shutil
        import subprocess
        import tempfile

        editor_cmd = None
        for var in ("VISUAL", "EDITOR"):
            v = os.environ.get(var)
            if v:
                editor_cmd = v
                break
        if not editor_cmd:
            for cmd in ("nvim", "vim", "vi", "nano"):
                if shutil.which(cmd):
                    editor_cmd = cmd
                    break
        if not editor_cmd:
            self.notify("No external editor found. Set $EDITOR.")
            return

        lines = event.context_lines or ([event.line] if event.line else [])
        new_side_lines = extract_new_side_lines(lines)
        if not new_side_lines:
            self.notify("Cannot suggest: no new-side lines in selection")
            return
        original_code = "\n".join(dl.content for dl in new_side_lines)
        file_path = event.file.new_path if event.file else "unknown"

        template = build_suggestion_template(original_code)
        n_original = len(new_side_lines)

        ext = os.path.splitext(file_path)[1] or ".txt"
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="tongs-suggest-")
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(template)

            with self.app.suspend():
                subprocess.run([*shlex.split(editor_cmd), tmp_path], check=False)

            with open(tmp_path) as f:
                edited = f.read()

            comment_text, suggested_code = parse_suggestion_template(edited)

            if not suggested_code or suggested_code == original_code.strip():
                self.notify("No changes made, suggestion cancelled.")
                return

            forge_type = self.mr_summary.forge_host.forge_type
            body = format_suggestion_block(
                suggested_code, n_original, forge_type, comment_text
            )

            from tongs.diff.position import position_from_diff_line

            pos_line, start_line, start_side = resolve_suggestion_position(
                new_side_lines, forge_type
            )
            position = position_from_diff_line(event.file, pos_line)
            self._post_inline_comment(
                body, position, start_line=start_line, start_side=start_side
            )
        except Exception as exc:
            self.notify(f"Suggestion failed: {exc}", severity="error")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def on_comment_submitted(self, event: CommentSubmitted) -> None:
        """Handle inline comment submission from CommentEditor."""
        self._post_inline_comment(event.body, event.position)

    def on_general_comment_submitted(self, event: GeneralCommentSubmitted) -> None:
        """Handle general MR comment submission."""
        self._post_general_comment(event.body)

    def on_reply_requested(self, event: ReplyRequested) -> None:
        """Open reply editor for an existing discussion thread."""
        editor = self.query_one("#comment-editor", CommentEditor)
        editor.open_reply(event.discussion_id, event.file, event.line, event.author)

    def on_reply_submitted(self, event: ReplySubmitted) -> None:
        """Post a reply to an existing discussion thread."""
        self._post_reply(event.discussion_id, event.body)

    def on_resolve_requested(self, event: ResolveRequested) -> None:
        """Resolve or unresolve a discussion thread."""
        self._resolve_thread(event.discussion_id, event.resolved)

    @work(exclusive=True, group="mr-comment")
    async def _post_reply(self, discussion_id: str, body: str) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            await client.reply_to_discussion(
                self.mr_summary.repo_path,
                self.mr_summary.number,
                discussion_id,
                body,
            )
            self.notify("[green]Reply posted[/]", severity="information")
            self._diff_loaded = False
            self._discussions_loaded = False
        except Exception as exc:
            self.notify(f"Failed to post reply: {exc}", severity="error")

    @work(exclusive=True, group="mr-comment")
    async def _resolve_thread(self, discussion_id: str, resolved: bool) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            await client.resolve_discussion(
                self.mr_summary.repo_path,
                self.mr_summary.number,
                discussion_id,
                resolved,
            )
            action = "Resolved" if resolved else "Reopened"
            self.notify(f"[green]{action} thread[/]", severity="information")
            self._diff_loaded = False
            self._discussions_loaded = False
        except Exception as exc:
            self.notify(f"Failed to resolve thread: {exc}", severity="error")

    @work(exclusive=True, group="mr-comment")
    async def _post_general_comment(self, body: str) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            await client.add_comment(
                self.mr_summary.repo_path, self.mr_summary.number, body
            )
            self.notify("[green]Comment posted[/]", severity="information")
        except Exception as exc:
            self.notify(f"Failed to post comment: {exc}", severity="error")

    @work(exclusive=True, group="mr-comment")
    async def _post_inline_comment(
        self,
        body: str,
        position,
        start_line: int | None = None,
        start_side: str | None = None,
    ) -> None:
        try:
            client = await self.app.forge_registry.get_client(
                self.mr_summary.forge_host.hostname
            )
            await client.create_inline_comment(
                self.mr_summary.repo_path,
                self.mr_summary.number,
                file_path=position.new_path
                if position.side == "RIGHT"
                else position.old_path,
                line=position.new_line
                if position.side == "RIGHT"
                else position.old_line,
                side=position.side,
                body=body,
                start_line=start_line,
                start_side=start_side,
            )
            self.notify("[green]Comment posted[/]", severity="information")
        except Exception as exc:
            self.notify(f"Failed to post comment: {exc}", severity="error")

    def action_refresh(self) -> None:
        self._diff_loaded = False
        self._commits_loaded = False
        self._discussions_loaded = False
        self._pipeline_loaded = False
        self._cached_diff_files = None
        self._load_detail()
