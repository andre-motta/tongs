"""Async application session and UI-independent read services."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Protocol, Self, TypeVar, cast

from tongs.cache.store import CacheStore
from tongs.config import Config, load_config
from tongs.forges.base import ForgeClient
from tongs.forges.models import (
    Commit,
    Discussion,
    ForgeHost,
    MRDetail,
    MRSummary,
)
from tongs.forges.registry import ForgeRegistry
from tongs.scanner.discovery import discover_repos
from tongs.scanner.repo import Repo
from tongs.services.errors import ServiceError, ServiceErrorCode, translate_error
from tongs.services.models import (
    ForgeCapabilities,
    HostFailure,
    JobRef,
    Pipeline,
    PipelineJob,
    PipelineRef,
    RawDiffSnapshot,
    RepositoryRef,
    RepositorySnapshot,
    ReviewListItem,
    ReviewPage,
    ReviewQuery,
    ReviewRef,
    ReviewRevision,
    ReviewScope,
    ReviewSnapshot,
    ServiceEvent,
    ServiceEventKind,
    validate_hostname,
)


class CacheResource(Protocol):
    """Cache lifecycle required by the application session."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...


class ForgeRegistryResource(Protocol):
    """Forge-registry operations required by read services."""

    def active_hostnames(self) -> list[str]: ...

    def get_host(self, hostname: str) -> ForgeHost | None: ...

    async def get_client(self, hostname: str) -> ForgeClient: ...

    async def close_all(self) -> None: ...


ConfigLoader = Callable[[Path | None], Config]
RepositoryDiscoverer = Callable[..., Sequence[Repo]]
_EVENT_STREAM_CLOSED = object()
_ResultT = TypeVar("_ResultT")
_ItemT = TypeVar("_ItemT")


class _SessionState(Enum):
    NEW = "new"
    STARTING = "starting"
    STARTED = "started"
    CLOSING = "closing"
    CLOSED = "closed"


class ApplicationSession:
    """Own shared Python resources and expose typed asynchronous read services.

    ``RepositoryRef`` and ``ReviewRef`` are semantic identities, not renderer
    authorization tokens. This session accepts only repository identities it issued
    from discovery, configured-host inbox results, or :meth:`open_repository`.
    Renderer-facing opaque handles and reconnection semantics belong to S6.
    """

    def __init__(
        self,
        *,
        config: Config | None = None,
        config_path: Path | None = None,
        cache_path: Path | None = None,
        cache: CacheResource | None = None,
        forge_registry: ForgeRegistryResource | None = None,
        config_loader: ConfigLoader = load_config,
        discoverer: RepositoryDiscoverer = discover_repos,
        shutdown_timeout: float = 5.0,
        event_queue_size: int = 256,
    ) -> None:
        if shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout must be positive")
        if event_queue_size <= 0:
            raise ValueError("event_queue_size must be positive")
        self._provided_config = config
        self._config_path = config_path
        self._cache_path = cache_path
        self._provided_cache = cache
        self._provided_registry = forge_registry
        self._config_loader = config_loader
        self._discoverer = discoverer
        self._shutdown_timeout = shutdown_timeout
        self._event_queue_size = event_queue_size

        self._config: Config | None = None
        self._cache: CacheResource | None = None
        self._registry: ForgeRegistryResource | None = None
        self._configured_hosts: frozenset[str] = frozenset()
        self._issued_repositories: set[RepositoryRef] = set()
        self._repositories: dict[RepositoryRef, RepositorySnapshot] = {}
        self._subscribers: set[asyncio.Queue[ServiceEvent | object]] = set()
        self._sequence = 0
        self._state = _SessionState.NEW
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._cache_open_attempted = False
        self._registry_owned = False
        self._shutdown_error: ServiceError | None = None

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await self.close()
        except ServiceError:
            if exc is None:
                raise

    @property
    def config(self) -> Config:
        """Return the loaded configuration after startup."""
        self._require_started()
        return cast(Config, self._config)

    @property
    def shutdown_error(self) -> ServiceError | None:
        """Return a safe cleanup failure retained when another error took priority."""
        return self._shutdown_error

    @property
    def issued_repositories(self) -> frozenset[RepositoryRef]:
        """Return a snapshot of semantic repository identities issued this session."""
        self._require_started()
        return frozenset(self._issued_repositories)

    async def start(self) -> ApplicationSession:
        """Open owned resources exactly once and return this session."""
        async with self._start_lock:
            if self._state == _SessionState.STARTED:
                return self
            if self._state != _SessionState.NEW:
                raise ServiceError(
                    ServiceErrorCode.CLOSED,
                    "The application session cannot be started again.",
                )
            self._state = _SessionState.STARTING
            try:
                self._config = self._provided_config or self._config_loader(
                    self._config_path
                )
                self._cache = self._provided_cache or CacheStore(
                    db_path=self._cache_path,
                    max_size_mb=self._config.max_cache_size_mb,
                )
                self._cache_open_attempted = True
                await self._cache.open()
                self._registry = self._provided_registry or ForgeRegistry(
                    extra_gitlab_hosts=self._config.extra_gitlab_hosts,
                    extra_github_hosts=self._config.extra_github_hosts,
                    request_timeout=self._config.request_timeout,
                    cache=self._cache,
                    mr_list_ttl=self._config.mr_list_ttl,
                    diff_ttl=self._config.diff_ttl,
                )
                self._registry_owned = True
                hosts = self._registry.active_hostnames()
                if self._config.max_parallel <= 0:
                    raise ValueError("max_parallel must be positive")
                for hostname in hosts:
                    validate_hostname(hostname)
                self._configured_hosts = frozenset(hosts)
                self._state = _SessionState.STARTED
                return self
            except asyncio.CancelledError:
                await self._cleanup_after_failed_start()
                raise
            except Exception as error:  # noqa: BLE001 - Sanitize startup boundary.
                await self._cleanup_after_failed_start()
                raise translate_error(error, operation="start_session") from None

    async def close(self) -> None:
        """Close registry and cache once, within the configured time bound."""
        # Serialize the transition with startup. A close requested while an owned
        # resource is opening waits for startup to finish, then closes everything
        # startup created before returning.
        async with self._start_lock:
            task = await self._ensure_close_task()
        await self._await_close_task(task)

    async def _ensure_close_task(self) -> asyncio.Task[None]:
        async with self._close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._close_resources())
            return self._close_task

    async def _await_close_task(self, task: asyncio.Task[None]) -> None:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(task)
            raise

    async def _cleanup_after_failed_start(self) -> None:
        task = await self._ensure_close_task()
        try:
            await asyncio.shield(task)
        except (asyncio.CancelledError, ServiceError):
            pass

    async def _close_resources(self) -> None:
        if self._state == _SessionState.CLOSED:
            return
        self._state = _SessionState.CLOSING
        failures: list[Exception] = []
        try:
            if self._registry_owned and self._registry is not None:
                try:
                    await asyncio.wait_for(
                        self._registry.close_all(), timeout=self._shutdown_timeout
                    )
                except asyncio.CancelledError:
                    failures.append(RuntimeError("forge registry cleanup cancelled"))
                except Exception as error:  # noqa: BLE001 - Continue owned cleanup.
                    failures.append(error)
            if self._cache_open_attempted and self._cache is not None:
                try:
                    await asyncio.wait_for(
                        self._cache.close(), timeout=self._shutdown_timeout
                    )
                except asyncio.CancelledError:
                    failures.append(RuntimeError("cache cleanup cancelled"))
                except Exception as error:  # noqa: BLE001 - Report after all cleanup.
                    failures.append(error)
        finally:
            self._close_event_streams()
            self._issued_repositories.clear()
            self._repositories.clear()
            self._state = _SessionState.CLOSED
        if failures:
            self._shutdown_error = ServiceError(
                ServiceErrorCode.SHUTDOWN_FAILED,
                "One or more application resources did not close cleanly.",
            )
            raise self._shutdown_error

    async def discover_repositories(self) -> tuple[RepositorySnapshot, ...]:
        """Discover local repositories and issue their semantic references."""
        self._require_started()
        config = cast(Config, self._config)
        try:
            repos = await asyncio.to_thread(
                self._discoverer,
                config.scan_root_path,
                max_depth=config.scan_depth,
                extra_gitlab_hosts=config.extra_gitlab_hosts,
                extra_github_hosts=config.extra_github_hosts,
            )
            snapshots: dict[RepositoryRef, RepositorySnapshot] = {}
            for repo in repos:
                remote = repo.primary_remote
                if remote is None or remote.hostname not in self._configured_hosts:
                    continue
                ref = RepositoryRef(remote.hostname, remote.repo_path)
                snapshots[ref] = RepositorySnapshot(
                    ref=ref,
                    display_name=repo.display_name,
                    forge_type=remote.forge_type,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - Sanitize discovery boundary.
            raise translate_error(error, operation="discover_repositories") from None

        changed = snapshots != self._repositories
        self._repositories = snapshots
        self._issued_repositories.update(snapshots)
        if changed:
            self._emit(ServiceEventKind.REPOSITORIES_CHANGED)
        return tuple(
            sorted(snapshots.values(), key=lambda item: item.display_name.lower())
        )

    async def open_repository(
        self, hostname: str, project_path: str
    ) -> RepositorySnapshot:
        """Validate an explicit configured-host repository and issue its reference."""
        self._require_started()
        try:
            ref = RepositoryRef(hostname, project_path)
        except (TypeError, ValueError):
            raise ServiceError(
                ServiceErrorCode.INVALID_INPUT,
                "The repository identity is invalid.",
            ) from None
        if hostname not in self._configured_hosts:
            raise ServiceError(
                ServiceErrorCode.RESOURCE_NOT_ISSUED,
                "The repository host is not configured.",
            )
        client = await self._get_client(hostname, "open_repository")
        try:
            summaries = await client.list_mrs(project_path, state="open", per_page=1)
            self._validate_summaries(summaries, hostname, expected_repository=ref)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - Sanitize forge boundary.
            raise translate_error(
                error, operation="open_repository", hostname=hostname
            ) from None
        forge_host = cast(ForgeRegistryResource, self._registry).get_host(hostname)
        if forge_host is None:
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The configured forge type is unavailable.",
            )
        snapshot = RepositorySnapshot(ref, project_path, forge_host.forge_type)
        self._issue_repository(snapshot)
        return snapshot

    async def list_reviews(self, query: ReviewQuery) -> ReviewPage:
        """List reviews while preserving successful results from other hosts."""
        self._require_started()
        if query.repository is not None:
            self._require_repository(query.repository)

        operations: list[tuple[str, RepositoryRef | None]] = []
        if query.scope == ReviewScope.ALL_OPEN:
            repositories = (
                (query.repository,)
                if query.repository is not None
                else tuple(sorted(self._issued_repositories, key=repr))
            )
            operations.extend((ref.hostname, ref) for ref in repositories)
        else:
            hostnames = (
                (query.repository.hostname,)
                if query.repository is not None
                else tuple(sorted(self._configured_hosts))
            )
            operations.extend((hostname, query.repository) for hostname in hostnames)

        semaphore = asyncio.Semaphore(cast(Config, self._config).max_parallel)

        async def fetch(
            hostname: str, repository: RepositoryRef | None
        ) -> tuple[tuple[ReviewListItem, ...], HostFailure | None]:
            async with semaphore:
                try:
                    client = await self._get_client(hostname, "list_reviews")
                    if query.scope == ReviewScope.ALL_OPEN:
                        assert repository is not None
                        summaries = await client.list_mrs(
                            repository.project_path,
                            state=query.state.value,
                            per_page=query.per_page,
                        )
                    elif query.scope == ReviewScope.MY_REVIEWS:
                        summaries = await client.list_my_reviews()
                    else:
                        summaries = await client.list_my_mrs()
                    items = self._accept_summaries(
                        summaries,
                        hostname,
                        expected_repository=repository
                        if query.scope == ReviewScope.ALL_OPEN
                        else None,
                        filter_repository=repository
                        if query.scope != ReviewScope.ALL_OPEN
                        else None,
                    )
                    return items, None
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - Preserve partial results.
                    safe = translate_error(
                        error, operation="list_reviews", hostname=hostname
                    )
                    return (), HostFailure(
                        hostname=hostname,
                        code=safe.code,
                        message=safe.message,
                        retryable=safe.retryable,
                        repository=repository,
                    )

        results = await asyncio.gather(
            *(fetch(hostname, repository) for hostname, repository in operations)
        )
        items = [item for result, _failure in results for item in result]
        failures = [failure for _result, failure in results if failure is not None]
        items.sort(key=lambda item: item.summary.updated_at, reverse=True)
        return ReviewPage(tuple(items), tuple(failures))

    async def get_review(self, ref: ReviewRef) -> ReviewSnapshot:
        """Read review detail and bind it to complete revision metadata."""
        client = await self._client_for_review(ref, "get_review")
        try:
            detail = await client.get_mr_fresh(ref.repository.project_path, ref.number)
            self._validate_detail(detail, ref)
            capabilities = self._capabilities(client)
            try:
                revision = self._revision_from_detail(detail)
            except ServiceError as error:
                return ReviewSnapshot(
                    ref,
                    self._safe_detail(detail),
                    revision=None,
                    capabilities=capabilities,
                    revision_error=error,
                )
            return ReviewSnapshot(
                ref, self._safe_detail(detail), revision, capabilities
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if isinstance(error, ServiceError):
                raise
            raise translate_error(
                error, operation="get_review", hostname=ref.repository.hostname
            ) from None

    async def get_raw_diff(self, ref: ReviewRef) -> RawDiffSnapshot:
        """Read fresh raw changes and prove the complete revision stayed stable."""
        client = await self._client_for_review(ref, "get_raw_diff")
        try:
            before_detail = await client.get_mr_fresh(
                ref.repository.project_path, ref.number
            )
            self._validate_detail(before_detail, ref)
            before = self._revision_from_detail(before_detail)
            changes = await client.get_mr_diff_fresh(
                ref.repository.project_path, ref.number
            )
            after_detail = await client.get_mr_fresh(
                ref.repository.project_path, ref.number
            )
            self._validate_detail(after_detail, ref)
            after = self._revision_from_detail(after_detail)
            if before != after:
                raise ServiceError(
                    ServiceErrorCode.REVISION_CHANGED,
                    "The review changed while its diff was loading. Fetch it again.",
                    retryable=True,
                )
            if not isinstance(changes, list) or not all(
                isinstance(change, Mapping) for change in changes
            ):
                raise ServiceError(
                    ServiceErrorCode.INVALID_RESPONSE,
                    "The forge returned invalid diff data.",
                )
            copied = tuple(self._freeze_mapping(change) for change in changes)
            return RawDiffSnapshot(ref, before, copied)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if isinstance(error, ServiceError):
                raise
            raise translate_error(
                error, operation="get_raw_diff", hostname=ref.repository.hostname
            ) from None

    async def get_discussions(self, ref: ReviewRef) -> tuple[Discussion, ...]:
        """Read discussion threads for an issued review."""
        client = await self._client_for_review(ref, "get_discussions")
        return self._typed_tuple(
            await self._call(
                client.get_mr_discussions(
                    ref.repository.project_path,
                    ref.number,
                ),
                operation="get_discussions",
                hostname=ref.repository.hostname,
            ),
            Discussion,
            "discussion list",
        )

    async def get_commits(self, ref: ReviewRef) -> tuple[Commit, ...]:
        """Read commits for an issued review."""
        client = await self._client_for_review(ref, "get_commits")
        return self._typed_tuple(
            await self._call(
                client.list_mr_commits(ref.repository.project_path, ref.number),
                operation="get_commits",
                hostname=ref.repository.hostname,
            ),
            Commit,
            "commit list",
        )

    async def list_pipelines(
        self, repository: RepositoryRef, *, per_page: int = 20
    ) -> tuple[Pipeline, ...]:
        """Read recent pipelines for an issued repository."""
        self._validate_page_size(per_page)
        client = await self._client_for_repository(repository, "list_pipelines")
        return self._typed_tuple(
            await self._call(
                client.list_pipelines(repository.project_path, per_page=per_page),
                operation="list_pipelines",
                hostname=repository.hostname,
            ),
            Pipeline,
            "pipeline list",
        )

    async def list_review_pipelines(
        self, ref: ReviewRef, *, per_page: int = 20
    ) -> tuple[Pipeline, ...]:
        """Read pipelines associated with an issued review."""
        self._validate_page_size(per_page)
        client = await self._client_for_review(ref, "list_review_pipelines")
        return self._typed_tuple(
            await self._call(
                client.list_mr_pipelines(
                    ref.repository.project_path, ref.number, per_page=per_page
                ),
                operation="list_review_pipelines",
                hostname=ref.repository.hostname,
            ),
            Pipeline,
            "review pipeline list",
        )

    async def get_pipeline_jobs(self, ref: PipelineRef) -> tuple[PipelineJob, ...]:
        """Read jobs for a pipeline in an issued repository."""
        client = await self._client_for_repository(ref.repository, "get_pipeline_jobs")
        return self._typed_tuple(
            await self._call(
                client.get_pipeline_jobs(ref.repository.project_path, ref.pipeline_id),
                operation="get_pipeline_jobs",
                hostname=ref.repository.hostname,
            ),
            PipelineJob,
            "pipeline job list",
        )

    async def get_job_log(self, ref: JobRef) -> str:
        """Read a job log without storing it in service state or events."""
        client = await self._client_for_repository(ref.repository, "get_job_log")
        result = await self._call(
            client.get_job_log(ref.repository.project_path, ref.job_id),
            operation="get_job_log",
            hostname=ref.repository.hostname,
        )
        if not isinstance(result, str):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned an invalid job log.",
            )
        return result

    async def events(self) -> AsyncIterator[ServiceEvent]:
        """Subscribe to ordered, bounded resource invalidation hints."""
        self._require_started()
        queue: asyncio.Queue[ServiceEvent | object] = asyncio.Queue(
            maxsize=self._event_queue_size
        )
        self._subscribers.add(queue)
        try:
            while True:
                item = await queue.get()
                if item is _EVENT_STREAM_CLOSED:
                    return
                yield cast(ServiceEvent, item)
        finally:
            self._subscribers.discard(queue)

    def emit_change(
        self,
        kind: ServiceEventKind,
        resource: RepositoryRef | ReviewRef | PipelineRef | None = None,
        revision: ReviewRevision | None = None,
    ) -> None:
        """Emit a narrow refresh hint for future service mutation groups."""
        self._require_started()
        self._emit(kind, resource, revision)

    async def _client_for_review(self, ref: ReviewRef, operation: str) -> ForgeClient:
        if ref.number <= 0:
            raise ServiceError(
                ServiceErrorCode.INVALID_INPUT,
                "The review number must be positive.",
            )
        return await self._client_for_repository(ref.repository, operation)

    async def _client_for_repository(
        self, ref: RepositoryRef, operation: str
    ) -> ForgeClient:
        self._require_started()
        self._require_repository(ref)
        return await self._get_client(ref.hostname, operation)

    async def _get_client(self, hostname: str, operation: str) -> ForgeClient:
        registry = cast(ForgeRegistryResource, self._registry)
        try:
            return await registry.get_client(hostname)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - Sanitize registry boundary.
            raise translate_error(
                error, operation=operation, hostname=hostname
            ) from None

    async def _call(
        self,
        awaitable: Awaitable[_ResultT],
        *,
        operation: str,
        hostname: str,
    ) -> _ResultT:
        try:
            return await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - Sanitize forge boundary.
            raise translate_error(
                error, operation=operation, hostname=hostname
            ) from None

    def _accept_summaries(
        self,
        summaries: Sequence[MRSummary],
        hostname: str,
        *,
        expected_repository: RepositoryRef | None,
        filter_repository: RepositoryRef | None,
    ) -> tuple[ReviewListItem, ...]:
        refs = self._validate_summaries(
            summaries, hostname, expected_repository=expected_repository
        )
        items: list[ReviewListItem] = []
        for summary, ref in zip(summaries, refs):
            if filter_repository is not None and ref.repository != filter_repository:
                continue
            forge_type = summary.forge_host.forge_type
            self._issue_repository(
                RepositorySnapshot(ref.repository, summary.repo_path, forge_type)
            )
            items.append(ReviewListItem(ref, self._safe_summary(summary)))
        return tuple(items)

    def _validate_summaries(
        self,
        summaries: Sequence[MRSummary],
        hostname: str,
        *,
        expected_repository: RepositoryRef | None,
    ) -> tuple[ReviewRef, ...]:
        if not isinstance(summaries, (list, tuple)):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned an invalid review list.",
            )
        refs: list[ReviewRef] = []
        for summary in summaries:
            if not isinstance(summary, MRSummary):
                raise ServiceError(
                    ServiceErrorCode.INVALID_RESPONSE,
                    "The forge returned an invalid review item.",
                )
            if not isinstance(summary.updated_at, datetime):
                raise ServiceError(
                    ServiceErrorCode.INVALID_RESPONSE,
                    "The forge returned an invalid review item.",
                )
            try:
                repository = RepositoryRef(hostname, summary.repo_path)
                review = ReviewRef(repository, summary.number)
            except ValueError:
                raise ServiceError(
                    ServiceErrorCode.INVALID_RESPONSE,
                    "The forge returned an invalid review identity.",
                ) from None
            if summary.forge_host.hostname != hostname or (
                expected_repository is not None and repository != expected_repository
            ):
                raise ServiceError(
                    ServiceErrorCode.INVALID_RESPONSE,
                    "The forge returned a review for an unexpected resource.",
                )
            refs.append(review)
        return tuple(refs)

    def _validate_detail(self, detail: MRDetail, ref: ReviewRef) -> None:
        if not isinstance(detail, MRDetail):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned invalid review detail.",
            )
        if (
            detail.forge_host.hostname != ref.repository.hostname
            or detail.repo_path != ref.repository.project_path
            or detail.number != ref.number
        ):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned detail for an unexpected review.",
            )

    def _revision_from_detail(self, detail: MRDetail) -> ReviewRevision:
        try:
            return ReviewRevision(detail.head_sha, detail.base_sha, detail.start_sha)
        except ValueError:
            raise ServiceError(
                ServiceErrorCode.REVISION_UNAVAILABLE,
                "The forge did not provide a complete review revision.",
            ) from None

    @staticmethod
    def _safe_summary(summary: MRSummary) -> MRSummary:
        """Remove process-local routing data from an existing forge model."""
        safe_host = ForgeHost(
            summary.forge_host.hostname,
            summary.forge_host.forge_type,
            "",
        )
        return replace(summary, forge_host=safe_host, local_path="")

    @classmethod
    def _safe_detail(cls, detail: MRDetail) -> MRDetail:
        return cast(MRDetail, cls._safe_summary(detail))

    @staticmethod
    def _typed_tuple(
        value: object,
        expected_type: type[_ItemT],
        response_name: str,
    ) -> tuple[_ItemT, ...]:
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, expected_type) for item in value
        ):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                f"The forge returned an invalid {response_name}.",
            )
        return tuple(value)

    @classmethod
    def _freeze_mapping(cls, value: Mapping[object, object]) -> Mapping[str, object]:
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ServiceError(
                    ServiceErrorCode.INVALID_RESPONSE,
                    "The forge returned invalid diff data.",
                )
            frozen[key] = cls._freeze_value(item)
        return MappingProxyType(frozen)

    @classmethod
    def _freeze_value(cls, value: object) -> object:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Mapping):
            return cls._freeze_mapping(value)
        if isinstance(value, (list, tuple)):
            return tuple(cls._freeze_value(item) for item in value)
        raise ServiceError(
            ServiceErrorCode.INVALID_RESPONSE,
            "The forge returned invalid diff data.",
        )

    @staticmethod
    def _capabilities(client: ForgeClient) -> ForgeCapabilities:
        try:
            return ForgeCapabilities(
                batched_review=client.supports_batched_review,
                thread_resolution=client.supports_thread_resolution,
                draft_notes=client.supports_draft_notes,
                unapprove=client.supports_unapprove,
                job_cancel=client.supports_job_cancel,
            )
        except Exception as error:  # noqa: BLE001 - Sanitize adapter boundary.
            raise translate_error(error, operation="read_capabilities") from None

    def _require_repository(self, ref: RepositoryRef) -> None:
        if ref not in self._issued_repositories:
            raise ServiceError(
                ServiceErrorCode.RESOURCE_NOT_ISSUED,
                "The repository was not issued by this application session.",
            )

    def _issue_repository(self, snapshot: RepositorySnapshot) -> None:
        self._issued_repositories.add(snapshot.ref)
        self._repositories[snapshot.ref] = snapshot

    def _require_started(self) -> None:
        if self._state == _SessionState.CLOSED:
            raise ServiceError(
                ServiceErrorCode.CLOSED,
                "The application session is closed.",
            )
        if self._state != _SessionState.STARTED:
            raise ServiceError(
                ServiceErrorCode.NOT_STARTED,
                "The application session has not started.",
            )

    @staticmethod
    def _validate_page_size(per_page: int) -> None:
        if isinstance(per_page, bool) or not 1 <= per_page <= 100:
            raise ServiceError(
                ServiceErrorCode.INVALID_INPUT,
                "per_page must be between 1 and 100.",
            )

    def _emit(
        self,
        kind: ServiceEventKind,
        resource: RepositoryRef | ReviewRef | PipelineRef | None = None,
        revision: ReviewRevision | None = None,
    ) -> None:
        self._sequence += 1
        event = ServiceEvent(self._sequence, kind, resource, revision)
        for queue in tuple(self._subscribers):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(
                    ServiceEvent(
                        self._sequence,
                        ServiceEventKind.RESYNC_REQUIRED,
                    )
                )
            else:
                queue.put_nowait(event)

    def _close_event_streams(self) -> None:
        for queue in tuple(self._subscribers):
            while queue.full():
                queue.get_nowait()
            queue.put_nowait(_EVENT_STREAM_CLOSED)
        self._subscribers.clear()


__all__ = [
    "ApplicationSession",
    "CacheResource",
    "ForgeRegistryResource",
]
