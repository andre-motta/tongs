"""Pipeline panel with three-level drill-down: pipelines -> jobs -> log."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from rich.style import Style
from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, RichLog, Static

from tongs.forges.models import CIStatus, Pipeline, PipelineJob


class CancelPipelineRequested(Message):
    def __init__(self, pipeline_id: int) -> None:
        super().__init__()
        self.pipeline_id = pipeline_id


class RetryPipelineRequested(Message):
    def __init__(self, pipeline_id: int) -> None:
        super().__init__()
        self.pipeline_id = pipeline_id


class CancelJobRequested(Message):
    def __init__(self, job_id: int) -> None:
        super().__init__()
        self.job_id = job_id


class RetryJobRequested(Message):
    def __init__(self, job_id: int) -> None:
        super().__init__()
        self.job_id = job_id


class LoadJobsRequested(Message):
    def __init__(self, pipeline: Pipeline) -> None:
        super().__init__()
        self.pipeline = pipeline


class LoadJobLogRequested(Message):
    def __init__(self, job: PipelineJob, pipeline: Pipeline) -> None:
        super().__init__()
        self.job = job
        self.pipeline = pipeline


_CI_STYLES = {
    CIStatus.SUCCESS: ("●", Style(color="green")),
    CIStatus.FAILED: ("●", Style(color="red")),
    CIStatus.RUNNING: ("▶", Style(color="yellow")),
    CIStatus.PENDING: ("○", Style(dim=True)),
    CIStatus.CANCELED: ("—", Style(dim=True)),
    CIStatus.SKIPPED: ("—", Style(dim=True)),
    CIStatus.UNKNOWN: ("?", Style(dim=True)),
}


def _ci_icon_text(status: CIStatus) -> tuple[str, Style]:
    return _CI_STYLES.get(status, ("?", Style(dim=True)))


_CI_MARKUP = {
    CIStatus.SUCCESS: "[green]●[/]",
    CIStatus.FAILED: "[red]●[/]",
    CIStatus.RUNNING: "[yellow]▶[/]",
    CIStatus.PENDING: "[dim]○[/]",
    CIStatus.CANCELED: "[dim]—[/]",
    CIStatus.SKIPPED: "[dim]—[/]",
    CIStatus.UNKNOWN: "[dim]?[/]",
}


def _ci_icon_markup(status: CIStatus) -> str:
    return _CI_MARKUP.get(status, "[dim]?[/]")


def _format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins:02d}m"


def _relative_time(dt: datetime | None) -> str:
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


class PipelineCard(Static):
    """A single pipeline rendered as a card."""

    DEFAULT_CSS = """
    PipelineCard {
        margin: 0 0 1 0;
        padding: 1 2;
        border: solid $accent-darken-2;
        background: $surface;
        height: auto;
    }
    PipelineCard.focused {
        border: solid $accent;
        background: $foreground 8%;
    }
    PipelineCard.failed {
        border: solid $error;
    }
    PipelineCard.running {
        border: solid $warning;
    }
    """

    def __init__(self, pipeline: Pipeline) -> None:
        super().__init__()
        self.pipeline = pipeline

    def on_mount(self) -> None:
        p = self.pipeline
        icon_char, icon_style = _ci_icon_text(p.status)
        content = Text()
        content.append(f"{icon_char} ", icon_style)
        content.append(f"Pipeline #{p.id}", Style(bold=True))
        content.append(f"  {_relative_time(p.created_at)}", Style(dim=True))
        content.append("\n")
        content.append(f"  {p.sha[:7]}  {p.ref}", Style(dim=True))
        if p.source:
            content.append(f"  {p.source}", Style(dim=True))
        dur = _format_duration(p.duration_seconds)
        if dur:
            content.append(f"  {dur}", Style(dim=True))

        self.update(content)

        if p.status == CIStatus.FAILED:
            self.add_class("failed")
        elif p.status == CIStatus.RUNNING:
            self.add_class("running")

    def on_click(self) -> None:
        self.post_message(LoadJobsRequested(self.pipeline))


class JobCard(Static):
    """A single job rendered as a line."""

    DEFAULT_CSS = """
    JobCard {
        padding: 0 2;
        height: auto;
    }
    JobCard.focused {
        background: $foreground 8%;
    }
    """

    def __init__(self, job: PipelineJob) -> None:
        super().__init__()
        self.job = job

    def on_mount(self) -> None:
        j = self.job
        icon_char, icon_style = _ci_icon_text(j.status)
        content = Text()
        content.append(f"  {icon_char} ", icon_style)
        content.append(f"{j.name:<30}", Style(bold=j.status == CIStatus.FAILED))
        dur = _format_duration(j.duration_seconds)
        if dur:
            content.append(f"  {dur}", Style(dim=True))
        if j.status == CIStatus.FAILED:
            content.append("  FAILED", Style(color="red", bold=True))
        if j.allow_failure:
            content.append("  allow failure", Style(dim=True))
        self.update(content)

    def on_click(self) -> None:
        if self.parent and hasattr(self.parent, "parent"):
            panel = self.parent.parent
            if isinstance(panel, PipelinePanel) and panel._current_pipeline:
                self.post_message(
                    LoadJobLogRequested(self.job, panel._current_pipeline)
                )


class PipelinePanel(Widget, can_focus=True):
    """Three-level pipeline viewer: pipelines -> jobs -> log."""

    DEFAULT_CSS = """
    PipelinePanel {
        height: 1fr;
    }
    PipelinePanel VerticalScroll {
        height: 1fr;
    }
    PipelinePanel #job-log-container {
        height: 1fr;
    }
    PipelinePanel RichLog {
        height: 1fr;
    }
    PipelinePanel #log-search-input {
        dock: bottom;
        display: none;
        height: 1;
    }
    """

    BINDINGS = [
        Binding("j", "next_item", "Down", show=False),
        Binding("k", "prev_item", "Up", show=False),
        Binding("down", "next_item", "Down", show=False),
        Binding("up", "prev_item", "Up", show=False),
        Binding("enter", "drill_in", "Open", show=True),
        Binding("escape", "drill_out", "Back", show=True),
        Binding("C", "cancel", "Cancel", show=True, key_display="C"),
        Binding("R", "retry", "Retry", show=True, key_display="R"),
        Binding("o", "open_browser", "Browser", show=True, key_display="o"),
        Binding("f2", "open_in_editor", "Editor", show=False),
        Binding("slash", "search_log", "Search", show=False, key_display="/"),
    ]

    _focused_index: reactive[int] = reactive(0)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._view_level: int = 0
        self._pipelines: list[Pipeline] = []
        self._jobs: list[PipelineJob] = []
        self._current_pipeline: Pipeline | None = None
        self._current_job: PipelineJob | None = None
        self._job_log_text: str = ""
        self._pending_cancel: int | None = None
        self._pending_retry: int | None = None
        self._search_matches: list[int] = []
        self._search_index: int = 0
        self._render_gen: int = 0
        self._saved_pipeline_idx: int = 0
        self._saved_job_idx: int = 0
        self._job_card_map: dict[int, int] = {}

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="pipeline-list-scroll")
        yield VerticalScroll(id="job-list-scroll")
        with Vertical(id="job-log-container"):
            yield Static("", id="job-log-header")
            yield RichLog(id="job-log-content", max_lines=50000, wrap=False)
            yield Input(id="log-search-input", placeholder="Search log...")

    def on_mount(self) -> None:
        self.query_one("#job-list-scroll").display = False
        self.query_one("#job-log-container").display = False

    def set_pipelines(self, pipelines: list[Pipeline]) -> None:
        self._pipelines = pipelines
        self._view_level = 0
        self._focused_index = 0
        self._render_pipeline_list()
        self.focus()

    def _render_pipeline_list(self) -> None:
        self._render_gen += 1
        scroll = self.query_one("#pipeline-list-scroll", VerticalScroll)
        scroll.display = True
        self.query_one("#job-list-scroll").display = False
        self.query_one("#job-log-container").display = False
        scroll.remove_children()

        if not self._pipelines:
            scroll.mount(Static("[dim]No pipelines for this MR[/]"))
            return

        g = self._render_gen
        for i, p in enumerate(self._pipelines):
            card = PipelineCard(p)
            card.id = f"pipeline-card-{g}-{i}"
            if i == self._focused_index:
                card.add_class("focused")
            scroll.mount(card)

    def set_jobs(self, jobs: list[PipelineJob], pipeline: Pipeline) -> None:
        self._jobs = jobs
        self._current_pipeline = pipeline
        self._view_level = 1
        self._focused_index = 0
        self._render_job_list()
        self.focus()

    def _render_job_list(self) -> None:
        self._render_gen += 1
        self.query_one("#pipeline-list-scroll").display = False
        scroll = self.query_one("#job-list-scroll", VerticalScroll)
        scroll.display = True
        self.query_one("#job-log-container").display = False
        scroll.remove_children()

        if self._current_pipeline:
            p = self._current_pipeline
            header = Static(
                f"Pipeline #{p.id}  {_ci_icon_markup(p.status)} {p.status.value}  "
                f"{_format_duration(p.duration_seconds)}"
            )
            scroll.mount(header)

        stages: dict[str, list[tuple[int, PipelineJob]]] = {}
        job_index = 0
        for j in self._jobs:
            stage = j.stage or "default"
            stages.setdefault(stage, []).append((job_index, j))
            job_index += 1

        g = self._render_gen
        card_idx = 0
        self._job_card_map: dict[int, int] = {}
        for stage_name, stage_jobs in stages.items():
            scroll.mount(Static(f"\n  [bold]{stage_name}[/]"))
            for job_idx, job in stage_jobs:
                card = JobCard(job)
                card.id = f"job-card-{g}-{card_idx}"
                if card_idx == self._focused_index:
                    card.add_class("focused")
                scroll.mount(card)
                self._job_card_map[card_idx] = job_idx
                card_idx += 1

    def set_job_log(self, log_text: str, job: PipelineJob, pipeline: Pipeline) -> None:
        self._current_job = job
        self._current_pipeline = pipeline
        self._job_log_text = log_text
        self._view_level = 2
        self._render_job_log()
        self.focus()

    def _render_job_log(self) -> None:
        self.query_one("#pipeline-list-scroll").display = False
        self.query_one("#job-list-scroll").display = False
        container = self.query_one("#job-log-container")
        container.display = True

        job = self._current_job
        if job:
            header = self.query_one("#job-log-header", Static)
            header.update(
                f"Job: {job.name}  {_ci_icon_markup(job.status)} {job.status.value}  "
                f"{_format_duration(job.duration_seconds)}  "
                f"Stage: {job.stage}  "
                f"[dim]F2 open in editor  / search[/]"
            )

        log_widget = self.query_one("#job-log-content", RichLog)
        log_widget.clear()

        lines = self._job_log_text.split("\n")
        for i, line in enumerate(lines):
            line_num = Text(f"{i + 1:>6} ", style=Style(dim=True))
            content = Text.from_ansi(line)
            rendered = Text()
            rendered.append_text(line_num)
            rendered.append_text(content)
            log_widget.write(rendered)

    def _get_focused_pipeline(self) -> Pipeline | None:
        if self._view_level != 0:
            return None
        if 0 <= self._focused_index < len(self._pipelines):
            return self._pipelines[self._focused_index]
        return None

    def _get_focused_job(self) -> PipelineJob | None:
        if self._view_level != 1:
            return None
        job_idx = self._job_card_map.get(self._focused_index)
        if job_idx is not None and 0 <= job_idx < len(self._jobs):
            return self._jobs[job_idx]
        return None

    def watch__focused_index(self, old: int, new: int) -> None:
        g = self._render_gen
        if self._view_level == 0:
            self._update_card_focus(f"pipeline-card-{g}", old, new)
        elif self._view_level == 1:
            self._update_card_focus(f"job-card-{g}", old, new)

    def _update_card_focus(self, prefix: str, old: int, new: int) -> None:
        try:
            old_card = self.query_one(f"#{prefix}-{old}")
            old_card.remove_class("focused")
        except Exception:
            pass
        try:
            new_card = self.query_one(f"#{prefix}-{new}")
            new_card.add_class("focused")
            new_card.scroll_visible()
        except Exception:
            pass

    def _max_index(self) -> int:
        if self._view_level == 0:
            return max(0, len(self._pipelines) - 1)
        elif self._view_level == 1:
            return max(0, len(self._job_card_map) - 1)
        return 0

    def action_next_item(self) -> None:
        if self._focused_index < self._max_index():
            self._focused_index += 1

    def action_prev_item(self) -> None:
        if self._focused_index > 0:
            self._focused_index -= 1

    def action_drill_in(self) -> None:
        if self._view_level == 0:
            p = self._get_focused_pipeline()
            if p:
                self._saved_pipeline_idx = self._focused_index
                self.post_message(LoadJobsRequested(p))
        elif self._view_level == 1:
            j = self._get_focused_job()
            if j and self._current_pipeline:
                self._saved_job_idx = self._focused_index
                self.post_message(LoadJobLogRequested(j, self._current_pipeline))

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "drill_out":
            return self._view_level > 0
        return True

    def action_drill_out(self) -> None:
        if self._view_level == 2:
            self._view_level = 1
            self._focused_index = self._saved_job_idx
            self._render_job_list()
            search_input = self.query_one("#log-search-input", Input)
            search_input.display = False
            self.focus()
        elif self._view_level == 1:
            self._view_level = 0
            self._focused_index = self._saved_pipeline_idx
            self._render_pipeline_list()
            self.focus()

    def action_cancel(self) -> None:
        if self._view_level == 0:
            p = self._get_focused_pipeline()
            if not p or p.status not in (CIStatus.RUNNING, CIStatus.PENDING):
                self.app.notify("Can only cancel running or pending pipelines")
                return
            if self._pending_cancel == p.id:
                self._pending_cancel = None
                self.post_message(CancelPipelineRequested(p.id))
            else:
                self._pending_cancel = p.id
                self.app.notify(f"Cancel pipeline #{p.id}? Press C again.")
        elif self._view_level == 1:
            j = self._get_focused_job()
            if not j or j.status not in (CIStatus.RUNNING, CIStatus.PENDING):
                self.app.notify("Can only cancel running or pending jobs")
                return
            if self._pending_cancel == j.id:
                self._pending_cancel = None
                self.post_message(CancelJobRequested(j.id))
            else:
                self._pending_cancel = j.id
                self.app.notify(f"Cancel job {j.name}? Press C again.")

    def action_retry(self) -> None:
        if self._view_level == 0:
            p = self._get_focused_pipeline()
            if not p or p.status not in (CIStatus.FAILED, CIStatus.CANCELED):
                self.app.notify("Can only retry failed or canceled pipelines")
                return
            if self._pending_retry == p.id:
                self._pending_retry = None
                self.post_message(RetryPipelineRequested(p.id))
            else:
                self._pending_retry = p.id
                self.app.notify(f"Retry pipeline #{p.id}? Press R again.")
        elif self._view_level == 1:
            j = self._get_focused_job()
            if not j or j.status not in (CIStatus.FAILED, CIStatus.CANCELED):
                self.app.notify("Can only retry failed or canceled jobs")
                return
            if self._pending_retry == j.id:
                self._pending_retry = None
                self.post_message(RetryJobRequested(j.id))
            else:
                self._pending_retry = j.id
                self.app.notify(f"Retry job {j.name}? Press R again.")

    def action_open_browser(self) -> None:
        if self._view_level == 0:
            p = self._get_focused_pipeline()
            if p and p.web_url:
                self.app.open_url(p.web_url)
        elif self._view_level == 1:
            j = self._get_focused_job()
            if j and j.web_url:
                self.app.open_url(j.web_url)

    def action_open_in_editor(self) -> None:
        if self._view_level != 2 or not self._job_log_text:
            return
        editor_cmd = None
        for var in ("VISUAL", "EDITOR"):
            v = os.environ.get(var)
            if v:
                editor_cmd = v
                break
        if not editor_cmd:
            for cmd in ("nvim", "vim", "vi", "nano", "less"):
                if shutil.which(cmd):
                    editor_cmd = cmd
                    break
        if not editor_cmd:
            self.app.notify("No editor found. Set $EDITOR.")
            return

        tmp_path = None
        try:
            job_name = self._current_job.name if self._current_job else "job"
            fd, tmp_path = tempfile.mkstemp(suffix=".log", prefix=f"tongs-{job_name}-")
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(self._job_log_text)
            with self.app.suspend():
                subprocess.run([*shlex.split(editor_cmd), tmp_path], check=False)
        except Exception as exc:
            self.app.notify(f"Editor failed: {exc}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def action_search_log(self) -> None:
        if self._view_level != 2:
            return
        search_input = self.query_one("#log-search-input", Input)
        search_input.display = True
        search_input.value = ""
        search_input.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "log-search-input":
            query = event.value.strip()
            event.input.display = False
            if not query:
                return
            self._do_search(query)

    def _do_search(self, query: str) -> None:
        lines = self._job_log_text.split("\n")
        self._search_matches = []
        plain_query = query.lower()
        for i, line in enumerate(lines):
            plain = Text.from_ansi(line).plain.lower()
            if plain_query in plain:
                self._search_matches.append(i)

        if not self._search_matches:
            self.app.notify(f"No matches for '{query}'")
            return

        self._search_index = 0
        log_widget = self.query_one("#job-log-content", RichLog)
        target = self._search_matches[0]
        log_widget.scroll_to(y=target, animate=False)
        total = len(self._search_matches)
        self.app.notify(f"Match 1/{total}: line {target + 1}")
