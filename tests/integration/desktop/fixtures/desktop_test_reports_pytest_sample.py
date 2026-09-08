"""Real pytest emitter input retained for desktop test-report fixtures."""

from __future__ import annotations


def test_direct_pass() -> None:
    assert 2 + 2 == 4


class TestNestedIdentity:
    def test_class_pass(self) -> None:
        assert ["safe"] == ["safe"]
