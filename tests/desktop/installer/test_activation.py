"""Atomic desktop activation, recovery and ownership tests."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

import tongs.desktop.installer.activation as activation_module
from tongs.desktop.installer.activation import (
    BoundPythonEnvironment,
    DesktopInstallationPaths,
    DesktopInstallationStore,
    EnvironmentKind,
    RecoveryStatus,
    activate_staged_artifact,
    recover_interrupted_activation,
    repair_activation,
    uninstall_user_activation,
    validate_installed_payload,
)
from tongs.desktop.installer.extract import extract_verified_archive
from tongs.desktop.installer.models import AcceptedReleaseState, InstallerError

from .helpers import documents, verified_metadata


def _store(tmp_path: Path, *, timeout: float = 0.1) -> DesktopInstallationStore:
    return DesktopInstallationStore(
        DesktopInstallationPaths.under(tmp_path / "data"), lock_timeout=timeout
    )


def _environment(tmp_path: Path, name: str = "environment") -> BoundPythonEnvironment:
    root = tmp_path / name
    interpreter = root / "bin/python"
    console = root / "bin/tongs"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    console.write_text(f"#!{interpreter}\n")
    interpreter.chmod(0o755)
    console.chmod(0o755)
    return BoundPythonEnvironment(
        EnvironmentKind.VENV,
        console,
        interpreter,
        console.resolve(),
        interpreter.resolve(),
        "1.2.3",
    )


def _stage(store: DesktopInstallationStore):
    with store.transaction():
        pass
    return extract_verified_archive(
        documents()[1], verified_metadata(), store.paths.staging_root
    )


def test_activation_moves_complete_payload_and_retains_previous(tmp_path: Path) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    first_stage = _stage(store)
    with store.transaction() as transaction:
        first = activate_staged_artifact(transaction, first_stage, environment)

    second_stage = _stage(store)
    with store.transaction() as transaction:
        second = activate_staged_artifact(transaction, second_stage, environment)
        persisted = transaction.read_state()

    assert first.activated.payload.target_path.is_dir()
    assert second.activated.payload.target_path.is_dir()
    assert persisted == second.state
    assert persisted is not None and persisted.previous == first.activated
    assert persisted.recovery is RecoveryStatus.HEALTHY
    assert store.paths.menu_path.read_text().startswith("[Desktop Entry]\n")
    assert not store.paths.journal_path.exists()


def test_activation_rejects_inconsistent_verified_stage_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    staged = replace(_stage(store), archive_sha256="d" * 64)

    with (
        store.transaction() as transaction,
        pytest.raises(InstallerError, match="identity is inconsistent"),
    ):
        activate_staged_artifact(transaction, staged, environment)

    assert staged.staging_path.is_dir()
    assert not store.paths.journal_path.exists()


def test_payload_validation_rejects_oversized_sparse_member_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    staged = _stage(store)
    with store.transaction() as transaction:
        installed = activate_staged_artifact(transaction, staged, environment)
    payload = installed.activated.payload
    declared = payload.files[0]
    member = payload.target_path / declared.path
    member.write_bytes(b"")
    os.truncate(member, 8 * 1024**3)
    reads = 0
    original_read = activation_module.os.read

    def count_read(descriptor: int, byte_count: int) -> bytes:
        nonlocal reads
        reads += 1
        return original_read(descriptor, byte_count)

    monkeypatch.setattr(activation_module.os, "read", count_read)

    with pytest.raises(InstallerError, match="incomplete or changed"):
        validate_installed_payload(payload)

    assert reads == 0


def test_menu_failure_preserves_old_active_and_journal_recovers_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    first_stage = _stage(store)
    with store.transaction() as transaction:
        first = activate_staged_artifact(transaction, first_stage, environment)

    second_stage = _stage(store)

    def fail_menu(*_args: object, **_kwargs: object) -> str:
        raise InstallerError(
            activation_module.InstallerErrorCode.STATE_CONFLICT,
            "injected menu failure",
        )

    monkeypatch.setattr(activation_module, "install_user_menu", fail_menu)
    with store.transaction() as transaction:
        with pytest.raises(InstallerError, match="injected"):
            activate_staged_artifact(transaction, second_stage, environment)
        assert transaction.read_state() == first.state
        journal = transaction.read_journal()
        assert journal is not None
        assert journal.target.payload.target_path.is_dir()

    monkeypatch.undo()
    with store.transaction() as transaction:
        recovered = recover_interrupted_activation(transaction)

    assert recovered is not None and recovered.recovered is True
    assert recovered.state.active == recovered.activated
    assert recovered.state.previous == first.activated
    assert not store.paths.journal_path.exists()


def test_recovery_finishes_when_state_publish_reports_failure_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    first_environment = _environment(tmp_path, "first")
    first_stage = _stage(store)
    with store.transaction() as transaction:
        first = activate_staged_artifact(transaction, first_stage, first_environment)
    old_menu = store.paths.menu_path.read_bytes()
    second_stage = _stage(store)
    original_write = activation_module._write_private_document
    reported = False

    def publish_then_fail(path: Path, document: bytes) -> None:
        nonlocal reported
        original_write(path, document)
        if path == store.paths.state_path and not reported:
            reported = True
            raise InstallerError(
                activation_module.InstallerErrorCode.STATE_CONFLICT,
                "injected directory fsync uncertainty",
                retryable=True,
            )

    monkeypatch.setattr(activation_module, "_write_private_document", publish_then_fail)
    with store.transaction() as transaction, pytest.raises(InstallerError):
        activate_staged_artifact(transaction, second_stage, first_environment)
    monkeypatch.undo()

    assert store.paths.menu_path.read_bytes() == old_menu
    with store.transaction() as transaction:
        published = transaction.read_state()
        assert published is not None and published.active != first.activated
        recovered = recover_interrupted_activation(transaction)

    assert recovered is not None and recovered.activated == published.active
    assert first_environment.console_path.as_posix() in (
        store.paths.menu_path.read_text()
    )
    assert not store.paths.journal_path.exists()


@pytest.mark.parametrize("boundary", ["rename", "fsync", "state"])
def test_prepublication_failures_leave_prior_state_launchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    first_stage = _stage(store)
    with store.transaction() as transaction:
        first = activate_staged_artifact(transaction, first_stage, environment)
    original_menu = store.paths.menu_path.read_bytes()
    second_stage = _stage(store)

    if boundary == "rename":
        monkeypatch.setattr(
            activation_module.os,
            "rename",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("rename")),
        )
    elif boundary == "fsync":
        monkeypatch.setattr(
            activation_module,
            "_fsync_directory",
            lambda *_args: (_ for _ in ()).throw(OSError("fsync")),
        )
    elif boundary == "state":
        original_write = activation_module._write_private_document

        def fail_state(path: Path, document: bytes) -> None:
            if path == store.paths.state_path:
                raise InstallerError(
                    activation_module.InstallerErrorCode.STATE_CONFLICT,
                    "state",
                )
            original_write(path, document)

        monkeypatch.setattr(activation_module, "_write_private_document", fail_state)
    with store.transaction() as transaction, pytest.raises((InstallerError, OSError)):
        activate_staged_artifact(transaction, second_stage, environment)
    monkeypatch.undo()
    with store.transaction() as transaction:
        assert transaction.read_state() == first.state
    assert first.activated.payload.launcher_path.is_file()
    assert store.paths.menu_path.read_bytes() == original_menu


def test_lock_contention_is_bounded_and_retryable(tmp_path: Path) -> None:
    first = _store(tmp_path)
    second = _store(tmp_path, timeout=0.01)

    with (
        first.transaction(),
        pytest.raises(InstallerError) as raised,
        second.transaction(),
    ):
        pass

    assert raised.value.retryable is True


@pytest.mark.asyncio
async def test_uninstall_preserves_watermark_and_unrelated_rpm_files(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    staged = _stage(store)
    rpm = tmp_path / "usr/bin/tongs-desktop"
    rpm.parent.mkdir(parents=True)
    rpm.write_text("rpm-owned")
    watermark = AcceptedReleaseState(
        staged.release.version, staged.manifest_sha256, staged.source_commit
    )

    with store.transaction() as transaction:
        assert await transaction.release_state.compare_and_swap(None, watermark)
        activate_staged_artifact(transaction, staged, environment)
        removed = uninstall_user_activation(transaction)
        repeated = uninstall_user_activation(transaction)
        assert await transaction.release_state.read() == watermark

    assert removed is not None and removed.active is None
    assert repeated == removed
    assert removed.recovery is RecoveryStatus.UNINSTALLED
    assert rpm.read_text() == "rpm-owned"
    assert not store.paths.menu_path.exists()


@pytest.mark.parametrize("boundary", ["menu", "payload"])
def test_uninstall_cleanup_is_durable_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    stage = _stage(store)
    with store.transaction() as transaction:
        installed = activate_staged_artifact(transaction, stage, environment)
    target_path = installed.activated.payload.target_path

    if boundary == "menu":
        monkeypatch.setattr(
            activation_module,
            "remove_user_menu",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                InstallerError(
                    activation_module.InstallerErrorCode.STATE_CONFLICT,
                    "injected menu cleanup",
                    retryable=True,
                )
            ),
        )
    else:
        monkeypatch.setattr(
            activation_module,
            "_remove_owned_payload",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                InstallerError(
                    activation_module.InstallerErrorCode.STATE_CONFLICT,
                    "injected payload cleanup",
                    retryable=True,
                )
            ),
        )

    with store.transaction() as transaction, pytest.raises(InstallerError):
        uninstall_user_activation(transaction)
    monkeypatch.undo()
    with store.transaction() as transaction:
        interrupted = transaction.read_state()
        assert interrupted is not None
        assert interrupted.active is None
        assert interrupted.previous == installed.activated
        assert interrupted.recovery is RecoveryStatus.CLEANUP_REQUIRED
        completed = uninstall_user_activation(transaction)

    assert completed is not None
    assert completed.recovery is RecoveryStatus.UNINSTALLED
    assert not target_path.exists()
    assert not store.paths.menu_path.exists()


def test_uninstall_removes_owned_payload_even_when_content_is_corrupted(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    stage = _stage(store)
    with store.transaction() as transaction:
        installed = activate_staged_artifact(transaction, stage, environment)
    target = installed.activated.payload.target_path
    (target / "untrusted-extra").write_text("changed")

    with store.transaction() as transaction:
        result = uninstall_user_activation(transaction)

    assert result is not None and result.recovery is RecoveryStatus.UNINSTALLED
    assert not target.exists()


def test_uninstall_discards_inactive_recovery_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    first_stage = _stage(store)
    with store.transaction() as transaction:
        first = activate_staged_artifact(transaction, first_stage, environment)
    second_stage = _stage(store)

    def fail_menu(*_args: object, **_kwargs: object) -> str:
        raise InstallerError(
            activation_module.InstallerErrorCode.STATE_CONFLICT,
            "injected menu failure",
        )

    monkeypatch.setattr(activation_module, "install_user_menu", fail_menu)
    with store.transaction() as transaction, pytest.raises(InstallerError):
        activate_staged_artifact(transaction, second_stage, environment)
    monkeypatch.undo()
    with store.transaction() as transaction:
        journal = transaction.read_journal()
        assert journal is not None
        candidate_path = journal.target.payload.target_path
        assert transaction.read_state() == first.state
        removed = uninstall_user_activation(transaction)

    assert removed is not None and removed.recovery is RecoveryStatus.UNINSTALLED
    assert not candidate_path.exists()
    assert not first.activated.payload.target_path.exists()
    assert not store.paths.journal_path.exists()


def test_repair_rebinds_missing_menu_without_network(tmp_path: Path) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    staged = _stage(store)
    with store.transaction() as transaction:
        installed = activate_staged_artifact(transaction, staged, environment)
    store.paths.menu_path.unlink()

    with store.transaction() as transaction:
        repaired = repair_activation(transaction, environment)

    assert repaired.active == installed.activated
    assert repaired.recovery is RecoveryStatus.HEALTHY
    assert hashlib.sha256(store.paths.menu_path.read_bytes()).hexdigest() == (
        repaired.menu_sha256
    )


@pytest.mark.parametrize("published", [False, True])
def test_repair_state_failure_leaves_recorded_menu_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, published: bool
) -> None:
    store = _store(tmp_path)
    first_environment = _environment(tmp_path, "first")
    stage = _stage(store)
    with store.transaction() as transaction:
        installed = activate_staged_artifact(transaction, stage, first_environment)
    old_menu = store.paths.menu_path.read_bytes()
    second_environment = _environment(tmp_path, "second")
    original_write = activation_module._write_private_document

    def fail_repair_state(path: Path, document: bytes) -> None:
        if path != store.paths.state_path:
            original_write(path, document)
            return
        if published:
            original_write(path, document)
        raise InstallerError(
            activation_module.InstallerErrorCode.STATE_CONFLICT,
            "injected repair state failure",
            retryable=True,
        )

    monkeypatch.setattr(activation_module, "_write_private_document", fail_repair_state)
    with store.transaction() as transaction, pytest.raises(InstallerError):
        repair_activation(transaction, second_environment)
    monkeypatch.undo()

    with store.transaction() as transaction:
        state = transaction.read_state()
        assert state is not None
        if published:
            assert state.active is not None
            assert state.active.environment == second_environment
            assert store.paths.menu_path.read_bytes() != old_menu
        else:
            assert state == installed.state
            assert store.paths.menu_path.read_bytes() == old_menu
        repaired = repair_activation(transaction, second_environment)

    assert repaired.recovery is RecoveryStatus.HEALTHY
    assert repaired.active is not None
    assert repaired.active.environment == second_environment


def test_state_refuses_target_outside_private_version_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    staged = _stage(store)
    with store.transaction() as transaction:
        result = activate_staged_artifact(transaction, staged, environment)
    document = store.paths.state_path.read_text()
    store.paths.state_path.write_text(
        document.replace(os.fspath(result.activated.payload.target_path), "/usr")
    )
    store.paths.state_path.chmod(0o600)

    with (
        store.transaction() as transaction,
        pytest.raises(InstallerError, match="outside"),
    ):
        transaction.read_state()


def test_state_refuses_forged_system_ownership(tmp_path: Path) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    staged = _stage(store)
    with store.transaction() as transaction:
        activate_staged_artifact(transaction, staged, environment)
    document = store.paths.state_path.read_text()
    store.paths.state_path.write_text(
        document.replace('"ownership":"user"', '"ownership":"system"')
    )
    store.paths.state_path.chmod(0o600)

    with store.transaction() as transaction, pytest.raises(InstallerError):
        transaction.read_state()
