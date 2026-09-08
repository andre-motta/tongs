"""Real failing pytest emitter input retained as a negative control."""

from __future__ import annotations


def test_failure_is_reported() -> None:
    raise RuntimeError("controlled fixture failure")
