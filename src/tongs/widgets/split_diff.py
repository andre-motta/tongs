"""Stable mode and selection contracts for split diff widgets."""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from rich.console import Console
from rich.markdown import Markdown as RichMarkdown
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.color import Color as TextualColor
from textual.containers import Horizontal
from textual.message import Message
from textual.strip import Strip
from textual.style import Style as VisualStyle
from textual.widget import Widget
from textual.widgets import OptionList
from textual.widgets._option_list import Option

from tongs.diff.alignment import align_hunk
from tongs.diff.models import DiffFile, DiffLine, LineType
from tongs.forges.models import Discussion
from tongs.helpers import relative_time
from tongs.state.drafts import DiffSide


class DiffViewMode(str, Enum):
    """User-selectable diff layouts."""

    UNIFIED = "unified"
    SPLIT = "split"


@dataclass(frozen=True, slots=True)
class DiffModeState:
    """Requested layout and the layout currently possible at this width."""

    requested: DiffViewMode
    effective: DiffViewMode

    @property
    def narrow_fallback(self) -> bool:
        return self.requested is not self.effective


@dataclass(frozen=True, slots=True)
class DiffSelection:
    """Original source coordinates selected in either diff layout."""

    file: DiffFile
    side: DiffSide
    line: DiffLine
    lines: tuple[DiffLine, ...]

    def __post_init__(self) -> None:
        if not self.lines or self.line not in self.lines:
            raise ValueError("selection lines must include the selected line")
        if not all(is_actionable(line, self.side) for line in self.lines):
            raise ValueError("selection contains a non-actionable diff cell")


class DiffModeChanged(Message):
    """Published when requested or effective diff layout changes."""

    def __init__(self, state: DiffModeState) -> None:
        super().__init__()
        self.state = state


class DiffSelectionChanged(Message):
    """Published when the current original-coordinate selection changes."""

    def __init__(self, selection: DiffSelection | None) -> None:
        super().__init__()
        self.selection = selection


class SplitCommentRequested(Message):
    """Request a legacy immediate comment for an explicit split side."""

    def __init__(self, selection: DiffSelection, *, suggest: bool = False) -> None:
        super().__init__()
        self.selection = selection
        self.suggest = suggest


class SplitReplyRequested(Message):
    """Request a reply using an original-coordinate split selection."""

    def __init__(self, selection: DiffSelection, discussion: Discussion) -> None:
        super().__init__()
        self.selection = selection
        self.discussion = discussion


class SplitResolveRequested(Message):
    """Request a discussion resolution change from the split layout."""

    def __init__(self, discussion_id: str, resolved: bool) -> None:
        super().__init__()
        self.discussion_id = discussion_id
        self.resolved = resolved


class _SplitThreadToggled(Message):
    def __init__(self, discussion_ids: frozenset[str]) -> None:
        super().__init__()
        self.discussion_ids = discussion_ids


class _SplitCursorChanged(Message):
    def __init__(self, side: DiffSide, option_index: int) -> None:
        super().__init__()
        self.side = side
        self.option_index = option_index


class _SplitScrollChanged(Message):
    def __init__(self, side: DiffSide, offset: float) -> None:
        super().__init__()
        self.side = side
        self.offset = offset


def is_actionable(line: DiffLine | None, side: DiffSide) -> bool:
    """Return whether a rendered cell is a source-backed comment anchor."""
    if line is None or line.line_type in {LineType.HUNK_HEADER, LineType.NO_NEWLINE}:
        return False
    if side is DiffSide.OLD:
        return line.old_lineno is not None and line.line_type in {
            LineType.CONTEXT,
            LineType.DELETION,
        }
    return line.new_lineno is not None and line.line_type in {
        LineType.CONTEXT,
        LineType.ADDITION,
    }


class SplitDiffColumn(OptionList):
    """One synchronized, source-side column of a split diff."""

    DEFAULT_CSS = """
    SplitDiffColumn {
        width: 1fr;
        height: 1fr;
        border: none;
        padding: 0;
        overflow-x: auto;
        & > .option-list--option {
            padding: 0;
        }
        & > .option-list--option-highlighted {
            color: $foreground;
            background: $foreground 15%;
        }
        &:focus > .option-list--option-highlighted {
            color: $foreground;
            background: $foreground 20%;
        }
        & > .option-list--option-disabled {
            color: $text-disabled;
        }
    }
    SplitDiffColumn#split-old {
        border-right: solid $accent;
    }
    """

    BINDINGS: ClassVar[list] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("J", "extend_down", "Sel down", show=False),
        Binding("K", "extend_up", "Sel up", show=False),
        Binding("h", "focus_old", "Old side", show=False),
        Binding("l", "focus_new", "New side", show=False),
        Binding("escape", "clear_selection", "Clear sel", show=False),
        Binding("right_square_bracket", "next_comment", "Next comment", show=True),
        Binding("left_square_bracket", "prev_comment", "Prev comment", show=True),
        Binding("d", "toggle_discussion", "Show/Hide thread", show=True),
        Binding("r", "reply_discussion", "Reply", show=True),
        Binding("R", "resolve_discussion", "Resolve", show=True),
        Binding("c", "comment", "Comment", show=True),
        Binding("f3", "suggest", "Suggest", show=False),
    ]

    _ADDITION_BG = VisualStyle(background=TextualColor(0, 40, 0))
    _DELETION_BG = VisualStyle(background=TextualColor(40, 0, 0))
    _SELECTION_BG = VisualStyle(background=TextualColor(0, 50, 100))

    def __init__(self, side: DiffSide, **kwargs: object) -> None:
        self.side = side
        self._syncing = False
        super().__init__(compact=True, markup=False, **kwargs)
        self._line_map: dict[int, DiffLine] = {}
        self._line_types: dict[int, LineType] = {}
        self._comment_map: dict[int, list[Discussion]] = {}
        self._comment_indices: list[int] = []
        self._selection_anchor: int | None = None
        self._current_file: DiffFile | None = None
        self._expanded_threads: set[str] = set()
        self._pending_resolve: str | None = None

    @property
    def selection(self) -> DiffSelection | None:
        if (
            self._current_file is None
            or self.highlighted is None
            or (line := self._line_map.get(self.highlighted)) is None
        ):
            return None
        lines = self._selection_lines() or [line]
        return DiffSelection(self._current_file, self.side, line, tuple(lines))

    def reset(self, file: DiffFile, expanded_threads: set[str]) -> None:
        self.clear_options()
        self._line_map.clear()
        self._line_types.clear()
        self._comment_map.clear()
        self._comment_indices.clear()
        self._selection_anchor = None
        self._current_file = file
        self._expanded_threads = expanded_threads

    def register_line(
        self,
        option_index: int,
        line: DiffLine,
        discussions: list[Discussion],
    ) -> None:
        self._line_map[option_index] = line
        self._line_types[option_index] = line.line_type
        if discussions:
            self._comment_map[option_index] = discussions
            self._comment_indices.append(option_index)

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
        style = (
            self.get_visual_style("option-list--option", component_class)
            if component_class
            else self.get_visual_style("option-list--option")
        )
        if component_class != "option-list--option-highlighted":
            if self._in_selection_range(option_index):
                style += self._SELECTION_BG
            elif not option.disabled:
                line_type = self._line_types.get(option_index)
                if line_type == LineType.ADDITION:
                    style += self._ADDITION_BG
                elif line_type == LineType.DELETION:
                    style += self._DELETION_BG
        strips = self._get_option_render(option, style)
        try:
            return strips[line_offset]
        except IndexError:
            return Strip.blank(
                self.scrollable_content_region.width,
                self.get_visual_style("option-list--option").rich_style,
            )

    def watch_highlighted(self, highlighted: int | None) -> None:
        super().watch_highlighted(highlighted)
        if highlighted is not None and not self._syncing and self.is_mounted:
            self.post_message(_SplitCursorChanged(self.side, highlighted))

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if old_value != new_value and not self._syncing and self.is_mounted:
            self.post_message(_SplitScrollChanged(self.side, new_value))

    def _in_selection_range(self, option_index: int) -> bool:
        if self._selection_anchor is None or self.highlighted is None:
            return False
        low = min(self._selection_anchor, self.highlighted)
        high = max(self._selection_anchor, self.highlighted)
        return low <= option_index <= high and option_index in self._line_map

    def _selection_lines(self) -> list[DiffLine]:
        if self._selection_anchor is None or self.highlighted is None:
            return []
        low = min(self._selection_anchor, self.highlighted)
        high = max(self._selection_anchor, self.highlighted)
        return [
            self._line_map[index]
            for index in range(low, high + 1)
            if index in self._line_map
        ]

    def action_clear_selection(self) -> None:
        self._selection_anchor = None
        self.refresh()
        self.post_message(DiffSelectionChanged(self.selection))

    def action_cursor_down(self) -> None:
        self._selection_anchor = None
        OptionList.action_cursor_down(self)

    def action_cursor_up(self) -> None:
        self._selection_anchor = None
        OptionList.action_cursor_up(self)

    def action_extend_down(self) -> None:
        if self._selection_anchor is None:
            self._selection_anchor = self.highlighted
        OptionList.action_cursor_down(self)
        self.refresh()

    def action_extend_up(self) -> None:
        if self._selection_anchor is None:
            self._selection_anchor = self.highlighted
        OptionList.action_cursor_up(self)
        self.refresh()

    def action_focus_old(self) -> None:
        self._split_view().focus_side(DiffSide.OLD)

    def action_focus_new(self) -> None:
        self._split_view().focus_side(DiffSide.NEW)

    def action_comment(self) -> None:
        selection = self.selection
        if selection is None:
            self.app.notify("Move to a source line to comment")
            return
        self.post_message(SplitCommentRequested(selection))
        self._selection_anchor = None
        self.post_message(DiffSelectionChanged(self.selection))

    def action_suggest(self) -> None:
        if self.side is DiffSide.OLD:
            self.app.notify("Suggestions are available on the new side only")
            return
        selection = self.selection
        if selection is None:
            self.app.notify("Move to a new-side source line to suggest")
            return
        self.post_message(SplitCommentRequested(selection, suggest=True))
        self._selection_anchor = None
        self.post_message(DiffSelectionChanged(self.selection))

    def action_toggle_discussion(self) -> None:
        discussions = self._current_discussions()
        if not discussions:
            return
        ids = frozenset(discussion.id for discussion in discussions)
        if ids & self._expanded_threads:
            self._expanded_threads.difference_update(ids)
        else:
            self._expanded_threads.update(ids)
        self.post_message(_SplitThreadToggled(ids))

    def action_reply_discussion(self) -> None:
        discussion = self._target_discussion()
        selection = self.selection
        if discussion is None or selection is None:
            self.app.notify("No discussion on this line")
            return
        self.post_message(SplitReplyRequested(selection, discussion))

    def action_resolve_discussion(self) -> None:
        discussion = self._target_discussion()
        if discussion is None:
            self.app.notify("No discussion on this line")
            return
        if not discussion.resolvable:
            self.app.notify("Thread resolution not supported")
            return
        if self._pending_resolve == discussion.id:
            self._pending_resolve = None
            self.post_message(
                SplitResolveRequested(discussion.id, not discussion.is_resolved)
            )
            return
        self._pending_resolve = discussion.id
        action = "Unresolve" if discussion.is_resolved else "Resolve"
        self.app.notify(
            f"{action} thread by @{discussion.root_comment.author.username}? Press R again."
        )

    def action_next_comment(self) -> None:
        if not self._comment_indices:
            self.app.notify("No comments on this side")
            return
        current = self.highlighted or 0
        position = bisect.bisect_right(self._comment_indices, current)
        if position >= len(self._comment_indices):
            position = 0
        self.highlighted = self._comment_indices[position]
        self.scroll_to_highlight()

    def action_prev_comment(self) -> None:
        if not self._comment_indices:
            self.app.notify("No comments on this side")
            return
        current = self.highlighted or 0
        position = bisect.bisect_left(self._comment_indices, current) - 1
        if position < 0:
            position = len(self._comment_indices) - 1
        self.highlighted = self._comment_indices[position]
        self.scroll_to_highlight()

    async def _on_click(self, event: events.Click) -> None:
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

    def _current_discussions(self) -> list[Discussion]:
        if self.highlighted is None:
            return []
        return self._comment_map.get(self.highlighted, [])

    def _target_discussion(self) -> Discussion | None:
        discussions = self._current_discussions()
        return next(
            (discussion for discussion in discussions if not discussion.is_resolved),
            discussions[0] if discussions else None,
        )

    def _split_view(self) -> SplitDiffView:
        parent = self.parent
        if not isinstance(parent, SplitDiffView):
            raise TypeError("split diff column is detached")
        return parent


class SplitDiffView(Widget):
    """Two source-side columns with synchronized rows and vertical scrolling."""

    DEFAULT_CSS = """
    SplitDiffView {
        width: 1fr;
        height: 1fr;
    }
    SplitDiffView > Horizontal {
        width: 1fr;
        height: 1fr;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._file: DiffFile | None = None
        self._discussions: list[Discussion] = []
        self._discussion_index: dict[
            tuple[int | None, int | None], list[Discussion]
        ] = {}
        self._highlight_map: dict[int, Text] = {}
        self._expanded_threads: set[str] = set()
        self._active_side = DiffSide.NEW

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield SplitDiffColumn(DiffSide.OLD, id="split-old")
            yield SplitDiffColumn(DiffSide.NEW, id="split-new")

    @property
    def selection(self) -> DiffSelection | None:
        return self._column(self._active_side).selection

    @property
    def expanded_threads(self) -> set[str]:
        return self._expanded_threads

    def show_file(
        self,
        file: DiffFile,
        discussions: list[Discussion],
        highlight_map: dict[int, Text],
        selection: DiffSelection | None = None,
    ) -> None:
        self._file = file
        self._discussions = discussions
        self._discussion_index = _discussion_index(discussions)
        self._highlight_map = highlight_map
        self._populate()
        if selection is not None:
            self.restore_selection(selection)

    def show_placeholder(self, message: str) -> None:
        for column in self._columns():
            column.clear_options()
            column._line_map.clear()
            column._line_types.clear()
            column._comment_map.clear()
            column._comment_indices.clear()
            column._selection_anchor = None
            column._current_file = self._file
            column.add_option(
                Option(Text(message, style=Style(dim=True)), disabled=True)
            )

    def focus_side(self, side: DiffSide) -> None:
        target = self._column(side)
        source = self._column(self._active_side)
        index = source.highlighted
        if index is None or index not in target._line_map:
            index = self._nearest_actionable(target, index or 0)
        if index is None:
            self.app.notify(f"No {side.value}-side source lines in this file")
            return
        self._active_side = side
        self._set_synchronized_highlight(index)
        target.focus()
        self.post_message(DiffSelectionChanged(self.selection))

    def restore_selection(self, selection: DiffSelection) -> bool:
        if self._file is None or selection.file != self._file:
            return False
        column = self._column(selection.side)
        selected = self._find_line(column, selection.line)
        if selected is None:
            return False
        self._active_side = selection.side
        self._set_synchronized_highlight(selected)
        range_indices = [
            index
            for line in selection.lines
            if (index := self._find_line(column, line)) is not None
        ]
        if len(range_indices) > 1:
            column._selection_anchor = (
                min(range_indices)
                if selected == max(range_indices)
                else max(range_indices)
            )
        column.focus()
        column.scroll_to_highlight()
        return True

    def jump_to(
        self, line_number: int, side: DiffSide, discussion_id: str = ""
    ) -> bool:
        if discussion_id:
            self._expanded_threads.add(discussion_id)
            selection = self.selection
            self._populate()
            if selection is not None:
                self.restore_selection(selection)
        column = self._column(side)
        for index, line in column._line_map.items():
            coordinate = line.old_lineno if side is DiffSide.OLD else line.new_lineno
            if coordinate == line_number:
                self._active_side = side
                self._set_synchronized_highlight(index)
                column.focus()
                column.scroll_to_highlight()
                return True
        return False

    def action_comment(self) -> None:
        self._column(self._active_side).action_comment()

    def on__split_cursor_changed(self, event: _SplitCursorChanged) -> None:
        event.stop()
        source = self._column(event.side)
        if not source.has_focus:
            return
        self._active_side = event.side
        other = self._column(
            DiffSide.OLD if event.side is DiffSide.NEW else DiffSide.NEW
        )
        if other.highlighted != event.option_index:
            other._syncing = True
            other.highlighted = event.option_index
            other.scroll_to_highlight()
            other._syncing = False
        self.post_message(DiffSelectionChanged(self.selection))

    def on__split_scroll_changed(self, event: _SplitScrollChanged) -> None:
        event.stop()
        other = self._column(
            DiffSide.OLD if event.side is DiffSide.NEW else DiffSide.NEW
        )
        if other.scroll_y != event.offset:
            other._syncing = True
            other.scroll_to(y=event.offset, animate=False, immediate=True)
            other._syncing = False

    def on__split_thread_toggled(self, event: _SplitThreadToggled) -> None:
        event.stop()
        selection = self.selection
        self._populate()
        if selection is not None:
            self.restore_selection(selection)

    def _populate(self) -> None:
        file = self._file
        if file is None:
            return
        old_column, new_column = self._columns()
        for column in (old_column, new_column):
            column.reset(file, self._expanded_threads)

        old_options = [Option(Text(" OLD", style=Style(bold=True)), disabled=True)]
        new_options = [Option(Text(" NEW", style=Style(bold=True)), disabled=True)]
        option_index = 1
        first_actionable: dict[DiffSide, int | None] = {
            DiffSide.OLD: None,
            DiffSide.NEW: None,
        }
        first_changed: dict[DiffSide, int | None] = {
            DiffSide.OLD: None,
            DiffSide.NEW: None,
        }

        if file.is_binary or not file.hunks:
            message = "[Binary file]" if file.is_binary else "Diff not available"
            placeholder = Option(Text(message, style=Style(dim=True)), disabled=True)
            old_options.append(placeholder)
            new_options.append(
                Option(Text(message, style=Style(dim=True)), disabled=True)
            )
        else:
            for hunk in file.hunks:
                header = Text(f" {hunk.header}", style=Style(bold=True, dim=True))
                old_options.append(Option(header.copy(), disabled=True))
                new_options.append(Option(header.copy(), disabled=True))
                option_index += 1
                for row in align_hunk(hunk):
                    old_discussions = _line_discussions(
                        row.old, DiffSide.OLD, self._discussion_index
                    )
                    new_discussions = _line_discussions(
                        row.new, DiffSide.NEW, self._discussion_index
                    )
                    row_discussions = list(
                        {
                            discussion.id: discussion
                            for discussion in [*old_discussions, *new_discussions]
                        }.values()
                    )
                    old_options.append(
                        self._cell_option(row.old, DiffSide.OLD, old_discussions)
                    )
                    new_options.append(
                        self._cell_option(row.new, DiffSide.NEW, new_discussions)
                    )
                    for side, column, line, cell_discussions in (
                        (DiffSide.OLD, old_column, row.old, old_discussions),
                        (DiffSide.NEW, new_column, row.new, new_discussions),
                    ):
                        if is_actionable(line, side):
                            assert line is not None
                            column.register_line(option_index, line, cell_discussions)
                            if first_actionable[side] is None:
                                first_actionable[side] = option_index
                            if (
                                line.line_type in {LineType.ADDITION, LineType.DELETION}
                                and first_changed[side] is None
                            ):
                                first_changed[side] = option_index
                    option_index += 1

                    expanded = [
                        discussion
                        for discussion in row_discussions
                        if discussion.id in self._expanded_threads
                    ]
                    for thread_line in _thread_lines(expanded):
                        old_options.append(Option(Text(""), disabled=True))
                        new_options.append(Option(thread_line, disabled=True))
                        option_index += 1

        old_column.add_options(old_options)
        new_column.add_options(new_options)
        preferred = (
            first_changed[self._active_side] or first_actionable[self._active_side]
        )
        if preferred is None:
            other_side = (
                DiffSide.OLD if self._active_side is DiffSide.NEW else DiffSide.NEW
            )
            preferred = first_changed[other_side] or first_actionable[other_side]
            if preferred is not None:
                self._active_side = other_side
        if preferred is not None:
            self._set_synchronized_highlight(preferred)

    def _cell_option(
        self,
        line: DiffLine | None,
        side: DiffSide,
        discussions: list[Discussion],
    ) -> Option:
        if line is None:
            return Option(Text("      ", style=Style(dim=True)), disabled=True)
        if line.line_type == LineType.NO_NEWLINE:
            return Option(
                Text(f"      {line.content}", style=Style(dim=True)), disabled=True
            )
        number = line.old_lineno if side is DiffSide.OLD else line.new_lineno
        if number is None:
            return Option(Text("      ", style=Style(dim=True)), disabled=True)
        marker = "*" if discussions else " "
        prefix = (
            "-"
            if line.line_type == LineType.DELETION
            else "+"
            if line.line_type == LineType.ADDITION
            else " "
        )
        text = Text(f"{number:>5} {marker} ", style=Style(dim=True))
        prefix_style = (
            Style(color="red")
            if prefix == "-"
            else Style(color="green")
            if prefix == "+"
            else Style()
        )
        text.append(prefix, prefix_style)
        highlighted = self._highlight_map.get(id(line))
        text.append_text(
            highlighted.copy() if highlighted is not None else Text(line.content)
        )
        return Option(text, disabled=not is_actionable(line, side))

    def _set_synchronized_highlight(self, option_index: int) -> None:
        for column in self._columns():
            if column.highlighted != option_index:
                column._syncing = True
                column.highlighted = option_index
                column._syncing = False

    @staticmethod
    def _nearest_actionable(column: SplitDiffColumn, origin: int) -> int | None:
        if not column._line_map:
            return None
        return min(column._line_map, key=lambda index: abs(index - origin))

    @staticmethod
    def _find_line(column: SplitDiffColumn, target: DiffLine) -> int | None:
        return next(
            (
                index
                for index, line in column._line_map.items()
                if line is target or line == target
            ),
            None,
        )

    def _column(self, side: DiffSide) -> SplitDiffColumn:
        return self.query_one(
            "#split-old" if side is DiffSide.OLD else "#split-new",
            SplitDiffColumn,
        )

    def _columns(self) -> tuple[SplitDiffColumn, SplitDiffColumn]:
        return (
            self.query_one("#split-old", SplitDiffColumn),
            self.query_one("#split-new", SplitDiffColumn),
        )


def _discussion_index(
    discussions: list[Discussion],
) -> dict[tuple[int | None, int | None], list[Discussion]]:
    index: dict[tuple[int | None, int | None], list[Discussion]] = {}
    for discussion in discussions:
        if discussion.is_inline:
            root = discussion.root_comment
            index.setdefault((root.old_line, root.new_line), []).append(discussion)
    return index


def _line_discussions(
    line: DiffLine | None,
    side: DiffSide,
    index: dict[tuple[int | None, int | None], list[Discussion]],
) -> list[Discussion]:
    if line is None:
        return []
    exact = index.get((line.old_lineno, line.new_lineno), [])
    if exact:
        return exact
    if (
        side is DiffSide.NEW
        and line.new_lineno is not None
        and (new_side := index.get((None, line.new_lineno), []))
    ):
        return new_side
    if side is DiffSide.OLD and line.old_lineno is not None:
        return index.get((line.old_lineno, None), [])
    return []


def _thread_lines(discussions: list[Discussion]) -> list[Text]:
    lines: list[Text] = []
    console = Console(width=80)
    for discussion in discussions:
        root = discussion.root_comment
        state = "resolved" if discussion.is_resolved else "open"
        lines.append(
            Text(
                f" [{state}] @{root.author.username} {relative_time(root.created_at)}",
                style=Style(
                    dim=discussion.is_resolved, bold=not discussion.is_resolved
                ),
            )
        )
        for segments in console.render_lines(
            RichMarkdown(root.body or ""), console.options.update_width(60)
        ):
            text = Text("   ")
            for segment in segments:
                if segment.text and segment.text != "\n":
                    text.append(segment.text, segment.style)
            lines.append(text)
        for reply in root.replies:
            lines.append(Text(f"   @{reply.author.username}", style=Style(dim=True)))
            for segments in console.render_lines(
                RichMarkdown(reply.body or ""), console.options.update_width(57)
            ):
                text = Text("     ")
                for segment in segments:
                    if segment.text and segment.text != "\n":
                        text.append(segment.text, segment.style)
                lines.append(text)
    return lines


__all__ = [
    "DiffModeChanged",
    "DiffModeState",
    "DiffSelection",
    "DiffSelectionChanged",
    "DiffSide",
    "DiffViewMode",
    "SplitCommentRequested",
    "SplitDiffColumn",
    "SplitDiffView",
    "SplitReplyRequested",
    "SplitResolveRequested",
    "is_actionable",
]
