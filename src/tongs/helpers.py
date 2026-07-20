"""Shared utility helpers used across tongs widgets and views."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.style import Style

from tongs.forges.models import CIStatus


def relative_time(dt: datetime | None) -> str:
    """Format a datetime as a relative time string (e.g., '3m ago')."""
    if dt is None:
        return ""
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


def format_duration(seconds: int | float | None) -> str:
    """Format seconds as '45s', '2m 34s', '1h 05m'."""
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins:02d}m"


_CI_STYLES = {
    CIStatus.SUCCESS: ("●", Style(color="green")),
    CIStatus.FAILED: ("●", Style(color="red")),
    CIStatus.RUNNING: ("▶", Style(color="yellow")),
    CIStatus.PENDING: ("○", Style(dim=True)),
    CIStatus.CANCELED: ("—", Style(dim=True)),
    CIStatus.SKIPPED: ("—", Style(dim=True)),
    CIStatus.UNKNOWN: ("?", Style(dim=True)),
}

_CI_MARKUP = {
    CIStatus.SUCCESS: "[green]●[/]",
    CIStatus.FAILED: "[red]●[/]",
    CIStatus.RUNNING: "[yellow]▶[/]",
    CIStatus.PENDING: "[dim]○[/]",
    CIStatus.CANCELED: "[dim]—[/]",
    CIStatus.SKIPPED: "[dim]—[/]",
    CIStatus.UNKNOWN: "[dim]?[/]",
}


def ci_icon_text(status: CIStatus) -> tuple[str, Style]:
    """Return (character, Style) for a CI status icon."""
    return _CI_STYLES.get(status, ("?", Style(dim=True)))


def ci_icon_markup(status: CIStatus) -> str:
    """Return Rich markup string for a CI status icon."""
    return _CI_MARKUP.get(status, "[dim]?[/]")


def ci_icon(status: CIStatus, ascii_mode: bool = False) -> str:
    """Return a Rich markup CI status icon (or ASCII fallback)."""
    if ascii_mode:
        labels = {
            CIStatus.SUCCESS: "[green]OK[/]",
            CIStatus.FAILED: "[red]FAIL[/]",
            CIStatus.RUNNING: "[yellow]RUN[/]",
            CIStatus.PENDING: "[dim]PEND[/]",
            CIStatus.CANCELED: "[dim]CANC[/]",
            CIStatus.SKIPPED: "[dim]SKIP[/]",
            CIStatus.UNKNOWN: "[dim]?[/]",
        }
        return labels.get(status, "[dim]?[/]")
    return ci_icon_markup(status)
