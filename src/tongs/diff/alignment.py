"""Pure side-by-side alignment for parsed diff hunks."""

from __future__ import annotations

from tongs.diff.models import DiffHunk, DiffLine, LineType, SplitDiffRow


def align_hunk(hunk: DiffHunk) -> tuple[SplitDiffRow, ...]:
    """Project one hunk into immutable side-by-side rows.

    Context lines occupy both cells.  Consecutive change blocks are aligned
    by source order, pairing the deletion and addition runs by index.  The
    block is bounded by context lines (and the hunk itself), so no pair can
    cross a hunk boundary.  Unmatched lines remain on their originating side.

    No-newline markers have no source line number and therefore cannot be
    anchors.  They are retained in a side-specific row adjacent to the
    change block that contains them.
    """

    lines = hunk.lines
    rows: list[SplitDiffRow] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        if line.line_type == LineType.CONTEXT:
            rows.append(SplitDiffRow(old=line, new=line))
            index += 1
            continue

        if line.line_type == LineType.NO_NEWLINE:
            # A marker after context belongs to both visible sides.  It has
            # no line number, so both cells remain intentionally anchorless.
            rows.append(SplitDiffRow(old=line, new=line))
            index += 1
            continue

        if line.line_type not in (LineType.DELETION, LineType.ADDITION):
            # DiffLine currently has no other content types, but preserving a
            # future marker on one side is safer than silently dropping it.
            rows.append(SplitDiffRow(old=line))
            index += 1
            continue

        block_start = index
        while index < len(lines) and lines[index].line_type in (
            LineType.DELETION,
            LineType.ADDITION,
            LineType.NO_NEWLINE,
        ):
            index += 1

        block = lines[block_start:index]
        old_lines: list[DiffLine] = []
        new_lines: list[DiffLine] = []
        marker_rows: list[tuple[int, LineType | None, DiffLine]] = []
        last_side: LineType | None = None
        for block_line in block:
            if block_line.line_type == LineType.DELETION:
                old_lines.append(block_line)
                last_side = LineType.DELETION
            elif block_line.line_type == LineType.ADDITION:
                new_lines.append(block_line)
                last_side = LineType.ADDITION
            else:
                # A marker follows the source line whose newline state it
                # describes.  Capture that side's ordinal while scanning so
                # asymmetric runs and repeated marker objects stay correct.
                if last_side == LineType.DELETION:
                    marker_rows.append((len(old_lines) - 1, last_side, block_line))
                elif last_side == LineType.ADDITION:
                    marker_rows.append((len(new_lines) - 1, last_side, block_line))
                else:
                    marker_rows.append((0, None, block_line))

        block_rows = [
            SplitDiffRow(
                old=old_lines[row_index] if row_index < len(old_lines) else None,
                new=new_lines[row_index] if row_index < len(new_lines) else None,
            )
            for row_index in range(max(len(old_lines), len(new_lines)))
        ]

        if not block_rows:
            # A marker without a neighbouring source line is still data worth
            # exposing, but it carries no anchor on either side.
            rows.extend(
                SplitDiffRow(old=marker, new=marker) for _, _, marker in marker_rows
            )
            continue

        # Keep markers visible and associate each with the side of the nearest
        # source line.  Markers are deliberately emitted as separate rows so a
        # marker can never turn an otherwise empty cell into a comment target.
        if marker_rows:
            markers_by_row: dict[int, list[SplitDiffRow]] = {}
            for row_index, side, marker in marker_rows:
                marker_row = (
                    SplitDiffRow(old=marker, new=None)
                    if side == LineType.DELETION
                    else SplitDiffRow(old=None, new=marker)
                    if side == LineType.ADDITION
                    else SplitDiffRow(old=marker, new=marker)
                )
                markers_by_row.setdefault(
                    min(row_index, len(block_rows) - 1), []
                ).append(marker_row)
            for row_index, row in enumerate(block_rows):
                rows.append(row)
                rows.extend(markers_by_row.get(row_index, ()))
        else:
            rows.extend(block_rows)

    return tuple(rows)
