"""Narrow, UI-independent workspace utility authority."""

from __future__ import annotations

import asyncio
import os
import secrets
import shlex
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath
from urllib.parse import urlsplit

from tongs.config import Config
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import JobRef, ReviewRef, ReviewSnapshot

MAX_EDITOR_LOG_BYTES = 4 * 1024 * 1024
MAX_EDITOR_COMMAND_BYTES = 16 * 1024
MAX_EDITOR_ARGUMENTS = 64
MAX_EDITOR_ARGUMENT_BYTES = 4096
MAX_REVIEW_URL_BYTES = 4096

_TERMINAL_EDITORS = frozenset(
    {"joe", "less", "micro", "more", "nano", "nvim", "pico", "vi", "vim"}
)


class EditorPlanStatus(str, Enum):
    """Safe outcomes produced before Electron starts an editor process."""

    READY = "ready"
    DISABLED = "disabled"
    MISSING = "missing"
    MALFORMED = "malformed"
    TERMINAL_UNSUPPORTED = "terminal_unsupported"
    LOG_TOO_LARGE = "log_too_large"
    CAPACITY_EXCEEDED = "capacity_exceeded"


@dataclass(frozen=True, slots=True)
class ReviewUrl:
    """Credential-free HTTPS URL bound to an admitted review."""

    review: ReviewRef
    url: str


@dataclass(frozen=True, slots=True)
class EditorLogPlan:
    """Trusted editor argv and bounded log content for the Electron main process."""

    status: EditorPlanStatus
    message: str
    job: JobRef
    argv: tuple[str, ...] = ()
    content: str | None = None
    reservation: EditorReservation | None = None


@dataclass(frozen=True, slots=True)
class EditorReservation:
    """Opaque reservation bound to one slot and generated export basename."""

    slot: int
    token: str
    export_name: str


class WorkspaceUtilityService:
    """Resolve renderer-safe identities into narrowly scoped local utilities."""

    def __init__(
        self,
        *,
        config: Config,
        get_review: Callable[[ReviewRef], Awaitable[ReviewSnapshot]],
        get_job_log: Callable[[JobRef], Awaitable[str]],
        clear_cache: Callable[[], Awaitable[None]],
        reserve_editor_export: Callable[
            [int, str], Awaitable[EditorReservation | None]
        ],
        release_editor_export: Callable[[int, str], Awaitable[bool]],
        environment: Mapping[str, str] | None = None,
        max_editor_log_bytes: int = MAX_EDITOR_LOG_BYTES,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if max_editor_log_bytes <= 0:
            raise ValueError("max_editor_log_bytes must be positive")
        self._config = config
        self._get_review = get_review
        self._get_job_log = get_job_log
        self._clear_cache = clear_cache
        self._reserve_editor_export = reserve_editor_export
        self._release_editor_export = release_editor_export
        self._environment = os.environ if environment is None else environment
        self._max_editor_log_bytes = max_editor_log_bytes
        self._token_factory = token_factory or _new_editor_token

    async def review_url(self, review: ReviewRef) -> ReviewUrl:
        """Return the current URL only when it belongs to the admitted forge."""
        snapshot = await self._get_review(review)
        if snapshot.ref != review:
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The current review identity could not be confirmed.",
            )
        url = snapshot.detail.web_url
        url_bytes = _utf8_length(url) if isinstance(url, str) else None
        parsed = None
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            _ = parsed.port
        except (TypeError, ValueError):
            hostname = None
        if (
            not isinstance(url, str)
            or not url
            or url_bytes is None
            or url_bytes > MAX_REVIEW_URL_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in url)
            or parsed is None
            or parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or hostname is None
            or hostname.casefold() != review.repository.hostname.casefold()
        ):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The current review URL is not a permitted HTTPS forge URL.",
            )
        return ReviewUrl(review, url)

    async def clear_shared_cache(self) -> None:
        """Clear only the shared API cache, leaving durable review drafts intact."""
        try:
            await self._clear_cache()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise ServiceError(
                ServiceErrorCode.INTERNAL,
                "The shared API cache could not be cleared.",
            ) from error

    async def prepare_editor_log(self, job: JobRef) -> EditorLogPlan:
        """Build a bounded launch plan for one admitted job identity."""
        status, message, argv = self._editor_argv()
        if status is not EditorPlanStatus.READY:
            return EditorLogPlan(status, message, job)

        token = self._token_factory()
        reservation = await self._reserve_editor_export(job.job_id, token)
        if reservation is None:
            return EditorLogPlan(
                EditorPlanStatus.CAPACITY_EXCEEDED,
                "The private editor export limit was reached. Retry after older exports expire.",
                job,
            )
        try:
            content = await self._get_job_log(job)
        except BaseException:
            await self._release_reservation(reservation)
            raise
        if not isinstance(content, str):
            await self._release_reservation(reservation)
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned an invalid job log.",
            )
        try:
            byte_count = len(content.encode("utf-8"))
        except UnicodeEncodeError as error:
            await self._release_reservation(reservation)
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The job log could not be encoded safely.",
            ) from error
        if byte_count > self._max_editor_log_bytes:
            await self._release_reservation(reservation)
            return EditorLogPlan(
                EditorPlanStatus.LOG_TOO_LARGE,
                "The job log is too large to export to an external editor.",
                job,
            )
        return EditorLogPlan(
            status,
            message,
            job,
            argv=argv,
            content=content,
            reservation=reservation,
        )

    async def _release_reservation(self, reservation: EditorReservation) -> None:
        with suppress(Exception):
            await self._release_editor_export(reservation.slot, reservation.token)
            # A retained row stays bounded and is reclaimed on a later operation.

    def _editor_argv(self) -> tuple[EditorPlanStatus, str, tuple[str, ...]]:
        enabled = self._config.external_editor_enabled
        if not isinstance(enabled, bool):
            return (
                EditorPlanStatus.MALFORMED,
                "The external editor setting is malformed.",
                (),
            )
        if not enabled:
            return (
                EditorPlanStatus.DISABLED,
                "External editor access is disabled in Tongs configuration.",
                (),
            )

        command = self._config.editor_command
        if not isinstance(command, str):
            return (
                EditorPlanStatus.MALFORMED,
                "The configured external editor command is malformed.",
                (),
            )
        if not command:
            command = self._environment.get("VISUAL", "") or self._environment.get(
                "EDITOR", ""
            )
        if not command:
            return (
                EditorPlanStatus.MISSING,
                "Configure [editor].command, $VISUAL, or $EDITOR to use an external editor.",
                (),
            )
        if (
            not isinstance(command, str)
            or (_command_bytes := _utf8_length(command)) is None
            or _command_bytes > MAX_EDITOR_COMMAND_BYTES
            or any(
                ord(character) < 32 or ord(character) == 127 for character in command
            )
        ):
            return (
                EditorPlanStatus.MALFORMED,
                "The configured external editor command is malformed.",
                (),
            )
        try:
            argv = tuple(shlex.split(command, posix=True))
        except ValueError:
            argv = ()
        if (
            not argv
            or len(argv) > MAX_EDITOR_ARGUMENTS
            or any(
                not argument
                or (_argument_bytes := _utf8_length(argument)) is None
                or _argument_bytes > MAX_EDITOR_ARGUMENT_BYTES
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in argument
                )
                for argument in argv
            )
        ):
            return (
                EditorPlanStatus.MALFORMED,
                "The configured external editor command is malformed.",
                (),
            )

        executable = PurePath(argv[0]).name.casefold()
        terminal_only = executable in _TERMINAL_EDITORS or (
            executable in {"emacs", "emacsclient"}
            and any(argument in {"-nw", "-t", "--tty"} for argument in argv[1:])
        )
        if terminal_only:
            return (
                EditorPlanStatus.TERMINAL_UNSUPPORTED,
                "Configure a wait-capable graphical editor; terminal editors cannot attach to Tongs Desktop.",
                (),
            )
        return (
            EditorPlanStatus.READY,
            "The configured editor launch plan is ready.",
            argv,
        )


def _utf8_length(value: str) -> int | None:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def _new_editor_token() -> str:
    return secrets.token_hex(16)


__all__ = [
    "MAX_EDITOR_LOG_BYTES",
    "EditorLogPlan",
    "EditorPlanStatus",
    "EditorReservation",
    "ReviewUrl",
    "WorkspaceUtilityService",
]
