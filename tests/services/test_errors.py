"""Tests for safe service error translation."""

from __future__ import annotations

import pytest

from tongs.errors import NetworkError, RateLimitError
from tongs.services.errors import ServiceErrorCode, translate_error


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (
            NetworkError("Bearer highly-secret"),
            ServiceErrorCode.NETWORK_UNAVAILABLE,
            True,
        ),
        (RateLimitError("ghp_secret", 17), ServiceErrorCode.RATE_LIMITED, True),
        (RuntimeError("PRIVATE-TOKEN: secret"), ServiceErrorCode.INTERNAL, False),
    ],
)
def test_error_translation_never_copies_exception_text(
    error: Exception, code: ServiceErrorCode, retryable: bool
) -> None:
    translated = translate_error(error, operation="read", hostname="github.com")
    assert translated.code == code
    assert translated.retryable is retryable
    assert "secret" not in str(translated).lower()
    assert "secret" not in repr(translated.details).lower()
