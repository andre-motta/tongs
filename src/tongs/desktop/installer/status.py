"""Read-only desktop installation health and ownership reporting."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tongs.desktop.installer.activation import (
    DesktopInstallationState,
    DesktopInstallationStore,
    RecoveryStatus,
)
from tongs.desktop.installer.launcher import validate_bound_launch
from tongs.desktop.installer.menu import desktop_entry_digest
from tongs.desktop.installer.models import InstallerError


@dataclass(frozen=True, slots=True)
class DesktopInstallationStatus:
    """User-visible immutable status for user and RPM installations."""

    installed: bool
    version: str | None
    active_target: Path | None
    previous_target: Path | None
    ownership: str | None
    environment: str | None
    recovery: RecoveryStatus
    menu_registered: bool
    launch_ready: bool
    rpm_detected: bool
    coexistence: bool
    detail: str


def inspect_desktop_installation(
    store: DesktopInstallationStore,
    *,
    rpm_launcher: Path = Path("/usr/bin/tongs-desktop"),
    rpm_menu: Path = Path("/usr/share/applications/tongs.desktop"),
) -> DesktopInstallationStatus:
    """Inspect consistent user state without modifying menu or payload content."""
    with store.transaction() as transaction:
        state = transaction.read_state()
        journal = transaction.read_journal()
        rpm_detected = rpm_launcher.is_file() or rpm_menu.is_file()
        if state is None or state.active is None:
            recovery = (
                RecoveryStatus.ACTIVATION_PENDING
                if journal is not None
                else (
                    state.recovery if state is not None else RecoveryStatus.UNINSTALLED
                )
            )
            return DesktopInstallationStatus(
                False,
                None,
                None,
                state.previous.payload.target_path
                if state is not None and state.previous is not None
                else None,
                None,
                None,
                recovery,
                False,
                False,
                rpm_detected,
                False,
                (
                    "A verified activation is recoverable with 'tongs desktop repair'."
                    if journal is not None
                    else (
                        "Per-user desktop cleanup is incomplete; run 'tongs desktop uninstall' again."
                        if recovery is RecoveryStatus.CLEANUP_REQUIRED
                        else "No per-user desktop installation is active."
                    )
                ),
            )
        return _active_status(store, state, journal is not None, rpm_detected)


def _active_status(
    store: DesktopInstallationStore,
    state: DesktopInstallationState,
    journal_present: bool,
    rpm_detected: bool,
) -> DesktopInstallationStatus:
    active = state.active
    assert active is not None
    menu_registered = desktop_entry_digest(store.paths.menu_path) == state.menu_sha256
    launch_ready = False
    detail = (
        "The per-user desktop installation can launch, but old payload cleanup "
        "is incomplete; run 'tongs desktop repair'."
        if state.recovery is RecoveryStatus.CLEANUP_REQUIRED
        else "The per-user desktop installation is ready."
    )
    recovery = state.recovery
    try:
        validate_bound_launch(active)
        launch_ready = True
    except InstallerError as error:
        detail = error.message
        recovery = RecoveryStatus.PAYLOAD_REPAIR_REQUIRED
    if not menu_registered:
        recovery = RecoveryStatus.MENU_REPAIR_REQUIRED
        detail = "The per-user desktop menu entry needs repair."
    if journal_present:
        recovery = RecoveryStatus.ACTIVATION_PENDING
        detail = "An interrupted verified activation can be recovered."
    return DesktopInstallationStatus(
        True,
        active.payload.version,
        active.payload.target_path,
        state.previous.payload.target_path if state.previous is not None else None,
        active.payload.ownership,
        active.environment.kind.value,
        recovery,
        menu_registered,
        launch_ready,
        rpm_detected,
        rpm_detected,
        detail,
    )


__all__ = ["DesktopInstallationStatus", "inspect_desktop_installation"]
