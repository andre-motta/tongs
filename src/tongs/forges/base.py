"""Abstract base class for forge client implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from tongs.forges.models import (
    Commit,
    Discussion,
    InlineComment,
    MRDetail,
    MRSummary,
    Pipeline,
    PipelineJob,
    ReviewDecision,
)


class ForgeClient(ABC):
    """Abstract interface for forge operations.

    Each concrete implementation (GitHub, GitLab) handles API differences
    internally and returns the shared data models.
    """

    @abstractmethod
    async def list_mrs(
        self,
        repo_path: str,
        state: str = "open",
        per_page: int = 20,
        page: int = 1,
    ) -> list[MRSummary]: ...

    @abstractmethod
    async def list_my_reviews(self) -> list[MRSummary]: ...

    @abstractmethod
    async def list_my_mrs(self) -> list[MRSummary]: ...

    @abstractmethod
    async def get_mr(self, repo_path: str, number: int) -> MRDetail: ...

    @abstractmethod
    async def get_mr_diff(self, repo_path: str, number: int) -> list[dict]: ...

    @abstractmethod
    async def list_mr_commits(self, repo_path: str, number: int) -> list[Commit]: ...

    @abstractmethod
    async def get_mr_discussions(
        self, repo_path: str, number: int
    ) -> list[Discussion]: ...

    @abstractmethod
    async def create_inline_comment(
        self,
        repo_path: str,
        number: int,
        file_path: str,
        line: int,
        side: str,
        body: str,
    ) -> InlineComment: ...

    @abstractmethod
    async def reply_to_discussion(
        self,
        repo_path: str,
        number: int,
        discussion_id: str,
        body: str,
    ) -> InlineComment: ...

    @abstractmethod
    async def resolve_discussion(
        self,
        repo_path: str,
        number: int,
        discussion_id: str,
        resolved: bool,
    ) -> None: ...

    @abstractmethod
    async def submit_review(
        self,
        repo_path: str,
        number: int,
        verdict: ReviewDecision,
        body: str,
        inline_comments: list[dict] | None = None,
    ) -> None: ...

    @abstractmethod
    async def approve_mr(self, repo_path: str, number: int) -> None: ...

    @abstractmethod
    async def merge_mr(
        self,
        repo_path: str,
        number: int,
        squash: bool = False,
        delete_branch: bool = True,
    ) -> None: ...

    @abstractmethod
    async def close_mr(self, repo_path: str, number: int) -> None: ...

    @abstractmethod
    async def reopen_mr(self, repo_path: str, number: int) -> None: ...

    @abstractmethod
    async def add_comment(self, repo_path: str, number: int, body: str) -> None: ...

    @abstractmethod
    async def list_pipelines(
        self,
        repo_path: str,
        per_page: int = 20,
    ) -> list[Pipeline]: ...

    @abstractmethod
    async def get_pipeline_jobs(
        self, repo_path: str, pipeline_id: int
    ) -> list[PipelineJob]: ...

    @abstractmethod
    async def get_job_log(self, repo_path: str, job_id: int) -> str: ...

    async def stream_job_log(self, repo_path: str, job_id: int) -> AsyncIterator[str]:
        """Stream job log lines. Default: return full log as single yield."""
        yield await self.get_job_log(repo_path, job_id)

    @abstractmethod
    async def retry_job(self, repo_path: str, job_id: int) -> None: ...

    @abstractmethod
    async def cancel_pipeline(self, repo_path: str, pipeline_id: int) -> None: ...

    @property
    def supports_batched_review(self) -> bool:
        return False

    @property
    def supports_thread_resolution(self) -> bool:
        return False

    @property
    def supports_draft_notes(self) -> bool:
        return False

    @abstractmethod
    async def close(self) -> None:
        """Close the underlying HTTP client."""
        ...
