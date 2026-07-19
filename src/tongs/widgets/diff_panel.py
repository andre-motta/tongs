"""Split-pane diff viewer: file tree on left, diff content on right."""

from __future__ import annotations

import difflib
from enum import Enum

from rich.markup import escape
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.color import Color as TextualColor
from textual.containers import Horizontal, Vertical
from textual import events
from textual.message import Message
from textual.strip import Strip
from textual.style import Style as VisualStyle
from textual.widget import Widget
from textual.widgets import Markdown, OptionList, Static, Tree
from textual.widgets._option_list import Option

from tongs.diff.models import DiffFile, DiffHunk, DiffLine, LineType


class CommentMode(Enum):
    """Mode for inline comment creation."""

    COMMENT = "comment"
    SUGGEST = "suggest"


class CommentRequested(Message):
    """Posted when the user wants to comment from the diff view."""

    def __init__(
        self,
        file: DiffFile | None = None,
        line: DiffLine | None = None,
        mode: CommentMode = CommentMode.COMMENT,
        context_lines: list[DiffLine] | None = None,
    ) -> None:
        super().__init__()
        self.file = file
        self.line = line
        self.mode = mode
        self.context_lines = context_lines


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


class DiffOptionList(OptionList):
    """Per-line interactive diff list with cursor navigation."""

    DEFAULT_CSS = """
    DiffOptionList {
        height: 1fr;
        border: none;
        padding: 0;
        & > .option-list--option {
            padding: 0;
        }
        & > .option-list--option-highlighted {
            color: $foreground;
            background: $foreground 15%;
        }
        &:focus > .option-list--option-highlighted {
            color: $foreground;
            background: $foreground 15%;
        }
        & > .option-list--option-disabled {
            color: $text-disabled;
        }
    }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("J", "extend_down", "Sel down", show=False),
        Binding("K", "extend_up", "Sel up", show=False),
        Binding("escape", "clear_selection", "Clear sel", show=False),
        Binding("c", "comment", "Comment", show=True),
        Binding("f3", "suggest", "Suggest", show=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(compact=True, markup=False, **kwargs)
        self._line_map: dict[int, DiffLine] = {}
        self._line_types: dict[int, LineType] = {}
        self._current_file: DiffFile | None = None
        self._selection_anchor: int | None = None

    _ADDITION_BG = VisualStyle(background=TextualColor(0, 40, 0))
    _DELETION_BG = VisualStyle(background=TextualColor(40, 0, 0))
    _SELECTION_BG = VisualStyle(background=TextualColor(0, 50, 100))

    def render_line(self, y: int) -> Strip:
        line_number = self.scroll_offset.y + y
        try:
            option_index, line_offset = self._lines[line_number]
            option = self.options[option_index]
        except IndexError:
            return Strip.blank(
                self.scrollable_content_region.width,
                self.get_visual_style("option-list--option").rich_style,
            )

        mouse_over = self._mouse_hovering_over == option_index
        component_class = ""
        if option.disabled:
            component_class = "option-list--option-disabled"
        elif self.highlighted == option_index:
            component_class = "option-list--option-highlighted"
        elif mouse_over:
            component_class = "option-list--option-hover"

        if component_class:
            style = self.get_visual_style("option-list--option", component_class)
        else:
            style = self.get_visual_style("option-list--option")

        if component_class != "option-list--option-highlighted":
            if self._in_selection_range(option_index):
                style = style + self._SELECTION_BG
            elif not option.disabled:
                line_type = self._line_types.get(option_index)
                if line_type == LineType.ADDITION:
                    style = style + self._ADDITION_BG
                elif line_type == LineType.DELETION:
                    style = style + self._DELETION_BG

        strips = self._get_option_render(option, style)
        try:
            strip = strips[line_offset]
        except IndexError:
            return Strip.blank(
                self.scrollable_content_region.width,
                self.get_visual_style("option-list--option").rich_style,
            )
        return strip

    def _in_selection_range(self, option_index: int) -> bool:
        """Check whether an option index falls within the active visual selection."""
        if self._selection_anchor is None:
            return False
        cursor = self.highlighted
        if cursor is None:
            return False
        lo = min(self._selection_anchor, cursor)
        hi = max(self._selection_anchor, cursor)
        return lo <= option_index <= hi

    def _get_selection_lines(self) -> list[DiffLine]:
        """Return DiffLine objects in the current selection range."""
        if self._selection_anchor is None or self.highlighted is None:
            return []
        lo = min(self._selection_anchor, self.highlighted)
        hi = max(self._selection_anchor, self.highlighted)
        return [self._line_map[i] for i in range(lo, hi + 1) if i in self._line_map]

    # -- key handlers -------------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "clear_selection":
            return self._selection_anchor is not None
        return True

    def action_clear_selection(self) -> None:
        self._selection_anchor = None
        self.refresh()

    def action_comment(self) -> None:
        """Post a CommentRequested message for the highlighted line."""
        if self.highlighted is None:
            return
        dl = self._line_map.get(self.highlighted)
        if dl is None:
            self.app.notify("Move to a code line to comment")
            return

        context = (
            self._get_selection_lines() if self._selection_anchor is not None else None
        )
        self.post_message(
            CommentRequested(
                file=self._current_file,
                line=dl,
                mode=CommentMode.COMMENT,
                context_lines=context,
            )
        )
        self._selection_anchor = None

    def action_suggest(self) -> None:
        """Post a CommentRequested message with SUGGEST mode."""
        if self.highlighted is None:
            return
        dl = self._line_map.get(self.highlighted)
        if dl is None:
            self.app.notify("Move to a code line to suggest")
            return
        if dl.line_type == LineType.DELETION:
            self.app.notify("Cannot suggest on deletion lines")
            return

        context = (
            self._get_selection_lines() if self._selection_anchor is not None else None
        )
        self.post_message(
            CommentRequested(
                file=self._current_file,
                line=dl,
                mode=CommentMode.SUGGEST,
                context_lines=context,
            )
        )
        self._selection_anchor = None

    def _move_cursor_down(self) -> None:
        """Move cursor down via OptionList (no selection logic)."""
        OptionList.action_cursor_down(self)

    def _move_cursor_up(self) -> None:
        """Move cursor up via OptionList (no selection logic)."""
        OptionList.action_cursor_up(self)

    def action_cursor_down(self) -> None:
        self._selection_anchor = None
        self._move_cursor_down()

    def action_cursor_up(self) -> None:
        self._selection_anchor = None
        self._move_cursor_up()

    def action_extend_down(self) -> None:
        """Shift+J: extend selection downward."""
        if self._selection_anchor is None:
            self._selection_anchor = self.highlighted
        self._move_cursor_down()
        self.refresh()

    def action_extend_up(self) -> None:
        """Shift+K: extend selection upward."""
        if self._selection_anchor is None:
            self._selection_anchor = self.highlighted
        self._move_cursor_up()
        self.refresh()

    async def _on_click(self, event: events.Click) -> None:
        """Handle click: Ctrl+Click extends selection, plain click clears."""
        clicked_option: int | None = event.style.meta.get("option")
        if clicked_option is None or self._options[clicked_option].disabled:
            return
        if event.ctrl:
            if self._selection_anchor is None:
                self._selection_anchor = self.highlighted
            self.highlighted = clicked_option
            self.refresh()
        else:
            self._selection_anchor = None
            self.highlighted = clicked_option


class DiffContent(Widget):
    """Container composing DiffOptionList and Markdown preview."""

    DEFAULT_CSS = """
    DiffContent {
        height: 1fr;
    }
    """

    _showing_preview: bool = False
    _current_file: DiffFile | None = None

    def compose(self) -> ComposeResult:
        yield DiffOptionList(id="diff-option-list")
        yield Markdown(id="diff-markdown-preview")

    def on_mount(self) -> None:
        self.query_one("#diff-markdown-preview", Markdown).display = False

    def show_file(self, file: DiffFile) -> None:
        self._showing_preview = False
        self._current_file = file
        self._show_diff(file)

    def _show_diff(self, file: DiffFile) -> None:
        option_list = self.query_one(DiffOptionList)
        option_list.clear_options()
        option_list._line_map.clear()
        option_list._line_types.clear()
        option_list._current_file = file
        option_list._selection_anchor = None

        option_list.display = True
        self.query_one("#diff-markdown-preview", Markdown).display = False

        if file.is_binary:
            option_list.add_option(
                Option(Text("[Binary file]", style=Style(dim=True)), disabled=True)
            )
            return

        if not file.hunks:
            option_list.add_option(
                Option(Text("[No changes]", style=Style(dim=True)), disabled=True)
            )
            return

        renderer = DiffRenderer(file.language)
        option_idx = 0
        first_changed_idx: int | None = None

        for hunk in file.hunks:
            header_text = Text(f"  {hunk.header}", style=Style(bold=True, dim=True))
            option_list.add_option(Option(header_text, disabled=True))
            option_idx += 1

            for dl, text in renderer.render_lines(hunk):
                if dl is None:
                    # Fold marker (non-selectable)
                    option_list.add_option(Option(text, disabled=True))
                elif dl.line_type == LineType.NO_NEWLINE:
                    option_list.add_option(Option(text, disabled=True))
                    option_list._line_types[option_idx] = dl.line_type
                else:
                    option_list.add_option(Option(text))
                    option_list._line_map[option_idx] = dl
                    option_list._line_types[option_idx] = dl.line_type
                    if first_changed_idx is None and dl.line_type in (
                        LineType.ADDITION,
                        LineType.DELETION,
                    ):
                        first_changed_idx = option_idx
                option_idx += 1

        if first_changed_idx is not None:
            option_list.highlighted = first_changed_idx

    def toggle_markdown_preview(self, file: DiffFile) -> None:
        """Toggle between diff view and rendered markdown preview."""
        if not _is_markdown_file(file):
            self.app.notify("Not a markdown file")
            return

        option_list = self.query_one(DiffOptionList)
        md = self.query_one("#diff-markdown-preview", Markdown)

        if self._showing_preview:
            self._showing_preview = False
            option_list.display = True
            md.display = False
            self._show_diff(file)
        else:
            self._showing_preview = True
            option_list.display = False
            md.display = True
            new_content = _reconstruct_new_content(file)
            md.update(new_content if new_content else "*No content to preview*")

    def show_placeholder(self, message: str) -> None:
        option_list = self.query_one(DiffOptionList)
        option_list.clear_options()
        option_list._line_map.clear()
        option_list._line_types.clear()
        option_list.add_option(
            Option(Text(message, style=Style(dim=True)), disabled=True)
        )
        option_list.display = True
        self.query_one("#diff-markdown-preview", Markdown).display = False


class DiffRenderer:
    """Renders diff hunks with syntax highlighting and word-level diffs."""

    def __init__(self, language: str = ""):
        self._language = language or "text"

    CONTEXT_LINES = 3

    def render_lines(self, hunk: DiffHunk) -> list[tuple[DiffLine | None, Text]]:
        """Render a hunk into per-line items.

        Returns a list of ``(DiffLine | None, Text)`` tuples:
        - ``(DiffLine, Text)`` for selectable diff lines.
        - ``(None, Text)`` for fold markers (non-selectable).
        """
        result: list[tuple[DiffLine | None, Text]] = []
        folded = self._fold_context(list(hunk.lines))
        i = 0
        while i < len(folded):
            item = folded[i]

            if isinstance(item, str):
                # Fold marker
                result.append((None, Text(f"         {item}", style=Style(dim=True))))
                i += 1
                continue

            dl = item
            if dl.line_type == LineType.DELETION:
                old_lines, new_lines, consumed = _collect_change_block(folded, i)
                if new_lines:
                    for old_dl, new_dl in zip(old_lines, new_lines):
                        result.append(
                            (
                                old_dl,
                                self._render_word_diff_line(
                                    old_dl, new_dl, is_old=True
                                ),
                            )
                        )
                    for old_dl, new_dl in zip(old_lines, new_lines):
                        result.append(
                            (
                                new_dl,
                                self._render_word_diff_line(
                                    old_dl, new_dl, is_old=False
                                ),
                            )
                        )
                    for extra in old_lines[len(new_lines) :]:
                        result.append((extra, self._render_line(extra)))
                    for extra in new_lines[len(old_lines) :]:
                        result.append((extra, self._render_line(extra)))
                else:
                    for old_dl in old_lines:
                        result.append((old_dl, self._render_line(old_dl)))
                i += consumed
                continue

            result.append((dl, self._render_line(dl)))
            i += 1

        return result

    # -- kept for backward compatibility if anything calls render_hunk ------

    def render_hunk(self, hunk: DiffHunk) -> Text:
        result = Text()
        for _dl, text in self.render_lines(hunk):
            result.append_text(text)
            result.append("\n")
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
        """Render a single diff line using foreground-only styling.

        Background colors are NOT set here; they are applied by
        DiffOptionList.render_line() so the OptionList cursor highlight
        does not conflict.
        """
        gutter = self._gutter(dl)
        content = self._highlight_content(dl.content)

        line = Text()
        line.append_text(gutter)

        if dl.line_type == LineType.ADDITION:
            line.append("+", Style(color="green"))
            line.append_text(content)
        elif dl.line_type == LineType.DELETION:
            line.append("-", Style(color="red"))
            line.append_text(content)
        elif dl.line_type == LineType.NO_NEWLINE:
            line.append(dl.content, Style(dim=True))
        else:
            line.append(" ")
            line.append_text(content)

        return line

    def _render_word_diff_line(
        self, old_dl: DiffLine, new_dl: DiffLine, is_old: bool
    ) -> Text:
        """Render one side of a word-diff pair.

        Changed words are shown bold + underlined. No bgcolor is applied;
        line-level backgrounds come from DiffOptionList.render_line().
        """
        dl = old_dl if is_old else new_dl
        gutter = self._gutter(dl)

        old_words = old_dl.content.split()
        new_words = new_dl.content.split()
        sm = difflib.SequenceMatcher(None, old_words, new_words)

        line = Text()
        line.append_text(gutter)

        if is_old:
            line.append("-", Style(color="red"))
            highlight = Style(color="red", bold=True, underline=True)
        else:
            line.append("+", Style(color="green"))
            highlight = Style(color="green", bold=True, underline=True)

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

    DiffPanel #diff-right-pane {
        width: 1fr;
    }

    DiffPanel #diff-file-header {
        height: auto;
        padding: 0 1;
    }

    DiffPanel DiffContent {
        width: 1fr;
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
            with Vertical(id="diff-right-pane"):
                yield Static(id="diff-file-header")
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
            file = self._files[index]

            header = self.query_one("#diff-file-header", Static)
            header.update(
                f"[bold]{escape(file.new_path)}[/]  "
                f"[green]+{file.additions}[/] [red]-{file.deletions}[/]  "
                f"[dim]{file.language or ''}[/]"
            )

            content = self.query_one("#diff-content", DiffContent)
            content.show_file(file)

    def action_next_file(self) -> None:
        if self._files:
            self._show_file((self._current_index + 1) % len(self._files))

    def action_prev_file(self) -> None:
        if self._files:
            self._show_file((self._current_index - 1) % len(self._files))

    def action_comment(self) -> None:
        """Fallback comment action when DiffOptionList does not have focus."""
        if self._files and 0 <= self._current_index < len(self._files):
            file = self._files[self._current_index]
            line = self._find_first_changed_line(file)
            if line:
                self.post_message(CommentRequested(file=file, line=line))
                return
        self.post_message(CommentRequested())

    def _find_first_changed_line(self, file: DiffFile) -> DiffLine | None:
        for hunk in file.hunks:
            for dl in hunk.lines:
                if dl.line_type in (LineType.ADDITION, LineType.DELETION):
                    return dl
        return None

    def action_preview_markdown(self) -> None:
        if self._files and 0 <= self._current_index < len(self._files):
            content = self.query_one("#diff-content", DiffContent)
            content.toggle_markdown_preview(self._files[self._current_index])
