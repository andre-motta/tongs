from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MODULE_PATH = (
    Path(__file__).parents[1]
    / "wheel"
    / "src"
    / "tongs_electron_prototype"
    / "__init__.py"
)


def _launcher_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "tongs_electron_prototype", MODULE_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wayland_session_defaults_to_xwayland_before_electron_starts() -> None:
    chromium, application = _launcher_module()._split_arguments(
        ["--smoke-report", "result.json"], session_type="wayland"
    )

    assert chromium == ["--ozone-platform=x11"]
    assert application == ["--smoke-report", "result.json"]


def test_explicit_inline_wayland_override_is_preserved() -> None:
    chromium, application = _launcher_module()._split_arguments(
        ["--ozone-platform=wayland", "--disable-vulkan", "--frontend", "dist"],
        session_type="wayland",
    )

    assert chromium == ["--ozone-platform=wayland", "--disable-vulkan"]
    assert application == ["--frontend", "dist"]


def test_explicit_separate_wayland_override_is_preserved() -> None:
    chromium, application = _launcher_module()._split_arguments(
        ["--ozone-platform", "wayland", "--frontend", "dist"],
        session_type="wayland",
    )

    assert chromium == ["--ozone-platform", "wayland"]
    assert application == ["--frontend", "dist"]
