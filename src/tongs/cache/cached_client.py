"""Caching wrapper for ForgeClient that intercepts read methods."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any


from tongs.cache.store import CacheStore
from tongs.forges.base import ForgeClient
from tongs.forges.models import (
    CIStatus,
    InlineComment,
    MRState,
    MRSummary,
    User,
)


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

    def _key(self, *parts: str | int) -> str:
        return f"{self._hostname}:{':'.join(str(p) for p in parts)}"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    # -- Cached reads --

    async def list_mrs(
        self, repo_path: str, state: str = "open", per_page: int = 100
    ) -> list[MRSummary]:
        key = self._key(repo_path, "mrs", state)
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
        cached = await self._cache.get_json(key)
        if cached is not None:
            return cached
        result = await self._inner.get_mr_diff(repo_path, number)
        await self._cache.put_json(key, result, self._diff_ttl)
        return result

    # -- Mutations that invalidate cache --

    async def approve_mr(self, repo_path: str, number: int) -> None:
        await self._inner.approve_mr(repo_path, number)
        await self._cache.invalidate_prefix(self._key(repo_path, "mrs"))

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

    async def add_comment(self, repo_path: str, number: int, body: str) -> None:
        await self._inner.add_comment(repo_path, number, body)

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
    ) -> InlineComment:
        result = await self._inner.create_inline_comment(
            repo_path, number, file_path, line, side, body, start_line, start_side
        )
        return result

    async def resolve_discussion(
        self, repo_path: str, number: int, discussion_id: str, resolved: bool
    ) -> None:
        await self._inner.resolve_discussion(repo_path, number, discussion_id, resolved)

    async def close(self) -> None:
        await self._inner.close()
