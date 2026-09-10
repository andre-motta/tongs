"""Atomic desktop activation, recovery and ownership tests."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

import tongs.desktop.installer.activation as activation_module
from tongs.desktop.artifact_contract import ArchiveEntry, ArchiveEntryType, InstallFile
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
from tongs.desktop.installer.menu import render_desktop_entry
from tongs.desktop.installer.models import (
    AcceptedReleaseState,
    InstallerError,
    VerifiedStagedArtifact,
)

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


def _stage_with_icon(
    store: DesktopInstallationStore, content: bytes = b"packaged-icon"
) -> VerifiedStagedArtifact:
    staged = _stage(store)
    relative = "runtime/share/pixmaps/tongs.png"
    icon_path = staged.staging_path / relative
    icon_path.parent.mkdir(parents=True)
    icon_path.write_bytes(content)
    icon_path.chmod(0o644)
    digest = hashlib.sha256(content).hexdigest()
    declared_file = InstallFile(relative, len(content), digest)
    additions = (
        ArchiveEntry("runtime/share", ArchiveEntryType.DIRECTORY, 0o755, 0),
        ArchiveEntry("runtime/share/pixmaps", ArchiveEntryType.DIRECTORY, 0o755, 0),
        ArchiveEntry(
            relative,
            ArchiveEntryType.FILE,
            0o644,
            len(content),
            digest,
        ),
    )
    contract = replace(
        staged.contract,
        install=replace(
            staged.contract.install,
            files=(*staged.contract.install.files, declared_file),
        ),
        layout=replace(
            staged.contract.layout,
            entries=(*staged.contract.layout.entries, *additions),
            file_count=staged.contract.layout.file_count + 1,
            total_bytes=staged.contract.layout.total_bytes + len(content),
        ),
    )
    return replace(staged, contract=contract)


def _installed_icon_path(target_path: Path) -> Path:
    return target_path / "runtime/share/pixmaps/tongs.png"


def test_activation_moves_complete_payload_and_retains_previous(tmp_path: Path) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    first_stage = _stage_with_icon(store, b"first-icon")
    with store.transaction() as transaction:
        first = activate_staged_artifact(transaction, first_stage, environment)

    second_stage = _stage_with_icon(store, b"second-icon")
    with store.transaction() as transaction:
        second = activate_staged_artifact(transaction, second_stage, environment)
        persisted = transaction.read_state()

    assert first.activated.payload.target_path.is_dir()
    assert second.activated.payload.target_path.is_dir()
    assert persisted == second.state
    assert persisted is not None and persisted.previous == first.activated
    assert persisted.recovery is RecoveryStatus.HEALTHY
    menu = store.paths.menu_path.read_text()
    assert menu.startswith("[Desktop Entry]\n")
    assert (
        f"Icon={_installed_icon_path(second.activated.payload.target_path)}\n" in menu
    )
    assert str(_installed_icon_path(first.activated.payload.target_path)) not in menu
    assert not store.paths.journal_path.exists()


def test_third_activation_removes_only_obsolete_recorded_payload(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    results = []
    for _ in range(3):
        staged = _stage(store)
        with store.transaction() as transaction:
            results.append(activate_staged_artifact(transaction, staged, environment))

    with store.transaction() as transaction:
        state = transaction.read_state()

    assert state == results[2].state
    assert state is not None and state.active == results[2].activated
    assert state.previous == results[1].activated
    assert state.cleanup == ()
    assert not results[0].activated.payload.target_path.exists()
    assert results[1].activated.payload.target_path.is_dir()
    assert results[2].activated.payload.target_path.is_dir()


def test_third_activation_retains_obsolete_identity_until_cleanup_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    installed = []
    for _ in range(2):
        staged = _stage(store)
        with store.transaction() as transaction:
            installed.append(activate_staged_artifact(transaction, staged, environment))
    obsolete = installed[0].activated
    third_stage = _stage(store)
    original_remove = activation_module._remove_owned_payload

    def fail_obsolete_cleanup(transaction, payload) -> None:
        if payload.target_path == obsolete.payload.target_path:
            raise InstallerError(
                activation_module.InstallerErrorCode.STATE_CONFLICT,
                "injected obsolete cleanup failure",
                retryable=True,
            )
        original_remove(transaction, payload)

    monkeypatch.setattr(
        activation_module, "_remove_owned_payload", fail_obsolete_cleanup
    )
    with store.transaction() as transaction, pytest.raises(InstallerError):
        activate_staged_artifact(transaction, third_stage, environment)
    monkeypatch.undo()

    with store.transaction() as transaction:
        interrupted = transaction.read_state()
        assert interrupted is not None and interrupted.active is not None
        assert interrupted.previous == installed[1].activated
        assert interrupted.cleanup == (obsolete,)
        assert interrupted.recovery is RecoveryStatus.CLEANUP_REQUIRED
        repaired = repair_activation(transaction, environment)

    assert repaired.recovery is RecoveryStatus.HEALTHY
    assert repaired.cleanup == ()
    assert not obsolete.payload.target_path.exists()


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


def test_rename_error_after_move_retains_recoverable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    staged = _stage(store)
    original_rename = activation_module.os.rename

    def rename_then_report(*args, **kwargs) -> None:
        original_rename(*args, **kwargs)
        raise OSError("injected rename completion uncertainty")

    monkeypatch.setattr(activation_module.os, "rename", rename_then_report)
    with (
        store.transaction() as transaction,
        pytest.raises(InstallerError, match="activated safely"),
    ):
        activate_staged_artifact(transaction, staged, environment)
    monkeypatch.undo()

    with store.transaction() as transaction:
        journal = transaction.read_journal()
        assert journal is not None
        assert journal.target.payload.target_path.is_dir()
        recovered = recover_interrupted_activation(transaction)

    assert recovered is not None and recovered.recovered
    assert recovered.state.recovery is RecoveryStatus.HEALTHY
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


@pytest.mark.parametrize("mutation", ["extra-directory", "directory-mode"])
def test_payload_validation_enforces_complete_directory_identity(
    tmp_path: Path, mutation: str
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    staged = _stage(store)
    with store.transaction() as transaction:
        installed = activate_staged_artifact(transaction, staged, environment)
    payload = installed.activated.payload
    if mutation == "extra-directory":
        (payload.target_path / "injected-empty").mkdir()
    else:
        directory = payload.target_path / payload.directories[-1]
        directory.chmod(0o700)

    with pytest.raises(InstallerError, match="incomplete or changed"):
        validate_installed_payload(payload)


def test_menu_failure_preserves_old_active_and_journal_recovers_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    first_stage = _stage_with_icon(store, b"first-icon")
    with store.transaction() as transaction:
        first = activate_staged_artifact(transaction, first_stage, environment)

    second_stage = _stage_with_icon(store, b"second-icon")

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
    assert (
        f"Icon={_installed_icon_path(recovered.activated.payload.target_path)}\n"
        in store.paths.menu_path.read_text()
    )
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
    first_stage = _stage_with_icon(store, b"first-icon")
    with store.transaction() as transaction:
        first = activate_staged_artifact(transaction, first_stage, environment)
    original_menu = store.paths.menu_path.read_bytes()
    second_stage = _stage_with_icon(store, b"second-icon")

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
    assert (
        f"Icon={_installed_icon_path(first.activated.payload.target_path)}\n"
        in original_menu.decode()
    )


def test_prepublication_failure_restores_a_legacy_menu_without_icon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    first_stage = _stage_with_icon(store, b"first-icon")
    original_icon_path = activation_module._payload_icon_path
    monkeypatch.setattr(activation_module, "_payload_icon_path", lambda _payload: None)
    with store.transaction() as transaction:
        first = activate_staged_artifact(transaction, first_stage, environment)
    legacy_menu = store.paths.menu_path.read_bytes()
    assert legacy_menu == render_desktop_entry(environment.console_path)
    monkeypatch.setattr(activation_module, "_payload_icon_path", original_icon_path)

    second_stage = _stage_with_icon(store, b"second-icon")
    original_write = activation_module._write_private_document

    def fail_state(path: Path, document: bytes) -> None:
        if path == store.paths.state_path:
            raise InstallerError(
                activation_module.InstallerErrorCode.STATE_CONFLICT,
                "injected state failure",
            )
        original_write(path, document)

    monkeypatch.setattr(activation_module, "_write_private_document", fail_state)
    with store.transaction() as transaction, pytest.raises(InstallerError):
        activate_staged_artifact(transaction, second_stage, environment)
    monkeypatch.undo()

    with store.transaction() as transaction:
        persisted = transaction.read_state()
    assert persisted == first.state
    assert persisted is not None
    assert hashlib.sha256(legacy_menu).hexdigest() == persisted.menu_sha256
    assert store.paths.menu_path.read_bytes() == legacy_menu


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
    staged = _stage_with_icon(store)
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


def test_uninstall_removes_active_and_previous_owned_payloads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    installed = []
    for _ in range(2):
        staged = _stage(store)
        with store.transaction() as transaction:
            installed.append(activate_staged_artifact(transaction, staged, environment))

    with store.transaction() as transaction:
        removed = uninstall_user_activation(transaction)

    assert removed is not None
    assert removed.recovery is RecoveryStatus.UNINSTALLED
    assert removed.active is None and removed.previous is None
    assert removed.cleanup == ()
    assert not installed[0].activated.payload.target_path.exists()
    assert not installed[1].activated.payload.target_path.exists()


@pytest.mark.parametrize("failed_index", [0, 1])
def test_uninstall_cleanup_retry_retains_every_remaining_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_index: int
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    installed = []
    for _ in range(2):
        staged = _stage(store)
        with store.transaction() as transaction:
            installed.append(activate_staged_artifact(transaction, staged, environment))
    failed_path = installed[1 - failed_index].activated.payload.target_path
    original_remove = activation_module._remove_owned_payload
    failed = False

    def fail_selected_cleanup(transaction, payload) -> None:
        nonlocal failed
        if payload.target_path == failed_path and not failed:
            failed = True
            raise InstallerError(
                activation_module.InstallerErrorCode.STATE_CONFLICT,
                "injected owned cleanup failure",
                retryable=True,
            )
        original_remove(transaction, payload)

    monkeypatch.setattr(
        activation_module, "_remove_owned_payload", fail_selected_cleanup
    )
    with store.transaction() as transaction, pytest.raises(InstallerError):
        uninstall_user_activation(transaction)
    monkeypatch.undo()

    with store.transaction() as transaction:
        interrupted = transaction.read_state()
        assert interrupted is not None
        assert interrupted.recovery is RecoveryStatus.CLEANUP_REQUIRED
        remaining = {target.payload.target_path for target in interrupted.cleanup}
        assert failed_path in remaining
        completed = uninstall_user_activation(transaction)

    assert completed is not None
    assert completed.recovery is RecoveryStatus.UNINSTALLED
    assert completed.cleanup == ()
    assert not any(item.activated.payload.target_path.exists() for item in installed)


def test_uninstall_retries_after_delete_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    staged = _stage(store)
    with store.transaction() as transaction:
        installed = activate_staged_artifact(transaction, staged, environment)
    original_remove = activation_module._remove_owned_payload
    reported = False

    def delete_then_report(transaction, payload) -> None:
        nonlocal reported
        original_remove(transaction, payload)
        if not reported:
            reported = True
            raise InstallerError(
                activation_module.InstallerErrorCode.STATE_CONFLICT,
                "injected post-delete fsync uncertainty",
                retryable=True,
            )

    monkeypatch.setattr(activation_module, "_remove_owned_payload", delete_then_report)
    with store.transaction() as transaction, pytest.raises(InstallerError):
        uninstall_user_activation(transaction)
    monkeypatch.undo()

    assert not installed.activated.payload.target_path.exists()
    with store.transaction() as transaction:
        interrupted = transaction.read_state()
        assert interrupted is not None
        assert interrupted.cleanup == (installed.activated,)
        completed = uninstall_user_activation(transaction)

    assert completed is not None
    assert completed.recovery is RecoveryStatus.UNINSTALLED
    assert completed.cleanup == ()


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
        assert interrupted.previous is None
        assert installed.activated in interrupted.cleanup
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
    staged = _stage_with_icon(store)
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
    assert repaired.active is not None
    assert (
        f"Icon={_installed_icon_path(repaired.active.payload.target_path)}\n"
        in store.paths.menu_path.read_text()
    )


def test_repair_rejects_a_mutated_declared_icon(tmp_path: Path) -> None:
    store = _store(tmp_path)
    environment = _environment(tmp_path)
    staged = _stage_with_icon(store)
    with store.transaction() as transaction:
        installed = activate_staged_artifact(transaction, staged, environment)
    _installed_icon_path(installed.activated.payload.target_path).write_bytes(
        b"mutated-icon"
    )

    with (
        store.transaction() as transaction,
        pytest.raises(InstallerError, match="incomplete or changed"),
    ):
        repair_activation(transaction, environment)


def test_repair_rebinds_recovered_journal_to_invoking_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    first_environment = _environment(tmp_path, "first")
    second_environment = _environment(tmp_path, "second")
    staged = _stage_with_icon(store)

    def fail_menu(*_args: object, **_kwargs: object) -> str:
        raise InstallerError(
            activation_module.InstallerErrorCode.STATE_CONFLICT,
            "injected menu failure",
        )

    monkeypatch.setattr(activation_module, "install_user_menu", fail_menu)
    with store.transaction() as transaction, pytest.raises(InstallerError):
        activate_staged_artifact(transaction, staged, first_environment)
    monkeypatch.undo()

    with store.transaction() as transaction:
        repaired = repair_activation(transaction, second_environment)

    assert repaired.active is not None
    assert repaired.active.environment == second_environment
    assert (
        second_environment.console_path.as_posix() in store.paths.menu_path.read_text()
    )
    assert (
        f"Icon={_installed_icon_path(repaired.active.payload.target_path)}\n"
        in store.paths.menu_path.read_text()
    )
    assert not store.paths.journal_path.exists()


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
