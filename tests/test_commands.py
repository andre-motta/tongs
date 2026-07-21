"""P0 tests for tongs.commands.TongsCommandProvider."""

from __future__ import annotations

from unittest.mock import MagicMock


from tongs.commands import TongsCommandProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_provider(screen_name: str = "UnknownScreen") -> TongsCommandProvider:
    """Instantiate TongsCommandProvider without going through Textual init."""
    provider = TongsCommandProvider.__new__(TongsCommandProvider)
    mock_screen = MagicMock()
    type(mock_screen).__name__ = screen_name
    mock_app = MagicMock()
    # Provider stores screen/app as private mangled attrs internally,
    # but _get_commands accesses self.screen / self.app which are properties
    # delegating to __screen / __app. Patch them via property on the instance type.
    provider._Provider__screen = mock_screen
    provider._Provider__app = mock_app
    return provider


# ===================================================================
# 1. _global_commands returns expected entries
# ===================================================================

class TestGlobalCommands:
    def test_returns_three_entries(self):
        provider = _make_provider()
        commands = provider._global_commands(MagicMock())
        assert len(commands) == 4

    def test_command_names(self):
        provider = _make_provider()
        commands = provider._global_commands(MagicMock())
        names = [c[0] for c in commands]
        assert "Repos" in names
        assert "Inbox" in names
        assert "Help" in names


# ===================================================================
# 2. _get_commands includes globals for unknown screen
# ===================================================================

class TestGetCommandsUnknown:
    def test_includes_global_for_unknown_screen(self):
        provider = _make_provider("SomeRandomScreen")
        commands = provider._get_commands()
        names = [c[0] for c in commands]
        assert "Repos" in names
        assert "Inbox" in names
        assert "Help" in names
        # Should be exactly the 3 globals, no extra screen-specific commands
        assert len(commands) == 4


# ===================================================================
# 3. _inbox_commands returns expected entries
# ===================================================================

class TestInboxCommands:
    def test_returns_expected_entries(self):
        provider = _make_provider("InboxScreen")
        mock_screen = MagicMock()
        commands = provider._inbox_commands(mock_screen)
        names = [c[0] for c in commands]
        assert "My Reviews" in names
        assert "My MRs" in names
        assert "All Open" in names
        assert "Refresh" in names
        assert "Open in Browser" in names
        assert len(commands) == 5


# ===================================================================
# 4. _mr_detail_commands returns expected entries
# ===================================================================

class TestMRDetailCommands:
    def test_returns_expected_entries(self):
        provider = _make_provider("MRDetailScreen")
        mock_screen = MagicMock()
        commands = provider._mr_detail_commands(mock_screen)
        names = [c[0] for c in commands]
        assert "Approve" in names
        assert "Merge" in names
        assert "Diff" in names
        assert "Comment" in names
        assert "Overview" in names
        assert "Pipeline" in names
        assert "Close" in names
        assert "Copy URL" in names
        assert "Refresh" in names
        assert len(commands) == 13


# ===================================================================
# 5. _get_commands dispatches by screen class name
# ===================================================================

class TestGetCommandsDispatch:
    def test_inbox_screen_includes_inbox_commands(self):
        provider = _make_provider("InboxScreen")
        commands = provider._get_commands()
        names = [c[0] for c in commands]
        # Globals (4) + inbox (5) = 8
        assert len(commands) == 9
        assert "My Reviews" in names
        assert "Repos" in names

    def test_mr_detail_screen_includes_detail_commands(self):
        provider = _make_provider("MRDetailScreen")
        commands = provider._get_commands()
        names = [c[0] for c in commands]
        # Globals (4) + detail (13) = 16
        assert len(commands) == 17
        assert "Approve" in names
        assert "Merge" in names
        assert "Help" in names

    def test_repo_list_screen_includes_repo_commands(self):
        provider = _make_provider("RepoListScreen")
        commands = provider._get_commands()
        names = [c[0] for c in commands]
        # Globals (4) + repo (3) = 7
        assert len(commands) == 7
        assert "Filter Repos" in names
        assert "Cycle Forge" in names
        assert "Repos" in names
