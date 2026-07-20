"""MR table widget and helper functions."""

from __future__ import annotations

from textual.widgets import DataTable

from tongs.forges.models import MRSummary
from tongs.helpers import ci_icon, relative_time


class MRTable(DataTable):
    """MR list table with consistent columns."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._mr_data: dict[str, MRSummary] = {}

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

    def clear(self, *args, **kwargs) -> None:
        self._mr_data.clear()
        super().clear(*args, **kwargs)
