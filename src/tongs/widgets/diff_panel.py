"""Split-pane diff viewer: file tree on left, diff content on right."""

from __future__ import annotations

from rich.markup import escape

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widget import Widget
from textual.widgets import Static, Tree

from tongs.diff.models import DiffFile, DiffLine, LineType


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
            label = f"{icon} {f.new_path}  {stats}"
            self.root.add_leaf(label, data=i)
        self.root.expand()


class DiffContent(VerticalScroll):
    """Scrollable diff content for a single file."""

    def show_file(self, file: DiffFile) -> None:
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

        for hunk in file.hunks:
            hunk_header = Static(
                f"\n[bold dim]{escape(hunk.header)}[/]",
                classes="hunk-header",
            )
            self.mount(hunk_header)

            lines_text = []
            for dl in hunk.lines:
                lines_text.append(_render_diff_line(dl))

            if lines_text:
                block = Static("\n".join(lines_text), classes="diff-block")
                self.mount(block)

    def show_placeholder(self, message: str) -> None:
        self.remove_children()
        self.mount(Static(f"[dim]{message}[/]"))


def _render_diff_line(dl: DiffLine) -> str:
    content = escape(dl.content)
    old = f"{dl.old_lineno:>4}" if dl.old_lineno is not None else "    "
    new = f"{dl.new_lineno:>4}" if dl.new_lineno is not None else "    "
    gutter = f"[dim]{old} {new}[/] "

    if dl.line_type == LineType.ADDITION:
        return f"{gutter}[on dark_green][green]+{content}[/][/]"
    if dl.line_type == LineType.DELETION:
        return f"{gutter}[on dark_red][red]-{content}[/][/]"
    if dl.line_type == LineType.NO_NEWLINE:
        return f"         [dim]{content}[/]"
    return f"{gutter} {content}"


class DiffPanel(Widget):
    """Split-pane diff viewer with file tree and content."""

    DEFAULT_CSS = """
    DiffPanel {
        height: 1fr;
        layout: horizontal;
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
