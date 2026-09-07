"""Legacy half of a dual-group fixture."""

from __future__ import annotations

from tongs.plugins.base import TongsPlugin


class TerminalProvider(TongsPlugin):
    name = "dual"
    version = "1.0"
