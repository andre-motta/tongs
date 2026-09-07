"""Caching wrapper for ForgeClient that intercepts read methods."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any

from tongs.cache.store import CacheStore
from tongs.forges.base import ForgeClient
from tongs.forges.models import (
    CIStatus,
    ForgeMutationResult,
    MRDetail,
    MRState,
    MRSummary,
    User,
)

_MAX_BACKGROUND_INVALIDATIONS = 64
_BACKGROUND_CLOSE_TIMEOUT = 1.0


def _finish_background_invalidation(
    tasks: dict[tuple[str, int], asyncio.Task[bool]],
    key: tuple[str, int],
    task: asyncio.Task[bool],
) -> None:
    if tasks.get(key) is task:
        del tasks[key]
    try:
        task.result()
    except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
        pass


def _serialize_datetime(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_datetime(val: str | None) -> datetime | None:
    if not val:
        return None
    return datetime.fromisoformat(val)


def _mr_summary_to_dict(mr: MRSummary) -> dict:
    d = asdict(mr)
    d["ci_status"] = mr.ci_status.value
    d["state"] = mr.state.value
    d["created_at"] = _serialize_datetime(mr.created_at)
    d["updated_at"] = _serialize_datetime(mr.updated_at)
    d["forge_host"] = {
        "hostname": mr.forge_host.hostname,
        "forge_type": mr.forge_host.forge_type.value,
        "api_base": mr.forge_host.api_base,
    }
    d["author"] = asdict(mr.author)
    return d


def _dict_to_mr_summary(d: dict) -> MRSummary:
    from tongs.forges.models import ForgeHost
    from tongs.scanner.repo import ForgeType

    return MRSummary(
        number=d["number"],
        title=d["title"],
        author=User(**d["author"]),
        source_branch=d["source_branch"],
        target_branch=d["target_branch"],
        state=MRState(d["state"]),
        ci_status=CIStatus(d["ci_status"]),
        web_url=d["web_url"],
        repo_path=d["repo_path"],
        local_path=d.get("local_path", ""),
        is_draft=d.get("is_draft", False),
        forge_host=ForgeHost(
            hostname=d["forge_host"]["hostname"],
            forge_type=ForgeType(d["forge_host"]["forge_type"]),
            api_base=d["forge_host"]["api_base"],
        ),
        created_at=_parse_datetime(d.get("created_at")),
        updated_at=_parse_datetime(d.get("updated_at")),
    )


class CachedForgeClient:
    """Wraps a ForgeClient with SQLite caching on read methods.

    Mutations bypass the cache and invalidate related entries.
    Job logs are never cached (security: may contain secrets).
    """

    def __init__(
        self,
        inner: ForgeClient,
        cache: CacheStore,
        hostname: str,
        mr_list_ttl: int = 60,
        diff_ttl: int = 300,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._hostname = hostname
        self._mr_list_ttl = mr_list_ttl
        self._diff_ttl = diff_ttl
        self._dirty_review_prefixes: dict[str, int] = {}
        self._dirty_generation = 0
        self._invalidation_tasks: dict[tuple[str, int], asyncio.Task[bool]] = {}
        self._closed = False

    def _key(self, *parts: str | int) -> str:
        return f"{self._hostname}:{':'.join(str(p) for p in parts)}"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    # -- Cached reads --

    async def list_mrs(
        self, repo_path: str, state: str = "open", per_page: int = 100
    ) -> list[MRSummary]:
        key = self._key(repo_path, "mrs", state)
        if any(key.startswith(prefix) for prefix in self._dirty_review_prefixes):
            return await self._inner.list_mrs(repo_path, state, per_page)
        cached = await self._cache.get_json(key)
        if cached is not None:
            return [_dict_to_mr_summary(d) for d in cached]
        result = await self._inner.list_mrs(repo_path, state, per_page)
        await self._cache.put_json(
            key, [_mr_summary_to_dict(mr) for mr in result], self._mr_list_ttl
        )
        return result

    async def get_mr_diff(self, repo_path: str, number: int) -> list[dict]:
        key = self._key(repo_path, "mr", number, "diff")
        if any(key.startswith(prefix) for prefix in self._dirty_review_prefixes):
            return await self._inner.get_mr_diff(repo_path, number)
        cached = await self._cache.get_json(key)
        if cached is not None:
            return cached
        result = await self._inner.get_mr_diff(repo_path, number)
        await self._cache.put_json(key, result, self._diff_ttl)
        return result

    async def get_mr_fresh(self, repo_path: str, number: int) -> MRDetail:
        """Bypass any present or future wrapper cache for revision checks."""
        return await self._inner.get_mr_fresh(repo_path, number)

    async def get_mr_diff_fresh(self, repo_path: str, number: int) -> list[dict]:
        """Bypass cached diff payloads for revision-bound reads."""
        return await self._inner.get_mr_diff_fresh(repo_path, number)

    # -- Mutations that invalidate cache --

    async def approve_mr(
        self, repo_path: str, number: int, *, head_sha: str | None = None
    ) -> ForgeMutationResult:
        if head_sha is None:
            result = await self._inner.approve_mr(repo_path, number)
        else:
            result = await self._inner.approve_mr(repo_path, number, head_sha=head_sha)
        return await self._finish_review_mutation(repo_path, number, result)

    async def unapprove_mr(self, repo_path: str, number: int) -> None:
        await self._inner.unapprove_mr(repo_path, number)
        await self._cache.invalidate_prefix(self._key(repo_path, "mrs"))

    async def merge_mr(
        self,
        repo_path: str,
        number: int,
        squash: bool = False,
        delete_branch: bool = True,
    ) -> None:
        await self._inner.merge_mr(repo_path, number, squash, delete_branch)
        await self._cache.invalidate_prefix(self._key(repo_path))

    async def close_mr(self, repo_path: str, number: int) -> None:
        await self._inner.close_mr(repo_path, number)
        await self._cache.invalidate_prefix(self._key(repo_path, "mrs"))

    async def reopen_mr(self, repo_path: str, number: int) -> None:
        await self._inner.reopen_mr(repo_path, number)
        await self._cache.invalidate_prefix(self._key(repo_path, "mrs"))

    async def add_comment(
        self, repo_path: str, number: int, body: str
    ) -> ForgeMutationResult:
        result = await self._inner.add_comment(repo_path, number, body)
        return await self._finish_review_mutation(repo_path, number, result)

    async def create_inline_comment(
        self,
        repo_path: str,
        number: int,
        file_path: str,
        line: int,
        side: str,
        body: str,
        start_line: int | None = None,
        start_side: str | None = None,
        **revision: Any,
    ) -> ForgeMutationResult:
        result = await self._inner.create_inline_comment(
            repo_path,
            number,
            file_path,
            line,
            side,
            body,
            start_line,
            start_side,
            **revision,
        )
        return await self._finish_review_mutation(repo_path, number, result)

    async def reply_to_discussion(
        self,
        repo_path: str,
        number: int,
        discussion_id: str,
        body: str,
        *,
        root_comment_id: str | None = None,
    ) -> ForgeMutationResult:
        if root_comment_id is None:
            result = await self._inner.reply_to_discussion(
                repo_path, number, discussion_id, body
            )
        else:
            result = await self._inner.reply_to_discussion(
                repo_path,
                number,
                discussion_id,
                body,
                root_comment_id=root_comment_id,
            )
        return await self._finish_review_mutation(repo_path, number, result)

    async def resolve_discussion(
        self, repo_path: str, number: int, discussion_id: str, resolved: bool
    ) -> ForgeMutationResult:
        result = await self._inner.resolve_discussion(
            repo_path, number, discussion_id, resolved
        )
        return await self._finish_review_mutation(repo_path, number, result)

    async def submit_review(
        self,
        repo_path: str,
        number: int,
        verdict: Any,
        body: str,
        inline_comments: list[dict] | None = None,
        *,
        head_sha: str | None = None,
    ) -> ForgeMutationResult:
        if head_sha is None:
            result = await self._inner.submit_review(
                repo_path, number, verdict, body, inline_comments
            )
        else:
            result = await self._inner.submit_review(
                repo_path,
                number,
                verdict,
                body,
                inline_comments,
                head_sha=head_sha,
            )
        return await self._finish_review_mutation(repo_path, number, result)

    async def invalidate_review_reads(self, repo_path: str, number: int) -> bool:
        prefixes = self._review_prefixes(repo_path, number)
        generation = self._mark_review_dirty(prefixes)
        return await self._invalidate_marked(prefixes, generation)

    async def _invalidate_marked(
        self, prefixes: tuple[str, str], generation: int
    ) -> bool:
        results = await asyncio.gather(
            *(self._cache.invalidate_prefix(prefix) for prefix in prefixes),
            return_exceptions=True,
        )
        complete = not any(isinstance(result, BaseException) for result in results)
        if complete:
            for prefix in prefixes:
                if self._dirty_review_prefixes.get(prefix) == generation:
                    del self._dirty_review_prefixes[prefix]
        return complete

    async def _finish_review_mutation(
        self, repo_path: str, number: int, result: ForgeMutationResult
    ) -> ForgeMutationResult:
        prefixes = self._review_prefixes(repo_path, number)
        self._mark_review_dirty(prefixes)
        self._schedule_review_invalidation(repo_path, number, prefixes)
        return replace(result, cache_invalidated=False)

    def _schedule_review_invalidation(
        self, repo_path: str, number: int, prefixes: tuple[str, str]
    ) -> None:
        key = (repo_path, number)
        active = self._invalidation_tasks.get(key)
        if self._closed or (active is not None and not active.done()):
            return
        if len(self._invalidation_tasks) >= _MAX_BACKGROUND_INVALIDATIONS:
            return
        task = asyncio.create_task(self._drain_review_invalidation(prefixes))
        self._invalidation_tasks[key] = task
        task.add_done_callback(
            lambda completed: _finish_background_invalidation(
                self._invalidation_tasks, key, completed
            )
        )

    async def _drain_review_invalidation(self, prefixes: tuple[str, str]) -> bool:
        while not self._closed:
            generations = [
                self._dirty_review_prefixes.get(prefix) for prefix in prefixes
            ]
            if any(generation is None for generation in generations):
                return True
            generation = max(value for value in generations if value is not None)
            if not await self._invalidate_marked(prefixes, generation):
                return False
            if not any(prefix in self._dirty_review_prefixes for prefix in prefixes):
                return True
        return False

    def _mark_review_dirty(self, prefixes: tuple[str, str]) -> int:
        self._dirty_generation += 1
        for prefix in prefixes:
            self._dirty_review_prefixes[prefix] = self._dirty_generation
        return self._dirty_generation

    def _review_prefixes(self, repo_path: str, number: int) -> tuple[str, str]:
        return (
            self._key(repo_path, "mrs"),
            self._key(repo_path, "mr", number),
        )

    async def close(self) -> None:
        self._closed = True
        tasks = tuple(self._invalidation_tasks.values())
        for task in tasks:
            task.cancel()
        pending: set[asyncio.Task[bool]] = set()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=_BACKGROUND_CLOSE_TIMEOUT)
        await self._inner.close()
        if pending:
            raise RuntimeError("cache invalidation tasks did not stop")
