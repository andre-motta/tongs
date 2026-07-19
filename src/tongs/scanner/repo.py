"""Repository data model."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ForgeType(Enum):
    GITHUB = "github"
    GITLAB = "gitlab"


@dataclass(frozen=True)
class Remote:
    """A git remote with parsed forge metadata."""

    name: str
    url: str
    hostname: str
    repo_path: str
    forge_type: ForgeType


@dataclass(frozen=True)
class Repo:
    """A discovered git repository on the local filesystem."""

    path: Path
    remotes: tuple[Remote, ...]
    primary_remote: Remote | None = field(default=None, repr=False)

    @property
    def display_name(self) -> str:
        if self.primary_remote:
            return self.primary_remote.repo_path
        return self.path.name

    @property
    def forge_type(self) -> ForgeType | None:
        if self.primary_remote:
            return self.primary_remote.forge_type
        return None

    @property
    def hostname(self) -> str | None:
        if self.primary_remote:
            return self.primary_remote.hostname
        return None

    @property
    def namespace(self) -> str:
        if self.primary_remote:
            parts = self.primary_remote.repo_path.rsplit("/", 1)
            return parts[0] if len(parts) > 1 else ""
        return ""
