"""Text contract binding the Python XWayland mirror to the shell's own guard."""

from __future__ import annotations

import re
from pathlib import Path

from tongs.desktop.installer.launcher import (
    _XWAYLAND_SWITCH,
    xwayland_launch_arguments,
)

ROOT = Path(__file__).parents[3]
MAIN_PROCESS = ROOT / "desktop" / "src" / "main" / "index.ts"
REFUSAL = "Fedora KDE Wayland sessions must launch the desktop through XWayland"
_ENVIRONMENT = re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)")
_PLATFORM = re.compile(r'process\.platform === "([a-z0-9]+)"')
_JOINED_SWITCH = re.compile(r'item === "(--[a-z-]+=[a-z0-9]+)"')


def _source() -> str:
    return MAIN_PROCESS.read_text()


def _guard_condition(source: str) -> str:
    """Return the text of the refusal's own `if` condition, nothing around it."""
    assert source.count(REFUSAL) == 1
    head = source.split(REFUSAL, 1)[0]
    guard = source[head.rindex("if (") :]
    condition, _ = guard.split(") {", 1)
    return condition


def _uses_x11_body(source: str) -> str:
    body = source.split("function usesX11(", 1)[1]
    return body.split("\n}\n", 1)[0]


def test_shell_still_refuses_a_wayland_launch_without_the_switch() -> None:
    source = _source()

    condition = _guard_condition(source)

    assert 'process.platform === "linux"' in condition
    assert "process.env.WAYLAND_DISPLAY" in condition
    assert "!usesX11(process.argv)" in condition
    assert condition.count("&&") == 2 and "||" not in condition
    assert f'throw new Error(\n    "{REFUSAL}",\n  );' in source


def test_python_mirror_keys_on_the_same_condition_as_the_shell_guard() -> None:
    condition = _guard_condition(_source())
    variables = set(_ENVIRONMENT.findall(condition))
    platform = _PLATFORM.findall(condition)

    # The Python launcher must supply the switch under exactly this condition, so
    # a new term on either side has to be added on both sides or fail here.
    assert variables == {"WAYLAND_DISPLAY"}
    assert platform == ["linux"]
    for variable in variables:
        assert xwayland_launch_arguments(
            platform=platform[0], environ={variable: "wayland-0"}
        ) == (_XWAYLAND_SWITCH,)
    assert xwayland_launch_arguments(platform=platform[0], environ={}) == ()
    assert (
        xwayland_launch_arguments(
            platform=f"not-{platform[0]}", environ=dict.fromkeys(variables, "wayland-0")
        )
        == ()
    )


def test_python_switch_is_the_literal_the_shell_guard_accepts() -> None:
    body = _uses_x11_body(_source())
    joined = _JOINED_SWITCH.findall(body)
    name, value = _XWAYLAND_SWITCH.split("=", 1)

    assert joined == [_XWAYLAND_SWITCH]
    assert f'item === "{name}" && argv[index + 1] === "{value}"' in body
