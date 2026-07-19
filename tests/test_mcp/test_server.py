"""Tests for MCP server helpers."""

import pytest

mcp_available = True
try:
    from tongs.mcp.server import _parse_host_repo
except ImportError:
    mcp_available = False

pytestmark = pytest.mark.skipif(not mcp_available, reason="mcp not installed")


class TestParseHostRepo:
    def test_valid_github(self):
        """Standard github.com/owner/repo parses correctly."""
        host, repo = _parse_host_repo("github.com/owner/repo")
        assert host == "github.com"
        assert repo == "owner/repo"

    def test_nested_gitlab(self):
        """Nested GitLab group path is kept intact after the hostname."""
        host, repo = _parse_host_repo("gitlab.com/group/subgroup/repo")
        assert host == "gitlab.com"
        assert repo == "group/subgroup/repo"

    def test_no_slash_raises(self):
        """Input without any slash is rejected."""
        with pytest.raises(ValueError, match="repo_path must be"):
            _parse_host_repo("noslash")

    def test_empty_string_raises(self):
        """Empty string is rejected."""
        with pytest.raises(ValueError, match="repo_path must be"):
            _parse_host_repo("")

    def test_path_traversal_rejected(self):
        """Path traversal segments (..) are rejected by the regex."""
        with pytest.raises(ValueError, match="repo_path must be"):
            _parse_host_repo("../foo/bar")
