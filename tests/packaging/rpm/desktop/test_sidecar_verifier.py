from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[4]
SCRIPT = ROOT / "packaging" / "rpm" / "desktop" / "verify_sidecar_plugin.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "tongs_desktop_verify_sidecar_plugin", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verify_sidecar_plugin = _load_script()


def test_sidecar_child_ignores_invalid_ambient_config_and_cleans_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ambient_config = tmp_path / "ambient-config"
    config_file = ambient_config / "tongs" / "config.toml"
    config_file.parent.mkdir(parents=True)
    sentinel = b"user configuration sentinel\n"
    config_file.write_bytes(sentinel)
    monkeypatch.setenv("XDG_CONFIG_HOME", os.fspath(ambient_config))
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads(sentinel.decode())

    with verify_sidecar_plugin._isolated_sidecar_environment() as (
        environment,
        state_root,
    ):
        retained_root = state_root
        for variable in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
            child_path = Path(environment[variable])
            assert child_path.parent == state_root
            assert child_path.is_dir()
            assert stat.S_IMODE(child_path.stat().st_mode) == 0o700
        assert Path(environment["XDG_CONFIG_HOME"]) != ambient_config
        assert environment["PYTHONNOUSERSITE"] == "1"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from tongs.config import load_config; load_config()",
            ],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert config_file.read_bytes() == sentinel

    assert config_file.read_bytes() == sentinel
    assert not retained_root.exists()
