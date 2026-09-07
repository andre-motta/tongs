"""Process-lifetime ownership locks for draft submission attempts."""

from __future__ import annotations

import os
import stat
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from tongs.state.drafts.errors import DraftPermissionError, DraftStoreError


class AttemptLock:
    """An advisory lock released by the OS when its process exits."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self._fd = fd

    @classmethod
    def try_acquire(cls, directory: Path, attempt_id: str) -> AttemptLock | None:
        fd = -1
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory_info = directory.lstat()
            if not stat.S_ISDIR(directory_info.st_mode):
                raise DraftPermissionError(
                    "draft lock path must be a private directory"
                )
            if os.name != "nt":
                mode = stat.S_IMODE(directory_info.st_mode)
                if mode & 0o077:
                    raise DraftPermissionError(
                        "draft lock directory permits access by other users"
                    )
            path = directory / f"{attempt_id}.lock"
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DraftPermissionError(
                    "draft attempt lock must be a private regular file"
                )
            if os.name != "nt":
                mode = stat.S_IMODE(info.st_mode)
                if mode & 0o077:
                    raise DraftPermissionError(
                        "draft attempt lock permits access by other users"
                    )
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    fd = -1
                    return None
            else:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    os.close(fd)
                    fd = -1
                    return None
            lock = cls(path, fd)
            fd = -1
            return lock
        except DraftStoreError:
            raise
        except OSError as error:
            raise DraftPermissionError(
                "cannot create private draft submission lock"
            ) from error
        finally:
            if fd >= 0:
                os.close(fd)

    def release(self) -> None:
        if self._fd < 0:
            return
        try:
            if os.name != "nt":
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            else:
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(self._fd)
            self._fd = -1
