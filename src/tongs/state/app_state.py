"""Reactive application state shared across screens."""

from __future__ import annotations

from dataclasses import dataclass, field

from tongs.forges.models import ReviewDecision


@dataclass
class MRFilter:
    state: str = "open"
    author: str = ""
    search: str = ""

    @classmethod
    def default(cls) -> MRFilter:
        return cls()


@dataclass
class ReviewDraft:
    """Accumulates inline comments before submission (GitHub model)."""

    repo_path: str = ""
    mr_number: int = 0
    verdict: ReviewDecision | None = None
    body: str = ""
    inline_comments: list[dict] = field(default_factory=list)
