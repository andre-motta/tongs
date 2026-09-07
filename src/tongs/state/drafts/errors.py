"""Typed failures for durable review draft storage."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tongs.state.drafts.models import DraftContent, DraftSnapshot


class DraftStoreError(Exception):
    """Base class for safe draft storage failures."""


class DraftNotOpenError(DraftStoreError):
    """Raised when an operation needs an open store."""


class DraftNotFoundError(DraftStoreError):
    """Raised when a draft or submission attempt does not exist."""


class DraftSchemaError(DraftStoreError):
    """Raised when the database schema cannot be used safely."""


class DraftCorruptionError(DraftStoreError):
    """Raised when persisted draft data is corrupt."""


class DraftPermissionError(DraftStoreError):
    """Raised when private storage permissions cannot be guaranteed."""


class DraftConflictError(DraftStoreError):
    """An optimistic write conflict retaining stored and caller content."""

    def __init__(
        self,
        *,
        expected_version: int,
        current: DraftSnapshot,
        caller_content: DraftContent | None,
    ) -> None:
        super().__init__(
            f"draft version conflict: expected {expected_version}, "
            f"current version is {current.version}"
        )
        self.expected_version = expected_version
        self.current = current
        self.caller_content = caller_content


class DraftStateError(DraftStoreError):
    """Raised when an operation is invalid for the durable draft state."""

    def __init__(self, message: str, *, current: DraftSnapshot) -> None:
        super().__init__(message)
        self.current = current


class DraftAttemptOwnedError(DraftStoreError):
    """Raised when another live process owns a submission attempt."""


class DraftReceiptConflictError(DraftStoreError):
    """Raised when a stable step already has a different remote receipt."""
