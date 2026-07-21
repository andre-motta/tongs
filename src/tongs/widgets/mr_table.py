"""MR table widget and helper functions."""

from __future__ import annotations

from textual.binding import Binding
from textual.widgets import DataTable

from tongs.forges.models import CIStatus, MRSummary
from tongs.helpers import ci_icon, relative_time

MR_SORT_KEYS = ("updated", "title", "ci", "author")

_CI_PRIORITY = {
    CIStatus.FAILED: 0,
    CIStatus.RUNNING: 1,
    CIStatus.PENDING: 2,
    CIStatus.SUCCESS: 3,
    CIStatus.CANCELED: 4,
    CIStatus.SKIPPED: 5,
    CIStatus.UNKNOWN: 6,
}


def sort_mrs(mrs: list[MRSummary], key: str) -> list[MRSummary]:
    if key == "title":
        return sorted(mrs, key=lambda m: m.title.lower())
    if key == "ci":
        return sorted(
            mrs, key=lambda m: (_CI_PRIORITY.get(m.ci_status, 9), m.title.lower())
        )
    if key == "author":
        return sorted(mrs, key=lambda m: (m.author.username.lower(), m.title.lower()))
    return sorted(
        mrs, key=lambda m: m.updated_at or m.created_at or m.updated_at, reverse=True
    )


class MRTable(DataTable):
    """MR list table with consistent columns."""

    BINDINGS = [
        Binding("s", "cycle_sort", "Sort", show=True, key_display="s"),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._mr_data: dict[str, MRSummary] = {}
        self._sort_key: str = "updated"

    _show_repo: bool = True

    def setup_columns(self, show_repo: bool = True) -> None:
        self._show_repo = show_repo
        self.add_column("CI", key="ci", width=4)
        self.add_column("#", key="number", width=6)
        self.add_column("Title", key="title")
        self.add_column("Author", key="author", width=14)
        if show_repo:
            self.add_column("Repo", key="repo", width=35)
        self.add_column("Updated", key="updated", width=8)

    def add_mr_row(self, mr: MRSummary, ascii_mode: bool = False) -> None:
        ci = ci_icon(mr.ci_status, ascii_mode)
        draft = "[dim]D [/]" if mr.is_draft else "  "
        row_key = f"{mr.forge_host.hostname}:{mr.repo_path}:{mr.number}"
        self._mr_data[row_key] = mr
        row = [
            ci,
            str(mr.number),
            f"{draft}{mr.title}",
            mr.author.username,
        ]
        if self._show_repo:
            row.append(mr.repo_path)
        row.append(relative_time(mr.updated_at))
        self.add_row(*row, key=row_key)

    def get_selected_mr(self) -> MRSummary | None:
        if self.cursor_row is None or self.row_count == 0:
            return None
        try:
            row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
            return self._mr_data.get(row_key.value)
        except Exception:
            return None

    def action_cycle_sort(self) -> None:
        idx = (
            MR_SORT_KEYS.index(self._sort_key) if self._sort_key in MR_SORT_KEYS else -1
        )
        self._sort_key = MR_SORT_KEYS[(idx + 1) % len(MR_SORT_KEYS)]
        self._rerender_sorted()
        self.app.notify(f"Sort: {self._sort_key}", timeout=2)

    def _rerender_sorted(self) -> None:
        mrs = list(self._mr_data.values())
        sorted_mrs = sort_mrs(mrs, self._sort_key)
        ascii_mode = getattr(self.app, "config", None) and self.app.config.ascii_mode
        super().clear()
        for mr in sorted_mrs:
            self.add_mr_row(mr, ascii_mode=bool(ascii_mode))

    def clear(self, *args, **kwargs) -> None:
        self._mr_data.clear()
        super().clear(*args, **kwargs)
