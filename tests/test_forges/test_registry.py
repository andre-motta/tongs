"""Tests for forge registry."""

from unittest.mock import AsyncMock

import pytest

from tongs.errors import AuthError
from tongs.forges.registry import ForgeRegistry, _github_api_base, _gitlab_api_base
from tongs.scanner.repo import ForgeType


class TestApiBaseUrls:
    def test_github_com(self):
        assert _github_api_base("github.com") == "https://api.github.com"

    def test_github_enterprise(self):
        assert _github_api_base("github.corp.com") == "https://github.corp.com/api/v3"

    def test_gitlab_com(self):
        assert _gitlab_api_base("gitlab.com") == "https://gitlab.com/api/v4"

    def test_gitlab_internal(self):
        assert (
            _gitlab_api_base("gitlab.cee.redhat.com")
            == "https://gitlab.cee.redhat.com/api/v4"
        )


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

    def test_unknown_host_returns_none(self):
        registry = ForgeRegistry()
        assert registry.get_host("bitbucket.org") is None

    def test_caches_host(self):
        registry = ForgeRegistry()
        host1 = registry.get_host("gitlab.com")
        host2 = registry.get_host("gitlab.com")
        assert host1 is host2

    def test_gitlab_subdomain_auto_detected(self):
        registry = ForgeRegistry()
        host = registry.get_host("gitlab.example.org")
        assert host is not None
        assert host.forge_type == ForgeType.GITLAB

    @pytest.mark.asyncio
    async def test_get_client_unknown_host_raises_auth_error(self):
        registry = ForgeRegistry()
        with pytest.raises(AuthError, match="Unknown forge host"):
            await registry.get_client("bitbucket.org")

    @pytest.mark.asyncio
    async def test_close_all_clears_cache(self):
        registry = ForgeRegistry()
        # Manually inject a mock client into the cache
        mock_client = AsyncMock()
        registry._clients["gitlab.example.com"] = mock_client
        assert len(registry._clients) == 1

        await registry.close_all()

        mock_client.close.assert_awaited_once()
        assert len(registry._clients) == 0
