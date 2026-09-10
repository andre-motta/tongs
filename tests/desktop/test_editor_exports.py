"""Cross-process editor export reservation and recovery tests."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

from tongs.desktop.editor_exports import (
    MAX_EDITOR_EXPORTS,
    STALE_EDITOR_EXPORT_SECONDS,
    EditorExportLedger,
)


def _token(value: int) -> str:
    return f"{value:032x}"


def _write_export(root: Path, name: str, *, modified_at: float) -> Path:
    path = root / name
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, b"bounded log\n")
    finally:
        os.close(descriptor)
    os.utime(path, (modified_at, modified_at))
    return path


@pytest.mark.asyncio
async def test_two_ledgers_atomically_share_the_eighth_slot(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    first = EditorExportLedger(root)
    second = EditorExportLedger(root)
    try:
        for value in range(1, MAX_EDITOR_EXPORTS):
            assert await first.reserve(value, _token(value), now=10) is not None

        left, right = await asyncio.gather(
            first.reserve(80, _token(80), now=10),
            second.reserve(81, _token(81), now=10),
        )

        assert sum(result is not None for result in (left, right)) == 1
        assert await first.reserve(82, _token(82), now=10) is None
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_concurrent_stale_reclaim_cannot_release_reused_slot(
    tmp_path: Path,
) -> None:
    root = tmp_path / "exports"
    first = EditorExportLedger(root)
    second = EditorExportLedger(root)
    old_token = _token(1)
    try:
        old = await first.reserve(1, old_token, now=0)
        assert old is not None
        old_path = _write_export(root, old.export_name, modified_at=0)
        for value in range(2, MAX_EDITOR_EXPORTS + 1):
            assert await first.reserve(value, _token(value), now=1) is not None

        timestamp = STALE_EDITOR_EXPORT_SECONDS + 0.5
        left, right = await asyncio.gather(
            first.reserve(90, _token(90), now=timestamp),
            second.reserve(91, _token(91), now=timestamp),
        )

        admitted = [result for result in (left, right) if result is not None]
        assert len(admitted) == 1
        replacement = admitted[0]
        assert replacement.slot == old.slot
        assert not old_path.exists()
        replacement_path = _write_export(
            root, replacement.export_name, modified_at=timestamp
        )
        assert await first.release(old.slot, old_token) is False
        assert replacement_path.read_text() == "bounded log\n"
        assert await second.release(replacement.slot, replacement.token) is False
        replacement_path.unlink()
        assert await second.release(replacement.slot, replacement.token) is True
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_stale_empty_reservation_is_recoverable(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    ledger = EditorExportLedger(root)
    try:
        old = await ledger.reserve(1, _token(1), now=0)
        assert old is not None

        replacement = await ledger.reserve(
            2, _token(2), now=STALE_EDITOR_EXPORT_SECONDS + 1
        )

        assert replacement is not None
        assert replacement.slot == old.slot
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_close_only_live_expiry_is_reclaimed_after_stale_lease(
    tmp_path: Path,
) -> None:
    root = tmp_path / "exports"
    ledger = EditorExportLedger(root)
    try:
        reservation = await ledger.reserve(1, _token(1), now=0)
        assert reservation is not None
        export = _write_export(root, reservation.export_name, modified_at=0)

        before_expiry = await ledger.reserve(
            2, _token(2), now=STALE_EDITOR_EXPORT_SECONDS - 1
        )
        assert before_expiry is not None
        assert export.exists()

        after_expiry = await ledger.reserve(
            3, _token(3), now=STALE_EDITOR_EXPORT_SECONDS + 1
        )
        assert after_expiry is not None
        assert after_expiry.slot == reservation.slot
        assert not export.exists()
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_fresh_legacy_or_orphan_export_blocks_new_slots(
    tmp_path: Path,
) -> None:
    root = tmp_path / "exports"
    ledger = EditorExportLedger(root)
    try:
        seed = await ledger.reserve(1, _token(1), now=10)
        assert seed is not None
        assert await ledger.release(seed.slot, seed.token) is True
        legacy = _write_export(
            root,
            "tongs-job-9-123e4567-e89b-42d3-a456-426614174000.log",
            modified_at=10,
        )

        assert await ledger.reserve(2, _token(2), now=10) is None

        os.utime(legacy, (0, 0))
        result = await ledger.reserve(3, _token(3), now=STALE_EDITOR_EXPORT_SECONDS + 1)
        assert result is not None
        assert not legacy.exists()
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_malformed_row_and_fresh_orphan_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    ledger = EditorExportLedger(root)
    seed = await ledger.reserve(1, _token(1), now=10)
    assert seed is not None
    assert await ledger.release(seed.slot, seed.token) is True
    await ledger.close()

    database = root / ".tongs-editor-ledger.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO editor_exports(slot, token, export_name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (9, _token(9), "outside-policy.log", 0),
        )
    malformed = EditorExportLedger(root)
    try:
        assert (
            await malformed.reserve(2, _token(2), now=STALE_EDITOR_EXPORT_SECONDS + 1)
            is None
        )
    finally:
        await malformed.close()

    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM editor_exports WHERE slot = 9")
    malformed_name = "tongs-slot-1-job-invalid.log"
    malformed_file = _write_export(root, malformed_name, modified_at=20)
    permissive = EditorExportLedger(root)
    try:
        admitted = await permissive.reserve(3, _token(3), now=20)
        assert admitted is not None
        assert await permissive.release(admitted.slot, admitted.token) is True
        assert malformed_file.exists()
    finally:
        await permissive.close()

    orphan_name = f"tongs-slot-1-job-4-{_token(4)}.log"
    orphan = _write_export(root, orphan_name, modified_at=20)
    recovered = EditorExportLedger(root)
    try:
        assert await recovered.reserve(4, _token(4), now=20) is None
        assert orphan.exists()
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_replaced_root_or_ledger_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    ledger = EditorExportLedger(root)
    reservation = await ledger.reserve(1, _token(1), now=10)
    assert reservation is not None
    database = root / ".tongs-editor-ledger.sqlite3"
    moved = root / ".old-ledger"
    database.rename(moved)
    database.write_bytes(b"replacement")
    database.chmod(0o600)
    try:
        with pytest.raises(OSError, match="storage changed"):
            await ledger.reserve(2, _token(2), now=10)
    finally:
        await ledger.close()
