"""Owned per-user desktop menu registration."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from pathlib import Path

from tongs.desktop.installer.models import InstallerError, InstallerErrorCode

_MAX_ENTRY_BYTES = 16 * 1024


def render_desktop_entry(console_path: Path) -> bytes:
    """Render a fixed desktop entry bound to one absolute Tongs console script."""
    invocation = _exec_argument(console_path)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Tongs\n"
        "Comment=Review GitHub and GitLab changes\n"
        f"Exec={invocation} desktop\n"
        "Terminal=false\n"
        "Categories=Development;RevisionControl;\n"
        "StartupNotify=true\n"
    ).encode("ascii")


def install_user_menu(
    path: Path,
    console_path: Path,
    *,
    replace_digest: str | None,
) -> str:
    """Atomically install an entry without replacing unrelated existing content."""
    desired = render_desktop_entry(console_path)
    digest = hashlib.sha256(desired).hexdigest()
    parent_fd = _open_menu_parent(path.parent)
    temporary: str | None = None
    try:
        current = _read_entry(parent_fd, path.name)
        if current is not None:
            current_digest = hashlib.sha256(current).hexdigest()
            if current == desired:
                return digest
            if replace_digest is None or current_digest != replace_digest:
                raise InstallerError(
                    InstallerErrorCode.STATE_CONFLICT,
                    "The per-user Tongs menu entry is owned by another installation.",
                )
        temporary = f".{path.name}.tmp-{secrets.token_hex(8)}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            _write_all(descriptor, desired)
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary = None
        os.fsync(parent_fd)
        return digest
    except InstallerError:
        raise
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The per-user desktop menu entry could not be updated.",
            retryable=True,
        ) from error
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def remove_user_menu(path: Path, *, expected_digest: str | None) -> bool:
    """Remove only the exact menu content recorded by this installation."""
    if expected_digest is None:
        return False
    try:
        parent_fd = _open_existing_menu_parent(path.parent)
    except FileNotFoundError:
        return False
    try:
        current = _read_entry(parent_fd, path.name)
        if current is None:
            return False
        if hashlib.sha256(current).hexdigest() != expected_digest:
            raise InstallerError(
                InstallerErrorCode.STATE_CONFLICT,
                "The per-user Tongs menu entry changed and was not removed.",
            )
        os.unlink(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
    except InstallerError:
        raise
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The per-user desktop menu entry could not be removed.",
            retryable=True,
        ) from error
    finally:
        os.close(parent_fd)


def desktop_entry_digest(path: Path) -> str | None:
    """Return the bounded digest of an existing regular entry."""
    try:
        parent_fd = _open_existing_menu_parent(path.parent)
    except FileNotFoundError:
        return None
    try:
        content = _read_entry(parent_fd, path.name)
        return hashlib.sha256(content).hexdigest() if content is not None else None
    finally:
        os.close(parent_fd)


def _exec_argument(path: Path) -> str:
    value = os.fspath(path)
    if (
        not path.is_absolute()
        or not value
        or "=" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise InstallerError(
            InstallerErrorCode.INCOMPATIBLE,
            "The Tongs console path cannot be represented by a desktop entry.",
        )
    try:
        value.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        raise InstallerError(
            InstallerErrorCode.INCOMPATIBLE,
            "The Tongs console path cannot be represented by a desktop entry.",
        ) from None
    value = value.replace("%", "%%")
    quoted = "".join(f"\\{char}" if char in '"`$\\' else char for char in value)
    escaped = quoted.replace("\\", "\\\\")
    return f'"{escaped}"'


def _open_menu_parent(path: Path) -> int:
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The per-user applications directory is unavailable.",
            retryable=True,
        ) from error
    return _open_existing_menu_parent(path)


def _open_existing_menu_parent(path: Path) -> int:
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except FileNotFoundError:
        raise
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The per-user applications directory is unsafe.",
        ) from error
    try:
        details = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The per-user applications directory is unsafe.",
        ) from error
    if details.st_uid != os.geteuid():
        os.close(descriptor)
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The per-user applications directory has unsafe ownership.",
        )
    return descriptor


def _read_entry(parent_fd: int, name: str) -> bytes | None:
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The per-user Tongs menu entry is unsafe.",
        ) from error
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_size > _MAX_ENTRY_BYTES
        ):
            raise InstallerError(
                InstallerErrorCode.STATE_CONFLICT,
                "The per-user Tongs menu entry is unsafe.",
            )
        chunks: list[bytes] = []
        remaining = details.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise InstallerError(
                    InstallerErrorCode.STATE_CONFLICT,
                    "The per-user Tongs menu entry changed while it was read.",
                    retryable=True,
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise InstallerError(
                InstallerErrorCode.STATE_CONFLICT,
                "The per-user Tongs menu entry changed while it was read.",
                retryable=True,
            )
        return b"".join(chunks)
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The per-user Tongs menu entry is unsafe.",
        ) from error
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short desktop entry write")
        view = view[written:]


__all__ = [
    "desktop_entry_digest",
    "install_user_menu",
    "remove_user_menu",
    "render_desktop_entry",
]
