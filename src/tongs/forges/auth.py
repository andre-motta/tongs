"""Token resolution for forge authentication.

Cascade: CLI credential store -> .netrc -> keyring -> error with instructions.
"""

from __future__ import annotations

import logging
import netrc
import stat
import subprocess
import sys
from pathlib import Path

from tongs.errors import AuthError, redact_credentials
from tongs.scanner.repo import ForgeType

log = logging.getLogger(__name__)


def resolve_token(hostname: str, forge_type: ForgeType) -> str:
    """Resolve an auth token for the given host.

    Tries in order:
    1. CLI credential store (gh auth token / glab config get token)
    2. ~/.netrc
    3. System keyring (requires optional ``keyring`` package)
    4. Raises AuthError with setup instructions
    """
    token = _token_from_cli(hostname, forge_type)
    if token:
        return token

    token = _token_from_netrc(hostname)
    if token:
        return token

    token = _token_from_keyring(hostname)
    if token:
        return token

    cli = (
        "gh auth login"
        if forge_type == ForgeType.GITHUB
        else f"glab auth login --hostname {hostname}"
    )
    raise AuthError(
        f"No credentials found for {hostname}. "
        f"Run `{cli}` or add an entry to ~/.netrc:\n"
        f"  machine {hostname}\n"
        f"    login __token__\n"
        f"    password YOUR_TOKEN"
    )


def refresh_token(hostname: str, forge_type: ForgeType) -> str | None:
    """Resolve a replacement token after the current one was rejected.

    glab only refreshes an expired OAuth token when it makes an API request, so
    run ``glab auth status`` (a GET /user) first; it persists the refreshed
    token for the ``glab config get token`` lookup in :func:`resolve_token`.
    Returns ``None`` when no credential can be resolved.
    """
    if forge_type == ForgeType.GITLAB:
        _run_cli(["glab", "auth", "status", "--hostname", hostname], timeout=15)

    try:
        return resolve_token(hostname, forge_type)
    except AuthError as e:
        log.debug(
            "Token refresh for %s failed: %s", hostname, redact_credentials(str(e))
        )
        return None


def _token_from_cli(hostname: str, forge_type: ForgeType) -> str | None:
    """Extract token from gh/glab CLI credential store."""
    if forge_type == ForgeType.GITHUB:
        cmd = ["gh", "auth", "token"]
        if hostname != "github.com":
            cmd.extend(["--hostname", hostname])
    else:
        # glab has no `auth token` subcommand; `config get token` returns the
        # stored credential, including OAuth and keyring-backed tokens.
        cmd = ["glab", "config", "get", "token", "--host", hostname]

    result = _run_cli(cmd, timeout=5)
    if result is None:
        return None

    if result.returncode != 0:
        log.debug("%s exited with %d; skipping CLI token", cmd[0], result.returncode)
        return None

    token = result.stdout.strip()
    return token if token else None


def _run_cli(cmd: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    """Run a forge CLI command, returning ``None`` if it is missing or hangs."""
    try:
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _token_from_netrc(hostname: str) -> str | None:
    """Read token from ~/.netrc with permission enforcement."""
    netrc_path = Path.home() / ("_netrc" if sys.platform == "win32" else ".netrc")

    if not netrc_path.exists():
        return None

    if sys.platform != "win32":
        mode = netrc_path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
            raise AuthError(
                f"~/.netrc has permissions {oct(mode & 0o777)}. "
                f"Expected 0600. Run: chmod 600 ~/.netrc"
            )

    try:
        nrc = netrc.netrc(str(netrc_path))
    except netrc.NetrcParseError as e:
        # The parser quotes the offending token, which can be the secret
        # itself, so name only the file and line and drop the original error.
        raise AuthError(
            f"Failed to parse ~/{netrc_path.name} near line {e.lineno}; "
            "check its syntax"
        ) from None

    # Only a ``machine`` entry naming this host counts. ``authenticators``
    # would fall back to the ``default`` entry, which usually belongs to an
    # unrelated service, and send its password to the forge.
    if hostname == "default":
        return None
    auth = nrc.hosts.get(hostname)
    if auth is None:
        return None

    _login, _account, password = auth
    return password if password else None


def _token_from_keyring(hostname: str) -> str | None:
    """Read token from system keyring (optional dependency)."""
    try:
        import keyring
    except ImportError:
        return None

    try:
        password = keyring.get_password("tongs", hostname)
    except Exception:  # noqa: BLE001 - Optional keyring backends raise backend-specific errors.
        return None

    return password if password else None
