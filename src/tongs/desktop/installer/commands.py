"""Explicit desktop lifecycle command orchestration."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import platform
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TextIO

from tongs.desktop.artifact_contract import DesktopPlatform
from tongs.desktop.installer.activation import (
    ActivationResult,
    BoundPythonEnvironment,
    DesktopInstallationPaths,
    DesktopInstallationStore,
    DesktopInstallationTransaction,
    InstallationTarget,
    activate_staged_artifact,
    discard_interrupted_activation,
    repair_activation,
    require_activation_environment,
    uninstall_user_activation,
)
from tongs.desktop.installer.launcher import (
    classify_current_environment,
    launch_desktop,
    locate_console_script,
)
from tongs.desktop.installer.models import (
    InstallerError,
    InstallerErrorCode,
    InstallerLimits,
    InstallRequest,
    VerifiedStagedArtifact,
)
from tongs.desktop.installer.status import (
    DesktopInstallationStatus,
    inspect_desktop_installation,
)


class StageRelease(Protocol):
    async def __call__(
        self,
        transaction: DesktopInstallationTransaction,
        request: InstallRequest,
    ) -> VerifiedStagedArtifact: ...


@dataclass(frozen=True, slots=True)
class DesktopCommandContext:
    store: DesktopInstallationStore
    environment: BoundPythonEnvironment | None
    stage_release: StageRelease
    launch: Callable[[InstallationTarget], None] = launch_desktop


def default_desktop_paths(
    environ: Mapping[str, str] | None = None,
) -> DesktopInstallationPaths:
    """Build absolute XDG paths while ignoring invalid relative overrides."""
    values = os.environ if environ is None else environ
    configured = values.get("XDG_DATA_HOME", "")
    if configured and Path(configured).is_absolute():
        data_home = Path(configured)
    else:
        data_home = Path.home() / ".local" / "share"
    return DesktopInstallationPaths.under(data_home)


def build_desktop_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tongs desktop")
    actions = parser.add_subparsers(dest="action")
    install = actions.add_parser("install", help="install a verified desktop release")
    install.add_argument("--version")
    install.add_argument("--allow-downgrade", action="store_true")
    actions.add_parser("update", help="install the newest verified desktop release")
    repair = actions.add_parser("repair", help="repair activation and menu state")
    repair.add_argument(
        "--redownload",
        action="store_true",
        help="explicitly download a verified replacement when local recovery fails",
    )
    status = actions.add_parser("status", help="show installation health")
    status.add_argument("--json", action="store_true", dest="as_json")
    actions.add_parser("uninstall", help="remove only the per-user activation")
    return parser


def run_desktop_cli(
    argv: Sequence[str],
    *,
    console_argv0: str,
    context: DesktopCommandContext | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Parse and run one explicit desktop command."""
    arguments = build_desktop_parser().parse_args(list(argv))
    try:
        actual = context or _default_context(
            console_argv0,
            bind_environment=arguments.action in {"install", "update", "repair"},
        )
        return asyncio.run(_run(arguments, actual, stdout))
    except InstallerError as error:
        print(error.message, file=stderr)
        return 2


async def _run(
    arguments: argparse.Namespace,
    context: DesktopCommandContext,
    stdout: TextIO,
) -> int:
    action = arguments.action
    if action is None:
        with context.store.transaction() as transaction:
            state = transaction.read_state()
            if state is None or state.active is None:
                raise InstallerError(
                    code=InstallerErrorCode.STATE_CONFLICT,
                    message="No per-user desktop installation is active. Run 'tongs desktop install'.",
                )
        context.launch(state.active)
        return 0
    if action in {"install", "update"}:
        if (
            action == "install"
            and arguments.allow_downgrade
            and arguments.version is None
        ):
            raise InstallerError(
                InstallerErrorCode.INVALID_METADATA,
                "--allow-downgrade requires an explicit --version.",
            )
        request = (
            InstallRequest(arguments.version, arguments.allow_downgrade)
            if action == "install"
            else InstallRequest()
        )
        with context.store.transaction() as transaction:
            if context.environment is None:
                raise AssertionError("install requires a bound environment")
            require_activation_environment(transaction, context.environment)
            staged = await context.stage_release(transaction, request)
            result = _activate(transaction, staged, context.environment)
        print(
            f"Activated Tongs desktop {result.activated.payload.version} for this user.",
            file=stdout,
        )
        return 0
    if action == "repair":
        if context.environment is None:
            raise AssertionError("repair requires a bound environment")
        try:
            with context.store.transaction() as transaction:
                state = repair_activation(transaction, context.environment)
        except InstallerError:
            if not arguments.redownload:
                raise
            print(
                "Local recovery failed; downloading a verified replacement.",
                file=stdout,
            )
            with context.store.transaction() as transaction:
                require_activation_environment(transaction, context.environment)
                discard_interrupted_activation(transaction)
                staged = await context.stage_release(transaction, InstallRequest())
                state = _activate(transaction, staged, context.environment).state
        assert state.active is not None
        print(f"Repaired Tongs desktop {state.active.payload.version}.", file=stdout)
        return 0
    if action == "status":
        status = inspect_desktop_installation(context.store)
        if arguments.as_json:
            print(json.dumps(_status_json(status), sort_keys=True), file=stdout)
        else:
            print(status.detail, file=stdout)
            if status.coexistence:
                print(
                    "A separate RPM installation is also present; this command selected the per-user installation.",
                    file=stdout,
                )
        return 0 if status.launch_ready or not status.installed else 1
    if action == "uninstall":
        with context.store.transaction() as transaction:
            state = uninstall_user_activation(transaction)
        if state is None:
            print("No per-user desktop installation is active.", file=stdout)
        else:
            print("Removed the per-user Tongs desktop activation.", file=stdout)
        return 0
    raise AssertionError("unhandled desktop lifecycle command")


def _default_context(
    console_argv0: str, *, bind_environment: bool
) -> DesktopCommandContext:
    paths = default_desktop_paths()
    environment = None
    if bind_environment:
        console = locate_console_script(console_argv0)
        environment = classify_current_environment(console)
    return DesktopCommandContext(
        DesktopInstallationStore(paths),
        environment,
        _ProductionStage(paths),
    )


class _ProductionStage:
    """Lazy production verifier/download assembly for explicit install commands."""

    def __init__(self, paths: DesktopInstallationPaths) -> None:
        self._paths = paths

    async def __call__(
        self,
        transaction: DesktopInstallationTransaction,
        request: InstallRequest,
    ) -> VerifiedStagedArtifact:
        import httpx
        from sigstore.verify import Verifier

        from tongs.desktop.installer.extract import stage_desktop_release
        from tongs.desktop.protocol import PROTOCOL_MAJOR
        from tongs.plugins.desktop import DESKTOP_PLUGIN_API_MAJOR

        verifier = Verifier.production()
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await stage_desktop_release(
                client,
                verifier,
                transaction.release_state,
                _platform_probe,
                self._paths.staging_root,
                core_version=importlib.metadata.version("tongs"),
                rpc_api_major=PROTOCOL_MAJOR,
                plugin_api_major=DESKTOP_PLUGIN_API_MAJOR,
                request=request,
                limits=InstallerLimits(),
                clock=lambda: datetime.now(UTC),
            )


def _activate(
    transaction: DesktopInstallationTransaction,
    staged: VerifiedStagedArtifact,
    environment: BoundPythonEnvironment,
) -> ActivationResult:
    try:
        return activate_staged_artifact(transaction, staged, environment)
    except BaseException:
        from tongs.desktop.installer.extract import discard_staged_artifact

        try:
            discard_staged_artifact(staged)
        except InstallerError:
            pass
        raise


def _platform_probe() -> DesktopPlatform:
    from tongs.desktop.installer.metadata import normalize_platform_identity

    release = _read_os_release()
    return normalize_platform_identity(
        "linux",
        platform.machine().lower(),
        release.get("ID", ""),
        release.get("VERSION_ID", "").strip('"'),
        "gnu",
    )


def _read_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"ID", "VERSION_ID"}:
            result[key] = value.strip().strip('"')
    return result


def _status_json(status: DesktopInstallationStatus) -> dict[str, object]:
    value = asdict(status)
    for key in ("active_target", "previous_target"):
        if value[key] is not None:
            value[key] = os.fspath(value[key])
    recovery = value["recovery"]
    value["recovery"] = recovery.value
    return value


__all__ = [
    "DesktopCommandContext",
    "StageRelease",
    "build_desktop_parser",
    "default_desktop_paths",
    "run_desktop_cli",
]
