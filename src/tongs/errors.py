"""Structured error hierarchy for forge operations."""

import re


class ForgeError(Exception):
    """Base error for all forge operations."""


class AuthError(ForgeError):
    """Authentication failed or missing. User should run glab/gh auth login."""


class RateLimitError(ForgeError):
    """Forge API rate limit exceeded. Retry after the specified delay."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class NetworkError(ForgeError):
    """Network connectivity issue. Switch to offline/cached mode."""


class NotFoundError(ForgeError):
    """Resource not found (404). MR may have been deleted or repo inaccessible."""


class ConflictError(ForgeError):
    """Operation conflicts with current state (e.g. merge conflicts, already merged)."""


class ForgePermissionError(ForgeError):
    """Insufficient permissions for the requested operation."""


class ConfigError(ForgeError):
    """Configuration error (malformed config, invalid scan root, etc.)."""


_TOKEN_PATTERNS = re.compile(
    r"("
    r"glpat-|gldt-|glcbt-\d*_?|glptt-|glft-|glsoat-|glimt-|gloas-"
    r"|ghp_|gho_|ghs_|ghu_|github_pat_"
    r"|Bearer\s+"
    r"|PRIVATE-TOKEN:\s*"
    r")[A-Za-z0-9_.\-]+"
)


def redact_credentials(text: str) -> str:
    """Redact known token patterns from text."""
    return _TOKEN_PATTERNS.sub(r"\1[REDACTED]", text)
