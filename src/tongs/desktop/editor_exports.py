"""Cross-process reservation ledger for bounded desktop editor exports."""

from __future__ import annotations

import asyncio
import math
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import quote

import aiosqlite

from tongs.services.workspace_utilities import EditorReservation

MAX_EDITOR_EXPORTS = 8
STALE_EDITOR_EXPORT_SECONDS = 24 * 60 * 60
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_EXPORT = re.compile(r"^tongs-slot-([1-8])-job-([1-9][0-9]{0,18})-([0-9a-f]{32})\.log$")
_LEGACY_EXPORT = re.compile(
    r"^tongs-job-[1-9][0-9]{0,18}-[0-9a-f]{8}-[0-9a-f]{4}-"
    r"4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.log$"
)
_LEDGER_NAME = ".tongs-editor-ledger.sqlite3"
EDITOR_EXPORT_ROOT_ENV = "TONGS_DESKTOP_EDITOR_EXPORT_ROOT"


class EditorExportLedger:
    """Atomically reserve eight export slots shared by one trusted root."""

    def __init__(self, export_root: Path) -> None:
        if not export_root.is_absolute():
            raise ValueError("editor export root must be absolute")
        self._root = export_root
        self._db: aiosqlite.Connection | None = None
        self._open_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._root_identity: tuple[int, int] | None = None
        self._ledger_identity: tuple[int, int] | None = None

    async def reserve(
        self,
        job_id: int,
        token: str,
        *,
        now: float | None = None,
    ) -> EditorReservation | None:
        """Reserve one slot before a log fetch, reclaiming only safe stale rows."""
        if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
            raise ValueError("job_id must be positive")
        if not _TOKEN.fullmatch(token):
            raise ValueError("editor export token is invalid")
        timestamp = time.time() if now is None else now
        if (
            not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not math.isfinite(timestamp)
            or timestamp < 0
        ):
            raise ValueError("editor export timestamp is invalid")
        await self._ensure_open()
        async with self._operation_lock:
            self._verify_storage_identity()
            db = self._require_db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                rows = await self._rows(db)
                stale_before = float(timestamp) - STALE_EDITOR_EXPORT_SECONDS
                if not self._reclaim_files(rows, stale_before):
                    await db.rollback()
                    return None
                for slot, row_token, export_name, created_at in rows:
                    if created_at <= stale_before and not self._path_exists(
                        export_name
                    ):
                        await db.execute(
                            "DELETE FROM editor_exports WHERE slot = ? AND token = ?",
                            (slot, row_token),
                        )
                if self._has_blocking_orphan_or_legacy(rows, stale_before):
                    await db.rollback()
                    return None
                occupied = {
                    int(row[0]) for row in await self._rows(db) if _valid_row(*row)
                }
                slot = next(
                    (
                        candidate
                        for candidate in range(1, MAX_EDITOR_EXPORTS + 1)
                        if candidate not in occupied
                    ),
                    None,
                )
                if slot is None:
                    await db.rollback()
                    return None
                export_name = editor_export_name(slot, job_id, token)
                await db.execute(
                    "INSERT INTO editor_exports(slot, token, export_name, created_at) VALUES (?, ?, ?, ?)",
                    (slot, token, export_name, float(timestamp)),
                )
                await db.commit()
                return EditorReservation(slot, token, export_name)
            except BaseException:
                await db.rollback()
                raise

    async def release(self, slot: int, token: str) -> bool:
        """Release only the exact token after its expected export is absent."""
        if slot not in range(1, MAX_EDITOR_EXPORTS + 1) or not _TOKEN.fullmatch(token):
            raise ValueError("editor export reservation is invalid")
        await self._ensure_open()
        async with self._operation_lock:
            self._verify_storage_identity()
            db = self._require_db()
            cursor = await db.execute(
                "SELECT export_name FROM editor_exports WHERE slot = ? AND token = ?",
                (slot, token),
            )
            row = await cursor.fetchone()
            if row is None:
                return False
            export_name = row[0]
            if not isinstance(export_name, str) or not _EXPORT.fullmatch(export_name):
                return False
            if self._path_exists(export_name):
                return False
            cursor = await db.execute(
                "DELETE FROM editor_exports WHERE slot = ? AND token = ? AND export_name = ?",
                (slot, token, export_name),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def close(self) -> None:
        async with self._operation_lock:
            if self._db is not None:
                await self._db.close()
                self._db = None
                self._root_identity = None
                self._ledger_identity = None

    async def _ensure_open(self) -> None:
        if self._db is not None:
            return
        async with self._open_lock:
            if self._db is not None:
                return
            _ensure_private_root(self._root)
            ledger_path = self._root / _LEDGER_NAME
            _ensure_private_file(ledger_path)
            root_metadata = self._root.lstat()
            ledger_metadata = ledger_path.lstat()
            ledger_uri = f"file:{quote(str(ledger_path), safe='/')}?mode=rw&nofollow=1"
            db = await aiosqlite.connect(ledger_uri, timeout=5.0, uri=True)
            try:
                await db.execute("PRAGMA busy_timeout=5000")
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS editor_exports (
                        slot INTEGER PRIMARY KEY,
                        token TEXT NOT NULL UNIQUE,
                        export_name TEXT NOT NULL UNIQUE,
                        created_at REAL NOT NULL
                    )
                    """
                )
                await db.commit()
            except BaseException:
                await db.close()
                raise
            self._db = db
            self._root_identity = (root_metadata.st_dev, root_metadata.st_ino)
            self._ledger_identity = (ledger_metadata.st_dev, ledger_metadata.st_ino)

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("editor export ledger is closed")
        return self._db

    def _verify_storage_identity(self) -> None:
        root_metadata = self._root.lstat()
        ledger_metadata = (self._root / _LEDGER_NAME).lstat()
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or root_metadata.st_uid != os.getuid()
            or stat.S_IMODE(root_metadata.st_mode) & 0o077
            or (root_metadata.st_dev, root_metadata.st_ino) != self._root_identity
            or not stat.S_ISREG(ledger_metadata.st_mode)
            or stat.S_ISLNK(ledger_metadata.st_mode)
            or ledger_metadata.st_uid != os.getuid()
            or stat.S_IMODE(ledger_metadata.st_mode) & 0o077
            or (ledger_metadata.st_dev, ledger_metadata.st_ino) != self._ledger_identity
        ):
            raise OSError("editor export ledger storage changed")

    async def _rows(
        self, db: aiosqlite.Connection
    ) -> list[tuple[object, object, object, object]]:
        cursor = await db.execute(
            "SELECT slot, token, export_name, created_at FROM editor_exports ORDER BY slot"
        )
        return list(await cursor.fetchall())

    def _reclaim_files(
        self,
        rows: list[tuple[object, object, object, object]],
        stale_before: float,
    ) -> bool:
        for row in rows:
            if not _valid_row(*row):
                return False
            _slot, _token_value, export_name, created_at = row
            if created_at > stale_before:
                continue
            assert isinstance(export_name, str)
            if not self._remove_stale_owned(export_name, stale_before):
                return False
        return True

    def _has_blocking_orphan_or_legacy(
        self,
        rows: list[tuple[object, object, object, object]],
        stale_before: float,
    ) -> bool:
        admitted = {
            row[2] for row in rows if _valid_row(*row) and isinstance(row[2], str)
        }
        for entry in os.scandir(self._root):
            if entry.name in {_LEDGER_NAME, f"{_LEDGER_NAME}-journal"}:
                continue
            if not (
                _LEGACY_EXPORT.fullmatch(entry.name) or _EXPORT.fullmatch(entry.name)
            ):
                continue
            if entry.name in admitted:
                continue
            if not self._remove_stale_owned(entry.name, stale_before):
                return True
        return False

    def _remove_stale_owned(self, name: str, stale_before: float) -> bool:
        path = self._root / name
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return True
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mtime > stale_before
        ):
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def _path_exists(self, name: str) -> bool:
        try:
            (self._root / name).lstat()
        except FileNotFoundError:
            return False
        return True


def editor_export_name(slot: int, job_id: int, token: str) -> str:
    if slot not in range(1, MAX_EDITOR_EXPORTS + 1):
        raise ValueError("editor export slot is invalid")
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
        raise ValueError("job_id must be positive")
    if not _TOKEN.fullmatch(token):
        raise ValueError("editor export token is invalid")
    return f"tongs-slot-{slot}-job-{job_id}-{token}.log"


def _valid_row(
    slot: object, token: object, export_name: object, created_at: object
) -> bool:
    return (
        isinstance(slot, int)
        and not isinstance(slot, bool)
        and slot in range(1, MAX_EDITOR_EXPORTS + 1)
        and isinstance(token, str)
        and _TOKEN.fullmatch(token) is not None
        and isinstance(export_name, str)
        and _EXPORT.fullmatch(export_name) is not None
        and export_name.startswith(f"tongs-slot-{slot}-")
        and export_name.endswith(f"-{token}.log")
        and isinstance(created_at, (int, float))
        and not isinstance(created_at, bool)
        and math.isfinite(created_at)
        and created_at >= 0
    )


def _ensure_private_root(root: Path) -> None:
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    metadata = root.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise OSError("editor export root is not private")


def _ensure_private_file(path: Path) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise OSError("editor export ledger is not private") from None
    else:
        os.close(descriptor)


__all__ = [
    "EDITOR_EXPORT_ROOT_ENV",
    "MAX_EDITOR_EXPORTS",
    "STALE_EDITOR_EXPORT_SECONDS",
    "EditorExportLedger",
    "editor_export_name",
]
