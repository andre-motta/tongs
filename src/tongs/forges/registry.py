"""Config-driven registry mapping hostnames to ForgeClient instances."""

from __future__ import annotations

from tongs.forges.auth import resolve_token
from tongs.forges.base import ForgeClient
from tongs.forges.http import create_client
from tongs.forges.models import ForgeHost
from tongs.scanner.repo import ForgeType


def _github_api_base(hostname: str) -> str:
    if hostname == "github.com":
        return "https://api.github.com"
    return f"https://{hostname}/api/v3"


def _gitlab_api_base(hostname: str) -> str:
    return f"https://{hostname}/api/v4"


class ForgeRegistry:
    """Maps hostnames to authenticated ForgeClient instances.

    Clients are created lazily on first access and cached for reuse.
    """

    def __init__(
        self,
        extra_gitlab_hosts: frozenset[str] = frozenset(),
        extra_github_hosts: frozenset[str] = frozenset(),
        request_timeout: float = 30.0,
    ):
        self._extra_gitlab_hosts = extra_gitlab_hosts
        self._extra_github_hosts = extra_github_hosts
        self._timeout = request_timeout
        self._clients: dict[str, ForgeClient] = {}
        self._hosts: dict[str, ForgeHost] = {}

    def get_host(self, hostname: str) -> ForgeHost | None:
        """Get or create a ForgeHost for the given hostname."""
        if hostname in self._hosts:
            return self._hosts[hostname]

        forge_type = self._detect_type(hostname)
        if forge_type is None:
            return None

        if forge_type == ForgeType.GITHUB:
            api_base = _github_api_base(hostname)
        else:
            api_base = _gitlab_api_base(hostname)

        host = ForgeHost(
            hostname=hostname,
            forge_type=forge_type,
            api_base=api_base,
        )
        self._hosts[hostname] = host
        return host

    async def get_client(self, hostname: str) -> ForgeClient:
        """Get or create an authenticated ForgeClient for the given host."""
        if hostname in self._clients:
            return self._clients[hostname]

        host = self.get_host(hostname)
        if host is None:
            from tongs.errors import AuthError

            raise AuthError(f"Unknown forge host: {hostname}")

        token = resolve_token(hostname, host.forge_type)
        http_client = create_client(host.api_base, token, self._timeout)

        if host.forge_type == ForgeType.GITLAB:
            from tongs.forges.gitlab import GitLabClient

            client = GitLabClient(host, http_client)
        else:
            raise NotImplementedError("GitHub client not yet implemented")

        self._clients[hostname] = client
        return client

    async def close_all(self) -> None:
        """Close all cached clients."""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()

    def _detect_type(self, hostname: str) -> ForgeType | None:
        """Detect forge type from hostname."""
        if hostname in {"github.com"} or hostname in self._extra_github_hosts:
            return ForgeType.GITHUB
        if hostname in {"gitlab.com"} or hostname in self._extra_gitlab_hosts:
            return ForgeType.GITLAB
        if "gitlab" in hostname:
            return ForgeType.GITLAB
        if "github" in hostname:
            return ForgeType.GITHUB
        return None
