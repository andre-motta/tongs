from __future__ import annotations

import pytest

from tongs.desktop.protocol.messages import ProtocolError, ProtocolErrorCode
from tongs.desktop.protocol.state import HandleKind, HandleRegistry, SnapshotStore
from tongs.services import RepositoryRef, ReviewRef


def test_handles_are_stable_session_local_and_kind_bound() -> None:
    repository = RepositoryRef("git.example.com", "team/project")
    review = ReviewRef(repository, 7)
    first = HandleRegistry()
    second = HandleRegistry()

    handle = first.issue(HandleKind.REVIEW, review)
    assert first.issue(HandleKind.REVIEW, review) == handle
    assert first.resolve(handle, HandleKind.REVIEW, ReviewRef) == review
    assert second.session_id != first.session_id

    with pytest.raises(ProtocolError) as wrong_kind:
        first.resolve(handle, HandleKind.REPOSITORY, RepositoryRef)
    assert wrong_kind.value.code is ProtocolErrorCode.WRONG_HANDLE_KIND

    with pytest.raises(ProtocolError) as other_session:
        second.resolve(handle, HandleKind.REVIEW, ReviewRef)
    assert other_session.value.code is ProtocolErrorCode.INVALID_HANDLE


def test_snapshot_owns_nested_input_and_returns_copy_safe_pages() -> None:
    store = SnapshotStore()
    revision = {"head": {"sha": "original"}}
    row = {"cell": {"text": "original"}}
    snapshot = store.create("review", revision, [row])

    revision["head"]["sha"] = "mutated"  # type: ignore[index]
    row["cell"]["text"] = "mutated"  # type: ignore[index]
    first = store.page(snapshot, "review")
    assert first.revision == {"head": {"sha": "original"}}
    assert first.entries == ({"cell": {"text": "original"}},)

    first.entries[0]["cell"]["text"] = "page mutation"  # type: ignore[index]
    assert store.page(snapshot, "review").entries == ({"cell": {"text": "original"}},)


def test_snapshot_enforces_aggregate_bytes_and_expires_evicted_snapshot() -> None:
    store = SnapshotStore(max_retained_bytes=100, max_retained_rows=10)
    first = store.create("one", {}, [{"text": "a" * 40}])
    second = store.create("two", {}, [{"text": "b" * 40}])

    assert store.page(second, "two").entries
    with pytest.raises(ProtocolError) as caught:
        store.page(first, "one")
    assert caught.value.code is ProtocolErrorCode.SNAPSHOT_EXPIRED


def test_snapshot_rejects_single_value_over_retention_limit() -> None:
    store = SnapshotStore(max_retained_bytes=32, max_retained_rows=10)

    with pytest.raises(ProtocolError) as caught:
        store.create("one", {}, [{"text": "x" * 100}])

    assert caught.value.code is ProtocolErrorCode.RESPONSE_TOO_LARGE
