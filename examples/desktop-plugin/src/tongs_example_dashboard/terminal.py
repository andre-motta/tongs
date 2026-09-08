"""Legacy terminal plugin half of the dual-surface example distribution."""

from __future__ import annotations

from tongs.plugins.base import TongsPlugin


class ExampleDashboardTerminalPlugin(TongsPlugin):
    """Small terminal-only surface retained for backwards compatibility."""

    @property
    def name(self) -> str:
        return "example_dashboard"

    @property
    def version(self) -> str:
        return "0.1.0"

    def get_commands(self) -> list[tuple[str, str, object]]:
        return [
            (
                "Example dashboard (terminal)",
                "Show the deterministic example dashboard in a terminal plugin",
                self._show_dashboard,
            )
        ]

    @staticmethod
    def _show_dashboard() -> None:
        """Keep the legacy command side-effect free for the example fixture."""
