"""Crash-recoverable activation state for verified per-user desktop payloads."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Self, cast

from tongs.desktop.artifact_contract import (
    ArchiveEntryType,
    ArtifactOwnership,
    DesktopCompatibility,
    InstallFile,
    PackageKind,
)
from tongs.desktop.installer.menu import (
    desktop_entry_digest,
    install_user_menu,
    remove_user_menu,
    render_desktop_entry,
)
from tongs.desktop.installer.models import (
    AcceptedReleaseState,
    InstallerError,
    InstallerErrorCode,
    ReleaseStateStore,
    VerifiedStagedArtifact,
)

_STATE_SCHEMA = 1
_MAX_STATE_BYTES = 512 * 1024
_LOCK_POLL_SECONDS = 0.05
_TARGET_TOKEN = re.compile(r"^[0-9a-f]{12}$")
_STABLE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class EnvironmentKind(StrEnum):
    """Observed persistence class for the bound Python installation."""

    VENV = "venv"
    PIPX = "pipx"
    USER_SITE = "user-site"
    SYSTEM = "system"
    TRANSIENT_UVX = "transient-uvx"
    UNSUPPORTED = "unsupported"

    @property
    def persistent(self) -> bool:
        return self in {
            EnvironmentKind.VENV,
            EnvironmentKind.PIPX,
            EnvironmentKind.USER_SITE,
            EnvironmentKind.SYSTEM,
        }


class RecoveryStatus(StrEnum):
    """Durable installation recovery state."""

    HEALTHY = "healthy"
    UNINSTALLED = "uninstalled"
    ACTIVATION_PENDING = "activation-pending"
    MENU_REPAIR_REQUIRED = "menu-repair-required"
    PAYLOAD_REPAIR_REQUIRED = "payload-repair-required"
    CLEANUP_REQUIRED = "cleanup-required"


@dataclass(frozen=True, slots=True)
class BoundPythonEnvironment:
    """Exact console and interpreter invocation identity for desktop launches."""

    kind: EnvironmentKind
    console_path: Path
    interpreter_path: Path
    console_real_path: Path
    interpreter_real_path: Path
    core_version: str

    def __post_init__(self) -> None:
        paths = (
            self.console_path,
            self.interpreter_path,
            self.console_real_path,
            self.interpreter_real_path,
        )
        if not all(isinstance(path, Path) and path.is_absolute() for path in paths):
            raise ValueError("bound environment paths must be absolute")
        if not isinstance(self.kind, EnvironmentKind):
            raise TypeError("kind must be an EnvironmentKind")
        if not isinstance(self.core_version, str) or not self.core_version:
            raise ValueError("core_version is required")


@dataclass(frozen=True, slots=True)
class InstalledPayload:
    """Complete immutable identity for one locally activated payload."""

    version: str
    manifest_sha256: str
    source_commit: str
    archive_sha256: str
    archive_byte_count: int
    target_path: Path
    launcher_path: Path
    compatibility: DesktopCompatibility
    files: tuple[InstallFile, ...]
    directories: tuple[str, ...] = ()
    ownership: str = "user"


@dataclass(frozen=True, slots=True)
class InstallationTarget:
    payload: InstalledPayload
    environment: BoundPythonEnvironment


@dataclass(frozen=True, slots=True)
class DesktopInstallationState:
    """Atomic activation pointer and prior recoverable target."""

    generation: int
    active: InstallationTarget | None
    previous: InstallationTarget | None
    menu_sha256: str | None
    recovery: RecoveryStatus
    cleanup: tuple[InstallationTarget, ...] = ()
    schema_version: int = _STATE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != _STATE_SCHEMA or self.generation < 1:
            raise ValueError("invalid desktop installation state")


@dataclass(frozen=True, slots=True)
class ActivationResult:
    state: DesktopInstallationState
    activated: InstallationTarget
    recovered: bool = False


@dataclass(frozen=True, slots=True)
class DesktopInstallationPaths:
    root: Path
    staging_root: Path
    versions_root: Path
    state_path: Path
    watermark_path: Path
    journal_path: Path
    lock_path: Path
    menu_path: Path

    @classmethod
    def under(cls, data_home: Path) -> DesktopInstallationPaths:
        if not data_home.is_absolute():
            raise ValueError("desktop data home must be absolute")
        root = data_home / "tongs" / "desktop"
        return cls(
            root,
            root / "staging",
            root / "versions",
            root / "installation-v1.json",
            root / "accepted-release-v1.json",
            root / "activation-journal-v1.json",
            root / ".command.lock",
            data_home / "applications" / "tongs.desktop",
        )


@dataclass(frozen=True, slots=True)
class _ActivationJournal:
    expected_generation: int
    target: InstallationTarget
    prior_menu_sha256: str | None


class DesktopInstallationStore:
    """Private state owner with one process-wide command transaction."""

    def __init__(
        self, paths: DesktopInstallationPaths, *, lock_timeout: float = 5.0
    ) -> None:
        if lock_timeout <= 0:
            raise ValueError("lock_timeout must be positive")
        self.paths = paths
        self.lock_timeout = lock_timeout

    def transaction(self) -> DesktopInstallationTransaction:
        return DesktopInstallationTransaction(self)


class DesktopInstallationTransaction(
    AbstractContextManager["DesktopInstallationTransaction"]
):
    """One non-nestable lock owner for state, payload and menu changes."""

    def __init__(self, store: DesktopInstallationStore) -> None:
        self._store = store
        self.paths = store.paths
        self._lock_fd: int | None = None
        self.release_state: ReleaseStateStore = _LockedReleaseState(self)

    def __enter__(self) -> Self:
        if self._lock_fd is not None:
            raise RuntimeError("desktop command transaction is not reentrant")
        _ensure_private_roots(self.paths)
        try:
            descriptor = os.open(
                self.paths.lock_path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
        except OSError as error:
            raise InstallerError(
                InstallerErrorCode.STATE_CONFLICT,
                "The desktop command lock is unavailable.",
                retryable=True,
            ) from error
        try:
            details = os.fstat(descriptor)
            if (
                details.st_uid != os.geteuid()
                or not stat.S_ISREG(details.st_mode)
                or stat.S_IMODE(details.st_mode) & 0o077
            ):
                raise InstallerError(
                    InstallerErrorCode.STATE_CONFLICT,
                    "The desktop command lock has unsafe ownership or type.",
                )
            deadline = time.monotonic() + self._store.lock_timeout
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise InstallerError(
                            InstallerErrorCode.STATE_CONFLICT,
                            "Another desktop lifecycle command is still running.",
                            retryable=True,
                        ) from None
                    time.sleep(_LOCK_POLL_SECONDS)
            self._lock_fd = descriptor
            return self
        except InstallerError:
            os.close(descriptor)
            raise
        except OSError as error:
            os.close(descriptor)
            raise InstallerError(
                InstallerErrorCode.STATE_CONFLICT,
                "The desktop command lock is unavailable.",
                retryable=True,
            ) from error
        except BaseException:
            os.close(descriptor)
            raise

    def __exit__(self, *_args: object) -> None:
        descriptor = self._require_lock()
        self._lock_fd = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError as error:
            raise InstallerError(
                InstallerErrorCode.STATE_CONFLICT,
                "The desktop command lock could not be released cleanly.",
                retryable=True,
            ) from error
        finally:
            os.close(descriptor)

    def read_state(self) -> DesktopInstallationState | None:
        self._require_lock()
        document = _read_private_document(self.paths.state_path)
        return (
            _decode_state(document, self.paths.versions_root)
            if document is not None
            else None
        )

    def write_state(
        self,
        expected: DesktopInstallationState | None,
        replacement: DesktopInstallationState,
    ) -> None:
        self._require_lock()
        if self.read_state() != expected:
            raise InstallerError(
                InstallerErrorCode.STATE_CONFLICT,
                "The desktop installation state changed concurrently.",
                retryable=True,
            )
        expected_generation = 1 if expected is None else expected.generation + 1
        if replacement.generation != expected_generation:
            raise ValueError("replacement generation does not follow current state")
        _write_private_document(self.paths.state_path, _encode_state(replacement))

    def read_journal(self) -> _ActivationJournal | None:
        self._require_lock()
        document = _read_private_document(self.paths.journal_path)
        return (
            _decode_journal(document, self.paths.versions_root)
            if document is not None
            else None
        )

    def write_journal(self, journal: _ActivationJournal) -> None:
        self._require_lock()
        _write_private_document(self.paths.journal_path, _encode_journal(journal))

    def remove_journal(self) -> None:
        self._require_lock()
        _unlink_private_document(self.paths.journal_path)

    def _read_watermark(self) -> AcceptedReleaseState | None:
        self._require_lock()
        document = _read_private_document(self.paths.watermark_path)
        if document is None:
            return None
        return _decode_watermark(document)

    def _write_watermark(
        self,
        expected: AcceptedReleaseState | None,
        replacement: AcceptedReleaseState,
    ) -> bool:
        self._require_lock()
        if self._read_watermark() != expected:
            return False
        _write_private_document(
            self.paths.watermark_path, _encode_watermark(replacement)
        )
        return True

    def _require_lock(self) -> int:
        if self._lock_fd is None:
            raise RuntimeError("desktop installation transaction is not active")
        return self._lock_fd


class _LockedReleaseState:
    def __init__(self, transaction: DesktopInstallationTransaction) -> None:
        self._transaction = transaction

    async def read(self) -> AcceptedReleaseState | None:
        return self._transaction._read_watermark()

    async def compare_and_swap(
        self,
        expected: AcceptedReleaseState | None,
        replacement: AcceptedReleaseState,
    ) -> bool:
        return self._transaction._write_watermark(expected, replacement)


def activate_staged_artifact(
    transaction: DesktopInstallationTransaction,
    staged: VerifiedStagedArtifact,
    environment: BoundPythonEnvironment,
) -> ActivationResult:
    """Atomically publish a complete staged payload and retain the prior target."""
    require_activation_environment(transaction, environment)
    if transaction.read_journal() is not None:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "An interrupted desktop activation must be repaired before installing another release.",
            retryable=True,
        )
    current = transaction.read_state()
    target = _prepare_staged_target(transaction, staged, environment)
    expected_generation = current.generation if current is not None else 0
    transaction.write_journal(
        _ActivationJournal(
            expected_generation,
            target,
            current.menu_sha256 if current is not None else None,
        )
    )
    installed = False
    published = False
    candidate_menu_digest: str | None = None
    try:
        if target.payload.target_path.exists():
            raise InstallerError(
                InstallerErrorCode.DESTINATION_COLLISION,
                "The desktop version destination already exists.",
                retryable=True,
            )
        stage_details = staged.staging_path.lstat()
        try:
            os.rename(staged.staging_path, target.payload.target_path)
            installed = True
        except OSError:
            installed = _rename_reached_target(
                staged.staging_path, target.payload.target_path, stage_details
            )
            raise
        _fsync_directory(transaction.paths.staging_root)
        _fsync_directory(transaction.paths.versions_root)
        candidate_menu_digest = hashlib.sha256(
            render_desktop_entry(environment.console_path)
        ).hexdigest()
        menu_digest = install_user_menu(
            transaction.paths.menu_path,
            environment.console_path,
            replace_digest=current.menu_sha256 if current is not None else None,
        )
        if menu_digest != candidate_menu_digest:
            raise AssertionError("installed desktop menu digest changed")
        previous = current.active if current is not None else None
        cleanup = _cleanup_targets(current, active=target, previous=previous)
        replacement = DesktopInstallationState(
            expected_generation + 1,
            target,
            previous,
            menu_digest,
            (RecoveryStatus.CLEANUP_REQUIRED if cleanup else RecoveryStatus.HEALTHY),
            cleanup,
        )
        transaction.write_state(current, replacement)
        published = True
        try:
            transaction.remove_journal()
        except InstallerError:
            pass
        completed = _complete_cleanup(
            transaction, replacement, final_recovery=RecoveryStatus.HEALTHY
        )
        return ActivationResult(completed, target)
    except BaseException as error:
        if not installed:
            try:
                transaction.remove_journal()
            except InstallerError:
                pass
        if not published:
            _restore_previous_menu(transaction, current, candidate_menu_digest)
        if isinstance(error, OSError):
            raise InstallerError(
                InstallerErrorCode.STATE_CONFLICT,
                "The verified desktop payload could not be activated safely.",
                retryable=True,
            ) from error
        raise


def recover_interrupted_activation(
    transaction: DesktopInstallationTransaction,
) -> ActivationResult | None:
    """Finish only the exact locally journaled verified candidate."""
    journal = transaction.read_journal()
    if journal is None:
        return None
    current = transaction.read_state()
    generation = current.generation if current is not None else 0
    if generation != journal.expected_generation:
        if current is not None and current.active == journal.target:
            validate_installed_payload(journal.target.payload)
            _repair_journal_menu(transaction, current, journal)
            transaction.remove_journal()
            completed = _complete_cleanup(
                transaction, current, final_recovery=RecoveryStatus.HEALTHY
            )
            return ActivationResult(completed, journal.target, recovered=True)
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The interrupted desktop activation no longer matches current state.",
        )
    validate_installed_payload(journal.target.payload)
    digest = install_user_menu(
        transaction.paths.menu_path,
        journal.target.environment.console_path,
        replace_digest=_menu_replacement_digest(transaction, current, journal),
    )
    previous = current.active if current is not None else None
    cleanup = _cleanup_targets(current, active=journal.target, previous=previous)
    replacement = DesktopInstallationState(
        generation + 1,
        journal.target,
        previous,
        digest,
        RecoveryStatus.CLEANUP_REQUIRED if cleanup else RecoveryStatus.HEALTHY,
        cleanup,
    )
    transaction.write_state(current, replacement)
    transaction.remove_journal()
    completed = _complete_cleanup(
        transaction, replacement, final_recovery=RecoveryStatus.HEALTHY
    )
    return ActivationResult(completed, journal.target, recovered=True)


def discard_interrupted_activation(
    transaction: DesktopInstallationTransaction,
) -> bool:
    """Discard only an inactive candidate named by the durable local journal."""
    journal = transaction.read_journal()
    if journal is None:
        return False
    current = transaction.read_state()
    referenced_paths = (
        {
            target.payload.target_path
            for target in (current.active, current.previous, *current.cleanup)
            if target is not None
        }
        if current is not None
        else set()
    )
    if journal.target.payload.target_path in referenced_paths:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The interrupted candidate is already referenced by installation state.",
        )
    _remove_owned_payload(transaction, journal.target.payload)
    transaction.remove_journal()
    return True


def require_activation_environment(
    transaction: DesktopInstallationTransaction,
    environment: BoundPythonEnvironment,
) -> None:
    """Reject transient or differently bound environments before remote staging."""
    if not environment.kind.persistent:
        raise _persistent_environment_error()
    from tongs.desktop.installer.launcher import validate_environment_binding

    validate_environment_binding(environment)
    current = transaction.read_state()
    if current is not None and current.cleanup:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "Desktop payload cleanup is incomplete; run repair before installing another release.",
            retryable=True,
        )
    if (
        current is not None
        and current.active is not None
        and not _same_environment_binding(current.active.environment, environment)
    ):
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "This desktop installation is bound to another Python environment; run repair to rebind it explicitly.",
        )


def uninstall_user_activation(
    transaction: DesktopInstallationTransaction,
) -> DesktopInstallationState | None:
    """Clear user activation and remove only exact user-owned menu content."""
    current = transaction.read_state()
    _discard_journal_for_uninstall(transaction, current)
    if current is None:
        return None
    if (
        current.active is None
        and current.previous is None
        and not current.cleanup
        and current.menu_sha256 is None
        and current.recovery is RecoveryStatus.UNINSTALLED
    ):
        return current
    if current.active is None and current.recovery is RecoveryStatus.CLEANUP_REQUIRED:
        replacement = current
    else:
        cleanup = _unique_targets(
            (*current.cleanup, current.active, current.previous),
            exclude=(),
        )
        replacement = DesktopInstallationState(
            current.generation + 1,
            None,
            None,
            current.menu_sha256,
            RecoveryStatus.CLEANUP_REQUIRED,
            cleanup,
        )
        transaction.write_state(current, replacement)
    remove_user_menu(
        transaction.paths.menu_path, expected_digest=replacement.menu_sha256
    )
    return _complete_cleanup(
        transaction,
        replacement,
        final_recovery=RecoveryStatus.UNINSTALLED,
        clear_menu=True,
    )


def _cleanup_targets(
    current: DesktopInstallationState | None,
    *,
    active: InstallationTarget,
    previous: InstallationTarget | None,
) -> tuple[InstallationTarget, ...]:
    if current is None:
        return ()
    return _unique_targets(
        (*current.cleanup, current.active, current.previous),
        exclude=(active, previous),
    )


def _unique_targets(
    targets: tuple[InstallationTarget | None, ...],
    *,
    exclude: tuple[InstallationTarget | None, ...],
) -> tuple[InstallationTarget, ...]:
    excluded = {target.payload.target_path for target in exclude if target is not None}
    observed = set(excluded)
    result: list[InstallationTarget] = []
    for target in targets:
        if target is None or target.payload.target_path in observed:
            continue
        observed.add(target.payload.target_path)
        result.append(target)
    return tuple(result)


def _complete_cleanup(
    transaction: DesktopInstallationTransaction,
    state: DesktopInstallationState,
    *,
    final_recovery: RecoveryStatus,
    clear_menu: bool = False,
) -> DesktopInstallationState:
    if final_recovery not in {RecoveryStatus.HEALTHY, RecoveryStatus.UNINSTALLED}:
        raise ValueError("cleanup final recovery state is invalid")
    current = _drain_cleanup(transaction, state)
    if current.recovery is final_recovery and (
        not clear_menu or current.menu_sha256 is None
    ):
        return current
    replacement = replace(
        current,
        generation=current.generation + 1,
        menu_sha256=None if clear_menu else current.menu_sha256,
        recovery=final_recovery,
    )
    transaction.write_state(current, replacement)
    return replacement


def _drain_cleanup(
    transaction: DesktopInstallationTransaction,
    state: DesktopInstallationState,
) -> DesktopInstallationState:
    current = state
    while current.cleanup:
        _remove_owned_payload(transaction, current.cleanup[0].payload)
        replacement = replace(
            current,
            generation=current.generation + 1,
            cleanup=current.cleanup[1:],
        )
        transaction.write_state(current, replacement)
        current = replacement
    return current


def _discard_journal_for_uninstall(
    transaction: DesktopInstallationTransaction,
    current: DesktopInstallationState | None,
) -> None:
    journal = transaction.read_journal()
    if journal is None:
        return
    active_path = (
        current.active.payload.target_path
        if current is not None and current.active is not None
        else None
    )
    if journal.target.payload.target_path == active_path:
        assert current is not None
        _repair_journal_menu(transaction, current, journal)
    else:
        _remove_owned_payload(transaction, journal.target.payload)
    transaction.remove_journal()


def repair_activation(
    transaction: DesktopInstallationTransaction,
    environment: BoundPythonEnvironment,
) -> DesktopInstallationState:
    """Recover a journal or revalidate and explicitly rebind the active install."""
    if not environment.kind.persistent:
        raise _persistent_environment_error()
    from tongs.desktop.installer.launcher import validate_environment_binding

    validate_environment_binding(environment)
    recover_interrupted_activation(transaction)
    current = transaction.read_state()
    if current is None or current.active is None:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "No verified per-user desktop installation is available to repair.",
        )
    current = _drain_cleanup(transaction, current)
    assert current.active is not None
    validate_installed_payload(current.active.payload)
    digest = install_user_menu(
        transaction.paths.menu_path,
        environment.console_path,
        replace_digest=current.menu_sha256,
    )
    replacement = DesktopInstallationState(
        current.generation + 1,
        InstallationTarget(current.active.payload, environment),
        current.previous,
        digest,
        RecoveryStatus.HEALTHY,
        current.cleanup,
    )
    try:
        transaction.write_state(current, replacement)
    except BaseException:
        try:
            published = transaction.read_state()
        except InstallerError:
            published = None
        if published != replacement:
            _restore_previous_menu(transaction, current, digest)
        raise
    return replacement


def _prepare_staged_target(
    transaction: DesktopInstallationTransaction,
    staged: VerifiedStagedArtifact,
    environment: BoundPythonEnvironment,
) -> InstallationTarget:
    contract = staged.contract
    if (
        staged.release.version != contract.release.release_version
        or staged.release.version != contract.install.release_version
        or staged.source_commit != contract.release.source_commit
        or staged.source_commit != staged.build_identity.source_commit
        or staged.archive_sha256 != contract.artifact.sha256
        or staged.archive_byte_count != contract.artifact.byte_count
        or contract.artifact.package_kind is not PackageKind.USER_ARCHIVE
        or contract.install.package_kind is not PackageKind.USER_ARCHIVE
        or contract.artifact.ownership is not ArtifactOwnership.PER_USER
        or contract.install.ownership is not ArtifactOwnership.PER_USER
    ):
        raise InstallerError(
            InstallerErrorCode.INTEGRITY_FAILED,
            "The verified desktop staging identity is inconsistent.",
        )
    stage = staged.staging_path
    if stage.parent != transaction.paths.staging_root or stage.is_symlink():
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The verified desktop staging location is outside this installation.",
        )
    try:
        stage_real = stage.resolve(strict=True)
        root_real = transaction.paths.staging_root.resolve(strict=True)
        stage_real.relative_to(root_real)
    except (OSError, ValueError) as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The verified desktop staging location is unsafe.",
        ) from error
    stage_token = stage.name.removeprefix(".tongs-stage-")
    if len(stage_token) != 32 or any(
        character not in "0123456789abcdef" for character in stage_token
    ):
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The verified desktop staging identity is invalid.",
        )
    target_name = (
        f"{staged.release.version}-{staged.manifest_sha256[:16]}-"
        f"{staged.archive_sha256[:16]}-{stage_token[:12]}"
    )
    if PurePosixPath(target_name).name != target_name:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The verified desktop release identity is unsafe.",
        )
    target_path = transaction.paths.versions_root / target_name
    relative_launcher = contract.install.launcher_path
    if staged.launcher_path != stage / relative_launcher:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The verified desktop launcher identity changed after staging.",
        )
    payload = InstalledPayload(
        staged.release.version,
        staged.manifest_sha256,
        staged.source_commit,
        staged.archive_sha256,
        staged.archive_byte_count,
        target_path,
        target_path / relative_launcher,
        contract.install.compatibility,
        tuple(
            InstallFile(
                entry.path,
                entry.byte_count,
                entry.sha256 or "",
                bool(entry.mode & 0o111),
            )
            for entry in contract.layout.entries
            if entry.entry_type is ArchiveEntryType.FILE
        ),
        directories=tuple(
            entry.path
            for entry in contract.layout.entries
            if entry.entry_type is ArchiveEntryType.DIRECTORY
        ),
    )
    _validate_payload_at(stage, payload, staged_path=True)
    return InstallationTarget(payload, environment)


def _same_environment_binding(
    first: BoundPythonEnvironment, second: BoundPythonEnvironment
) -> bool:
    return (
        first.kind == second.kind
        and first.console_path == second.console_path
        and first.interpreter_path == second.interpreter_path
    )


def _persistent_environment_error() -> InstallerError:
    return InstallerError(
        InstallerErrorCode.INCOMPATIBLE,
        "Desktop menu registration requires a persistent Python installation. "
        "Install Tongs with 'pipx install tongs' or "
        "'python -m pip install --user tongs', then retry.",
    )


def validate_installed_payload(payload: InstalledPayload) -> None:
    """Validate the complete immutable content of an activated payload."""
    _validate_payload_at(payload.target_path, payload, staged_path=False)


def _remove_owned_payload(
    transaction: DesktopInstallationTransaction, payload: InstalledPayload
) -> None:
    if (
        payload.ownership != "user"
        or payload.target_path.parent != transaction.paths.versions_root
    ):
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The desktop payload is not owned by this per-user installation.",
        )
    _validate_payload_target(payload, transaction.paths.versions_root)
    try:
        parent_fd = os.open(
            transaction.paths.versions_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The private desktop version directory is unavailable.",
            retryable=True,
        ) from error
    try:
        try:
            details = os.stat(
                payload.target_path.name, dir_fd=parent_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
            raise InstallerError(
                InstallerErrorCode.STATE_CONFLICT,
                "The per-user desktop payload changed and was not removed.",
            )
        shutil.rmtree(payload.target_path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except InstallerError:
        raise
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The per-user desktop payload could not be removed.",
            retryable=True,
        ) from error
    finally:
        os.close(parent_fd)


def _validate_payload_at(
    root: Path, payload: InstalledPayload, *, staged_path: bool
) -> None:
    root_fd = -1
    try:
        root_fd = os.open(
            root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        details = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise OSError("unsafe payload root")
        expected_files, expected_directories = _declared_payload_paths(payload)
        actual_files: set[PurePosixPath] = set()
        actual_directories: set[PurePosixPath] = set()
        for directory, names, filenames in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            directory_details = directory_path.lstat()
            if (
                not stat.S_ISDIR(directory_details.st_mode)
                or directory_details.st_uid != os.geteuid()
            ):
                raise OSError("unsafe payload directory")
            if directory_path != root:
                relative_directory = PurePosixPath(
                    directory_path.relative_to(root).as_posix()
                )
                actual_directories.add(relative_directory)
                if stat.S_IMODE(directory_details.st_mode) != 0o755:
                    raise OSError("payload directory mode changed")
            for name in names:
                member_details = (directory_path / name).lstat()
                if not stat.S_ISDIR(member_details.st_mode):
                    raise OSError("payload contains a non-directory member")
            for name in filenames:
                member = directory_path / name
                member_details = member.lstat()
                if (
                    not stat.S_ISREG(member_details.st_mode)
                    or member_details.st_uid != os.geteuid()
                ):
                    raise OSError("payload contains an unsafe member")
                actual_files.add(PurePosixPath(member.relative_to(root).as_posix()))
        if actual_files != expected_files or actual_directories != expected_directories:
            raise OSError("payload members changed")
        for declared in payload.files:
            if _payload_file_digest(root_fd, declared) != declared.sha256:
                raise OSError("payload member identity changed")
        expected_launcher = root / payload.launcher_path.relative_to(
            payload.target_path
        )
        if not staged_path and expected_launcher != payload.launcher_path:
            raise OSError("launcher target changed")
    except (OSError, ValueError) as error:
        raise InstallerError(
            InstallerErrorCode.INTEGRITY_FAILED,
            "The installed desktop payload is incomplete or changed.",
        ) from error
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _payload_file_digest(root_fd: int, declared: InstallFile) -> str:
    relative = PurePosixPath(declared.path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise OSError("unsafe payload member path")
    parent_fd = root_fd
    opened_directories: list[int] = []
    descriptor = -1
    try:
        for part in relative.parts[:-1]:
            parent_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            opened_directories.append(parent_fd)
        descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        details = os.fstat(descriptor)
        expected_mode = 0o755 if declared.executable else 0o644
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_size != declared.byte_count
            or stat.S_IMODE(details.st_mode) != expected_mode
        ):
            raise OSError("payload member identity changed")
        digest = hashlib.sha256()
        remaining = declared.byte_count
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise OSError("payload member changed while read")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise OSError("payload member changed while read")
        final_details = os.fstat(descriptor)
        if (
            final_details.st_dev != details.st_dev
            or final_details.st_ino != details.st_ino
            or final_details.st_size != details.st_size
            or final_details.st_mode != details.st_mode
            or final_details.st_uid != details.st_uid
        ):
            raise OSError("payload member changed while read")
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for directory_fd in reversed(opened_directories):
            os.close(directory_fd)


def _restore_previous_menu(
    transaction: DesktopInstallationTransaction,
    current: DesktopInstallationState | None,
    candidate_digest: str | None,
) -> None:
    if candidate_digest is None:
        return
    try:
        if current is None or current.active is None:
            remove_user_menu(
                transaction.paths.menu_path, expected_digest=candidate_digest
            )
        else:
            install_user_menu(
                transaction.paths.menu_path,
                current.active.environment.console_path,
                replace_digest=candidate_digest,
            )
    except (InstallerError, OSError):
        return


def _rename_reached_target(
    staging_path: Path, target_path: Path, source_details: os.stat_result
) -> bool:
    try:
        target_details = target_path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        staging_path.lstat()
    except FileNotFoundError:
        return (
            target_details.st_dev == source_details.st_dev
            and target_details.st_ino == source_details.st_ino
        )
    except OSError:
        return True
    return False


def _repair_journal_menu(
    transaction: DesktopInstallationTransaction,
    current: DesktopInstallationState,
    journal: _ActivationJournal,
) -> None:
    desired = hashlib.sha256(
        render_desktop_entry(journal.target.environment.console_path)
    ).hexdigest()
    observed = desktop_entry_digest(transaction.paths.menu_path)
    if observed == desired:
        return
    install_user_menu(
        transaction.paths.menu_path,
        journal.target.environment.console_path,
        replace_digest=(
            journal.prior_menu_sha256
            if observed == journal.prior_menu_sha256
            else current.menu_sha256
        ),
    )


def _menu_replacement_digest(
    transaction: DesktopInstallationTransaction,
    current: DesktopInstallationState | None,
    journal: _ActivationJournal,
) -> str | None:
    observed = desktop_entry_digest(transaction.paths.menu_path)
    candidates = (
        current.menu_sha256 if current is not None else None,
        journal.prior_menu_sha256,
    )
    return next((digest for digest in candidates if observed == digest), None)


def _ensure_private_roots(paths: DesktopInstallationPaths) -> None:
    for path in (paths.root, paths.staging_root, paths.versions_root):
        try:
            path.mkdir(mode=0o700, parents=path == paths.root)
        except FileExistsError:
            pass
        except OSError as error:
            raise InstallerError(
                InstallerErrorCode.STATE_CONFLICT,
                "The private desktop installation directory is unavailable.",
                retryable=True,
            ) from error
        try:
            descriptor = os.open(
                path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
        except OSError as error:
            raise InstallerError(
                InstallerErrorCode.STATE_CONFLICT,
                "The private desktop installation directory is unsafe.",
            ) from error
        try:
            details = os.fstat(descriptor)
            if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
                raise InstallerError(
                    InstallerErrorCode.STATE_CONFLICT,
                    "The private desktop installation directory has unsafe permissions.",
                )
        except OSError as error:
            raise InstallerError(
                InstallerErrorCode.STATE_CONFLICT,
                "The private desktop installation directory is unsafe.",
            ) from error
        finally:
            os.close(descriptor)


def _write_private_document(path: Path, document: bytes) -> None:
    if len(document) > _MAX_STATE_BYTES:
        raise InstallerError(
            InstallerErrorCode.LIMIT_EXCEEDED,
            "The desktop installation state exceeds its size limit.",
        )
    try:
        parent_fd = os.open(
            path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The desktop installation state directory is unavailable.",
            retryable=True,
        ) from error
    temporary = f".{path.name}.tmp-{secrets.token_hex(8)}"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            _write_all(descriptor, document)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary = ""
        os.fsync(parent_fd)
    except InstallerError:
        raise
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The desktop installation state could not be published.",
            retryable=True,
        ) from error
    finally:
        if temporary:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _read_private_document(path: Path) -> bytes | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The desktop installation state is unsafe.",
        ) from error
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) & 0o077
            or details.st_size > _MAX_STATE_BYTES
        ):
            raise InstallerError(
                InstallerErrorCode.STATE_CONFLICT,
                "The desktop installation state is unsafe.",
            )
        chunks: list[bytes] = []
        remaining = details.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise _invalid_state("Desktop installation state changed while read.")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise _invalid_state("Desktop installation state changed while read.")
        return b"".join(chunks)
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The desktop installation state is unsafe.",
        ) from error
    finally:
        os.close(descriptor)


def _unlink_private_document(path: Path) -> None:
    try:
        parent_fd = os.open(
            path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The desktop recovery journal directory is unavailable.",
            retryable=True,
        ) from error
    try:
        try:
            os.unlink(path.name, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        os.fsync(parent_fd)
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The desktop recovery journal could not be removed.",
            retryable=True,
        ) from error
    finally:
        os.close(parent_fd)


def _encode_state(state: DesktopInstallationState) -> bytes:
    return _json_bytes(
        {
            "schema_version": state.schema_version,
            "generation": state.generation,
            "active": _target_json(state.active),
            "previous": _target_json(state.previous),
            "cleanup": [_target_json(target) for target in state.cleanup],
            "menu_sha256": state.menu_sha256,
            "recovery": state.recovery.value,
        }
    )


def _decode_state(document: bytes, versions_root: Path) -> DesktopInstallationState:
    value = _json_object(document, "installation state")
    _exact_keys(
        value,
        {
            "schema_version",
            "generation",
            "active",
            "previous",
            "cleanup",
            "menu_sha256",
            "recovery",
        },
    )
    try:
        raw_cleanup = value["cleanup"]
        if not isinstance(raw_cleanup, list) or len(raw_cleanup) > 10_000:
            raise _invalid_state("Desktop cleanup state is invalid.")
        cleanup = tuple(_required_target(item) for item in raw_cleanup)
        result = DesktopInstallationState(
            _positive_int(value["generation"]),
            _target_from_json(value["active"]),
            _target_from_json(value["previous"]),
            _optional_digest(value["menu_sha256"]),
            RecoveryStatus(_string(value["recovery"])),
            cleanup,
            _positive_int(value["schema_version"]),
        )
        _validate_state_targets(result, versions_root)
        _validate_state_shape(result)
        return result
    except (TypeError, ValueError) as error:
        raise _invalid_state("Desktop installation state is invalid.") from error


def _encode_journal(journal: _ActivationJournal) -> bytes:
    return _json_bytes(
        {
            "schema_version": _STATE_SCHEMA,
            "expected_generation": journal.expected_generation,
            "target": _target_json(journal.target),
            "prior_menu_sha256": journal.prior_menu_sha256,
        }
    )


def _decode_journal(document: bytes, versions_root: Path) -> _ActivationJournal:
    value = _json_object(document, "activation journal")
    _exact_keys(
        value,
        {"schema_version", "expected_generation", "target", "prior_menu_sha256"},
    )
    if value["schema_version"] != _STATE_SCHEMA:
        raise _invalid_state("Desktop activation journal is invalid.")
    generation = value["expected_generation"]
    if type(generation) is not int or generation < 0:
        raise _invalid_state("Desktop activation journal is invalid.")
    target = _target_from_json(value["target"])
    if target is None:
        raise _invalid_state("Desktop activation journal is invalid.")
    _validate_target_location(target, versions_root)
    return _ActivationJournal(
        generation, target, _optional_digest(value["prior_menu_sha256"])
    )


def _encode_watermark(state: AcceptedReleaseState) -> bytes:
    return _json_bytes(
        {
            "schema_version": _STATE_SCHEMA,
            "version": state.version,
            "manifest_sha256": state.manifest_sha256,
            "source_commit": state.source_commit,
        }
    )


def _decode_watermark(document: bytes) -> AcceptedReleaseState:
    value = _json_object(document, "accepted release state")
    _exact_keys(
        value, {"schema_version", "version", "manifest_sha256", "source_commit"}
    )
    if value["schema_version"] != _STATE_SCHEMA:
        raise _invalid_state("Desktop accepted-release state is invalid.")
    return AcceptedReleaseState(
        _string(value["version"]),
        _digest(value["manifest_sha256"]),
        _git_sha(value["source_commit"]),
    )


def _target_json(target: InstallationTarget | None) -> object:
    if target is None:
        return None
    payload = target.payload
    environment = target.environment
    return {
        "payload": {
            "version": payload.version,
            "manifest_sha256": payload.manifest_sha256,
            "source_commit": payload.source_commit,
            "archive_sha256": payload.archive_sha256,
            "archive_byte_count": payload.archive_byte_count,
            "target_path": os.fspath(payload.target_path),
            "launcher_path": os.fspath(payload.launcher_path),
            "ownership": payload.ownership,
            "compatibility": {
                "core_minimum": payload.compatibility.core_minimum,
                "core_maximum_exclusive": payload.compatibility.core_maximum_exclusive,
                "rpc_api_major": payload.compatibility.rpc_api_major,
                "plugin_api_major": payload.compatibility.plugin_api_major,
            },
            "files": [
                {
                    "path": item.path,
                    "byte_count": item.byte_count,
                    "sha256": item.sha256,
                    "executable": item.executable,
                }
                for item in payload.files
            ],
            "directories": list(payload.directories),
        },
        "environment": {
            "kind": environment.kind.value,
            "console_path": os.fspath(environment.console_path),
            "interpreter_path": os.fspath(environment.interpreter_path),
            "console_real_path": os.fspath(environment.console_real_path),
            "interpreter_real_path": os.fspath(environment.interpreter_real_path),
            "core_version": environment.core_version,
        },
    }


def _target_from_json(value: object) -> InstallationTarget | None:
    if value is None:
        return None
    item = _object(value)
    _exact_keys(item, {"payload", "environment"})
    payload = _object(item["payload"])
    environment = _object(item["environment"])
    _exact_keys(
        payload,
        {
            "version",
            "manifest_sha256",
            "source_commit",
            "archive_sha256",
            "archive_byte_count",
            "target_path",
            "launcher_path",
            "ownership",
            "compatibility",
            "files",
            "directories",
        },
    )
    _exact_keys(
        environment,
        {
            "kind",
            "console_path",
            "interpreter_path",
            "console_real_path",
            "interpreter_real_path",
            "core_version",
        },
    )
    compatibility = _object(payload["compatibility"])
    _exact_keys(
        compatibility,
        {"core_minimum", "core_maximum_exclusive", "rpc_api_major", "plugin_api_major"},
    )
    raw_files = payload["files"]
    if not isinstance(raw_files, list) or not raw_files or len(raw_files) > 10_000:
        raise _invalid_state("Desktop installation state is invalid.")
    files: list[InstallFile] = []
    for raw_file in raw_files:
        declared = _object(raw_file)
        _exact_keys(declared, {"path", "byte_count", "sha256", "executable"})
        executable = declared["executable"]
        if type(executable) is not bool:
            raise _invalid_state("Desktop installation state is invalid.")
        files.append(
            InstallFile(
                _string(declared["path"]),
                _positive_int(declared["byte_count"], allow_zero=True),
                _digest(declared["sha256"]),
                executable,
            )
        )
    raw_directories = payload["directories"]
    if (
        not isinstance(raw_directories, list)
        or len(raw_directories) > 10_000
        or not all(isinstance(item, str) for item in raw_directories)
    ):
        raise _invalid_state("Desktop installation directory state is invalid.")
    directories = tuple(_string(item) for item in raw_directories)
    target_path = _absolute_path(payload["target_path"])
    launcher_path = _absolute_path(payload["launcher_path"])
    try:
        launcher_path.relative_to(target_path)
        target = InstallationTarget(
            InstalledPayload(
                _stable_version(payload["version"]),
                _digest(payload["manifest_sha256"]),
                _git_sha(payload["source_commit"]),
                _digest(payload["archive_sha256"]),
                _positive_int(payload["archive_byte_count"]),
                target_path,
                launcher_path,
                DesktopCompatibility(
                    _string(compatibility["core_minimum"]),
                    _string(compatibility["core_maximum_exclusive"]),
                    _positive_int(compatibility["rpc_api_major"]),
                    _positive_int(compatibility["plugin_api_major"]),
                ),
                tuple(files),
                directories,
                _string(payload["ownership"]),
            ),
            BoundPythonEnvironment(
                EnvironmentKind(_string(environment["kind"])),
                _absolute_path(environment["console_path"]),
                _absolute_path(environment["interpreter_path"]),
                _absolute_path(environment["console_real_path"]),
                _absolute_path(environment["interpreter_real_path"]),
                _string(environment["core_version"]),
            ),
        )
    except (TypeError, ValueError) as error:
        raise _invalid_state("Desktop installation state is invalid.") from error
    if target.payload.ownership != "user":
        raise _invalid_state("Desktop installation state has unsupported ownership.")
    return target


def _required_target(value: object) -> InstallationTarget:
    target = _target_from_json(value)
    if target is None:
        raise _invalid_state("Desktop cleanup target is invalid.")
    return target


def _validate_state_targets(
    state: DesktopInstallationState, versions_root: Path
) -> None:
    for target in (state.active, state.previous, *state.cleanup):
        if target is not None:
            _validate_target_location(target, versions_root)


def _validate_state_shape(state: DesktopInstallationState) -> None:
    targets = tuple(
        target
        for target in (state.active, state.previous, *state.cleanup)
        if target is not None
    )
    paths = [target.payload.target_path for target in targets]
    if len(paths) != len(set(paths)):
        raise _invalid_state("Desktop installation targets are not distinct.")
    if state.active is None:
        if state.recovery is RecoveryStatus.UNINSTALLED:
            if (
                state.previous is not None
                or state.cleanup
                or state.menu_sha256 is not None
            ):
                raise _invalid_state("Desktop uninstall state is inconsistent.")
            return
        if state.recovery is not RecoveryStatus.CLEANUP_REQUIRED:
            raise _invalid_state("Desktop installation state has no active target.")
        if not state.cleanup and state.menu_sha256 is None:
            raise _invalid_state("Desktop cleanup state has no remaining work.")
        return
    if state.menu_sha256 is None or state.recovery is RecoveryStatus.UNINSTALLED:
        raise _invalid_state("Desktop active installation state is inconsistent.")
    if state.cleanup and state.recovery is not RecoveryStatus.CLEANUP_REQUIRED:
        raise _invalid_state("Desktop payload cleanup state is inconsistent.")


def _validate_target_location(target: InstallationTarget, versions_root: Path) -> None:
    payload = target.payload
    _validate_payload_target(payload, versions_root)
    try:
        _declared_payload_paths(payload)
        payload.launcher_path.relative_to(payload.target_path)
    except ValueError as error:
        raise _invalid_state(
            "Desktop launcher is outside its installation target."
        ) from error


def _declared_payload_paths(
    payload: InstalledPayload,
) -> tuple[set[PurePosixPath], set[PurePosixPath]]:
    files = {PurePosixPath(item.path) for item in payload.files}
    directories = {PurePosixPath(item) for item in payload.directories}
    if len(files) != len(payload.files) or len(directories) != len(payload.directories):
        raise ValueError("duplicate payload member")
    for raw, path in (
        *((item.path, PurePosixPath(item.path)) for item in payload.files),
        *((item, PurePosixPath(item)) for item in payload.directories),
    ):
        if (
            path.is_absolute()
            or not path.parts
            or ".." in path.parts
            or path.as_posix() != raw
        ):
            raise ValueError("unsafe payload member path")
    for path in (*files, *directories):
        parent = path.parent
        while parent != PurePosixPath("."):
            if parent not in directories:
                raise ValueError("payload parent directory is undeclared")
            parent = parent.parent
    return files, directories


def _validate_payload_target(payload: InstalledPayload, versions_root: Path) -> None:
    prefix = (
        f"{payload.version}-{payload.manifest_sha256[:16]}-"
        f"{payload.archive_sha256[:16]}-"
    )
    name = payload.target_path.name
    if (
        payload.target_path.parent != versions_root
        or not name.startswith(prefix)
        or _TARGET_TOKEN.fullmatch(name[len(prefix) :]) is None
    ):
        raise _invalid_state("Desktop installation target is outside its version root.")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _json_object(document: bytes, label: str) -> dict[str, object]:
    if len(document) > _MAX_STATE_BYTES:
        raise _invalid_state(f"Desktop {label} exceeds its size limit.")
    try:
        value = json.loads(document, object_pairs_hook=_reject_duplicate_keys)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        raise _invalid_state(f"Desktop {label} is invalid.") from error
    return _object(value)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _invalid_state("Desktop installation state is invalid.")
    return cast(dict[str, object], value)


def _exact_keys(value: dict[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise _invalid_state("Desktop installation state fields are invalid.")


def _string(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 4096:
        raise _invalid_state("Desktop installation state string is invalid.")
    return value


def _stable_version(value: object) -> str:
    text = _string(value)
    if _STABLE_VERSION.fullmatch(text) is None:
        raise _invalid_state("Desktop installation version is invalid.")
    return text


def _positive_int(value: object, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum or value > 2**63 - 1:
        raise _invalid_state("Desktop installation state number is invalid.")
    return value


def _digest(value: object) -> str:
    text = _string(value)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise _invalid_state("Desktop installation digest is invalid.")
    return text


def _optional_digest(value: object) -> str | None:
    return None if value is None else _digest(value)


def _git_sha(value: object) -> str:
    text = _string(value)
    if len(text) != 40 or any(char not in "0123456789abcdef" for char in text):
        raise _invalid_state("Desktop installation source identity is invalid.")
    return text


def _absolute_path(value: object) -> Path:
    path = Path(_string(value))
    text = os.fspath(path)
    if not path.is_absolute() or any(ord(char) < 32 for char in text):
        raise _invalid_state("Desktop installation path is invalid.")
    return path


def _invalid_state(message: str) -> InstallerError:
    return InstallerError(InstallerErrorCode.STATE_CONFLICT, message)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The desktop installation directory could not be synchronized.",
            retryable=True,
        ) from error


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short desktop state write")
        view = view[written:]


__all__ = [
    "ActivationResult",
    "BoundPythonEnvironment",
    "DesktopInstallationPaths",
    "DesktopInstallationState",
    "DesktopInstallationStore",
    "DesktopInstallationTransaction",
    "EnvironmentKind",
    "InstallationTarget",
    "InstalledPayload",
    "RecoveryStatus",
    "activate_staged_artifact",
    "discard_interrupted_activation",
    "recover_interrupted_activation",
    "repair_activation",
    "require_activation_environment",
    "uninstall_user_activation",
    "validate_installed_payload",
]
