"""Exact Python environment binding and launch tests."""

from __future__ import annotations

import errno
import hashlib
import importlib.metadata
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import mock_open

import pytest

import tongs.desktop.installer.launcher as launcher_module
from tongs.desktop.artifact_contract import DesktopCompatibility, InstallFile
from tongs.desktop.installer.activation import (
    BoundPythonEnvironment,
    EnvironmentKind,
    InstallationTarget,
    InstalledPayload,
)
from tongs.desktop.installer.launcher import (
    classify_current_environment,
    launch_desktop,
    validate_bound_launch,
    xwayland_launch_arguments,
)
from tongs.desktop.installer.models import InstallerError


def _executable(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)
    return path


def _payload(target_root: Path, launcher: Path) -> InstalledPayload:
    target_root.chmod(0o700)
    content = launcher.read_bytes()
    return InstalledPayload(
        "1.2.3",
        "a" * 64,
        "b" * 40,
        "c" * 64,
        1,
        target_root,
        launcher,
        DesktopCompatibility("0", "999", 1, 1),
        (
            InstallFile(
                launcher.relative_to(target_root).as_posix(),
                len(content),
                hashlib.sha256(content).hexdigest(),
                True,
            ),
        ),
        tuple(
            parent.as_posix()
            for parent in reversed(launcher.relative_to(target_root).parents[:-1])
        ),
    )


def _launchable_target(tmp_path: Path) -> InstallationTarget:
    target_root = tmp_path / "payload"
    launcher = _executable(target_root / "runtime/tongs-desktop", "#!/bin/sh\n")
    interpreter = Path(sys.executable)
    console = _executable(
        tmp_path / "bin/tongs", f"#!{interpreter}\nraise SystemExit\n"
    )
    environment = BoundPythonEnvironment(
        EnvironmentKind.SYSTEM,
        console,
        interpreter,
        console.resolve(),
        interpreter.resolve(),
        "0.0.1",
    )
    return InstallationTarget(_payload(target_root, launcher), environment)


def _process_is_running(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    state = stat[stat.rfind(")") + 2 :].split(maxsplit=1)[0]
    return state not in {"X", "Z"}


def test_process_state_read_handles_disappearance_after_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc_stat = mock_open()
    proc_stat.return_value.read.side_effect = ProcessLookupError(
        errno.ESRCH, "No such process"
    )
    monkeypatch.setattr(Path, "open", proc_stat)

    assert not _process_is_running(12345)


def test_process_state_read_does_not_hide_unrelated_io_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc_stat = mock_open()
    proc_stat.return_value.read.side_effect = PermissionError(
        errno.EACCES, "Permission denied"
    )
    monkeypatch.setattr(Path, "open", proc_stat)

    with pytest.raises(PermissionError, match="Permission denied"):
        _process_is_running(12345)


def test_process_state_read_reports_live_process() -> None:
    assert _process_is_running(os.getpid())


def test_venv_binding_preserves_lexical_interpreter_symlink(tmp_path: Path) -> None:
    prefix = tmp_path / "persistent venv"
    interpreter = prefix / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    console = _executable(
        prefix / "bin" / "tongs",
        f"#!/bin/sh\n'''exec' \"{interpreter}\" \"$0\" \"$@\"\n' '''\n",
    )

    result = classify_current_environment(
        console,
        interpreter_path=interpreter,
        prefix=prefix,
        base_prefix=Path(sys.base_prefix),
        environ={},
        core_version="1.2.3",
    )

    assert result.kind is EnvironmentKind.VENV
    assert result.interpreter_path == interpreter
    assert result.interpreter_real_path == Path(sys.executable).resolve()


def test_pipx_symlink_and_uvx_transience_are_distinguished(tmp_path: Path) -> None:
    prefix = tmp_path / "pipx" / "venvs" / "tongs"
    interpreter = prefix / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    installed_console = _executable(
        prefix / "bin" / "tongs", f"#!{interpreter}\nraise SystemExit\n"
    )
    exposed = tmp_path / "bin" / "tongs"
    exposed.parent.mkdir()
    exposed.symlink_to(installed_console)

    pipx = classify_current_environment(
        exposed,
        interpreter_path=interpreter,
        prefix=prefix,
        base_prefix=Path(sys.base_prefix),
        environ={},
        core_version="1.2.3",
    )
    uvx = classify_current_environment(
        installed_console,
        interpreter_path=interpreter,
        prefix=prefix,
        base_prefix=Path(sys.base_prefix),
        environ={"UV_RUN_RECURSION_DEPTH": "1"},
        core_version="1.2.3",
    )

    assert pipx.kind is EnvironmentKind.PIPX
    assert uvx.kind is EnvironmentKind.TRANSIENT_UVX
    assert uvx.kind.persistent is False


def test_mismatched_console_shebang_is_unsupported(tmp_path: Path) -> None:
    interpreter = _executable(tmp_path / "env/bin/python", "#!/bin/sh\n")
    console = _executable(tmp_path / "env/bin/tongs", "#!/usr/bin/python3\n")

    result = classify_current_environment(
        console,
        interpreter_path=interpreter,
        prefix=tmp_path / "env",
        base_prefix=Path(sys.base_prefix),
        environ={},
        core_version="1.2.3",
    )

    assert result.kind is EnvironmentKind.UNSUPPORTED


def test_launch_reprobes_bound_core_and_preserves_invocation_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    target_root = tmp_path / "payload"
    launcher = _executable(target_root / "runtime/tongs-desktop", "#!/bin/sh\n")
    interpreter = Path(sys.executable)
    console = _executable(
        tmp_path / "bin/tongs", f"#!{interpreter}\nraise SystemExit\n"
    )
    environment = BoundPythonEnvironment(
        EnvironmentKind.SYSTEM,
        console,
        interpreter,
        tmp_path / "recorded-old-console-target",
        tmp_path / "recorded-old-python-target",
        "0.0.1",
    )
    payload = _payload(target_root, launcher)
    target = InstallationTarget(payload, environment)

    launch = validate_bound_launch(target)

    assert launch.executable == launcher
    assert launch.core_version == importlib.metadata.version("tongs")
    assert launch.arguments[2] == os.fspath(interpreter)
    observed: list[object] = []
    monkeypatch.setattr(
        os,
        "execv",
        lambda executable, arguments: observed.extend([executable, arguments]),
    )
    launch_desktop(target, extra_arguments=("--ozone-platform=x11",))
    assert observed[0] == launcher
    assert observed[1][-1] == "--ozone-platform=x11"  # type: ignore[index]


def test_xwayland_switch_is_selected_only_on_a_linux_wayland_session() -> None:
    assert xwayland_launch_arguments(
        platform="linux", environ={"WAYLAND_DISPLAY": "wayland-0"}
    ) == ("--ozone-platform=x11",)
    assert xwayland_launch_arguments(platform="linux", environ={"DISPLAY": ":0"}) == ()
    assert (
        xwayland_launch_arguments(platform="linux", environ={"WAYLAND_DISPLAY": ""})
        == ()
    )
    assert (
        xwayland_launch_arguments(
            platform="darwin", environ={"WAYLAND_DISPLAY": "wayland-0"}
        )
        == ()
    )
    assert (
        xwayland_launch_arguments(
            platform="win32", environ={"WAYLAND_DISPLAY": "wayland-0"}
        )
        == ()
    )


def test_launch_arguments_select_xwayland_on_a_wayland_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher_module.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    target = _launchable_target(tmp_path)

    launch = validate_bound_launch(target)

    assert launch.arguments[0] == os.fspath(launch.executable)
    assert launch.arguments[1] == "--ozone-platform=x11"
    assert launch.arguments.count("--ozone-platform=x11") == 1
    assert launch.arguments.index("--ozone-platform=x11") < launch.arguments.index(
        "--tongs-python-executable"
    )


def test_launch_arguments_omit_xwayland_without_a_wayland_display(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher_module.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    target = _launchable_target(tmp_path)

    launch = validate_bound_launch(target)

    assert "--ozone-platform=x11" not in launch.arguments
    assert launch.arguments[1] == "--tongs-python-executable"


def test_launch_arguments_omit_xwayland_off_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher_module.sys, "platform", "darwin")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    target = _launchable_target(tmp_path)

    assert "--ozone-platform=x11" not in validate_bound_launch(target).arguments


def test_launch_exec_places_xwayland_before_extra_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher_module.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    target = _launchable_target(tmp_path)
    observed: list[object] = []
    monkeypatch.setattr(
        os,
        "execv",
        lambda executable, arguments: observed.extend([executable, arguments]),
    )

    launch_desktop(target, extra_arguments=("--tongs-smoke-report", "/tmp/report.json"))

    argv = observed[1]
    assert isinstance(argv, list)
    assert argv[1] == "--ozone-platform=x11"
    assert argv[-2:] == ["--tongs-smoke-report", "/tmp/report.json"]
    assert argv.index("--ozone-platform=x11") < argv.index("--tongs-smoke-report")


def test_launch_tolerates_extra_arguments_repeating_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher_module.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    target = _launchable_target(tmp_path)
    observed: list[object] = []
    monkeypatch.setattr(
        os,
        "execv",
        lambda executable, arguments: observed.extend([executable, arguments]),
    )

    launch_desktop(target, extra_arguments=("--ozone-platform=x11",))

    argv = observed[1]
    assert isinstance(argv, list)
    # A caller that already selects XWayland repeats a switch Chromium keys by
    # name with the same value, so the launch stays valid instead of failing.
    assert argv.count("--ozone-platform=x11") == 2
    assert argv[1] == "--ozone-platform=x11"
    assert argv[-1] == "--ozone-platform=x11"


def test_launch_rejects_unrecorded_payload_content(tmp_path: Path) -> None:
    target_root = tmp_path / "payload"
    launcher = _executable(target_root / "runtime/tongs-desktop", "#!/bin/sh\n")
    interpreter = Path(sys.executable)
    console = _executable(tmp_path / "bin/tongs", f"#!{interpreter}\n")
    environment = BoundPythonEnvironment(
        EnvironmentKind.SYSTEM,
        console,
        interpreter,
        console.resolve(),
        interpreter.resolve(),
        "1",
    )
    payload = _payload(target_root, launcher)
    (target_root / "injected").write_text("not verified")

    with pytest.raises(InstallerError, match="incomplete or changed"):
        validate_bound_launch(InstallationTarget(payload, environment))


def test_launch_rejects_changed_bound_console(tmp_path: Path) -> None:
    target_root = tmp_path / "payload"
    launcher = _executable(target_root / "runtime/tongs-desktop", "#!/bin/sh\n")
    interpreter = Path(sys.executable)
    console = _executable(tmp_path / "bin/tongs", f"#!{interpreter}\n")
    environment = BoundPythonEnvironment(
        EnvironmentKind.SYSTEM,
        console,
        interpreter,
        console.resolve(),
        interpreter.resolve(),
        "1",
    )
    payload = _payload(target_root, launcher)
    console.unlink()

    with pytest.raises(InstallerError, match="repair"):
        validate_bound_launch(InstallationTarget(payload, environment))


def test_launch_rejects_missing_bound_interpreter(tmp_path: Path) -> None:
    target_root = tmp_path / "payload"
    launcher = _executable(target_root / "runtime/tongs-desktop", "#!/bin/sh\n")
    interpreter = _executable(tmp_path / "env/bin/python", "#!/bin/sh\n")
    console = _executable(tmp_path / "env/bin/tongs", f"#!{interpreter}\n")
    environment = BoundPythonEnvironment(
        EnvironmentKind.VENV,
        console,
        interpreter,
        console.resolve(),
        interpreter.resolve(),
        "1",
    )
    interpreter.unlink()

    with pytest.raises(InstallerError, match="repair"):
        validate_bound_launch(
            InstallationTarget(_payload(target_root, launcher), environment)
        )


def test_launch_rejects_incompatible_current_core(tmp_path: Path) -> None:
    target_root = tmp_path / "payload"
    launcher = _executable(target_root / "runtime/tongs-desktop", "#!/bin/sh\n")
    interpreter = Path(sys.executable)
    console = _executable(tmp_path / "bin/tongs", f"#!{interpreter}\n")
    environment = BoundPythonEnvironment(
        EnvironmentKind.SYSTEM,
        console,
        interpreter,
        console.resolve(),
        interpreter.resolve(),
        "1",
    )
    payload = replace(
        _payload(target_root, launcher),
        compatibility=DesktopCompatibility("999", "1000", 1, 1),
    )

    with pytest.raises(InstallerError, match="incompatible"):
        validate_bound_launch(InstallationTarget(payload, environment))


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_launch_probe_stops_at_bounded_output(tmp_path: Path, stream: str) -> None:
    target_root = tmp_path / "payload"
    launcher = _executable(target_root / "runtime/tongs-desktop", "#!/bin/sh\n")
    destination = "1" if stream == "stdout" else "2"
    interpreter = _executable(
        tmp_path / "env/bin/python",
        f"#!/bin/sh\nhead -c 2097152 /dev/zero >&{destination}\nsleep 10\n",
    )
    console = _executable(tmp_path / "env/bin/tongs", f"#!{interpreter}\n")
    environment = BoundPythonEnvironment(
        EnvironmentKind.VENV,
        console,
        interpreter,
        console.resolve(),
        interpreter.resolve(),
        "1",
    )
    started = time.monotonic()

    with pytest.raises(InstallerError, match="could not be validated"):
        validate_bound_launch(
            InstallationTarget(_payload(target_root, launcher), environment),
            timeout=3.0,
        )

    assert time.monotonic() - started < 2.5


def test_launch_probe_terminates_and_reaps_hanging_child(tmp_path: Path) -> None:
    target_root = tmp_path / "payload"
    launcher = _executable(target_root / "runtime/tongs-desktop", "#!/bin/sh\n")
    interpreter = _executable(tmp_path / "env/bin/python", "#!/bin/sh\nsleep 10\n")
    console = _executable(tmp_path / "env/bin/tongs", f"#!{interpreter}\n")
    environment = BoundPythonEnvironment(
        EnvironmentKind.VENV,
        console,
        interpreter,
        console.resolve(),
        interpreter.resolve(),
        "1",
    )
    started = time.monotonic()

    with pytest.raises(InstallerError, match="could not be validated"):
        validate_bound_launch(
            InstallationTarget(_payload(target_root, launcher), environment),
            timeout=0.1,
        )

    assert time.monotonic() - started < 2.0


def test_probe_timeout_terminates_group_after_leader_exits(tmp_path: Path) -> None:
    pidfile = tmp_path / "descendant.pid"
    environment = os.environ.copy()
    environment["TONGS_TEST_DESCENDANT_PID"] = os.fspath(pidfile)
    child_pid: int | None = None
    try:
        assert _process_is_running(os.getpid())
        with pytest.raises(subprocess.TimeoutExpired):
            launcher_module._run_bounded_probe(
                [
                    "/bin/sh",
                    "-c",
                    '(sleep 30) & echo $! > "$TONGS_TEST_DESCENDANT_PID"; exit 0',
                ],
                timeout=0.15,
                env=environment,
            )
        child_pid = int(pidfile.read_text())
        deadline = time.monotonic() + 2.0
        while _process_is_running(child_pid):
            if time.monotonic() >= deadline:
                pytest.fail("probe descendant remained alive after timeout cleanup")
            time.sleep(0.01)
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
