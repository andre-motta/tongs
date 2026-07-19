"""MR table widget and helper functions."""

from __future__ import annotations

from datetime import datetime, timezone

from textual.widgets import DataTable

from tongs.forges.models import CIStatus, MRSummary


def _ci_icon(status: CIStatus, ascii_mode: bool = False) -> str:
    if ascii_mode:
        return {
            CIStatus.SUCCESS: "[OK]",
            CIStatus.FAILED: "[!!]",
            CIStatus.RUNNING: "[..]",
            CIStatus.PENDING: "[..]",
            CIStatus.CANCELED: "[--]",
            CIStatus.SKIPPED: "[--]",
            CIStatus.UNKNOWN: "[??]",
        }.get(status, "[??]")
    return {
        CIStatus.SUCCESS: "[green]●[/]",
        CIStatus.FAILED: "[red]●[/]",
        CIStatus.RUNNING: "[yellow]▶[/]",
        CIStatus.PENDING: "[dim]○[/]",
        CIStatus.CANCELED: "[dim]—[/]",
        CIStatus.SKIPPED: "[dim]—[/]",
        CIStatus.UNKNOWN: "[dim]?[/]",
    }.get(status, "[dim]?[/]")


def _relative_time(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 0:
        return "now"
    if seconds < 60:
        return "now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


class MRTable(DataTable):
    """MR list table with consistent columns."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._mr_data: dict[str, MRSummary] = {}

    def setup_columns(self) -> None:
        self.add_column("CI", key="ci", width=4)
        self.add_column("#", key="number", width=6)
        self.add_column("Title", key="title")
        self.add_column("Author", key="author", width=14)
        self.add_column("Repo", key="repo", width=35)
        self.add_column("Updated", key="updated", width=8)

    def add_mr_row(self, mr: MRSummary, ascii_mode: bool = False) -> None:
        ci = _ci_icon(mr.ci_status, ascii_mode)
        draft = "[dim]D [/]" if mr.is_draft else "  "
        row_key = f"{mr.forge_host.hostname}:{mr.repo_path}:{mr.number}"
        self._mr_data[row_key] = mr
        self.add_row(
            ci,
            str(mr.number),
            f"{draft}{mr.title}",
            mr.author.username,
            mr.repo_path,
            _relative_time(mr.updated_at),
            key=row_key,
        )

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
