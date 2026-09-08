"""Strict protocol adapters for desktop workspace utilities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from tongs.config import Config
from tongs.desktop.protocol.messages import (
    JsonObject,
    ProtocolError,
    ProtocolErrorCode,
)
from tongs.desktop.protocol.state import HandleKind, HandleRegistry
from tongs.services.models import JobRef, ReviewRef, ReviewSnapshot, ServiceEventKind
from tongs.services.workspace_utilities import WorkspaceUtilityService

UTILITY_CAPABILITY = "workspace_utilities"
UTILITY_METHODS = (
    "utilities.cache_clear",
    "utilities.job_log_export",
    "utilities.review_url",
)


class UtilitySession(Protocol):
    """Session authority required by workspace utility operations."""

    @property
    def config(self) -> Config: ...

    async def get_review(self, ref: ReviewRef) -> ReviewSnapshot: ...

    async def get_job_log(self, ref: JobRef) -> str: ...

    async def clear_cache(self) -> None: ...

    def emit_change(
        self, kind: ServiceEventKind, resource: object | None = None
    ) -> None: ...


type UtilityHandler = Callable[[JsonObject, object], Awaitable[object]]


class UtilityOperations:
    """Resolve opaque renderer handles before invoking local utility authority."""

    def __init__(
        self,
        *,
        session: UtilitySession,
        handles: HandleRegistry,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._session = session
        self._handles = handles
        self._environment = environment

    @property
    def handlers(self) -> Mapping[str, tuple[UtilityHandler, bool]]:
        return {
            "utilities.cache_clear": (self.cache_clear, True),
            "utilities.job_log_export": (self.job_log_export, False),
            "utilities.review_url": (self.review_url, False),
        }

    async def review_url(self, params: JsonObject, _context: object) -> object:
        _require_exact_handle(params, "review")
        handle = _handle(params["review"])
        review = self._handles.resolve(handle, HandleKind.REVIEW, ReviewRef)
        result = await self._service().review_url(review)
        return {"review": handle, "url": result.url}

    async def cache_clear(self, params: JsonObject, _context: object) -> object:
        _require_empty(params)
        await self._service().clear_shared_cache()
        self._session.emit_change(ServiceEventKind.RESYNC_REQUIRED)
        return {"cleared": True}

    async def job_log_export(self, params: JsonObject, _context: object) -> object:
        _require_exact_handle(params, "job")
        handle = _handle(params["job"])
        job = self._handles.resolve(handle, HandleKind.JOB, JobRef)
        result = await self._service().prepare_editor_log(job)
        return {
            "status": result.status.value,
            "message": result.message,
            "job": handle,
            "job_id": job.job_id,
            "argv": list(result.argv),
            "content": result.content,
        }

    def _service(self) -> WorkspaceUtilityService:
        return WorkspaceUtilityService(
            config=self._session.config,
            get_review=self._session.get_review,
            get_job_log=self._session.get_job_log,
            clear_cache=self._session.clear_cache,
            environment=self._environment,
        )


def _require_empty(params: JsonObject) -> None:
    if params:
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            "The utility request parameters are invalid.",
        )


def _require_exact_handle(params: JsonObject, name: str) -> None:
    if set(params) != {name}:
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            "The utility request parameters are invalid.",
        )
    _handle(params[name])


def _handle(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 100
        or any(ord(character) < 32 for character in value)
    ):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            "The utility resource handle is invalid.",
        )
    return value


__all__ = ["UTILITY_CAPABILITY", "UTILITY_METHODS", "UtilityOperations"]
