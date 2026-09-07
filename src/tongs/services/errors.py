"""Stable, redacted errors for the application service boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from tongs.errors import (
    AuthError,
    ConfigError,
    ConflictError,
    ForgePermissionError,
    NetworkError,
    NotFoundError,
    RateLimitError,
)


class ServiceErrorCode(str, Enum):
    """Machine-readable service failure categories."""

    NOT_STARTED = "not_started"
    CLOSED = "closed"
    INVALID_INPUT = "invalid_input"
    RESOURCE_NOT_ISSUED = "resource_not_issued"
    REVISION_UNAVAILABLE = "revision_unavailable"
    REVISION_CHANGED = "revision_changed"
    INVALID_RESPONSE = "invalid_response"
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    NETWORK_UNAVAILABLE = "network_unavailable"
    CONFIGURATION_INVALID = "configuration_invalid"
    SHUTDOWN_FAILED = "shutdown_failed"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class ServiceError(Exception):
    """Safe exception suitable for service and transport adapters."""

    code: ServiceErrorCode
    message: str
    retryable: bool = False
    details: tuple[tuple[str, str], ...] = ()

    def __str__(self) -> str:
        return self.message


def translate_error(
    error: Exception,
    *,
    operation: str,
    hostname: str | None = None,
) -> ServiceError:
    """Map an internal failure without copying its text or response body."""
    if isinstance(error, ServiceError):
        return error

    details = (("operation", operation),)
    if hostname is not None:
        details += (("hostname", hostname),)

    if isinstance(error, RateLimitError):
        if error.retry_after is not None:
            details += (("retry_after_seconds", str(error.retry_after)),)
        return ServiceError(
            ServiceErrorCode.RATE_LIMITED,
            "The forge rate limit was reached.",
            retryable=True,
            details=details,
        )
    if isinstance(error, AuthError):
        return ServiceError(
            ServiceErrorCode.AUTHENTICATION_FAILED,
            "Forge authentication is unavailable.",
            details=details,
        )
    if isinstance(error, ForgePermissionError):
        return ServiceError(
            ServiceErrorCode.PERMISSION_DENIED,
            "The forge denied access to this resource.",
            details=details,
        )
    if isinstance(error, NotFoundError):
        return ServiceError(
            ServiceErrorCode.NOT_FOUND,
            "The requested forge resource was not found.",
            details=details,
        )
    if isinstance(error, ConflictError):
        return ServiceError(
            ServiceErrorCode.CONFLICT,
            "The forge resource changed or conflicts with this request.",
            retryable=True,
            details=details,
        )
    if isinstance(error, NetworkError):
        return ServiceError(
            ServiceErrorCode.NETWORK_UNAVAILABLE,
            "The forge could not be reached.",
            retryable=True,
            details=details,
        )
    if isinstance(error, ConfigError):
        return ServiceError(
            ServiceErrorCode.CONFIGURATION_INVALID,
            "The Tongs configuration is invalid.",
            details=details,
        )
    if isinstance(error, (ValueError, KeyError, TypeError, AttributeError)):
        return ServiceError(
            ServiceErrorCode.INVALID_RESPONSE,
            "The forge returned an invalid response.",
            details=details,
        )
    return ServiceError(
        ServiceErrorCode.INTERNAL,
        "The operation failed.",
        details=details,
    )


__all__ = ["ServiceError", "ServiceErrorCode", "translate_error"]
