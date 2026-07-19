"""Token resolution for forge authentication.

Cascade: CLI credential store -> .netrc -> error with instructions.
"""

from __future__ import annotations

import netrc
import stat
import subprocess
import sys
from pathlib import Path

from tongs.errors import AuthError
from tongs.scanner.repo import ForgeType


def resolve_token(hostname: str, forge_type: ForgeType) -> str:
    """Resolve an auth token for the given host.

    Tries in order:
    1. CLI credential store (gh auth token / glab auth token)
    2. ~/.netrc
    3. Raises AuthError with setup instructions
    """
    token = _token_from_cli(hostname, forge_type)
    if token:
        return token

    token = _token_from_netrc(hostname)
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


def _token_from_cli(hostname: str, forge_type: ForgeType) -> str | None:
    """Extract token from gh/glab CLI credential store."""
    if forge_type == ForgeType.GITHUB:
        cmd = ["gh", "auth", "token"]
        if hostname != "github.com":
            cmd.extend(["--hostname", hostname])
    else:
        cmd = ["glab", "auth", "token", "--hostname", hostname]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    token = result.stdout.strip()
    return token if token else None


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
        raise AuthError(f"Failed to parse ~/.netrc: {e}") from e

    auth = nrc.authenticators(hostname)
    if auth is None:
        return None

    _login, _account, password = auth
    return password if password else None
