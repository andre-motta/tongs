"""Structured error hierarchy for forge operations."""

from __future__ import annotations

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


class ValidationError(ForgeError):
    """Forge rejected the request as invalid before applying the mutation."""


class ConfigError(ForgeError):
    """Configuration error (malformed config, invalid scan root, etc.)."""


_TOKEN_PATTERNS = re.compile(
    r"("
    r"glpat-|gldt-|glcbt-\d*_?|glptt-|glft-|glsoat-|glimt-|gloas-|glrt-"
    r"|ghp_|gho_|ghs_|ghu_|ghr_|github_pat_"
    r"|Bearer\s+"
    r"|PRIVATE-TOKEN:\s*"
    r")[A-Za-z0-9_.\-]+"
)

# The userinfo part of a URL (``scheme://user:password@host``).
_URL_USERINFO = re.compile(r"(://)[^/?#\s]+@")


def redact_credentials(text: str) -> str:
    """Redact known token patterns and URL userinfo from text."""
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    return _TOKEN_PATTERNS.sub(r"\1[REDACTED]", text)
