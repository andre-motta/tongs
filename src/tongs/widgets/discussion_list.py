"""Card-based discussion panel for the Discussion tab."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Console
from rich.markdown import Markdown as RichMarkdown
from rich.style import Style
from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from tongs.diff.models import DiffFile, DiffLine
from tongs.forges.models import Discussion


class JumpToDiffDiscussion(Message):
    """Switch to Diff tab and navigate to a discussion's file:line."""

    def __init__(self, discussion_id: str, file_path: str, line: int | None) -> None:
        super().__init__()
        self.discussion_id = discussion_id
        self.file_path = file_path
        self.line = line


class DiscussionReplyRequested(Message):
    """Reply to a discussion from the Discussion tab."""

    def __init__(
        self,
        discussion_id: str,
        file_path: str | None,
        line: int | None,
        author: str,
    ) -> None:
        super().__init__()
        self.discussion_id = discussion_id
        self.file_path = file_path
        self.line = line
        self.author = author


class DiscussionCard(Static):
    """A single discussion rendered as a rich card."""

    DEFAULT_CSS = """
    DiscussionCard {
        margin: 0 0 1 0;
        padding: 1 2;
        border: solid $accent-darken-2;
        background: $surface;
        height: auto;
    }
    DiscussionCard.focused {
        border: solid $accent;
        background: $foreground 8%;
    }
    DiscussionCard.resolved {
        border: dashed $surface-lighten-2;
    }
    DiscussionCard.resolved.focused {
        border: dashed $accent;
        background: $foreground 5%;
    }
    """

    def __init__(self, disc: Discussion, snippet_lines: list[Text] | None = None):
        super().__init__()
        self.disc = disc
        self._snippet_lines = snippet_lines or []

    def on_mount(self) -> None:
        self._render_card()

    def _render_card(self) -> None:
        disc = self.disc
        rc = disc.root_comment
        dim = disc.is_resolved

        header = Text()
        if disc.is_resolved:
            header.append("[resolved] ", Style(dim=True))
        else:
            header.append("* ", Style(color="yellow", bold=True))

        if disc.is_inline and rc.file_path:
            header.append(rc.file_path, Style(bold=not dim, dim=dim))
            line_num = rc.new_line or rc.old_line
            if line_num:
                header.append(f":{line_num}", Style(bold=not dim, dim=dim))
        else:
            header.append("[general]", Style(dim=True))

        header.append("  ")
        reply_count = len(rc.replies)
        if reply_count:
            header.append(
                f"{reply_count} repl{'ies' if reply_count != 1 else 'y'}  ",
                Style(dim=True),
            )
        header.append(_relative_time(rc.created_at), Style(dim=True))

        content = Text()
        content.append_text(header)
        content.append("\n")

        if self._snippet_lines:
            content.append("\n")
            for line in self._snippet_lines:
                content.append_text(line)
                content.append("\n")

        content.append("\n")
        thread_lines = _render_thread(disc)
        for line in thread_lines:
            content.append_text(line)
            content.append("\n")

        action_line = Text()
        action_line.append("[r]", Style(bold=True))
        action_line.append(" Reply  ", Style(dim=True))
        if disc.resolvable:
            action_line.append("[R]", Style(bold=True))
            label = " Unresolve  " if disc.is_resolved else " Resolve  "
            action_line.append(label, Style(dim=True))
        if disc.is_inline:
            action_line.append("[enter]", Style(bold=True))
            action_line.append(" Jump to diff", Style(dim=True))
        content.append_text(action_line)

        self.update(content)

        if disc.is_resolved:
            self.add_class("resolved")
        else:
            self.remove_class("resolved")


class DiscussionPanel(Widget):
    """Card-based discussion panel with keyboard navigation."""

    DEFAULT_CSS = """
    DiscussionPanel {
        height: 1fr;
    }
    DiscussionPanel VerticalScroll {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("j", "next_card", "Down", show=False),
        Binding("k", "prev_card", "Up", show=False),
        Binding("down", "next_card", "Down", show=False),
        Binding("up", "prev_card", "Up", show=False),
        Binding("enter", "jump_to_diff", "Jump to diff", show=True),
        Binding("r", "reply", "Reply", show=True, key_display="r"),
        Binding("R", "resolve", "Resolve", show=True, key_display="R"),
        Binding("f", "cycle_filter", "Filter", show=True, key_display="f"),
        Binding(
            "right_square_bracket",
            "next_unresolved",
            "Next unresolved",
            show=True,
            key_display="]",
        ),
        Binding(
            "left_square_bracket",
            "prev_unresolved",
            "Prev unresolved",
            show=False,
            key_display="[",
        ),
    ]

    _focused_index: reactive[int] = reactive(0)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._discussions: list[Discussion] = []
        self._diff_files: list[DiffFile] = []
        self._filtered: list[Discussion] = []
        self._filter: str = "all"
        self._pending_resolve: str | None = None

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="disc-scroll")

    def set_discussions(
        self, discussions: list[Discussion], diff_files: list[DiffFile] | None = None
    ) -> None:
        self._discussions = discussions
        self._diff_files = diff_files or []
        self._focused_index = 0
        self._render_cards()

    def _render_cards(self) -> None:
        scroll = self.query_one("#disc-scroll", VerticalScroll)
        scroll.remove_children()

        self._filtered = self._apply_filter(self._discussions)
        sorted_discs = self._sort_discussions(self._filtered)

        if not sorted_discs:
            msg = {
                "unresolved": "All discussions resolved",
                "resolved": "No resolved discussions",
            }.get(self._filter, "No discussions on this MR")
            scroll.mount(Static(f"[dim]{msg}[/]"))
            return

        diff_by_path: dict[str, DiffFile] = {f.new_path: f for f in self._diff_files}
        for f in self._diff_files:
            if f.old_path not in diff_by_path:
                diff_by_path[f.old_path] = f

        for i, disc in enumerate(sorted_discs):
            snippet = []
            if disc.is_inline and disc.root_comment.file_path:
                rc = disc.root_comment
                diff_file = diff_by_path.get(rc.file_path)
                if diff_file:
                    target_line = rc.new_line or rc.old_line
                    snippet = render_diff_snippet(diff_file, target_line)

            card = DiscussionCard(disc, snippet_lines=snippet)
            card.id = f"disc-card-{i}"
            if i == self._focused_index:
                card.add_class("focused")
            scroll.mount(card)

    def _apply_filter(self, discussions: list[Discussion]) -> list[Discussion]:
        if self._filter == "unresolved":
            return [d for d in discussions if not d.is_resolved]
        if self._filter == "resolved":
            return [d for d in discussions if d.is_resolved]
        return list(discussions)

    def _sort_discussions(self, discussions: list[Discussion]) -> list[Discussion]:
        def sort_key(d: Discussion) -> tuple[int, str, int]:
            resolved_order = 1 if d.is_resolved else 0
            if d.is_inline:
                fp = d.root_comment.file_path or ""
                ln = d.root_comment.new_line or d.root_comment.old_line or 0
            else:
                fp = "\xff"
                ln = 0
            return (resolved_order, fp, ln)

        return sorted(discussions, key=sort_key)

    def _get_focused_discussion(self) -> Discussion | None:
        sorted_discs = self._sort_discussions(self._filtered)
        if 0 <= self._focused_index < len(sorted_discs):
            return sorted_discs[self._focused_index]
        return None

    def _update_focus(self, old: int, new: int) -> None:
        try:
            old_card = self.query_one(f"#disc-card-{old}", DiscussionCard)
            old_card.remove_class("focused")
        except Exception:
            pass
        try:
            new_card = self.query_one(f"#disc-card-{new}", DiscussionCard)
            new_card.add_class("focused")
            new_card.scroll_visible()
        except Exception:
            pass

    def watch__focused_index(self, old: int, new: int) -> None:
        self._update_focus(old, new)

    def action_next_card(self) -> None:
        sorted_discs = self._sort_discussions(self._filtered)
        if sorted_discs and self._focused_index < len(sorted_discs) - 1:
            self._focused_index += 1

    def action_prev_card(self) -> None:
        if self._focused_index > 0:
            self._focused_index -= 1

    def action_next_unresolved(self) -> None:
        sorted_discs = self._sort_discussions(self._filtered)
        if not sorted_discs:
            self.app.notify("No discussions")
            return
        start = self._focused_index + 1
        for i in range(len(sorted_discs)):
            idx = (start + i) % len(sorted_discs)
            if not sorted_discs[idx].is_resolved:
                self._focused_index = idx
                return
        self.app.notify("No unresolved discussions")

    def action_prev_unresolved(self) -> None:
        sorted_discs = self._sort_discussions(self._filtered)
        if not sorted_discs:
            self.app.notify("No discussions")
            return
        start = self._focused_index - 1
        for i in range(len(sorted_discs)):
            idx = (start - i) % len(sorted_discs)
            if not sorted_discs[idx].is_resolved:
                self._focused_index = idx
                return
        self.app.notify("No unresolved discussions")

    def action_jump_to_diff(self) -> None:
        disc = self._get_focused_discussion()
        if disc is None:
            return
        if disc.is_inline and disc.root_comment.file_path:
            rc = disc.root_comment
            self.post_message(
                JumpToDiffDiscussion(
                    discussion_id=disc.id,
                    file_path=rc.file_path,
                    line=rc.new_line or rc.old_line,
                )
            )
        else:
            self.app.notify("General discussion has no diff location")

    def action_reply(self) -> None:
        disc = self._get_focused_discussion()
        if disc is None:
            self.app.notify("No discussion selected")
            return
        rc = disc.root_comment
        self.post_message(
            DiscussionReplyRequested(
                discussion_id=disc.id,
                file_path=rc.file_path if disc.is_inline else None,
                line=(rc.new_line or rc.old_line) if disc.is_inline else None,
                author=rc.author.username,
            )
        )

    def action_resolve(self) -> None:
        disc = self._get_focused_discussion()
        if disc is None:
            self.app.notify("No discussion selected")
            return
        if not disc.resolvable:
            self.app.notify("Thread resolution not supported")
            return
        from tongs.widgets.diff_panel import ResolveRequested

        if self._pending_resolve == disc.id:
            self._pending_resolve = None
            self.post_message(ResolveRequested(disc.id, resolved=not disc.is_resolved))
        else:
            self._pending_resolve = disc.id
            action = "Unresolve" if disc.is_resolved else "Resolve"
            self.app.notify(
                f"{action} thread by @{disc.root_comment.author.username}? Press R again."
            )

    def action_cycle_filter(self) -> None:
        cycle = {"all": "unresolved", "unresolved": "resolved", "resolved": "all"}
        self._filter = cycle[self._filter]
        self._focused_index = 0
        self._render_cards()
        self.app.notify(f"Filter: {self._filter}")


def render_diff_snippet(
    file: DiffFile, target_line: int | None, context: int = 2
) -> list[Text]:
    """Extract and render diff lines around a target line number."""
    if target_line is None or not file.hunks:
        return []

    from tongs.widgets.diff_panel import DiffRenderer

    renderer = DiffRenderer(file.language)
    matching_lines: list[tuple[int, DiffLine]] = []

    for hunk in file.hunks:
        for i, dl in enumerate(hunk.lines):
            if dl.new_lineno == target_line or dl.old_lineno == target_line:
                start = max(0, i - context)
                end = min(len(hunk.lines), i + context + 1)
                for j in range(start, end):
                    matching_lines.append((j, hunk.lines[j]))
                break

    if not matching_lines:
        return []

    seen = set()
    result: list[Text] = []
    for _idx, dl in matching_lines:
        key = (dl.old_lineno, dl.new_lineno, dl.content)
        if key in seen:
            continue
        seen.add(key)
        line = renderer._render_line(dl)
        is_target = dl.new_lineno == target_line or dl.old_lineno == target_line
        if is_target:
            marker = Text("> ", Style(color="yellow", bold=True))
            combined = Text()
            combined.append_text(marker)
            combined.append_text(line)
            result.append(combined)
        else:
            padded = Text("  ")
            padded.append_text(line)
            result.append(padded)

    return result


def _render_thread(disc: Discussion) -> list[Text]:
    """Render a full discussion thread with Rich Markdown."""
    lines: list[Text] = []
    rc = disc.root_comment

    for comment in [rc, *rc.replies]:
        if comment is not rc:
            blank = Text()
            lines.append(blank)

        is_reply = comment is not rc
        author_line = Text()
        indent = "  " if is_reply else ""
        author_line.append(
            f"{indent}@{comment.author.username}  {_relative_time(comment.created_at)}",
            Style(bold=not is_reply, dim=is_reply),
        )
        lines.append(author_line)

        if comment.body:
            console = Console(width=80)
            md = RichMarkdown(comment.body)
            for segments in console.render_lines(md, console.options.update_width(70)):
                line = Text()
                if is_reply:
                    line.append("  ")
                for seg in segments:
                    if seg.text and seg.text != "\n":
                        line.append(seg.text, seg.style)
                lines.append(line)

    return lines


def _relative_time(dt: datetime) -> str:
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
