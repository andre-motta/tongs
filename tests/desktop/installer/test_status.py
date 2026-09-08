"""Desktop installation status tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import tongs.desktop.installer.activation as activation_module
import tongs.desktop.installer.status as status_module
from tongs.desktop.installer.activation import (
    BoundPythonEnvironment,
    DesktopInstallationPaths,
    DesktopInstallationStore,
    EnvironmentKind,
    RecoveryStatus,
    activate_staged_artifact,
    uninstall_user_activation,
)
from tongs.desktop.installer.extract import extract_verified_archive
from tongs.desktop.installer.models import InstallerError, InstallerErrorCode
from tongs.desktop.installer.status import inspect_desktop_installation

from .helpers import documents, verified_metadata


def _installed(tmp_path: Path) -> DesktopInstallationStore:
    store = DesktopInstallationStore(DesktopInstallationPaths.under(tmp_path / "data"))
    with store.transaction():
        pass
    staged = extract_verified_archive(
        documents()[1], verified_metadata(), store.paths.staging_root
    )
    interpreter = tmp_path / "venv/bin/python"
    console = tmp_path / "venv/bin/tongs"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    console.write_text(f"#!{interpreter}\n")
    interpreter.chmod(0o755)
    console.chmod(0o755)
    environment = BoundPythonEnvironment(
        EnvironmentKind.VENV,
        console,
        interpreter,
        console.resolve(),
        interpreter.resolve(),
        "1.2.3",
    )
    with store.transaction() as transaction:
        activate_staged_artifact(transaction, staged, environment)
    return store


def test_status_reports_ready_user_and_separate_rpm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _installed(tmp_path)
    rpm_launcher = tmp_path / "usr/bin/tongs-desktop"
    rpm_launcher.parent.mkdir(parents=True)
    rpm_launcher.write_text("rpm")
    monkeypatch.setattr(status_module, "validate_bound_launch", lambda _target: None)

    result = inspect_desktop_installation(
        store,
        rpm_launcher=rpm_launcher,
        rpm_menu=tmp_path / "usr/share/applications/tongs.desktop",
    )

    assert result.installed is True
    assert result.ownership == "user"
    assert result.launch_ready is True
    assert result.rpm_detected is True
    assert result.coexistence is True
    assert result.recovery is RecoveryStatus.HEALTHY


def test_status_detects_missing_owned_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _installed(tmp_path)
    store.paths.menu_path.unlink()
    monkeypatch.setattr(status_module, "validate_bound_launch", lambda _target: None)

    result = inspect_desktop_installation(
        store, rpm_launcher=tmp_path / "missing", rpm_menu=tmp_path / "also-missing"
    )

    assert result.installed is True
    assert result.menu_registered is False
    assert result.recovery is RecoveryStatus.MENU_REPAIR_REQUIRED
    assert "repair" in result.detail.lower()


def test_status_does_not_select_rpm_as_user_install(tmp_path: Path) -> None:
    store = DesktopInstallationStore(DesktopInstallationPaths.under(tmp_path / "data"))
    rpm = tmp_path / "usr/bin/tongs-desktop"
    rpm.parent.mkdir(parents=True)
    rpm.write_text("rpm")

    result = inspect_desktop_installation(
        store, rpm_launcher=rpm, rpm_menu=tmp_path / "missing"
    )

    assert result.installed is False
    assert result.rpm_detected is True
    assert result.coexistence is False
    assert result.ownership is None


def test_status_preserves_retryable_cleanup_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _installed(tmp_path)
    monkeypatch.setattr(
        activation_module,
        "remove_user_menu",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            InstallerError(
                InstallerErrorCode.STATE_CONFLICT,
                "injected cleanup failure",
                retryable=True,
            )
        ),
    )
    with store.transaction() as transaction, pytest.raises(InstallerError):
        uninstall_user_activation(transaction)

    result = inspect_desktop_installation(
        store, rpm_launcher=tmp_path / "missing", rpm_menu=tmp_path / "also-missing"
    )

    assert result.installed is False
    assert result.recovery is RecoveryStatus.CLEANUP_REQUIRED
    assert "uninstall" in result.detail


def test_active_status_explains_pending_obsolete_payload_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _installed(tmp_path)
    with store.transaction() as transaction:
        first = transaction.read_state()
    assert first is not None and first.active is not None
    environment = first.active.environment
    for index in range(2):
        staged = extract_verified_archive(
            documents()[1], verified_metadata(), store.paths.staging_root
        )
        if index == 1:
            monkeypatch.setattr(
                activation_module,
                "_remove_owned_payload",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    InstallerError(
                        InstallerErrorCode.STATE_CONFLICT,
                        "injected obsolete cleanup failure",
                        retryable=True,
                    )
                ),
            )
        with store.transaction() as transaction:
            if index == 1:
                with pytest.raises(InstallerError):
                    activate_staged_artifact(transaction, staged, environment)
            else:
                activate_staged_artifact(transaction, staged, environment)
    monkeypatch.setattr(status_module, "validate_bound_launch", lambda _target: None)

    result = inspect_desktop_installation(
        store, rpm_launcher=tmp_path / "missing", rpm_menu=tmp_path / "also-missing"
    )

    assert result.installed is True
    assert result.launch_ready is True
    assert result.recovery is RecoveryStatus.CLEANUP_REQUIRED
    assert "old payload cleanup is incomplete" in result.detail
    assert "tongs desktop repair" in result.detail
