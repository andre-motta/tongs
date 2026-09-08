"""Textual-facing adapter for the shared application services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from tongs.forges.models import MRSummary
from tongs.scanner.repo import Repo
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import (
    HostFailure,
    RepositoryRef,
    ReviewListItem,
    ReviewPage,
    ReviewQuery,
    ReviewRef,
    ReviewScope,
)
from tongs.services.session import ApplicationSession


@dataclass(frozen=True, slots=True)
class TUIDiscoveryResult:
    """One repository refresh result suitable for publishing in the TUI."""

    generation: int
    repositories: tuple[Repo, ...]
    stale: bool = False


class TUIServiceAdapter:
    """Preserve terminal models while routing reads through one session.

    The adapter retains admitted service identities for later TUI consumers. Local
    ``Repo`` metadata remains trusted in-process state and never enters desktop
    snapshots or protocol DTOs.
    """

    def __init__(self, session: ApplicationSession) -> None:
        self.session = session
        self._discovery_generation = 0
        self._repositories: tuple[Repo, ...] = ()
        self._repository_refs: dict[Repo, RepositoryRef] = {}
        self._review_refs: dict[tuple[str, str, int], ReviewRef] = {}

    @property
    def discovery_generation(self) -> int:
        """Return the latest requested discovery generation."""
        return self._discovery_generation

    @property
    def repositories(self) -> tuple[Repo, ...]:
        """Return the latest repository inventory published by this adapter."""
        return self._repositories

    async def discover_repositories(self) -> TUIDiscoveryResult:
        """Refresh local repositories without publishing an older result late."""
        self._discovery_generation += 1
        generation = self._discovery_generation
        snapshots = await self.session.discover_repositories()
        if generation != self._discovery_generation:
            return TUIDiscoveryResult(generation, self._repositories, stale=True)

        repositories = self.session.local_repositories
        admitted = {snapshot.ref for snapshot in snapshots}
        refs: dict[Repo, RepositoryRef] = {}
        for repo in repositories:
            remote = repo.primary_remote
            if remote is None:
                continue
            try:
                ref = RepositoryRef(remote.hostname, remote.repo_path)
            except (TypeError, ValueError):
                continue
            if ref in admitted:
                refs[repo] = ref

        self._repositories = repositories
        self._repository_refs = refs
        return TUIDiscoveryResult(generation, repositories)

    def invalidate_discovery(self) -> None:
        """Prevent an active discovery generation from publishing afterward."""
        self._discovery_generation += 1

    def repository_ref(self, repo: Repo) -> RepositoryRef:
        """Return the admitted identity for an actual discovered repository."""
        try:
            return self._repository_refs[repo]
        except KeyError:
            raise ServiceError(
                ServiceErrorCode.UNSUPPORTED,
                "This local repository does not use a configured forge host.",
            ) from None

    def review_ref(self, summary: MRSummary) -> ReviewRef:
        """Return the admitted identity paired with a service list result."""
        key = (summary.forge_host.hostname, summary.repo_path, summary.number)
        try:
            return self._review_refs[key]
        except KeyError:
            raise ServiceError(
                ServiceErrorCode.RESOURCE_NOT_ISSUED,
                "The review was not issued by this application session.",
            ) from None

    async def list_reviews(
        self, scope: ReviewScope, *, repository: Repo | None = None
    ) -> ReviewPage:
        """Run an inbox read with the terminal's local workspace semantics."""
        if repository is not None:
            page = await self.session.list_reviews(
                ReviewQuery(scope, repository=self.repository_ref(repository))
            )
        elif scope is ReviewScope.ALL_OPEN:
            page = await self._list_all_open()
        else:
            page = await self._list_personal(scope)
        self._remember_review_refs(page.items)
        return page

    async def _list_personal(self, scope: ReviewScope) -> ReviewPage:
        hostnames = tuple(
            sorted({ref.hostname for ref in self._repository_refs.values()})
        )
        if not hostnames:
            return ReviewPage((), ())
        return await self.session.list_reviews(ReviewQuery(scope, hostnames=hostnames))

    async def _list_all_open(self) -> ReviewPage:
        refs_by_host: dict[str, list[RepositoryRef]] = {}
        for ref in dict.fromkeys(self._repository_refs.values()):
            refs_by_host.setdefault(ref.hostname, []).append(ref)
        if not refs_by_host:
            return ReviewPage((), ())
        semaphore = asyncio.Semaphore(self.session.config.max_parallel)

        async def fetch_host(refs: list[RepositoryRef]) -> ReviewPage:
            async with semaphore:
                items: list[ReviewListItem] = []
                failures: list[HostFailure] = []
                for ref in refs:
                    page = await self.session.list_reviews(
                        ReviewQuery(ReviewScope.ALL_OPEN, repository=ref)
                    )
                    items.extend(page.items)
                    failures.extend(page.failures)
                    if page.failures:
                        break
                return ReviewPage(tuple(items), tuple(failures))

        pages = await asyncio.gather(
            *(fetch_host(refs) for refs in refs_by_host.values())
        )
        items = [item for page in pages for item in page.items]
        failures = [failure for page in pages for failure in page.failures]
        items.sort(key=lambda item: item.summary.updated_at, reverse=True)
        return ReviewPage(tuple(items), self._deduplicate_failures(failures))

    def _remember_review_refs(self, items: tuple[ReviewListItem, ...]) -> None:
        for item in items:
            key = (
                item.ref.repository.hostname,
                item.ref.repository.project_path,
                item.ref.number,
            )
            self._review_refs[key] = item.ref

    @staticmethod
    def _deduplicate_failures(failures: list[HostFailure]) -> tuple[HostFailure, ...]:
        unique: dict[
            tuple[str, RepositoryRef | None, ServiceErrorCode], HostFailure
        ] = {}
        for failure in failures:
            key = (failure.hostname, failure.repository, failure.code)
            unique.setdefault(key, failure)
        return tuple(unique.values())


__all__ = ["TUIDiscoveryResult", "TUIServiceAdapter"]
