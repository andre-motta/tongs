"""Split-pane diff viewer: file tree on left, diff content on right."""

from __future__ import annotations

import difflib

from rich.markup import escape
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widget import Widget
from textual.widgets import Markdown, Static, Tree

from tongs.diff.models import DiffFile, DiffHunk, DiffLine, LineType


class DiffFileTree(Tree):
    """File tree showing changed files with status and stats."""

    def set_files(self, files: list[DiffFile]) -> None:
        self.clear()
        self.root.set_label(f"Changed files ({len(files)})")
        for i, f in enumerate(files):
            icon = {
                "modified": "[yellow]M[/]",
                "added": "[green]A[/]",
                "deleted": "[red]D[/]",
                "renamed": "[blue]R[/]",
            }.get(f.status.value, "?")
            stats = f"[green]+{f.additions}[/] [red]-{f.deletions}[/]"
            label = f"{icon} {escape(f.new_path)}  {stats}"
            self.root.add_leaf(label, data=i)
        self.root.expand()


class DiffContent(VerticalScroll):
    """Scrollable diff content for a single file."""

    _showing_preview: bool = False
    _current_file: DiffFile | None = None

    def show_file(self, file: DiffFile) -> None:
        self._showing_preview = False
        self._current_file = file
        self._show_diff(file)

    def _show_diff(self, file: DiffFile) -> None:
        self.remove_children()

        header = Static(
            f"[bold]{escape(file.new_path)}[/]  "
            f"[green]+{file.additions}[/] [red]-{file.deletions}[/]  "
            f"[dim]{file.language or ''}[/]"
        )
        self.mount(header)

        if file.is_binary:
            self.mount(Static("[dim]Binary file[/]"))
            return

        if not file.hunks:
            self.mount(Static("[dim]No changes[/]"))
            return

        renderer = DiffRenderer(file.language)

        for hunk in file.hunks:
            hunk_header = Static(
                f"\n[bold dim]{escape(hunk.header)}[/]",
                classes="hunk-header",
            )
            self.mount(hunk_header)

            rendered = renderer.render_hunk(hunk)
            if rendered:
                block = Static(rendered, classes="diff-block")
                self.mount(block)

    def toggle_markdown_preview(self, file: DiffFile) -> None:
        """Toggle between diff view and rendered markdown preview."""
        if not _is_markdown_file(file):
            self.app.notify("Not a markdown file")
            return
        if self._showing_preview:
            self._showing_preview = False
            self._show_diff(file)
        else:
            self._showing_preview = True
            self.remove_children()
            self.mount(
                Static("[bold]Rendered preview[/]  [dim]press m to return to diff[/]")
            )
            new_content = _reconstruct_new_content(file)
            if new_content:
                self.mount(Markdown(new_content))
            else:
                self.mount(Static("[dim]No content to preview[/]"))

    def show_placeholder(self, message: str) -> None:
        self.remove_children()
        self.mount(Static(f"[dim]{message}[/]"))


class DiffRenderer:
    """Renders diff hunks with syntax highlighting and word-level diffs."""

    def __init__(self, language: str = ""):
        self._language = language or "text"

    CONTEXT_LINES = 3

    def render_hunk(self, hunk: DiffHunk) -> Text:
        result = Text()
        folded = self._fold_context(list(hunk.lines))
        i = 0
        while i < len(folded):
            item = folded[i]

            if isinstance(item, str):
                result.append(f"         {item}\n", style=Style(dim=True))
                i += 1
                continue

            dl = item
            if dl.line_type == LineType.DELETION:
                old_lines, new_lines, consumed = _collect_change_block(folded, i)
                if new_lines:
                    for old_dl, new_dl in zip(old_lines, new_lines):
                        result.append_text(
                            self._render_word_diff_line(old_dl, new_dl, is_old=True)
                        )
                        result.append("\n")
                    for old_dl, new_dl in zip(old_lines, new_lines):
                        result.append_text(
                            self._render_word_diff_line(old_dl, new_dl, is_old=False)
                        )
                        result.append("\n")
                    for extra in old_lines[len(new_lines) :]:
                        result.append_text(self._render_line(extra))
                        result.append("\n")
                    for extra in new_lines[len(old_lines) :]:
                        result.append_text(self._render_line(extra))
                        result.append("\n")
                else:
                    for old_dl in old_lines:
                        result.append_text(self._render_line(old_dl))
                        result.append("\n")
                i += consumed
                continue

            result.append_text(self._render_line(dl))
            result.append("\n")
            i += 1

        result.rstrip()
        return result

    def _fold_context(self, lines: list[DiffLine]) -> list[DiffLine | str]:
        """Replace long runs of context lines with fold markers."""
        n = self.CONTEXT_LINES
        result: list[DiffLine | str] = []
        ctx_start = None

        for i, dl in enumerate(lines):
            if dl.line_type == LineType.CONTEXT:
                if ctx_start is None:
                    ctx_start = i
            else:
                if ctx_start is not None:
                    ctx_len = i - ctx_start
                    if ctx_len > n * 2:
                        result.extend(lines[ctx_start : ctx_start + n])
                        hidden = ctx_len - n * 2
                        result.append(f"... {hidden} unchanged lines ...")
                        result.extend(lines[i - n : i])
                    else:
                        result.extend(lines[ctx_start:i])
                    ctx_start = None
                result.append(dl)

        if ctx_start is not None:
            ctx_len = len(lines) - ctx_start
            if ctx_len > n * 2:
                result.extend(lines[ctx_start : ctx_start + n])
                hidden = ctx_len - n * 2
                result.append(f"... {hidden} unchanged lines ...")
                result.extend(lines[len(lines) - n :])
            else:
                result.extend(lines[ctx_start:])

        return result

    def _render_line(self, dl: DiffLine) -> Text:
        gutter = self._gutter(dl)
        content = self._highlight_content(dl.content)

        line = Text()
        line.append_text(gutter)

        if dl.line_type == LineType.ADDITION:
            line.append("+", Style(color="green"))
            line.append_text(content)
            line.stylize(Style(bgcolor="rgb(0,40,0)"))
        elif dl.line_type == LineType.DELETION:
            line.append("-", Style(color="red"))
            line.append_text(content)
            line.stylize(Style(bgcolor="rgb(40,0,0)"))
        elif dl.line_type == LineType.NO_NEWLINE:
            line.append(dl.content, Style(dim=True))
        else:
            line.append(" ")
            line.append_text(content)

        return line

    def _render_word_diff_line(
        self, old_dl: DiffLine, new_dl: DiffLine, is_old: bool
    ) -> Text:
        dl = old_dl if is_old else new_dl
        gutter = self._gutter(dl)

        old_words = old_dl.content.split()
        new_words = new_dl.content.split()
        sm = difflib.SequenceMatcher(None, old_words, new_words)

        line = Text()
        line.append_text(gutter)

        if is_old:
            line.append("-", Style(color="red"))
            bg = Style(bgcolor="rgb(40,0,0)")
            highlight = Style(bgcolor="rgb(80,0,0)", bold=True)
        else:
            line.append("+", Style(color="green"))
            bg = Style(bgcolor="rgb(0,40,0)")
            highlight = Style(bgcolor="rgb(0,80,0)", bold=True)

        words = old_words if is_old else new_words
        changed_indices: set[int] = set()

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            if is_old and tag in ("delete", "replace"):
                changed_indices.update(range(i1, i2))
            elif not is_old and tag in ("insert", "replace"):
                changed_indices.update(range(j1, j2))

        for idx, word in enumerate(words):
            if idx > 0:
                line.append(" ")
            if idx in changed_indices:
                line.append(word, highlight)
            else:
                line.append(word)

        line.stylize(bg)
        return line

    def _highlight_content(self, content: str) -> Text:
        if not content or self._language == "text":
            return Text(content)
        try:
            syntax = Syntax(
                content,
                self._language,
                theme="monokai",
                line_numbers=False,
                word_wrap=False,
            )
            highlighted = syntax.highlight(content)
            highlighted.rstrip()
            return highlighted
        except Exception:
            return Text(content)

    def _gutter(self, dl: DiffLine) -> Text:
        old = f"{dl.old_lineno:>4}" if dl.old_lineno is not None else "    "
        new = f"{dl.new_lineno:>4}" if dl.new_lineno is not None else "    "
        return Text(f"{old} {new} ", style=Style(dim=True))


def _collect_change_block(
    lines: list[DiffLine | str], start: int
) -> tuple[list[DiffLine], list[DiffLine], int]:
    """Collect consecutive deletion+addition lines as a change block."""
    old_lines: list[DiffLine] = []
    new_lines: list[DiffLine] = []
    i = start

    while (
        i < len(lines)
        and isinstance(lines[i], DiffLine)
        and lines[i].line_type == LineType.DELETION
    ):
        old_lines.append(lines[i])
        i += 1

    while (
        i < len(lines)
        and isinstance(lines[i], DiffLine)
        and lines[i].line_type == LineType.ADDITION
    ):
        new_lines.append(lines[i])
        i += 1

    return old_lines, new_lines, i - start


def _is_markdown_file(file: DiffFile) -> bool:
    """Check whether a diff file is a Markdown document."""
    return file.language == "markdown" or file.new_path.endswith(".md")


def _reconstruct_new_content(file: DiffFile) -> str:
    """Reconstruct the new-side file content from diff hunks.

    Collects CONTEXT and ADDITION lines (which together represent the
    post-change file) and joins them into a single string.
    """
    lines: list[str] = []
    for hunk in file.hunks:
        for dl in hunk.lines:
            if dl.line_type in (LineType.CONTEXT, LineType.ADDITION):
                lines.append(dl.content)
    return "\n".join(lines)


class DiffPanel(Widget):
    """Split-pane diff viewer with file tree and content."""

    DEFAULT_CSS = """
    DiffPanel {
        height: 1fr;
    }

    DiffPanel DiffFileTree {
        width: 35;
        min-width: 20;
        border-right: solid $accent;
    }

    DiffPanel DiffContent {
        width: 1fr;
    }

    DiffPanel .hunk-header {
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("n", "next_file", "Next file", show=True),
        Binding("shift+n", "prev_file", "Prev file", show=True),
        Binding("m", "preview_markdown", "Preview MD", show=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._files: list[DiffFile] = []
        self._current_index: int = 0

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield DiffFileTree("Files", id="diff-file-tree")
            yield DiffContent(id="diff-content")

    def set_files(self, files: list[DiffFile]) -> None:
        self._files = files
        tree = self.query_one("#diff-file-tree", DiffFileTree)
        tree.set_files(files)
        if files:
            self._show_file(0)
        else:
            content = self.query_one("#diff-content", DiffContent)
            content.show_placeholder("No changes")

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.node.data is not None and isinstance(event.node.data, int):
            self._show_file(event.node.data)

    def _show_file(self, index: int) -> None:
        if 0 <= index < len(self._files):
            self._current_index = index
            content = self.query_one("#diff-content", DiffContent)
            content.show_file(self._files[index])
            content.scroll_home()

    def action_next_file(self) -> None:
        if self._files:
            self._show_file((self._current_index + 1) % len(self._files))

    def action_prev_file(self) -> None:
        if self._files:
            self._show_file((self._current_index - 1) % len(self._files))

    def action_preview_markdown(self) -> None:
        if self._files and 0 <= self._current_index < len(self._files):
            content = self.query_one("#diff-content", DiffContent)
            content.toggle_markdown_preview(self._files[self._current_index])
