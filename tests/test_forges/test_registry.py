"""Tests for forge registry."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from tongs.errors import AuthError
from tongs.forges.http import RefreshingTokenAuth
from tongs.forges.registry import ForgeRegistry, _github_api_base, _gitlab_api_base
from tongs.scanner.repo import ForgeType


class TestApiBaseUrls:
    def test_github_com(self):
        assert _github_api_base("github.com") == "https://api.github.com"

    def test_github_enterprise(self):
        assert _github_api_base("github.corp.com") == "https://github.corp.com/api/v3"

    @pytest.mark.parametrize("hostname", ["gitlab.com", "gitlab.cee.redhat.com"])
    def test_gitlab(self, hostname):
        assert _gitlab_api_base(hostname) == f"https://{hostname}/api/v4"


class TestForgeRegistry:
    def test_detects_github(self):
        registry = ForgeRegistry()
        host = registry.get_host("github.com")
        assert host is not None
        assert host.forge_type == ForgeType.GITHUB

    def test_detects_gitlab(self):
        registry = ForgeRegistry()
        host = registry.get_host("gitlab.com")
        assert host is not None
        assert host.forge_type == ForgeType.GITLAB

    def test_detects_internal_gitlab(self):
        registry = ForgeRegistry(
            extra_gitlab_hosts=frozenset({"gitlab.cee.redhat.com"})
        )
        host = registry.get_host("gitlab.cee.redhat.com")
        assert host is not None
        assert host.forge_type == ForgeType.GITLAB

    def test_detects_extra_github(self):
        registry = ForgeRegistry(extra_github_hosts=frozenset({"git.mycorp.com"}))
        host = registry.get_host("git.mycorp.com")
        assert host is not None
        assert host.forge_type == ForgeType.GITHUB

    def test_caches_host(self):
        registry = ForgeRegistry()
        host1 = registry.get_host("gitlab.com")
        host2 = registry.get_host("gitlab.com")
        assert host1 is host2

    @pytest.mark.parametrize(
        "hostname",
        [
            "gitlab.example.org",
            "evil-gitlab.example",
            "github.com.evil.io",
            "mygithub.example",
            "bitbucket.org",
        ],
    )
    def test_unconfigured_host_is_not_mapped_by_substring(self, hostname):
        registry = ForgeRegistry()
        assert registry.get_host(hostname) is None

    @pytest.mark.asyncio
    async def test_get_client_unconfigured_host_skips_token_lookup(self):
        registry = ForgeRegistry()
        with (
            patch(
                "tongs.forges.registry.resolve_token",
                side_effect=AssertionError("token lookup must not run"),
            ),
            pytest.raises(AuthError, match="Unknown forge host"),
        ):
            await registry.get_client("evil-gitlab.example")

    def test_active_hostnames_are_the_configured_set(self):
        registry = ForgeRegistry(
            extra_gitlab_hosts=frozenset({"git.lab.corp"}),
            extra_github_hosts=frozenset({"git.hub.corp"}),
        )
        assert registry.active_hostnames() == [
            "git.hub.corp",
            "git.lab.corp",
            "github.com",
            "gitlab.com",
        ]

    @pytest.mark.asyncio
    async def test_get_client_refreshes_token_for_its_host(self):
        registry = ForgeRegistry()
        with (
            patch("tongs.forges.registry.resolve_token", return_value="tok"),
            patch(
                "tongs.forges.registry.refresh_token", return_value="fresh"
            ) as refresh,
        ):
            client = await registry.get_client("gitlab.com")
            auth = client._http.auth
            assert isinstance(auth, RefreshingTokenAuth)
            assert auth._refresh() == "fresh"
        refresh.assert_called_once_with("gitlab.com", ForgeType.GITLAB)
        await registry.close_all()

    @pytest.mark.asyncio
    async def test_close_all_finishes_other_clients_before_propagating_cancel(self):
        registry = ForgeRegistry()
        cancelled_client = AsyncMock()
        cancelled_client.close.side_effect = asyncio.CancelledError
        other_client = AsyncMock()
        registry._clients = {
            "github.com": cancelled_client,
            "gitlab.com": other_client,
        }

        with pytest.raises(asyncio.CancelledError):
            await registry.close_all()

        cancelled_client.close.assert_awaited_once()
        other_client.close.assert_awaited_once()
        assert registry._clients == {}
