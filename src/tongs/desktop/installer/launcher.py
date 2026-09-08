"""Exact-environment validation and desktop process launch."""

from __future__ import annotations

import importlib.metadata
import os
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from tongs.desktop.installer.activation import (
    BoundPythonEnvironment,
    EnvironmentKind,
    InstallationTarget,
    validate_installed_payload,
)
from tongs.desktop.installer.models import InstallerError, InstallerErrorCode
from tongs.desktop.protocol import PROTOCOL_MAJOR
from tongs.plugins.desktop import DESKTOP_PLUGIN_API_MAJOR

_PROBE_TIMEOUT_SECONDS = 5.0
_MAX_PROBE_BYTES = 4096
_VERSION_PROBE = (
    "import importlib.metadata,sys;"
    "print(importlib.metadata.version('tongs'));"
    "print(sys.executable)"
)


@dataclass(frozen=True, slots=True)
class DesktopLaunch:
    executable: Path
    arguments: tuple[str, ...]
    core_version: str


def locate_console_script(argv0: str) -> Path:
    """Capture the exact console script used for this explicit lifecycle call."""
    if not isinstance(argv0, str) or not argv0:
        raise InstallerError(
            InstallerErrorCode.INCOMPATIBLE,
            "The invoking Tongs console script could not be identified.",
        )
    candidate = Path(argv0)
    if not candidate.is_absolute():
        located = shutil.which(argv0)
        if located is None:
            raise InstallerError(
                InstallerErrorCode.INCOMPATIBLE,
                "The invoking Tongs console script could not be identified.",
            )
        candidate = Path(located)
    return Path(os.path.abspath(candidate))


def classify_current_environment(
    console_path: Path,
    *,
    interpreter_path: Path | None = None,
    prefix: Path | None = None,
    base_prefix: Path | None = None,
    environ: Mapping[str, str] | None = None,
    core_version: str | None = None,
) -> BoundPythonEnvironment:
    """Classify the current console without resolving away its invocation path."""
    environment = os.environ if environ is None else environ
    interpreter = Path(os.path.abspath(interpreter_path or Path(sys.executable)))
    console = Path(os.path.abspath(console_path))
    current_prefix = Path(os.path.abspath(prefix or Path(sys.prefix)))
    current_base = Path(os.path.abspath(base_prefix or Path(sys.base_prefix)))
    console_real = _executable_real_path(console, "Tongs console script")
    interpreter_real = _executable_real_path(interpreter, "Python interpreter")
    shebang = _console_interpreter(console_real)
    if shebang != interpreter:
        kind = EnvironmentKind.UNSUPPORTED
    elif _is_transient_uv(interpreter, current_prefix, environment):
        kind = EnvironmentKind.TRANSIENT_UVX
    elif (
        current_prefix != current_base
        and _within(console_real, current_prefix / "bin")
        and _within(interpreter, current_prefix / "bin")
    ):
        kind = (
            EnvironmentKind.PIPX
            if console != console_real or "pipx" in current_prefix.parts
            else EnvironmentKind.VENV
        )
    elif current_prefix == current_base:
        script_roots = _persistent_script_roots()
        if console.parent in script_roots or console_real.parent in script_roots:
            kind = (
                EnvironmentKind.USER_SITE
                if _user_script_root() in {console.parent, console_real.parent}
                else EnvironmentKind.SYSTEM
            )
        else:
            kind = EnvironmentKind.UNSUPPORTED
    else:
        kind = EnvironmentKind.UNSUPPORTED
    version = core_version or importlib.metadata.version("tongs")
    return BoundPythonEnvironment(
        kind,
        console,
        interpreter,
        console_real,
        interpreter_real,
        version,
    )


def validate_bound_launch(
    target: InstallationTarget,
    *,
    timeout: float = _PROBE_TIMEOUT_SECONDS,
) -> DesktopLaunch:
    """Reprobe the exact bound environment and return fixed S10 launch arguments."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    environment = target.environment
    validate_environment_binding(environment)
    payload = target.payload
    validate_installed_payload(payload)
    launcher_real = _executable_real_path(payload.launcher_path, "desktop launcher")
    try:
        launcher_real.relative_to(payload.target_path.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise _repair_error("The installed desktop launcher is unsafe.") from error
    try:
        completed = subprocess.run(
            [
                os.fspath(environment.interpreter_path),
                "-E",
                "-P",
                "-c",
                _VERSION_PROBE,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=_probe_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _repair_error(
            "The bound Tongs Python environment could not be validated."
        ) from error
    if (
        completed.returncode != 0
        or len(completed.stdout) > _MAX_PROBE_BYTES
        or len(completed.stderr) > _MAX_PROBE_BYTES
    ):
        raise _repair_error(
            "The bound Tongs Python environment could not be validated."
        )
    try:
        lines = completed.stdout.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as error:
        raise _repair_error(
            "The bound Tongs Python environment returned invalid data."
        ) from error
    if len(lines) != 2:
        raise _repair_error("The bound Tongs Python environment returned invalid data.")
    core_version = lines[0]
    reported_interpreter = Path(lines[1])
    if (
        not reported_interpreter.is_absolute()
        or Path(os.path.abspath(reported_interpreter)) != environment.interpreter_path
    ):
        raise _repair_error("The bound Python interpreter identity is invalid.")
    compatibility = payload.compatibility
    try:
        compatible = (
            Version(compatibility.core_minimum)
            <= Version(core_version)
            < Version(compatibility.core_maximum_exclusive)
        )
    except InvalidVersion:
        compatible = False
    if (
        not compatible
        or compatibility.rpc_api_major != PROTOCOL_MAJOR
        or compatibility.plugin_api_major != DESKTOP_PLUGIN_API_MAJOR
    ):
        raise _repair_error(
            "The bound Tongs core is incompatible with this desktop payload."
        )
    arguments = (
        os.fspath(payload.launcher_path),
        "--tongs-python-executable",
        os.fspath(environment.interpreter_path),
        "--tongs-core-version",
        core_version,
        "--tongs-safe-cwd",
        os.fspath(payload.target_path),
    )
    return DesktopLaunch(payload.launcher_path, arguments, core_version)


def validate_environment_binding(environment: BoundPythonEnvironment) -> None:
    """Validate an exact persistent console/interpreter pair without PATH lookup."""
    if not environment.kind.persistent:
        raise _repair_error(
            "The desktop installation is not bound to a persistent Python environment."
        )
    console_real = _executable_real_path(
        environment.console_path, "Tongs console script"
    )
    _executable_real_path(environment.interpreter_path, "Python interpreter")
    if _console_interpreter(console_real) != environment.interpreter_path:
        raise _repair_error(
            "The bound Tongs Python environment changed or disappeared."
        )


def launch_desktop(
    target: InstallationTarget,
    *,
    extra_arguments: Sequence[str] = (),
) -> None:
    """Replace the CLI process with the exact validated desktop launcher."""
    if any(not isinstance(argument, str) for argument in extra_arguments):
        raise TypeError("desktop launch arguments must be strings")
    launch = validate_bound_launch(target)
    os.execv(
        launch.executable,
        [*launch.arguments, *extra_arguments],
    )


def _console_interpreter(console_real: Path) -> Path:
    try:
        with console_real.open("rb") as source:
            first = source.readline(4097)
            second = source.readline(4097)
    except OSError as error:
        raise _repair_error("The bound Tongs console script is unreadable.") from error
    if len(first) > 4096 or not first.startswith(b"#!"):
        raise _repair_error(
            "The bound Tongs console script has an invalid interpreter."
        )
    try:
        declaration = first[2:].strip().decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise _repair_error(
            "The bound Tongs console script has an invalid interpreter."
        ) from error
    if declaration == "/bin/sh":
        return _shell_shebang_interpreter(second)
    if not declaration or any(char.isspace() for char in declaration):
        raise _repair_error(
            "The bound Tongs console script must name one exact interpreter."
        )
    result = Path(declaration)
    if not result.is_absolute():
        raise _repair_error(
            "The bound Tongs console script must name one exact interpreter."
        )
    return Path(os.path.abspath(result))


def _shell_shebang_interpreter(line: bytes) -> Path:
    """Parse the fixed distlib shell trampoline used when an env path has spaces."""
    try:
        declaration = line.decode("utf-8", errors="strict").rstrip("\n")
    except UnicodeDecodeError as error:
        raise _repair_error(
            "The bound Tongs console script has an invalid interpreter."
        ) from error
    prefix = "'''exec' "
    suffix = ' "$0" "$@"'
    if not declaration.startswith(prefix) or not declaration.endswith(suffix):
        raise _repair_error(
            "The bound Tongs console script must name one exact interpreter."
        )
    try:
        values = shlex.split(declaration[len(prefix) : -len(suffix)], posix=True)
    except ValueError as error:
        raise _repair_error(
            "The bound Tongs console script has an invalid interpreter."
        ) from error
    if len(values) != 1:
        raise _repair_error(
            "The bound Tongs console script must name one exact interpreter."
        )
    result = Path(values[0])
    if not result.is_absolute():
        raise _repair_error(
            "The bound Tongs console script must name one exact interpreter."
        )
    return Path(os.path.abspath(result))


def _executable_real_path(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise _repair_error(f"The bound {label} path is not absolute.")
    try:
        details = path.stat()
        real = path.resolve(strict=True)
    except OSError as error:
        raise _repair_error(f"The bound {label} is missing.") from error
    if not stat.S_ISREG(details.st_mode) or not os.access(path, os.X_OK):
        raise _repair_error(f"The bound {label} is not executable.")
    return real


def _is_transient_uv(
    interpreter: Path, prefix: Path, environ: Mapping[str, str]
) -> bool:
    if environ.get("UV_RUN_RECURSION_DEPTH"):
        return True
    roots: list[Path] = []
    configured = environ.get("UV_CACHE_DIR")
    if configured and Path(configured).is_absolute():
        roots.append(Path(configured))
    xdg = environ.get("XDG_CACHE_HOME")
    if xdg and Path(xdg).is_absolute():
        roots.append(Path(xdg) / "uv")
    roots.append(Path.home() / ".cache" / "uv")
    return any(_within(interpreter, root) or _within(prefix, root) for root in roots)


def _persistent_script_roots() -> set[Path]:
    roots = {Path(sysconfig.get_path("scripts")).absolute(), _user_script_root()}
    return roots


def _user_script_root() -> Path:
    return Path(sysconfig.get_path("scripts", scheme="posix_user")).absolute()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _probe_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    return environment


def _repair_error(message: str) -> InstallerError:
    return InstallerError(
        InstallerErrorCode.INCOMPATIBLE,
        f"{message} Run 'tongs desktop repair' from a persistent installation.",
    )


__all__ = [
    "DesktopLaunch",
    "classify_current_environment",
    "launch_desktop",
    "locate_console_script",
    "validate_bound_launch",
    "validate_environment_binding",
]
