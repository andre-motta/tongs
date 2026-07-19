"""Bottom-docked comment editor widget."""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import tempfile

from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static, TextArea

from tongs.diff.models import DiffFile, DiffLine
from tongs.diff.position import DiffPosition, position_from_diff_line


class CommentSubmitted(Message):
    """Fired when an inline comment is submitted."""

    def __init__(self, body: str, position: DiffPosition) -> None:
        super().__init__()
        self.body = body
        self.position = position


class GeneralCommentSubmitted(Message):
    """Fired when a general MR comment is submitted."""

    def __init__(self, body: str) -> None:
        super().__init__()
        self.body = body


class ReplySubmitted(Message):
    """Fired when a reply to an existing discussion is submitted."""

    def __init__(self, discussion_id: str, body: str) -> None:
        super().__init__()
        self.discussion_id = discussion_id
        self.body = body


class CommentEditor(Widget):
    """Bottom-docked comment editor. Shows below content, not as a modal."""

    DEFAULT_CSS = """
    CommentEditor {
        dock: bottom;
        height: auto;
        max-height: 40%;
        border-top: solid $accent;
        background: $surface;
        padding: 0 1;
        display: none;
    }

    CommentEditor TextArea {
        height: auto;
        min-height: 3;
        max-height: 10;
    }

    CommentEditor .editor-header {
        height: 1;
        background: $accent 15%;
        padding: 0 1;
    }

    CommentEditor .editor-hint {
        height: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("ctrl+s", "submit", "Submit", show=True, priority=True),
        Binding("ctrl+j", "submit", "Submit", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=True, priority=True),
        Binding("f2", "external_editor", "Editor (F2)", show=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._mode: str = "general"
        self._file: DiffFile | None = None
        self._line: DiffLine | None = None
        self._position: DiffPosition | None = None
        self._discussion_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="editor-header", classes="editor-header")
        yield TextArea("", id="comment-input")
        yield Static(
            "[dim]Ctrl+S / Enter submit | Esc cancel | F2 external editor[/]",
            classes="editor-hint",
        )

    def open_general(self) -> None:
        """Open for a general MR comment."""
        self._cancel_pending = False
        self._mode = "general"
        self._file = None
        self._line = None
        self._position = None
        header = self.query_one("#editor-header", Static)
        header.update("[bold]Add comment[/]")
        text_area = self.query_one("#comment-input", TextArea)
        text_area.clear()
        self.display = True
        text_area.focus()

    def open_inline(self, file: DiffFile, line: DiffLine) -> None:
        """Open for an inline comment on a specific diff line."""
        self._cancel_pending = False
        self._mode = "inline"
        self._file = file
        self._line = line
        self._position = position_from_diff_line(file, line)
        old = line.old_lineno or ""
        new = line.new_lineno or ""
        header = self.query_one("#editor-header", Static)
        header.update(f"[bold]Comment on {file.new_path}:{new or old}[/]")
        text_area = self.query_one("#comment-input", TextArea)
        text_area.clear()
        self.display = True
        text_area.focus()

    def open_reply(
        self, discussion_id: str, file: DiffFile, line: DiffLine, author: str = ""
    ) -> None:
        """Open for replying to an existing discussion thread."""
        self._cancel_pending = False
        self._mode = "reply"
        self._file = file
        self._line = line
        self._discussion_id = discussion_id
        self._position = None
        line_num = line.new_lineno or line.old_lineno or ""
        who = f"@{author} on " if author else ""
        header = self.query_one("#editor-header", Static)
        header.update(f"[bold]Reply to {who}{file.new_path}:{line_num}[/]")
        text_area = self.query_one("#comment-input", TextArea)
        text_area.clear()
        self.display = True
        text_area.focus()

    def action_submit(self) -> None:
        text_area = self.query_one("#comment-input", TextArea)
        body = text_area.text.strip()
        if not body:
            self.app.notify("Comment cannot be empty")
            return
        if self._mode == "reply" and self._discussion_id:
            self.post_message(ReplySubmitted(self._discussion_id, body))
        elif self._mode == "inline" and self._position:
            self.post_message(CommentSubmitted(body, self._position))
        else:
            self.post_message(GeneralCommentSubmitted(body))
        self.display = False

    _cancel_pending: bool = False

    def action_cancel(self) -> None:
        text_area = self.query_one("#comment-input", TextArea)
        if text_area.text.strip() and not self._cancel_pending:
            self._cancel_pending = True
            self.app.notify("Press Esc again to discard comment", severity="warning")
            return
        self._cancel_pending = False
        self.display = False

    def action_external_editor(self) -> None:
        if platform.system() == "Windows":
            self.app.notify("External editor not supported on Windows")
            return

        editor = self._find_editor()
        if not editor:
            self.app.notify("No external editor found. Set $EDITOR.")
            return

        text_area = self.query_one("#comment-input", TextArea)
        current_text = text_area.text
        tmp_path = None

        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".md", prefix="tongs-comment-")
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(current_text)

            with self.app.suspend():
                subprocess.run([*shlex.split(editor), tmp_path], check=False)

            with open(tmp_path) as f:
                new_text = f.read()

            text_area.clear()
            text_area.insert(new_text)
        except Exception as exc:
            self.app.notify(f"Editor failed: {exc}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _find_editor(self) -> str | None:
        for var in ("VISUAL", "EDITOR"):
            editor = os.environ.get(var)
            if editor:
                return editor
        for cmd in ("nvim", "vim", "vi", "nano"):
            if shutil.which(cmd):
                return cmd
        return None
