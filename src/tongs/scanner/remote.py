"""Parse git remote URLs and detect forge type."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from tongs.scanner.repo import ForgeType, Remote

GITHUB_HOSTS = frozenset({"github.com"})

GITLAB_HOSTS = frozenset({"gitlab.com"})

# ssh://user@host:port/path or ssh://user@host/path
SSH_PROTOCOL_RE = re.compile(
    r"^ssh://(?:[^@]+@)?([^:/]+)(?::\d+)?/(.+?)(?:\.git)?$"
)

# git@host:path (SCP-style, no port support)
SCP_RE = re.compile(
    r"^(?:[^@]+@)?([^:/]+):(.+?)(?:\.git)?$"
)

HTTPS_URL_RE = re.compile(
    r"^https?://([^/]+)/(.+?)(?:\.git)?$"
)


def parse_remote_url(
    name: str,
    url: str,
    extra_gitlab_hosts: frozenset[str] = frozenset(),
    extra_github_hosts: frozenset[str] = frozenset(),
) -> Remote | None:
    """Parse a git remote URL into a Remote with forge detection.

    Returns None if the URL cannot be parsed or forge type is unknown.
    """
    hostname, repo_path = _extract_host_and_path(url)
    if not hostname or not repo_path:
        return None

    hostname = _normalize_hostname(hostname)
    forge_type = _detect_forge_type(hostname, extra_gitlab_hosts, extra_github_hosts)
    if forge_type is None:
        return None

    sanitized_url = _strip_userinfo(url)

    return Remote(
        name=name,
        url=sanitized_url,
        hostname=hostname,
        repo_path=repo_path,
        forge_type=forge_type,
    )


def _extract_host_and_path(url: str) -> tuple[str | None, str | None]:
    """Extract hostname and repo path from SSH or HTTPS URL."""
    https_match = HTTPS_URL_RE.match(url)
    if https_match:
        return https_match.group(1), https_match.group(2)

    ssh_match = SSH_PROTOCOL_RE.match(url)
    if ssh_match:
        return ssh_match.group(1), ssh_match.group(2)

    scp_match = SCP_RE.match(url)
    if scp_match:
        return scp_match.group(1), scp_match.group(2)

    return None, None


def _normalize_hostname(hostname: str) -> str:
    """Normalize SSH hostname aliases and strip port/userinfo."""
    if hostname == "altssh.gitlab.com":
        return "gitlab.com"
    if "@" in hostname:
        hostname = hostname.rsplit("@", 1)[1]
    port_stripped = hostname.split(":")[0]
    return port_stripped.lower()


def _strip_userinfo(url: str) -> str:
    """Remove credentials from URL (user:token@host -> host)."""
    if "://" in url:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            return parsed._replace(netloc=netloc).geturl()
    return url


def _detect_forge_type(
    hostname: str,
    extra_gitlab_hosts: frozenset[str] = frozenset(),
    extra_github_hosts: frozenset[str] = frozenset(),
) -> ForgeType | None:
    """Detect forge type from hostname."""
    if hostname in GITHUB_HOSTS or hostname in extra_github_hosts:
        return ForgeType.GITHUB

    if hostname in GITLAB_HOSTS or hostname in extra_gitlab_hosts:
        return ForgeType.GITLAB

    if "gitlab" in hostname:
        return ForgeType.GITLAB

    if "github" in hostname:
        return ForgeType.GITHUB

    return None
