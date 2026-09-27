"""Tests for MCP server helpers."""

from unittest.mock import patch

import pytest

from tongs.errors import AuthError, NetworkError
from tongs.forges.registry import ForgeRegistry

mcp_available = True
try:
    from tongs.mcp import server
    from tongs.mcp.server import _parse_host_repo
except ImportError:
    mcp_available = False

pytestmark = pytest.mark.skipif(not mcp_available, reason="mcp not installed")

UNCONFIGURED_HOSTS = [
    "evil-gitlab.example",
    "github.com.evil.io",
    "gitlab.example.org",
    "gіthub.com",  # Cyrillic i look-alike
    "bitbucket.org",
]


@pytest.fixture(autouse=True)
def configured_registry(monkeypatch):
    """Use a registry with one extra host per forge instead of the user's config."""
    registry = ForgeRegistry(
        extra_gitlab_hosts=frozenset({"git.lab.corp"}),
        extra_github_hosts=frozenset({"git.hub.corp"}),
    )
    monkeypatch.setattr(server, "_registry", registry)
    return registry


@pytest.fixture
def forbid_token_lookup():
    with patch(
        "tongs.forges.registry.resolve_token",
        side_effect=AssertionError("token lookup must not run"),
    ) as resolve:
        yield resolve


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

    def test_configured_extra_hosts(self):
        """Hosts from config.toml are accepted for both forges."""
        assert _parse_host_repo("git.lab.corp/a/b/c") == ("git.lab.corp", "a/b/c")
        assert _parse_host_repo("git.hub.corp/a/b") == ("git.hub.corp", "a/b")

    def test_github_requires_exactly_owner_repo(self):
        """GitHub has no nested groups, so extra segments are rejected."""
        with pytest.raises(ValueError, match="GitHub host"):
            _parse_host_repo("github.com/owner/repo/extra")
        with pytest.raises(ValueError, match="GitHub host"):
            _parse_host_repo("git.hub.corp/owner/repo/extra")

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

    def test_non_ascii_segment_rejected(self):
        """Unicode word characters do not pass the ASCII-only path pattern."""
        with pytest.raises(ValueError, match="repo_path must be"):
            _parse_host_repo("github.com/оwner/repo")

    @pytest.mark.parametrize("hostname", UNCONFIGURED_HOSTS)
    def test_unconfigured_host_rejected(self, hostname):
        """Any host outside the configured set is rejected."""
        with pytest.raises(ValueError) as excinfo:
            _parse_host_repo(f"{hostname}/owner/repo")
        message = str(excinfo.value)
        assert "not configured" in message or "repo_path must be" in message


class TestToolsRejectUnconfiguredHosts:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("hostname", UNCONFIGURED_HOSTS)
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("list_mrs", ()),
            ("get_mr", (1,)),
            ("get_mr_diff", (1,)),
            ("post_comment", (1, "hi")),
            ("approve_mr", (1,)),
            ("list_pipelines", (1,)),
        ],
    )
    async def test_rejects_before_token_lookup(
        self, forbid_token_lookup, configured_registry, hostname, tool, args
    ):
        """Every tool, including the write tools, rejects before credentials."""
        with (
            patch(
                "tongs.forges.registry.create_client",
                side_effect=AssertionError("no HTTP client may be built"),
            ),
            pytest.raises(ValueError),
        ):
            await getattr(server, tool)(f"{hostname}/owner/repo", *args)
        forbid_token_lookup.assert_not_called()
        assert configured_registry._clients == {}

    @pytest.mark.asyncio
    async def test_configured_extra_host_reaches_client(self, configured_registry):
        """A configured self-hosted GitLab still gets a client and a request."""
        with (
            patch("tongs.forges.registry.resolve_token", return_value="tok"),
            patch(
                "tongs.forges.gitlab.GitLabClient.list_mrs", return_value=[]
            ) as list_mrs,
        ):
            result = await server.list_mrs("git.lab.corp/group/sub/app")
        assert result == []
        list_mrs.assert_awaited_once()
        assert list_mrs.await_args.args[0] == "group/sub/app"
        await configured_registry.close_all()


TOOL_CALLS = [
    ("list_mrs", {}),
    ("get_mr", {"number": 1}),
    ("get_mr_diff", {"number": 1}),
    ("post_comment", {"number": 1, "body": "hi"}),
    ("approve_mr", {"number": 1}),
    ("list_pipelines", {"number": 1}),
]


class TestToolsRedactForgeErrors:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("tool", "args"), TOOL_CALLS)
    async def test_forge_error_text_never_reaches_client(self, tool, args):
        """Every tool replaces forge error text with a fixed message."""
        secret = "https://user:zqSECRETzq@git.lab.corp token zqSECRETzq"
        with (
            patch(
                "tongs.forges.registry.resolve_token",
                side_effect=AuthError(secret),
            ),
            pytest.raises(server.ToolError) as excinfo,
        ):
            await getattr(server, tool)("git.lab.corp/group/app", **args)
        message = str(excinfo.value)
        assert "SECRET" not in message
        assert message == f"{tool} failed: Forge authentication is unavailable."
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True

    @pytest.mark.asyncio
    async def test_request_error_is_redacted(self, configured_registry):
        with (
            patch("tongs.forges.registry.resolve_token", return_value="tok"),
            patch(
                "tongs.forges.gitlab.GitLabClient.list_mrs",
                side_effect=NetworkError("connect to zqSECRETzq failed"),
            ),
            pytest.raises(server.ToolError) as excinfo,
        ):
            await server.list_mrs("git.lab.corp/group/app")
        assert "SECRET" not in str(excinfo.value)
        assert "could not be reached" in str(excinfo.value)
        await configured_registry.close_all()

    @pytest.mark.asyncio
    async def test_malformed_netrc_through_mcp_call(self, tmp_path):
        """A malformed .netrc reaches an MCP client without its contents."""
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine git.lab.corp login x zqSECRETzq-value\n")
        netrc_file.chmod(0o600)
        with (
            patch("tongs.forges.auth.Path.home", return_value=tmp_path),
            patch("tongs.forges.auth._token_from_cli", return_value=None),
            patch("tongs.forges.auth._token_from_keyring", return_value=None),
            pytest.raises(server.ToolError) as excinfo,
        ):
            await server.mcp.call_tool(
                "list_mrs", {"repo_path": "git.lab.corp/group/app"}
            )
        message = str(excinfo.value)
        assert "SECRET" not in message
        assert "Forge authentication is unavailable" in message

    @pytest.mark.asyncio
    async def test_host_check_still_raises_value_error(self):
        """The unconfigured host message is not replaced by the redaction."""
        with pytest.raises(ValueError, match="not configured"):
            await server.list_mrs("evil.example/owner/repo")
