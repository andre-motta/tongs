"""Production desktop request dispatcher over one bounded NDJSON connection."""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Protocol, cast

from tongs.desktop.assets import (
    ASSET_CHUNK_BYTES,
    AssetCatalog,
    AssetDescriptor,
    CoreAssetSpec,
)
from tongs.desktop.protocol.ci_operations import (
    CI_CAPABILITY,
    CI_METHODS,
    CIOperations,
)
from tongs.desktop.protocol.diff_projection import DiffLayout, flatten_diff
from tongs.desktop.protocol.messages import (
    MAX_EVENT_FRAME_BYTES,
    MAX_PENDING_REQUESTS,
    MAX_QUEUED_EVENTS,
    MAX_REQUEST_FRAME_BYTES,
    MAX_RESPONSE_FRAME_BYTES,
    CancelFrame,
    JsonObject,
    JsonValue,
    ProtocolError,
    ProtocolErrorCode,
    RequestFrame,
    decode_frame,
    encode_event,
    encode_response,
    to_json_value,
)
from tongs.desktop.protocol.review_operations import (
    REVIEW_CAPABILITY,
    REVIEW_METHODS,
    ReviewOperations,
)
from tongs.desktop.protocol.state import (
    MAX_SNAPSHOT_RETAINED_BYTES,
    HandleKind,
    HandleRegistry,
    SnapshotStore,
)
from tongs.desktop.protocol.utility_operations import (
    UTILITY_CAPABILITY,
    UTILITY_METHODS,
    UtilityOperations,
)
from tongs.diff.conversion import convert_forge_changes
from tongs.forges.models import MRState, MRSummary, Pipeline, PipelineJob
from tongs.plugins.desktop import (
    DesktopCallContext,
    DesktopCancellation,
    DesktopHostFacade,
    DesktopLocation,
    DesktopNotificationSeverity,
    DesktopPluginContractError,
    DesktopPluginError,
    DesktopPluginErrorCode,
    DesktopPluginManifest,
    DesktopPluginRecord,
    DesktopPluginState,
    DesktopReadKind,
    FrozenJsonObject,
    FrozenJsonValue,
    freeze_json,
    freeze_json_object,
)
from tongs.plugins.desktop import (
    JsonObject as PluginJsonObject,
)
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.services import (
    ApplicationSession,
    JobRef,
    PipelineRef,
    RepositoryRef,
    RepositorySnapshot,
    ReviewListItem,
    ReviewQuery,
    ReviewRef,
    ReviewScope,
    ReviewSnapshot,
    ServiceError,
    ServiceEvent,
)

SUPPORTED_CAPABILITIES = frozenset(
    {
        "assets",
        "cancellation",
        CI_CAPABILITY,
        "events",
        "opaque_handles",
        "paged_diffs",
        "paged_logs",
        "plugins",
        REVIEW_CAPABILITY,
        "split_diffs",
        UTILITY_CAPABILITY,
    }
)
SUPPORTED_METHODS = (
    "assets.list",
    "assets.read",
    *CI_METHODS,
    "commits.list",
    "diff.open",
    "diff.page",
    "discussions.list",
    "host.set_location",
    "jobs.list",
    "logs.open",
    "logs.page",
    "pipelines.list",
    "plugins.invoke",
    "plugins.list",
    "repositories.discover",
    "repositories.open",
    "review_pipelines.list",
    *UTILITY_METHODS,
    *REVIEW_METHODS,
    "reviews.get",
    "reviews.list",
)

_MAX_PROTOCOL_ERRORS = 8
_DEFAULT_SHUTDOWN_TIMEOUT = 5.0


class AsyncByteWriter(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...


class SessionResource(Protocol):
    @property
    def config(self) -> object: ...

    async def start(self) -> object: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class RequestContext:
    request_id: str
    cancellation: DesktopCancellation
    mutation: bool = False
    dispatched: bool = False
    response_started: bool = False


type OperationHandler = Callable[[JsonObject, RequestContext], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class _Operation:
    handler: OperationHandler
    mutation: bool


@dataclass(slots=True)
class _PendingRequest:
    task: asyncio.Task[None]
    context: RequestContext


@dataclass(frozen=True, slots=True)
class _QueuedEvent:
    sequence: int
    name: str
    data: object
    replaceable_key: str | None


class _EventBuffer:
    """Bounded event buffer with safe coalescing and explicit resync markers."""

    def __init__(self, max_events: int = MAX_QUEUED_EVENTS) -> None:
        self._max_events = max_events
        self._events: deque[_QueuedEvent] = deque()
        self._condition = asyncio.Condition()
        self._sequence = 0
        self._closed = False

    async def publish(
        self, name: str, data: object, *, replaceable_key: str | None = None
    ) -> None:
        async with self._condition:
            if self._closed:
                return
            if replaceable_key is not None:
                for index, event in enumerate(self._events):
                    if event.replaceable_key == replaceable_key:
                        replacement = _QueuedEvent(
                            event.sequence, name, data, replaceable_key
                        )
                        try:
                            encode_event(replacement.sequence, name, data)
                        except ProtocolError:
                            self._replace_with_resync("event_too_large")
                        else:
                            self._events[index] = replacement
                        self._condition.notify()
                        return
            self._sequence += 1
            event = _QueuedEvent(self._sequence, name, data, replaceable_key)
            try:
                encode_event(event.sequence, name, data)
            except ProtocolError:
                self._replace_with_resync("event_too_large")
            else:
                if len(self._events) >= self._max_events:
                    self._replace_with_resync("queue_overflow")
                else:
                    self._events.append(event)
            self._condition.notify()

    async def get(self) -> _QueuedEvent | None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._events or self._closed)
            if self._events:
                return self._events.popleft()
            return None

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._events.clear()
            self._condition.notify_all()

    def _replace_with_resync(self, reason: str) -> None:
        self._events.clear()
        self._sequence += 1
        self._events.append(
            _QueuedEvent(
                self._sequence,
                "protocol.resync_required",
                {"reason": reason},
                "protocol.resync_required",
            )
        )


class DesktopSidecarServer:
    """Own one desktop session and dispatch strict protocol-major-1 requests."""

    def __init__(
        self,
        *,
        session: ApplicationSession | None = None,
        plugin_registry: DesktopPluginRegistry | None = None,
        core_assets: tuple[CoreAssetSpec, ...] = (),
        shutdown_timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT,
    ) -> None:
        if shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout must be positive")
        self._session = session or ApplicationSession()
        self._plugin_registry = plugin_registry
        self._core_assets = core_assets
        self._shutdown_timeout = shutdown_timeout
        self._handles = HandleRegistry()
        self._snapshots = SnapshotStore()
        self._assets = AssetCatalog()
        self._operations: dict[str, _Operation] = {}
        self._pending: dict[str, _PendingRequest] = {}
        self._events = _EventBuffer()
        self._writer: AsyncByteWriter | None = None
        self._writer_lock = asyncio.Lock()
        self._event_task: asyncio.Task[None] | None = None
        self._service_event_task: asyncio.Task[None] | None = None
        self._handshaken = False
        self._stopping = False
        self._location: DesktopLocation | None = None
        self._install_read_operations()
        self._install_ci_operations()
        self._install_review_operations()
        self._install_utility_operations()

    @property
    def session_id(self) -> str:
        return self._handles.session_id

    def register_operation(
        self, method: str, handler: OperationHandler, *, mutation: bool
    ) -> None:
        """Register a future operation with explicit cancellation semantics.

        A cancel frame signals a dispatched mutation but does not cancel its task
        or claim that remote work rolled back.
        """
        if method in self._operations:
            raise ValueError(f"Duplicate sidecar operation: {method}")
        self._operations[method] = _Operation(handler, mutation)

    async def run(self, reader: asyncio.StreamReader, writer: AsyncByteWriter) -> None:
        """Run until clean shutdown, EOF, or an unrecoverable framing error."""
        self._writer = writer
        protocol_errors = 0
        try:
            while not self._stopping:
                try:
                    raw = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    await self._write_error(
                        None,
                        ProtocolError(
                            ProtocolErrorCode.FRAME_TOO_LARGE,
                            "The request frame exceeds the supported size.",
                        ),
                    )
                    break
                if raw == b"":
                    break
                try:
                    frame = decode_frame(raw)
                except ProtocolError as error:
                    protocol_errors += 1
                    await self._write_error(None, error)
                    if (
                        error.code is ProtocolErrorCode.FRAME_TOO_LARGE
                        or protocol_errors >= _MAX_PROTOCOL_ERRORS
                    ):
                        break
                    continue
                protocol_errors = 0
                if isinstance(frame, CancelFrame):
                    self._cancel(frame.request_id)
                    continue
                if frame.method == "handshake":
                    await self._handle_handshake(frame)
                    continue
                if not self._handshaken:
                    await self._write_error(
                        frame.request_id,
                        ProtocolError(
                            ProtocolErrorCode.HANDSHAKE_REQUIRED,
                            "A successful handshake is required before requests.",
                        ),
                    )
                    continue
                if frame.method == "shutdown":
                    try:
                        _require_params(frame.params, allowed=frozenset())
                    except ProtocolError as error:
                        await self._write_error(frame.request_id, error)
                        continue
                    await self._write_result(frame.request_id, {"accepted": True})
                    self._stopping = True
                    break
                await self._schedule(frame)
        finally:
            await self._shutdown()

    def _install_read_operations(self) -> None:
        operations: dict[str, OperationHandler] = {
            "assets.list": self._assets_list,
            "assets.read": self._assets_read,
            "commits.list": self._commits_list,
            "diff.open": self._diff_open,
            "diff.page": self._diff_page,
            "discussions.list": self._discussions_list,
            "host.set_location": self._set_location,
            "jobs.list": self._jobs_list,
            "logs.open": self._logs_open,
            "logs.page": self._logs_page,
            "pipelines.list": self._pipelines_list,
            "plugins.invoke": self._plugins_invoke,
            "plugins.list": self._plugins_list,
            "repositories.discover": self._repositories_discover,
            "repositories.open": self._repositories_open,
            "review_pipelines.list": self._review_pipelines_list,
            "reviews.get": self._reviews_get,
            "reviews.list": self._reviews_list,
        }
        for method, handler in operations.items():
            self.register_operation(method, handler, mutation=False)

    def _install_ci_operations(self) -> None:
        operations = CIOperations(session=self._session, handles=self._handles)
        for method, (handler, mutation) in operations.handlers.items():
            self.register_operation(
                method, cast(OperationHandler, handler), mutation=mutation
            )

    def _install_review_operations(self) -> None:
        operations = ReviewOperations(session=self._session, handles=self._handles)
        for method, (handler, mutation) in operations.handlers.items():
            self.register_operation(
                method, cast(OperationHandler, handler), mutation=mutation
            )

    def _install_utility_operations(self) -> None:
        operations = UtilityOperations(session=self._session, handles=self._handles)
        for method, (handler, mutation) in operations.handlers.items():
            self.register_operation(
                method, cast(OperationHandler, handler), mutation=mutation
            )

    async def _handle_handshake(self, frame: RequestFrame) -> None:
        if self._handshaken:
            await self._write_error(
                frame.request_id,
                ProtocolError(
                    ProtocolErrorCode.ALREADY_HANDSHAKEN,
                    "The desktop connection has already completed its handshake.",
                ),
            )
            return
        try:
            requested = _parse_handshake(frame.params)
            await self._session.start()
            if self._plugin_registry is None:
                config = cast(object, self._session.config)
                plugin_config = getattr(config, "plugin_config", {})
                self._plugin_registry = DesktopPluginRegistry(plugin_config)
            self._plugin_registry.discover()
            await self._plugin_registry.start_all(self._facade_for_plugin)
            self._assets.stage_core(self._core_assets)
            self._assets.stage_plugins(self._plugin_registry)
            self._handshaken = True
            await self._write_result(
                frame.request_id,
                {
                    "protocol_major": 1,
                    "core_version": _core_version(),
                    "session_id": self.session_id,
                    "capabilities": sorted(SUPPORTED_CAPABILITIES),
                    "accepted_capabilities": sorted(requested),
                    "methods": [*sorted(self._operations), "shutdown"],
                    "limits": {
                        "request_frame_bytes": MAX_REQUEST_FRAME_BYTES,
                        "response_frame_bytes": MAX_RESPONSE_FRAME_BYTES,
                        "event_frame_bytes": MAX_EVENT_FRAME_BYTES,
                        "pending_requests": MAX_PENDING_REQUESTS,
                        "queued_events": MAX_QUEUED_EVENTS,
                        "asset_chunk_bytes": ASSET_CHUNK_BYTES,
                    },
                },
            )
            self._event_task = asyncio.create_task(self._pump_events())
            self._service_event_task = asyncio.create_task(self._pump_service_events())
        except ProtocolError as error:
            await self._write_error(frame.request_id, error)
        except ServiceError as error:
            await self._write_error(frame.request_id, _service_protocol_error(error))
            self._stopping = True
        except Exception:  # noqa: BLE001 - startup boundary must stay redacted.
            await self._write_error(
                frame.request_id,
                ProtocolError(
                    ProtocolErrorCode.INTERNAL,
                    "The desktop session could not be started.",
                ),
            )
            self._stopping = True

    async def _schedule(self, frame: RequestFrame) -> None:
        if self._stopping:
            await self._write_error(
                frame.request_id,
                ProtocolError(
                    ProtocolErrorCode.SHUTTING_DOWN,
                    "The desktop sidecar is shutting down.",
                ),
            )
            return
        if frame.request_id in self._pending:
            await self._write_error(
                frame.request_id,
                ProtocolError(
                    ProtocolErrorCode.DUPLICATE_REQUEST,
                    "The request id is already pending.",
                ),
            )
            return
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            await self._write_error(
                frame.request_id,
                ProtocolError(
                    ProtocolErrorCode.TOO_MANY_REQUESTS,
                    "The pending request limit was reached.",
                    retryable=True,
                ),
            )
            return
        operation = self._operations.get(frame.method)
        if operation is None:
            await self._write_error(
                frame.request_id,
                ProtocolError(
                    ProtocolErrorCode.UNKNOWN_METHOD,
                    "The requested desktop method is not supported.",
                ),
            )
            return
        context = RequestContext(
            frame.request_id, DesktopCancellation(), mutation=operation.mutation
        )
        task = asyncio.create_task(self._serve(frame, operation, context))
        self._pending[frame.request_id] = _PendingRequest(task, context)

    async def _serve(
        self, frame: RequestFrame, operation: _Operation, context: RequestContext
    ) -> None:
        try:
            context.dispatched = True
            result = await operation.handler(frame.params, context)
            context.response_started = True
            await self._write_result(frame.request_id, result)
        except asyncio.CancelledError:
            if context.response_started:
                raise
            with suppress(Exception):
                await self._write_error(
                    frame.request_id,
                    ProtocolError(
                        ProtocolErrorCode.REQUEST_CANCELLED,
                        "The request was cancelled.",
                        retryable=not context.mutation,
                        details={"outcome": "unknown"}
                        if context.mutation and context.dispatched
                        else None,
                    ),
                )
        except ProtocolError as error:
            await self._write_error(frame.request_id, error)
        except ServiceError as error:
            await self._write_error(frame.request_id, _service_protocol_error(error))
        except DesktopPluginContractError as error:
            await self._write_error(frame.request_id, _plugin_protocol_error(error))
        except (BrokenPipeError, ConnectionError):
            self._stopping = True
        except Exception:  # noqa: BLE001 - operation boundary must stay redacted.
            if context.response_started:
                self._stopping = True
                return
            await self._write_error(
                frame.request_id,
                ProtocolError(
                    ProtocolErrorCode.INTERNAL,
                    "The desktop operation failed.",
                ),
            )
        finally:
            current = self._pending.get(frame.request_id)
            if current is not None and current.task is asyncio.current_task():
                self._pending.pop(frame.request_id, None)

    def _cancel(self, request_id: str) -> None:
        pending = self._pending.get(request_id)
        if pending is None:
            return
        pending.context.cancellation.cancel()
        if not pending.context.mutation and not pending.context.response_started:
            pending.task.cancel()

    async def _repositories_discover(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(params, allowed=frozenset())
        repositories = await self._session.discover_repositories()
        return {"repositories": [self._repository_wire(item) for item in repositories]}

    async def _repositories_open(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"hostname", "project_path"}),
            required=frozenset({"hostname", "project_path"}),
        )
        hostname = _text(params, "hostname", max_length=253)
        project_path = _text(params, "project_path", max_length=500)
        snapshot = await self._session.open_repository(hostname, project_path)
        return self._repository_wire(snapshot)

    async def _reviews_list(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"scope", "repository", "state", "per_page"}),
            required=frozenset({"scope"}),
        )
        try:
            scope = ReviewScope(_text(params, "scope", max_length=40))
            state = MRState(_optional_text(params, "state", "open", max_length=20))
        except ValueError:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "The review scope or state is invalid.",
            ) from None
        repository = None
        if "repository" in params and params["repository"] is not None:
            repository = self._handles.resolve(
                params["repository"], HandleKind.REPOSITORY, RepositoryRef
            )
        per_page = _optional_int(params, "per_page", 100, minimum=1, maximum=100)
        page = await self._session.list_reviews(
            ReviewQuery(scope, repository=repository, state=state, per_page=per_page)
        )
        return {
            "items": [self._review_list_wire(item) for item in page.items],
            "failures": [
                {
                    "code": failure.code.value,
                    "message": failure.message,
                    "retryable": failure.retryable,
                    "repository": self._handles.find(
                        HandleKind.REPOSITORY, failure.repository
                    )
                    if failure.repository is not None
                    else None,
                }
                for failure in page.failures
            ],
        }

    async def _reviews_get(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        review = self._review_param(params)
        snapshot = await self._session.get_review(review)
        return self._review_snapshot_wire(snapshot)

    async def _diff_open(self, params: JsonObject, _context: RequestContext) -> object:
        _require_params(
            params,
            allowed=frozenset({"review", "layout", "max_items"}),
            required=frozenset({"review"}),
        )
        try:
            layout = DiffLayout(
                _optional_text(params, "layout", "unified", max_length=20)
            )
        except ValueError:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "The diff layout is invalid.",
            ) from None
        review_handle = _text(params, "review", max_length=100)
        review = self._handles.resolve(review_handle, HandleKind.REVIEW, ReviewRef)
        raw = await self._session.get_raw_diff(review)
        files = convert_forge_changes(raw.changes)
        entries = flatten_diff(files, layout)
        revision = cast(JsonObject, to_json_value(raw.revision))
        snapshot_id = self._snapshots.create(
            review_handle, revision, entries, projection=layout.value
        )
        return self._snapshot_page_wire(
            self._snapshots.page(
                snapshot_id,
                review_handle,
                0,
                params.get("max_items", 400),
            )
        )

    async def _diff_page(self, params: JsonObject, _context: RequestContext) -> object:
        _require_params(
            params,
            allowed=frozenset({"snapshot", "resource", "cursor", "max_items"}),
            required=frozenset({"snapshot", "resource", "cursor"}),
        )
        self._handles.resolve(params["resource"], HandleKind.REVIEW, ReviewRef)
        page = self._snapshots.page(
            params["snapshot"],
            params["resource"],
            params["cursor"],
            params.get("max_items", 400),
        )
        return self._snapshot_page_wire(page)

    async def _discussions_list(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        review = self._review_param(params)
        return {"discussions": await self._session.get_discussions(review)}

    async def _commits_list(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        review = self._review_param(params)
        return {"commits": await self._session.get_commits(review)}

    async def _pipelines_list(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"repository", "per_page"}),
            required=frozenset({"repository"}),
        )
        repository = self._handles.resolve(
            params["repository"], HandleKind.REPOSITORY, RepositoryRef
        )
        per_page = _optional_int(params, "per_page", 20, minimum=1, maximum=100)
        pipelines = await self._session.list_pipelines(repository, per_page=per_page)
        return {
            "pipelines": [self._pipeline_wire(repository, item) for item in pipelines]
        }

    async def _review_pipelines_list(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"review", "per_page"}),
            required=frozenset({"review"}),
        )
        review = self._handles.resolve(params["review"], HandleKind.REVIEW, ReviewRef)
        per_page = _optional_int(params, "per_page", 20, minimum=1, maximum=100)
        pipelines = await self._session.list_review_pipelines(review, per_page=per_page)
        return {
            "pipelines": [
                self._pipeline_wire(review.repository, item) for item in pipelines
            ]
        }

    async def _jobs_list(self, params: JsonObject, _context: RequestContext) -> object:
        _require_params(
            params,
            allowed=frozenset({"pipeline"}),
            required=frozenset({"pipeline"}),
        )
        pipeline = self._handles.resolve(
            params["pipeline"], HandleKind.PIPELINE, PipelineRef
        )
        jobs = await self._session.get_pipeline_jobs(pipeline)
        return {"jobs": [self._job_wire(pipeline, item) for item in jobs]}

    async def _logs_open(self, params: JsonObject, _context: RequestContext) -> object:
        _require_params(
            params,
            allowed=frozenset({"job", "max_items"}),
            required=frozenset({"job"}),
        )
        job_handle = _text(params, "job", max_length=100)
        job = self._handles.resolve(job_handle, HandleKind.JOB, JobRef)
        text = await self._session.get_job_log(job)
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_SNAPSHOT_RETAINED_BYTES:
            raise ProtocolError(
                ProtocolErrorCode.RESPONSE_TOO_LARGE,
                "The job log exceeds the session retention limit.",
            )
        revision: JsonObject = {
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "byte_count": len(encoded),
        }
        snapshot_id = self._snapshots.create(
            job_handle, revision, _log_entries(encoded)
        )
        return self._snapshot_page_wire(
            self._snapshots.page(
                snapshot_id,
                job_handle,
                0,
                params.get("max_items", 8),
            )
        )

    async def _logs_page(self, params: JsonObject, _context: RequestContext) -> object:
        _require_params(
            params,
            allowed=frozenset({"snapshot", "resource", "cursor", "max_items"}),
            required=frozenset({"snapshot", "resource", "cursor"}),
        )
        self._handles.resolve(params["resource"], HandleKind.JOB, JobRef)
        return self._snapshot_page_wire(
            self._snapshots.page(
                params["snapshot"],
                params["resource"],
                params["cursor"],
                params.get("max_items", 8),
            )
        )

    async def _plugins_list(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(params, allowed=frozenset())
        registry = self._require_plugin_registry()
        descriptors = {
            (item.plugin_id, item.asset_id): item
            for item in self._assets.descriptors
            if item.plugin_id is not None
        }
        return {
            "plugins": [
                _plugin_record_wire(record, descriptors) for record in registry.plugins
            ]
        }

    async def _plugins_invoke(
        self, params: JsonObject, context: RequestContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"plugin", "method", "params"}),
            required=frozenset({"plugin", "method", "params"}),
        )
        plugin_id = _text(params, "plugin", max_length=80)
        method = _text(params, "method", max_length=80)
        call_params = params["params"]
        if not isinstance(call_params, dict):
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "Plugin invocation parameters must be an object.",
            )
        result = await self._require_plugin_registry().call(
            plugin_id,
            method,
            cast(PluginJsonObject, call_params),
            DesktopCallContext(
                context.request_id, context.cancellation, self._location
            ),
        )
        if result.error is not None:
            raise DesktopPluginContractError(result.error)
        return {"value": result.value}

    async def _assets_list(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(params, allowed=frozenset())
        return {"assets": self._assets.descriptors}

    async def _assets_read(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"asset", "offset", "length"}),
            required=frozenset({"asset", "offset", "length"}),
        )
        return self._assets.read(params["asset"], params["offset"], params["length"])

    async def _set_location(
        self, params: JsonObject, _context: RequestContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"location"}),
            required=frozenset({"location"}),
        )
        location = params["location"]
        if location is None:
            self._location = None
        elif isinstance(location, dict):
            self._location = DesktopLocation(
                freeze_json_object(cast(PluginJsonObject, location))
            )
        else:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "Desktop location must be an object or null.",
            )
        return {"accepted": True}

    def _review_param(self, params: JsonObject) -> ReviewRef:
        _require_params(
            params,
            allowed=frozenset({"review"}),
            required=frozenset({"review"}),
        )
        return self._handles.resolve(params["review"], HandleKind.REVIEW, ReviewRef)

    def _repository_wire(self, snapshot: RepositorySnapshot) -> JsonObject:
        return {
            "handle": self._handles.issue(HandleKind.REPOSITORY, snapshot.ref),
            "display_name": snapshot.display_name,
            "forge_type": snapshot.forge_type.value,
        }

    def _review_list_wire(self, item: ReviewListItem) -> JsonObject:
        repository_handle = self._handles.issue(
            HandleKind.REPOSITORY, item.ref.repository
        )
        review_handle = self._handles.issue(HandleKind.REVIEW, item.ref)
        return {
            "handle": review_handle,
            "repository": repository_handle,
            "summary": cast(JsonValue, _summary_wire(item.summary)),
        }

    def _review_snapshot_wire(self, snapshot: ReviewSnapshot) -> JsonObject:
        repository_handle = self._handles.issue(
            HandleKind.REPOSITORY, snapshot.ref.repository
        )
        review_handle = self._handles.issue(HandleKind.REVIEW, snapshot.ref)
        result: JsonObject = {
            "handle": review_handle,
            "repository": repository_handle,
            "detail": cast(JsonValue, _detail_wire(snapshot)),
            "capabilities": cast(JsonValue, to_json_value(snapshot.capabilities)),
        }
        if snapshot.revision is not None:
            result["revision"] = to_json_value(snapshot.revision)
            result["revision_error"] = None
        else:
            assert snapshot.revision_error is not None
            result["revision"] = None
            result["revision_error"] = {
                "code": snapshot.revision_error.code.value,
                "message": snapshot.revision_error.message,
                "retryable": snapshot.revision_error.retryable,
            }
        return result

    def _pipeline_wire(
        self, repository: RepositoryRef, pipeline: Pipeline
    ) -> JsonObject:
        ref = PipelineRef(repository, pipeline.id)
        return {
            "handle": self._handles.issue(HandleKind.PIPELINE, ref),
            "value": cast(JsonValue, to_json_value(pipeline)),
        }

    def _job_wire(self, pipeline: PipelineRef, job: PipelineJob) -> JsonObject:
        ref = JobRef(pipeline.repository, job.id)
        return {
            "handle": self._handles.issue(HandleKind.JOB, ref, parent=pipeline),
            "value": cast(JsonValue, to_json_value(job)),
        }

    @staticmethod
    def _snapshot_page_wire(page: object) -> JsonObject:
        value = cast(JsonObject, to_json_value(page))
        value["resource"] = value.pop("resource_handle")
        value.pop("projection")
        return value

    def _facade_for_plugin(
        self, plugin_id: str, manifest: DesktopPluginManifest
    ) -> DesktopHostFacade:
        return _BoundPluginFacade(self, plugin_id, manifest)

    def _require_plugin_registry(self) -> DesktopPluginRegistry:
        if self._plugin_registry is None:
            raise ProtocolError(
                ProtocolErrorCode.INTERNAL,
                "The desktop plugin registry is unavailable.",
            )
        return self._plugin_registry

    async def _dispatch_plugin_read(
        self,
        kind: DesktopReadKind,
        params: FrozenJsonObject,
        cancellation: DesktopCancellation,
    ) -> FrozenJsonValue:
        method = {
            DesktopReadKind.REPOSITORIES: "repositories.discover",
            DesktopReadKind.REVIEWS: "reviews.list",
            DesktopReadKind.REVIEW: "reviews.get",
            DesktopReadKind.DISCUSSIONS: "discussions.list",
            DesktopReadKind.COMMITS: "commits.list",
            DesktopReadKind.PIPELINES: "pipelines.list",
            DesktopReadKind.JOBS: "jobs.list",
            DesktopReadKind.LOG: "logs.open",
        }[kind]
        operation = self._operations[method]
        context = RequestContext(
            f"plugin-{id(cancellation):x}",
            cancellation,
            mutation=False,
            dispatched=True,
        )
        task = asyncio.create_task(
            operation.handler(cast(JsonObject, _thaw_json(params)), context)
        )
        cancellation_task = asyncio.create_task(cancellation.wait())
        completed = False
        try:
            done, _pending = await asyncio.wait(
                (task, cancellation_task), return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation_task in done:
                raise asyncio.CancelledError
            result = freeze_json(cast(PluginJsonObject, to_json_value(task.result())))
            completed = True
            return result
        finally:
            if not completed:
                cancellation.cancel()
            task.cancel()
            cancellation_task.cancel()
            await _bounded_tasks_cleanup(
                (cast(asyncio.Task[object], task), cancellation_task),
                self._shutdown_timeout,
            )

    async def _publish_plugin_event(
        self, plugin_id: str, event_id: str, payload: FrozenJsonObject
    ) -> None:
        await self._events.publish(
            "plugin.event",
            {"plugin": plugin_id, "event": event_id, "payload": payload},
            replaceable_key=f"plugin:{plugin_id}:{event_id}",
        )

    async def _publish_plugin_notification(
        self,
        plugin_id: str,
        message: str,
        severity: DesktopNotificationSeverity,
    ) -> None:
        await self._events.publish(
            "plugin.notification",
            {"plugin": plugin_id, "message": message, "severity": severity.value},
        )

    async def _publish_plugin_focus(
        self, plugin_id: str, target_id: str, metadata: FrozenJsonObject
    ) -> None:
        await self._events.publish(
            "plugin.focus",
            {"plugin": plugin_id, "target": target_id, "metadata": metadata},
            replaceable_key=f"plugin-focus:{plugin_id}",
        )

    async def _pump_events(self) -> None:
        while True:
            event = await self._events.get()
            if event is None:
                return
            try:
                frame = encode_event(event.sequence, event.name, event.data)
                await self._write(frame)
            except (ProtocolError, BrokenPipeError, ConnectionError):
                return

    async def _pump_service_events(self) -> None:
        try:
            async for event in self._session.events():
                await self._publish_service_event(event)
        except (asyncio.CancelledError, ServiceError):
            return

    async def _publish_service_event(self, event: ServiceEvent) -> None:
        resource_handle: str | None = None
        if isinstance(event.resource, RepositoryRef):
            resource_handle = self._handles.find(HandleKind.REPOSITORY, event.resource)
        elif isinstance(event.resource, ReviewRef):
            resource_handle = self._handles.find(HandleKind.REVIEW, event.resource)
            if resource_handle is not None:
                self._snapshots.expire_for_resource(resource_handle)
        elif isinstance(event.resource, PipelineRef):
            resource_handle = self._handles.find(HandleKind.PIPELINE, event.resource)
        await self._events.publish(
            "service.changed",
            {
                "kind": event.kind.value,
                "resource": resource_handle,
                "revision": event.revision,
                "source_sequence": event.sequence,
            },
            replaceable_key=f"service:{event.kind.value}:{resource_handle or '*'}",
        )

    async def _write_result(self, request_id: str, result: object) -> None:
        await self._write(encode_response(request_id, result=result))

    async def _write_error(self, request_id: str | None, error: ProtocolError) -> None:
        await self._write(encode_response(request_id, error=error))

    async def _write(self, frame: bytes) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            await asyncio.wait_for(
                self._writer_lock.acquire(), timeout=self._shutdown_timeout
            )
        except TimeoutError as error:
            raise ConnectionError("desktop protocol writer is backlogged") from error
        try:
            writer.write(frame)
            drain_task = asyncio.create_task(writer.drain())
            try:
                _done, pending = await asyncio.wait(
                    (drain_task,), timeout=self._shutdown_timeout
                )
            except BaseException:
                drain_task.cancel()
                drain_task.add_done_callback(_consume_task)
                raise
            if pending:
                drain_task.cancel()
                drain_task.add_done_callback(_consume_task)
                raise ConnectionError("desktop protocol writer did not drain")
            drain_task.result()
        finally:
            self._writer_lock.release()

    async def _shutdown(self) -> None:
        if not self._stopping:
            self._stopping = True
        for pending in tuple(self._pending.values()):
            pending.context.cancellation.cancel()
            pending.task.cancel()
        tasks = {pending.task for pending in self._pending.values()}
        if tasks:
            _done, survivors = await asyncio.wait(tasks, timeout=self._shutdown_timeout)
            for task in survivors:
                task.cancel()
                task.add_done_callback(_consume_task)
        self._pending.clear()

        if self._service_event_task is not None:
            self._service_event_task.cancel()
            await _bounded_task_cleanup(
                self._service_event_task, self._shutdown_timeout
            )

        registry = self._plugin_registry
        if registry is not None:
            await _bounded_cleanup(registry.stop_all(), self._shutdown_timeout)
        await _bounded_cleanup(self._session.close(), self._shutdown_timeout)
        self._snapshots.clear()
        self._handles.clear()
        self._assets.clear()
        await self._events.close()
        if self._event_task is not None:
            self._event_task.cancel()
            await _bounded_task_cleanup(self._event_task, self._shutdown_timeout)


class _BoundPluginFacade:
    """Facade bound to one manifest and one sidecar session."""

    def __init__(
        self,
        server: DesktopSidecarServer,
        plugin_id: str,
        manifest: DesktopPluginManifest,
    ) -> None:
        self._server = server
        self._plugin_id = plugin_id
        self._reads = frozenset(manifest.reads)
        self._methods = frozenset(item.id for item in manifest.methods)
        self._events = frozenset(item.id for item in manifest.events)
        self._focus_targets = frozenset(item.id for item in manifest.focus_targets)

    async def read(
        self,
        kind: DesktopReadKind,
        params: FrozenJsonObject,
        cancellation: DesktopCancellation,
    ) -> FrozenJsonValue:
        if kind not in self._reads:
            raise _plugin_contract_error(
                self._plugin_id, "undeclared_read", "Plugin read is not declared."
            )
        return await self._server._dispatch_plugin_read(kind, params, cancellation)

    async def invoke(
        self,
        method: str,
        params: FrozenJsonObject,
        context: DesktopCallContext,
    ) -> FrozenJsonValue:
        if method not in self._methods:
            raise _plugin_contract_error(
                self._plugin_id,
                "undeclared_method",
                "Plugin method is not declared.",
            )
        result = await self._server._require_plugin_registry().call(
            self._plugin_id,
            method,
            cast(PluginJsonObject, _thaw_json(params)),
            context,
        )
        if result.error is not None:
            raise DesktopPluginContractError(result.error)
        return result.value

    async def notify(
        self,
        message: str,
        severity: DesktopNotificationSeverity = DesktopNotificationSeverity.INFORMATION,
    ) -> None:
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message) > 500
            or any(
                ord(character) < 32 and character not in "\t\n" for character in message
            )
            or not isinstance(severity, DesktopNotificationSeverity)
        ):
            raise _plugin_contract_error(
                self._plugin_id, "invalid_data", "Plugin notification is invalid."
            )
        await self._server._publish_plugin_notification(
            self._plugin_id, message, severity
        )

    async def publish_event(self, event_id: str, payload: FrozenJsonObject) -> None:
        if event_id not in self._events:
            raise _plugin_contract_error(
                self._plugin_id, "undeclared_target", "Plugin event is not declared."
            )
        frozen = freeze_json_object(cast(PluginJsonObject, _thaw_json(payload)))
        await self._server._publish_plugin_event(self._plugin_id, event_id, frozen)

    def current_location(self) -> DesktopLocation | None:
        return self._server._location

    async def focus(self, target_id: str, metadata: FrozenJsonObject) -> None:
        if target_id not in self._focus_targets:
            raise _plugin_contract_error(
                self._plugin_id,
                "undeclared_target",
                "Plugin focus target is not declared.",
            )
        frozen = freeze_json_object(cast(PluginJsonObject, _thaw_json(metadata)))
        await self._server._publish_plugin_focus(self._plugin_id, target_id, frozen)


def _parse_handshake(params: JsonObject) -> frozenset[str]:
    _require_params(
        params,
        allowed=frozenset({"protocol_major", "core_version", "capabilities", "client"}),
        required=frozenset({"protocol_major", "core_version", "capabilities"}),
    )
    major = params["protocol_major"]
    if type(major) is not int or major != 1:
        raise ProtocolError(
            ProtocolErrorCode.UNSUPPORTED_PROTOCOL,
            "The requested desktop protocol major is not supported.",
        )
    core_version = params["core_version"]
    if not isinstance(core_version, str) or core_version != _core_version():
        raise ProtocolError(
            ProtocolErrorCode.UNSUPPORTED_PROTOCOL,
            "The desktop client requires a different Tongs core version.",
        )
    capabilities = params["capabilities"]
    if not isinstance(capabilities, list) or not all(
        isinstance(item, str) for item in capabilities
    ):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            "Handshake capabilities must be a list of names.",
        )
    requested = frozenset(capabilities)
    if len(requested) != len(capabilities) or not requested <= SUPPORTED_CAPABILITIES:
        raise ProtocolError(
            ProtocolErrorCode.UNSUPPORTED_PROTOCOL,
            "One or more required desktop capabilities are not supported.",
        )
    if "client" in params:
        _text(params, "client", max_length=120)
    return requested


def _require_params(
    params: JsonObject,
    *,
    allowed: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> None:
    keys = frozenset(params)
    if not required <= keys or not keys <= allowed:
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            "The request parameters have unknown or missing fields.",
        )


def _text(params: JsonObject, key: str, *, max_length: int) -> str:
    value = params.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            f"The {key} parameter is invalid.",
        )
    return value


def _optional_text(
    params: JsonObject, key: str, default: str, *, max_length: int
) -> str:
    if key not in params:
        return default
    return _text(params, key, max_length=max_length)


def _optional_int(
    params: JsonObject,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = params.get(key, default)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            f"The {key} parameter is invalid.",
        )
    return value


def _summary_wire(summary: MRSummary) -> JsonObject:
    return {
        "number": summary.number,
        "title": summary.title,
        "author": cast(JsonValue, to_json_value(summary.author)),
        "state": summary.state.value,
        "is_draft": summary.is_draft,
        "source_branch": summary.source_branch,
        "target_branch": summary.target_branch,
        "ci_status": summary.ci_status.value,
        "created_at": cast(JsonValue, to_json_value(summary.created_at)),
        "updated_at": cast(JsonValue, to_json_value(summary.updated_at)),
        "web_url": summary.web_url,
        "comment_count": summary.comment_count,
        "has_conflicts": summary.has_conflicts,
        "labels": cast(JsonValue, to_json_value(summary.labels)),
        "review_decision": cast(JsonValue, to_json_value(summary.review_decision)),
        "additions": summary.additions,
        "deletions": summary.deletions,
    }


def _detail_wire(snapshot: ReviewSnapshot) -> JsonObject:
    detail = snapshot.detail
    result = _summary_wire(detail)
    result.update(
        {
            "description": detail.description,
            "merge_status": detail.merge_status,
            "approvals": cast(JsonValue, to_json_value(detail.approvals)),
            "reviewers": cast(JsonValue, to_json_value(detail.reviewers)),
            "assignees": cast(JsonValue, to_json_value(detail.assignees)),
            "changes_count": detail.changes_count,
            "detailed_merge_status": detail.detailed_merge_status,
            "draft_notes_count": detail.draft_notes_count,
            "status_check_rollup": detail.status_check_rollup,
        }
    )
    return result


def _log_entries(encoded: bytes) -> tuple[JsonObject, ...]:
    """Split UTF-8 log bytes into independently decodable bounded rows."""
    if not encoded:
        return ({"text": ""},)
    entries: list[JsonObject] = []
    start = 0
    target = 256 * 1024
    while start < len(encoded):
        end = min(start + target, len(encoded))
        while True:
            try:
                text = encoded[start:end].decode("utf-8")
                break
            except UnicodeDecodeError as error:
                end = start + error.start
                if end <= start:
                    raise ProtocolError(
                        ProtocolErrorCode.INTERNAL,
                        "The job log could not be encoded safely.",
                    ) from None
        entries.append({"text": text})
        start = end
    return tuple(entries)


def _plugin_record_wire(
    record: DesktopPluginRecord,
    descriptors: Mapping[tuple[str, str], AssetDescriptor],
) -> JsonObject:
    plugin_id = record.plugin_id
    state = record.state
    manifest = record.manifest
    error = record.error
    result: JsonObject = {
        "plugin_id": plugin_id,
        "state": state.value,
        "has_terminal_entry_point": record.has_terminal_entry_point,
        "has_desktop_entry_point": record.has_desktop_entry_point,
    }
    if error is not None:
        result["error"] = {
            "code": error.code.value,
            "message": error.message,
            "retryable": error.retryable,
        }
    else:
        result["error"] = None
    if manifest is None:
        result["manifest"] = None
        return result

    assets_available = state is DesktopPluginState.STARTED

    def asset_handle(asset_id: str) -> str | None:
        if not assets_available:
            return None
        descriptor = descriptors.get((plugin_id, asset_id))
        if descriptor is None:
            raise ProtocolError(
                ProtocolErrorCode.INTERNAL,
                "A declared plugin asset is unavailable.",
            )
        return descriptor.handle

    result["manifest"] = {
        "title": manifest.title,
        "version": manifest.version,
        "api_major": manifest.compatibility.api_major,
        "modules": [
            {
                "id": module.id,
                "title": module.title,
                "entry_asset": asset_handle(module.entry_asset_id),
                "stylesheets": [
                    asset_handle(item) for item in module.stylesheet_asset_ids
                ],
            }
            for module in manifest.modules
        ],
        "navigation": [to_json_value(item) for item in manifest.navigation],
        "commands": [to_json_value(item) for item in manifest.commands],
        "methods": [item.id for item in manifest.methods],
        "events": [item.id for item in manifest.events],
        "focus_targets": [to_json_value(item) for item in manifest.focus_targets],
        "help_asset": asset_handle(manifest.help_asset_id)
        if manifest.help_asset_id is not None
        else None,
        "reads": [item.value for item in manifest.reads],
        "assets_available": assets_available,
    }
    return result


def _service_protocol_error(error: ServiceError) -> ProtocolError:
    details: dict[str, str] = {"service_code": error.code.value}
    details.update(dict(error.details))
    return ProtocolError(
        ProtocolErrorCode.SERVICE_ERROR,
        error.message,
        retryable=error.retryable,
        details=details,
    )


def _plugin_protocol_error(error: DesktopPluginContractError) -> ProtocolError:
    return ProtocolError(
        ProtocolErrorCode.PLUGIN_ERROR,
        error.error.message,
        retryable=error.error.retryable,
        details={
            "plugin_id": error.error.plugin_id,
            "plugin_code": error.error.code.value,
        },
    )


def _plugin_contract_error(
    plugin_id: str, code: str, message: str
) -> DesktopPluginContractError:
    return DesktopPluginContractError(
        DesktopPluginError(DesktopPluginErrorCode(code), message, plugin_id)
    )


def _thaw_json(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    if isinstance(value, list):
        return [_thaw_json(item) for item in value]
    raise ProtocolError(
        ProtocolErrorCode.INVALID_PARAMS,
        "The plugin data is not valid JSON.",
    )


async def _bounded_cleanup(awaitable: Awaitable[object], timeout: float) -> None:
    task = asyncio.create_task(awaitable)
    _done, pending = await asyncio.wait((task,), timeout=timeout)
    if pending:
        task.cancel()
        task.add_done_callback(_consume_task)
        return
    with suppress(BaseException):
        task.result()


async def _bounded_task_cleanup(task: asyncio.Task[object], timeout: float) -> None:
    _done, pending = await asyncio.wait((task,), timeout=timeout)
    if pending:
        task.cancel()
        task.add_done_callback(_consume_task)
        return
    with suppress(BaseException):
        task.result()


async def _bounded_tasks_cleanup(
    tasks: tuple[asyncio.Task[object], ...], timeout: float
) -> None:
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
        task.add_done_callback(_consume_task)
    for task in done:
        _consume_task(task)


def _consume_task(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        with suppress(BaseException):
            task.exception()


def _core_version() -> str:
    try:
        return version("tongs")
    except PackageNotFoundError:
        return "0+unknown"


__all__ = [
    "SUPPORTED_CAPABILITIES",
    "SUPPORTED_METHODS",
    "AsyncByteWriter",
    "DesktopSidecarServer",
    "OperationHandler",
    "RequestContext",
]
