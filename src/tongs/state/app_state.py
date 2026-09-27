"""Reactive application state shared across screens."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MRFilter:
    state: str = "open"
    author: str = ""
    search: str = ""

    @classmethod
    def default(cls) -> MRFilter:
        return cls()
